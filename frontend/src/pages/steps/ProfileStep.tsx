import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowRight, FileText, LoaderCircle, Mic, Plus, Search } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import EmployeeProfileCard from '../../components/EmployeeProfileCard';
import { useWorkflow } from '../../context/WorkflowContext';
import { useSpeechToText } from '../../hooks/useSpeechToText';
import { useLanguage } from '../../i18n/LanguageContext';
import type { EmployeeRecord } from '../../types/domain';
import {
  dossierEmployeeProfileRows,
  profileRows,
  supplementalInfoToText,
} from '../../utils/format';
import { profileGoalSource } from '../../utils/profileGoals';
import { userFacingErrorMessage } from '../../utils/displayText';
import { speechRecognitionLanguage } from '../../utils/speechRecordingLocale';
import {
  EMPLOYEE_SEARCH_DEBOUNCE_MS,
  shouldCloseEmployeeResults,
} from '../../utils/employeeSearch';

const PROFILE_PREVIEW_SUMMARY_LABELS = new Set([
  '工号',
  '姓名',
  '员工代称',
  '岗位',
  '部门',
  '职级',
  '当前绩效评级',
  'TCL',
  '汇报关系',
]);

const PROFILE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '工号': 'Employee ID',
  '姓名': 'Name',
  '员工代称': 'Employee alias',
  '岗位': 'Role',
  '部门': 'Department',
  '职级': 'Level',
  '当前绩效评级': 'Current performance rating',
  '汇报关系': 'Reporting line',
  '员工其他背景信息': 'Additional employee context',
};

function employeeInputLabel(record: EmployeeRecord | null) {
  if (!record) return '';
  const name = record.name || record.employee_alias || '员工';
  const parts = [record.employee_id, name].filter(Boolean);
  return parts.length ? parts.join(' · ') : String(name);
}


function normalizeEmployeeMatchText(value: string | null | undefined) {
  return String(value || '').trim().replace(/\s*·\s*/g, ' · ').replace(/\s+/g, ' ').toLowerCase();
}

function queryMatchesSelectedEmployee(query: string, record: EmployeeRecord | null) {
  if (!record) return false;
  const normalizedQuery = normalizeEmployeeMatchText(query);
  if (!normalizedQuery) return false;
  const candidates = [
    employeeInputLabel(record),
    record.employee_id,
    record.name,
    record.employee_alias,
  ];
  return candidates.some((candidate) => normalizeEmployeeMatchText(candidate) === normalizedQuery);
}

export default function ProfileStep() {
  const navigate = useNavigate();
  const { language, translate, translateTemplate } = useLanguage();
  const {
    session,
    profileText,
    setProfileText,
    selectedFile,
    setSelectedFile,
    selectedFileMetadata,
    discardSelectedFileMetadata,
    employeeResults,
    selectedEmployee,
    displayedProfile,
    lookupEmployee,
    selectEmployee,
    confirmProfile,
    ensureSessionLocale,
    showToast,
  } = useWorkflow();
  const [employeeQuery, setEmployeeQuery] = useState('');
  const [resultsOpen, setResultsOpen] = useState(false);
  const [employeeOptionsLoading, setEmployeeOptionsLoading] = useState(false);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const employeeLookupRef = useRef<HTMLElement | null>(null);
  const employeeLookupTimerRef = useRef<number | null>(null);
  const employeeLookupControllerRef = useRef<AbortController | null>(null);
  const employeeLookupSequenceRef = useRef(0);
  const speechBaseProfileTextRef = useRef('');
  const speechInputActiveRef = useRef(false);
  const { employeeProfileRows, supplementalLines } = useMemo(() => {
    const structuredRows = displayedProfile
      ? profileRows(displayedProfile).filter(
        ([, label]) => label !== '员工代称' && label !== '员工其他背景信息',
      )
      : [];
    const previewRows = displayedProfile
      ? [
        ...dossierEmployeeProfileRows(displayedProfile),
        ...structuredRows.filter(([, label]) => !PROFILE_PREVIEW_SUMMARY_LABELS.has(label)),
      ]
      : [];
    const preview = supplementalInfoToText(
      [displayedProfile?.supplemental_info, profileText],
      structuredRows,
    );
    return {
      employeeProfileRows: previewRows.map(([key, label, value]) => ([
        key,
        translate(label, PROFILE_LABELS_ENGLISH[label]),
        value,
      ] as typeof previewRows[number])),
      supplementalLines: preview.split('\n').map((line) => line.trim()).filter(Boolean),
    };
  }, [displayedProfile, profileText, translate]);
  const selectedFileName = selectedFile?.name || selectedFileMetadata?.name || null;
  const selectedFileNeedsReselection = Boolean(
    selectedFileMetadata && selectedFile === null,
  );
  const primaryActionLabel = translate('确认员工信息', 'Confirm employee information');

  const cancelEmployeeLookup = useCallback(() => {
    if (employeeLookupTimerRef.current !== null) {
      window.clearTimeout(employeeLookupTimerRef.current);
      employeeLookupTimerRef.current = null;
    }
    employeeLookupControllerRef.current?.abort();
    employeeLookupControllerRef.current = null;
  }, []);

  useEffect(() => {
    if (!selectedEmployee) return;
    setEmployeeQuery(employeeInputLabel(selectedEmployee));
    setResultsOpen(false);
  }, [selectedEmployee]);

  useEffect(() => {
    if (!resultsOpen) return;

    const handleOutsidePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;

      const targetElement = target instanceof Element ? target : target.parentElement;
      const insideLookup = employeeLookupRef.current?.contains(target) ?? false;
      const insideSidebar = Boolean(targetElement?.closest('.app-sidebar'));
      if (shouldCloseEmployeeResults({ insideLookup, insideSidebar })) {
        setResultsOpen(false);
      }
    };

    document.addEventListener('pointerdown', handleOutsidePointerDown, true);
    return () => {
      document.removeEventListener('pointerdown', handleOutsidePointerDown, true);
    };
  }, [resultsOpen]);

  useEffect(() => {
    if (!resultsOpen) {
      cancelEmployeeLookup();
      setEmployeeOptionsLoading(false);
      return;
    }

    cancelEmployeeLookup();
    const requestId = ++employeeLookupSequenceRef.current;
    const controller = new AbortController();
    const searchQuery = queryMatchesSelectedEmployee(employeeQuery, selectedEmployee)
      ? ''
      : employeeQuery;
    employeeLookupControllerRef.current = controller;
    setEmployeeOptionsLoading(true);
    const searchDelay = searchQuery.trim() ? EMPLOYEE_SEARCH_DEBOUNCE_MS : 0;
    employeeLookupTimerRef.current = window.setTimeout(() => {
      employeeLookupTimerRef.current = null;
      void lookupEmployee(searchQuery, {
        autoSelectSingle: false,
        silent: true,
        signal: controller.signal,
      }).catch((error: unknown) => {
        if (!controller.signal.aborted && requestId === employeeLookupSequenceRef.current) {
          showToast(userFacingErrorMessage(error, undefined, '员工搜索失败，请稍后重试。'), 'error');
        }
      }).finally(() => {
        if (employeeLookupControllerRef.current === controller) {
          employeeLookupControllerRef.current = null;
        }
        if (requestId === employeeLookupSequenceRef.current) {
          setEmployeeOptionsLoading(false);
        }
      });
    }, searchDelay);

    return () => {
      if (employeeLookupTimerRef.current !== null) {
        window.clearTimeout(employeeLookupTimerRef.current);
        employeeLookupTimerRef.current = null;
      }
      controller.abort();
      if (employeeLookupControllerRef.current === controller) {
        employeeLookupControllerRef.current = null;
      }
    };
  }, [cancelEmployeeLookup, employeeQuery, lookupEmployee, resultsOpen, selectedEmployee, showToast, translate]);

  useEffect(() => () => cancelEmployeeLookup(), [cancelEmployeeLookup]);

  useEffect(() => {
    const textarea = composerRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    const styles = window.getComputedStyle(textarea);
    const lineHeight = Number.parseFloat(styles.lineHeight) || 22;
    const paddingY = Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
    const borderY = Number.parseFloat(styles.borderTopWidth) + Number.parseFloat(styles.borderBottomWidth);
    const maxHeight = Math.ceil(lineHeight * 5 + paddingY + borderY);
    textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
    textarea.scrollTop = textarea.scrollHeight;
  }, [profileText]);

  const applySpeechTranscript = useCallback((text: string) => {
    if (!speechInputActiveRef.current) return;
    const transcript = text.trim();
    const baseText = speechBaseProfileTextRef.current.trimEnd();
    const composed = [baseText, transcript].filter(Boolean).join('\n');
    setProfileText(composed);
    window.requestAnimationFrame(() => {
      const textarea = composerRef.current;
      if (textarea) textarea.scrollTop = textarea.scrollHeight;
    });
  }, [setProfileText]);

  const handleSpeechTranscript = useCallback((text: string) => {
    applySpeechTranscript(text);
    speechInputActiveRef.current = false;
    speechBaseProfileTextRef.current = '';
    window.requestAnimationFrame(() => composerRef.current?.focus());
  }, [applySpeechTranscript]);

  const handleSpeechError = useCallback((message: string, fatal: boolean) => {
    if (fatal && speechInputActiveRef.current) {
      setProfileText(speechBaseProfileTextRef.current);
      speechInputActiveRef.current = false;
      speechBaseProfileTextRef.current = '';
    }
    showToast(message, 'error');
  }, [setProfileText, showToast]);

  const speechInputOptions = useMemo(() => ({
    sessionId: session?.session_id,
    language: speechRecognitionLanguage(language),
    beforeStart: ensureSessionLocale,
    onPartialTranscript: applySpeechTranscript,
    onTranscript: handleSpeechTranscript,
    onError: handleSpeechError,
  }), [applySpeechTranscript, ensureSessionLocale, handleSpeechError, handleSpeechTranscript, language, session?.session_id]);
  const speech = useSpeechToText(speechInputOptions);
  const speechFinalizingLabel = speech.finalizingProgress?.total
    ? translateTemplate(
      '正在完成最终转写（{completed}/{total}）',
      'Finalizing transcription ({completed}/{total})',
      {
        completed: speech.finalizingProgress.completed,
        total: speech.finalizingProgress.total,
      },
    )
    : translate('正在完成最终转写', 'Finalizing transcription');

  const handleEmployeeLookup = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (queryMatchesSelectedEmployee(employeeQuery, selectedEmployee)) {
      setEmployeeQuery(employeeInputLabel(selectedEmployee));
      setResultsOpen(false);
      return;
    }
    cancelEmployeeLookup();
    const requestId = ++employeeLookupSequenceRef.current;
    setEmployeeOptionsLoading(true);
    try {
      await lookupEmployee(employeeQuery.trim());
      if (requestId === employeeLookupSequenceRef.current) setResultsOpen(true);
    } finally {
      if (requestId === employeeLookupSequenceRef.current) setEmployeeOptionsLoading(false);
    }
  };

  const handleSelectEmployee = (record: EmployeeRecord) => {
    cancelEmployeeLookup();
    employeeLookupSequenceRef.current += 1;
    setEmployeeQuery(employeeInputLabel(record));
    setResultsOpen(false);
    selectEmployee(record);
  };

  const openEmployeeResults = () => {
    setResultsOpen(true);
  };

  const openFilePicker = () => {
    const input = fileInputRef.current;
    if (!input) return;
    input.value = '';
    input.click();
  };

  const submit = async () => {
    const canContinue = await confirmProfile();
    if (canContinue) navigate('/app/intent');
  };

  const showResults = resultsOpen;

  return (
    <section id="screen-profile" className="screen active">
      <div className="page-intro">
        <h1>{translate('员工信息', 'Employee information')}</h1>
      </div>
      <div className="split-layout profile-layout">
        <div className="stack input-workspace">
          <section ref={employeeLookupRef} className="soft-card employee-lookup-card section-card-accent">
            <div className="card-title"><span className="title-icon"><Search aria-hidden="true" /></span><div><h2>{translate('员工库匹配', 'Match employee directory')}</h2></div></div>
            <form className="lookup-grid" onSubmit={handleEmployeeLookup}>
              <label className="lookup-field">
                <input
                  value={employeeQuery}
                  onChange={(event) => {
                    setEmployeeQuery(event.target.value);
                    setResultsOpen(true);
                  }}
                  onFocus={openEmployeeResults}
                  onClick={openEmployeeResults}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      setResultsOpen(false);
                    } else if (event.key === 'ArrowDown' && !resultsOpen) {
                      event.preventDefault();
                      openEmployeeResults();
                    }
                  }}
                  type="search"
                  aria-label={translate('员工姓名或工号', 'Employee name or ID')}
                  aria-haspopup="listbox"
                  aria-expanded={showResults}
                  aria-controls="employee-match-results"
                  placeholder={translate(
                    '员工姓名 / 工号 / 代称，例如：张三 / Alex / E001',
                    'Name / employee ID / alias, e.g. Alex / E001',
                  )}
                  autoComplete="off"
                />
              </label>
              <button className="btn btn-primary lookup-submit" type="submit"><Search aria-hidden="true" />{translate('匹配员工', 'Match employee')}</button>
            </form>
            {showResults && (
              <div id="employee-match-results" className="employee-results" role="listbox" aria-label={translate('员工匹配结果', 'Employee match results')}>
                {employeeOptionsLoading && (
                  <div className="employee-results-status" role="status">{translate('正在载入员工列表...', 'Loading employees...')}</div>
                )}
                {!employeeOptionsLoading && employeeResults.length === 0 && (
                  <div className="employee-results-status">{translate('暂无可选员工，可输入姓名或工号后搜索。', 'No employees available. Search by name or employee ID.')}</div>
                )}
                {!employeeOptionsLoading && employeeResults.map((record) => {
                  const alias = record.employee_alias || record.name || record.employee_id || 'A';
                  const selected = selectedEmployee?.employee_id === record.employee_id;
                  return (
                    <button key={String(record.employee_id || record.name)} className={`employee-result ${selected ? 'selected' : ''}`} type="button" role="option" aria-selected={selected} onClick={() => handleSelectEmployee(record)}>
                      <span className="employee-avatar-mini">{String(alias).slice(0, 1).toUpperCase()}</span>
                      <span className="employee-result-main"><strong>{record.name || alias}</strong><small>{record.employee_id || '—'} · {record.role || '—'} · {record.department || '—'}</small></span>
                    </button>
                  );
                })}
              </div>
            )}
          </section>

          <section className="soft-card profile-composer-card">
            <div className="card-title"><span className="title-icon"><FileText aria-hidden="true" /></span><div><h2>{translate('员工的其他背景信息', 'Additional employee context')}</h2></div></div>
            <div className="profile-composer">
              <div className="composer-input-area">
                <textarea
                  id="profileText"
                  ref={composerRef}
                  className="profile-composer-textarea"
                  value={profileText}
                  onChange={(event) => setProfileText(event.target.value)}
                  rows={5}
                  readOnly={speech.requesting || speech.recording || speech.transcribing}
                  aria-busy={speech.requesting || speech.transcribing}
                  aria-label={translate('员工的其他背景信息', 'Additional employee context')}
                  placeholder={translate(
                    '请输入员工的其他背景信息，例如个人诉求、生活情况、工作状态。',
                    'Add employee context, such as personal needs, circumstances, or current work status.',
                  )}
                />
              </div>
              <div className="profile-composer-toolbar">
                <input
                  ref={fileInputRef}
                  hidden
                  type="file"
                  accept=".pdf,.docx,.pptx,.xlsx,.txt,.md"
                  onChange={(event) => setSelectedFile(event.target.files?.[0] || null)}
                />
                <button
                  className={'profile-upload-button ' + (selectedFileName ? 'has-file' : '')}
                  type="button"
                  onClick={openFilePicker}
                  aria-label={selectedFileName
                    ? (selectedFileNeedsReselection
                      ? translate('重新选择上传文件：', 'Reselect uploaded file: ')
                      : translate('更换上传文件：', 'Replace uploaded file: ')) + selectedFileName
                    : translate('上传文件', 'Upload file')}
                  title={selectedFileName
                    ? (selectedFileNeedsReselection
                      ? translate('请重新选择 ', 'Please reselect ')
                      : translate('已选择 ', 'Selected ')) + selectedFileName + translate('，点击更换', '; click to replace')
                    : translate('上传文件', 'Upload file')}
                >
                  <Plus aria-hidden="true" />
                </button>
                {selectedFileName && (
                  <span className="profile-selected-file" role="status" title={selectedFileName}>
                    {selectedFileName}
                  </span>
                )}
                <button
                  className={`mic-button profile-mic-button${speech.recording ? ` recording realtime-${speech.realtimeStatus}` : ''}${speech.requesting || speech.transcribing ? ' transcribing' : ''}`}
                  title={speech.recording
                    ? speech.realtimeStatus === 'connected'
                      ? translate('停止录音（正在实时转写）', 'Stop recording (live transcription active)')
                      : speech.realtimeStatus === 'connecting'
                        ? translate('停止录音（正在连接本地语音模型）', 'Stop recording (connecting to the local speech model)')
                      : translate('停止录音（停止后完成转写）', 'Stop recording (transcription completes after stopping)')
                    : speech.requesting
                      ? translate('正在请求麦克风权限', 'Requesting microphone permission')
                      : speech.transcribing
                        ? speechFinalizingLabel
                        : speech.readinessStatus === 'warming'
                          ? translate('实时语音准备中', 'Preparing live speech input')
                          : speech.readinessStatus === 'degraded'
                            ? translate('语音输入（实时预览暂不可用）', 'Speech input (live preview unavailable)')
                            : translate('语音输入', 'Speech input')}
                  aria-label={speech.recording
                    ? translate('停止录音', 'Stop recording')
                    : speech.requesting
                      ? translate('正在请求麦克风权限', 'Requesting microphone permission')
                      : speech.transcribing
                        ? translate('正在转写', 'Transcribing')
                        : speech.readinessStatus === 'warming'
                          ? translate('实时语音准备中', 'Preparing live speech input')
                          : translate('开始语音输入', 'Start speech input')}
                  aria-pressed={speech.recording}
                  type="button"
                  disabled={speech.transcribing}
                  onClick={() => {
                    if (speech.requesting) {
                      setProfileText(speechBaseProfileTextRef.current);
                      speechInputActiveRef.current = false;
                      speechBaseProfileTextRef.current = '';
                      void speech.stopRecording();
                      return;
                    }
                    if (speech.recording) {
                      void speech.stopRecording();
                      return;
                    }
                    speechBaseProfileTextRef.current = profileText;
                    speechInputActiveRef.current = true;
                    void speech.startRecording();
                  }}
                >
                  {speech.recording && (
                    <span className="voice-wave-glyph" aria-hidden="true">
                      <i />
                      <i />
                      <i />
                      <i />
                      <i />
                    </span>
                  )}
                  {(speech.requesting || (!speech.recording && !speech.transcribing && speech.readinessStatus === 'warming')) && (
                    <LoaderCircle className="spin" size={20} aria-hidden="true" />
                  )}
                  {!speech.requesting && !speech.recording && (speech.transcribing || speech.readinessStatus !== 'warming') && (
                    <Mic size={20} aria-hidden="true" />
                  )}
                </button>
              </div>
            </div>
            {selectedFileNeedsReselection && selectedFileMetadata && (
              <div className="auth-error profile-file-recovery" role="alert">
                <span>
                  {translateTemplate(
                    '刷新后无法恢复文件“{filename}”，请点击上传按钮重新选择。',
                    'The file “{filename}” could not be restored after refresh. Use the upload button to select it again.',
                    { filename: selectedFileMetadata.name },
                  )}
                </span>
                <button
                  className="btn btn-secondary"
                  type="button"
                  onClick={discardSelectedFileMetadata}
                >
                  {translate('忽略该文件', 'Dismiss file')}
                </button>
              </div>
            )}
          </section>

          <div className="profile-confirm-row"><button className="btn btn-primary btn-wide profile-confirm-button" type="button" onClick={submit}><span>{primaryActionLabel}</span><ArrowRight aria-hidden="true" /></button></div>
        </div>

        <EmployeeProfileCard
          title={translate('员工档案预览', 'Employee profile preview')}
          rows={employeeProfileRows}
          goalSource={profileGoalSource(displayedProfile)}
          className="profile-dossier-grid profile-dossier-consistent profile-page-dossier-grid"
          emptyText=""
          contextTitle={supplementalLines.length ? translate('本次会话补充', 'Additional session context') : undefined}
        >
          {supplementalLines.length > 0 && (
            <div className="profile-supplemental-preview" aria-live="polite">
              <span className="profile-supplemental-label">{translate('补充内容', 'Additional context')}</span>
              <div className="profile-supplemental-copy">
                {supplementalLines.map((line, index) => (
                  <p key={`${index}-${line}`}>{line}</p>
                ))}
              </div>
            </div>
          )}
        </EmployeeProfileCard>
      </div>
    </section>
  );
}
