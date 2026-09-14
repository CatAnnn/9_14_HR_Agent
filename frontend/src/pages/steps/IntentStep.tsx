import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  PanelRightClose,
  PanelRightOpen,
  PencilLine,
} from 'lucide-react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import EmployeeProfileCard from '../../components/EmployeeProfileCard';
import { useWorkflow } from '../../context/WorkflowContext';
import {
  normalizeAppLanguage,
  useLanguage,
  type AppLanguage,
} from '../../i18n/LanguageContext';
import { intentDescription, intentName } from '../../i18n/businessLabels';
import type {
  IntentGoalPerformanceItem,
  IntentPerformanceDraftResponse,
} from '../../types/domain';
import { dossierEmployeeProfileRows, profileRows } from '../../utils/format';
import { normalizeDisplayText, userFacingErrorMessage } from '../../utils/displayText';
import {
  emptyDraft,
  goalsMatch,
  migrateLegacyPerformanceItems,
  normalizeGeneratedPerformanceItem,
  normalizeGeneratedPerformanceItems,
  performanceSectionTitles,
} from '../../utils/intentPerformanceSections';
import { evaluateIntentEligibility } from '../../utils/intentEligibility';
import { profileGoalSource } from '../../utils/profileGoals';

const PERFORMANCE_CONTEXT_DISCLAIMER = '本内容基于有限信息推演模拟，仅用于管理者绩效面谈预演，不可直接作为正式绩效评估文件';

const PERFORMANCE_SECTION_TITLES_ENGLISH: Readonly<Record<string, string>> = {
  '目标达成总览': 'Overall goal attainment',
  '正向表现/取得进展': 'Positive performance / progress made',
  '现存差距与行为实例': 'Current gaps and behavioral examples',
  'Goal Achievement Overview': 'Overall goal attainment',
  'Positive Performance / Progress': 'Positive performance / progress made',
  'Current Gaps and Behavioral Examples': 'Current gaps and behavioral examples',
};

const PROFILE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '工号': 'Employee ID',
  '姓名': 'Name',
  '岗位': 'Role',
  '部门': 'Department',
  '职级': 'Level',
  '当前绩效评级': 'Current performance rating',
  '汇报关系': 'Reporting line',
  '员工目标': 'Employee goals',
};

function buildPerformanceContext(
  items: IntentGoalPerformanceItem[],
  disclaimer = PERFORMANCE_CONTEXT_DISCLAIMER,
) {
  const body = items
    .map((item) => `${item.goal.trim()}\n${item.current_performance.trim()}`)
    .join('\n\n');
  return `${body}\n\n${disclaimer}`;
}

function joinEligibilityValues(
  values: string[],
  language: AppLanguage,
  type: 'and' | 'or',
): string {
  if (values.length <= 1) return values[0] || '';
  const connector = {
    'zh-CN': type === 'and' ? '和' : '或',
    en: type === 'and' ? ' and ' : ' or ',
    de: type === 'and' ? ' und ' : ' oder ',
    ja: type === 'and' ? 'と' : 'または',
  }[language];
  return `${values.slice(0, -1).join(language === 'en' || language === 'de' ? ', ' : '、')}${connector}${values[values.length - 1]}`;
}

function PerformanceLineEditor({
  value,
  onChange,
  placeholder,
  label,
  readOnly,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
  readOnly: boolean;
}) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const resizeFrameRef = useRef<number | null>(null);
  const resizeHeightRef = useRef<(() => void) | null>(null);

  const scheduleResize = useCallback(() => {
    if (resizeFrameRef.current !== null) return;
    resizeFrameRef.current = window.requestAnimationFrame(() => {
      resizeFrameRef.current = null;
      resizeHeightRef.current?.();
    });
  }, []);

  useLayoutEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;

    const measurementEditor = editor.cloneNode(false) as HTMLTextAreaElement;
    measurementEditor.removeAttribute('aria-busy');
    measurementEditor.removeAttribute('aria-label');
    measurementEditor.removeAttribute('id');
    measurementEditor.removeAttribute('name');
    measurementEditor.removeAttribute('required');
    measurementEditor.setAttribute('aria-hidden', 'true');
    measurementEditor.tabIndex = -1;
    measurementEditor.style.position = 'absolute';
    measurementEditor.style.top = '0';
    measurementEditor.style.left = '0';
    measurementEditor.style.width = '100%';
    measurementEditor.style.height = '0px';
    measurementEditor.style.minHeight = '0px';
    measurementEditor.style.maxHeight = 'none';
    measurementEditor.style.overflow = 'hidden';
    measurementEditor.style.visibility = 'hidden';
    measurementEditor.style.pointerEvents = 'none';
    editor.parentElement?.appendChild(measurementEditor);

    const resizeHeight = () => {
      measurementEditor.value = editor.value;
      const nextHeight = `${Math.max(58, measurementEditor.scrollHeight)}px`;
      if (editor.style.height === nextHeight) return;
      editor.style.height = nextHeight;
    };
    resizeHeightRef.current = resizeHeight;
    scheduleResize();

    let observer: ResizeObserver | null = null;
    let observedWidth: number | null = null;

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', scheduleResize);
    } else {
      observer = new ResizeObserver(([entry]) => {
        const nextWidth = entry?.contentRect.width ?? editor.clientWidth;
        if (observedWidth !== null && Math.abs(nextWidth - observedWidth) < 0.5) return;
        observedWidth = nextWidth;
        scheduleResize();
      });
      observer.observe(editor);
    }

    return () => {
      if (resizeFrameRef.current !== null) {
        window.cancelAnimationFrame(resizeFrameRef.current);
        resizeFrameRef.current = null;
      }
      observer?.disconnect();
      window.removeEventListener('resize', scheduleResize);
      if (resizeHeightRef.current === resizeHeight) resizeHeightRef.current = null;
      measurementEditor.remove();
    };
  }, [scheduleResize]);

  useLayoutEffect(() => {
    scheduleResize();
  }, [scheduleResize, value]);

  return (
    <div className={`intent-performance-editor-shell${readOnly ? ' is-readonly' : ''}`}>
      <textarea
        ref={editorRef}
        className="intent-performance-line-editor"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={2}
        placeholder={placeholder}
        aria-label={label}
        readOnly={readOnly}
        aria-busy={readOnly}
      />
      <PencilLine aria-hidden="true" />
    </div>
  );
}

export default function IntentStep() {
  const navigate = useNavigate();
  const { language, translate, translateTemplate } = useLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get('stage') === 'performance' ? 'performance' : 'select';
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isGoalsPanelOpen, setIsGoalsPanelOpen] = useState(true);
  const requestVersions = useRef<Record<string, number>>({});
  const edited = useRef<Record<string, boolean>>({});
  const isSubmittingRef = useRef(false);
  const requestControllers = useRef<Record<string, AbortController | undefined>>({});
  const {
    options,
    session,
    selectedIntentId,
    setSelectedIntentId,
    intentPerformanceDrafts: drafts,
    setIntentPerformanceDrafts: setDrafts,
    displayedProfile,
    generateIntentPerformanceDraft,
    streamIntentPerformanceDraft,
    confirmIntent,
  } = useWorkflow();
  const intent = options.intents.find((item) => item.id === selectedIntentId) || null;
  const eligibility = evaluateIntentEligibility(displayedProfile, intent);
  const intentWarning = Boolean(
    selectedIntentId
    && (eligibility.status === 'unknown' || eligibility.status === 'mismatch'),
  );
  const employeeRows = dossierEmployeeProfileRows(displayedProfile).map(([key, label, value]) => ([
    key,
    translate(label, PROFILE_LABELS_ENGLISH[label]),
    value,
  ] as ReturnType<typeof dossierEmployeeProfileRows>[number]));
  const employeeGoalRows = profileRows(displayedProfile)
    .filter(([rowKey]) => rowKey === 'GO')
    .map(([key, label, value]) => ([
      key,
      translate(label, PROFILE_LABELS_ENGLISH[label]),
      value,
    ] as ReturnType<typeof profileRows>[number]));
  const performanceContextDisclaimer = translate(
    PERFORMANCE_CONTEXT_DISCLAIMER,
    'This content is a simulation based on limited information for manager performance-conversation rehearsal only and must not be used directly as a formal performance evaluation document.',
  );
  const performanceTitle = (title: string) => translate(
    title,
    PERFORMANCE_SECTION_TITLES_ENGLISH[title],
  );
  const eligibilityMessage = (() => {
    if (eligibility.status === 'unknown') {
      return translate(
        '员工档案缺少可识别的绩效评级或 TCL，暂时无法判断该选择是否符合建议范围。请补充或核对员工信息；如已掌握相关业务背景，仍可继续当前沟通意图。',
        'The employee profile has no recognizable performance rating or TCL, so the recommended scope cannot be assessed yet. Verify or add the employee information; if you have sufficient business context, you may still continue with this conversation intent.',
      );
    }
    if (eligibility.status !== 'mismatch' || !intent) return null;
    const currentValues = [
      eligibility.performanceRating == null
        ? ''
        : translateTemplate('绩效评级 {value}', 'performance rating {value}', {
          value: eligibility.performanceRating,
        }),
      eligibility.tcl == null
        ? ''
        : translateTemplate('TCL {value}', 'TCL {value}', { value: eligibility.tcl }),
    ].filter(Boolean);
    const expectedValues = [
      intent.eligibility?.performance_ratings?.length
        ? translateTemplate('绩效评级 {value}', 'performance rating {value}', {
          value: intent.eligibility.performance_ratings.join('/'),
        })
        : '',
      intent.eligibility?.tcl_values?.length
        ? translateTemplate('TCL {value}', 'TCL {value}', {
          value: intent.eligibility.tcl_values.join('/'),
        })
        : '',
    ].filter(Boolean);
    return translateTemplate(
      '当前员工为 {current}；“{intent}”的建议适用范围为 {expected}。请核对并选择更匹配的沟通意图；如有额外业务依据，仍可继续当前选择。',
      'This employee currently has {current}; the recommended scope for “{intent}” is {expected}. Verify and select a better-matched intent, or continue if you have additional business context.',
      {
        current: joinEligibilityValues(currentValues, language, 'and'),
        intent: intentName(intent.id, intent.name, language),
        expected: joinEligibilityValues(expectedValues, language, 'or'),
      },
    );
  })();

  const currentDraft = selectedIntentId && drafts[selectedIntentId]?.locale === language
    ? drafts[selectedIntentId]
    : null;
  const currentItems = currentDraft?.performance_items || [];
  const hasDraft = currentItems.length > 0;
  const hasCompleteDraft = hasDraft
    && currentItems.every((item) => item.current_performance.trim());
  const currentError = selectedIntentId ? errors[selectedIntentId] : null;
  const isGenerating = selectedIntentId ? Boolean(loading[selectedIntentId]) : false;
  const isEditorLocked = isGenerating || isSubmitting;
  const confirmedIntentId = session?.intent?.intent_id || session?.intent?.id;
  const confirmedPerformanceItems = session?.intent?.performance_items;
  const confirmedPerformanceLocale = session?.intent?.performance_locale || 'zh-CN';
  const profilePerformanceItems = hasDraft
    ? currentItems
    : (
      confirmedIntentId === selectedIntentId
        && confirmedPerformanceLocale === language
        && goalsMatch(confirmedPerformanceItems, confirmedPerformanceLocale)
        ? confirmedPerformanceItems || []
        : []
    );
  const canReturnToPerformance = Boolean(
    selectedIntentId
    && (
      drafts[selectedIntentId]?.locale === language
      || loading[selectedIntentId]
      || (
        confirmedIntentId === selectedIntentId
        && confirmedPerformanceLocale === language
        && goalsMatch(confirmedPerformanceItems, confirmedPerformanceLocale)
      )
    ),
  );

  const setView = useCallback((nextView: 'select' | 'performance') => {
    const nextParams = new URLSearchParams(searchParams);
    if (nextView === 'performance') nextParams.set('stage', 'performance');
    else nextParams.delete('stage');
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  const generateDraft = useCallback(async (intentId: string) => {
    if (requestControllers.current[intentId]) return;
    const requestVersion = (requestVersions.current[intentId] || 0) + 1;
    requestVersions.current[intentId] = requestVersion;
    edited.current[intentId] = false;
    const controller = new AbortController();
    requestControllers.current[intentId] = controller;
    setLoading((current) => ({ ...current, [intentId]: true }));
    setErrors((current) => ({ ...current, [intentId]: null }));

    let completed = false;
    let fallbackAttempted = false;
    const completeFromFallback = async (): Promise<boolean> => {
      fallbackAttempted = true;
      const fallback = await generateIntentPerformanceDraft(intentId);
      const performanceItems = normalizeGeneratedPerformanceItems(
        fallback.performance_items,
        fallback.locale,
      );
      if (!performanceItems) {
        throw new Error(translate(
          '生成结果缺少完整的三个综合维度，请稍后重试。',
          'The generated result is missing one or more of the three required dimensions. Please try again later.',
        ));
      }
      if (
        requestVersions.current[intentId] !== requestVersion
        || edited.current[intentId]
      ) return false;

      completed = true;
      setDrafts((current) => ({
        ...current,
        [intentId]: {
          intent_id: fallback.intent_id || intentId,
          locale: fallback.locale,
          performance_context: fallback.performance_context || '',
          performance_items: performanceItems,
        },
      }));
      return true;
    };

    try {
      await streamIntentPerformanceDraft(
        intentId,
        (event, data) => {
          if (
            requestVersions.current[intentId] !== requestVersion
            || edited.current[intentId]
          ) return;

          if (event === "reset") {
            setDrafts((current) => ({
              ...current,
              [intentId]: emptyDraft(intentId, language),
            }));
            return;
          }

          if (event === "section") {
            const sectionIndex = Number(data.section_index);
            const rawItem = data.item;
            if (
              !Number.isInteger(sectionIndex)
              || sectionIndex < 0
            ) return;
            setDrafts((current) => {
              const draft = current[intentId] || emptyDraft(intentId, language);
              const item = normalizeGeneratedPerformanceItem(
                rawItem,
                sectionIndex,
                draft.locale,
              );
              if (!item) return current;
              const items = draft.performance_items.map((existing, index) => (
                index === sectionIndex
                  ? item
                  : existing
              ));
              return {
                ...current,
                [intentId]: {
                  ...draft,
                  performance_items: items,
                },
              };
            });
            return;
          }

          if (event === "complete") {
            const responseLocale = normalizeAppLanguage(data.locale) || language;
            const performanceItems = normalizeGeneratedPerformanceItems(
              data.performance_items,
              responseLocale,
            );
            if (!performanceItems) {
              throw new Error(translate(
                '生成结果缺少完整的三个综合维度，请稍后重试。',
                'The generated result is missing one or more of the three required dimensions. Please try again later.',
              ));
            }
            const response: IntentPerformanceDraftResponse = {
              intent_id: typeof data.intent_id === "string"
                ? data.intent_id
                : intentId,
              locale: responseLocale,
              performance_context: typeof data.performance_context === "string"
                ? normalizeDisplayText(data.performance_context)
                : "",
              performance_items: performanceItems,
            };
            completed = true;
            setDrafts((current) => ({ ...current, [intentId]: response }));
          }
        },
        controller.signal,
      );
      if (!completed) await completeFromFallback();
      if (!completed) {
        throw new Error(translate(
          '当前表现流式响应提前结束，请稍后重试。',
          'The employee-performance stream ended early. Please try again later.',
        ));
      }
    } catch (error) {
      if ((error as Error)?.name === "AbortError") return;
      let failure: unknown = error;
      if (
        error instanceof TypeError
        && !controller.signal.aborted
        && !fallbackAttempted
      ) {
        try {
          if (await completeFromFallback()) return;
        } catch (fallbackError) {
          failure = fallbackError;
        }
      }
      if (requestVersions.current[intentId] === requestVersion) {
        setDrafts((current) => {
          const draft = current[intentId];
          const hasGeneratedContent = draft?.performance_items.some(
            (item) => item.current_performance.trim(),
          );
          if (hasGeneratedContent) return current;
          const next = { ...current };
          delete next[intentId];
          return next;
        });
        setErrors((current) => ({
          ...current,
          [intentId]: userFacingErrorMessage(
            failure,
            undefined,
            translate(
              '当前表现初稿生成失败，请手动填写。',
              'The employee-performance draft could not be generated. Please enter it manually.',
            ),
          ),
        }));
      }
    } finally {
      if (requestControllers.current[intentId] === controller) {
        delete requestControllers.current[intentId];
      }
      if (requestVersions.current[intentId] === requestVersion) {
        setLoading((current) => ({ ...current, [intentId]: false }));
      }
    }
  }, [generateIntentPerformanceDraft, language, setDrafts, streamIntentPerformanceDraft, translate]);

  const loadPerformanceDraft = useCallback((intentId: string) => {
    if (
      requestControllers.current[intentId]
      || loading[intentId]
      || errors[intentId]
    ) return;

    const cachedDraft = drafts[intentId];
    // An existing three-section draft may be temporarily empty while the
    // manager replaces its text. Never interpret that edit as a generation
    // failure or start a model request that could overwrite it.
    if (
      cachedDraft?.locale === language
      && goalsMatch(cachedDraft.performance_items, cachedDraft.locale)
    ) return;
    if (cachedDraft?.locale === language) {
      const migratedItems = migrateLegacyPerformanceItems(
        cachedDraft.performance_items,
        cachedDraft.locale,
      );
      if (migratedItems) {
        setDrafts((current) => ({
          ...current,
          [intentId]: {
            ...cachedDraft,
            performance_context: buildPerformanceContext(
              migratedItems,
              performanceContextDisclaimer,
            ),
            performance_items: migratedItems,
          },
        }));
        return;
      }
    }

    const confirmedIntentId = session?.intent?.intent_id || session?.intent?.id;
    const confirmedItems = session?.intent?.performance_items;
    const confirmedLocale = session?.intent?.performance_locale || 'zh-CN';
    if (
      confirmedIntentId === intentId
      && confirmedLocale === language
      && goalsMatch(confirmedItems, confirmedLocale)
    ) {
      const items = confirmedItems!.map((item) => ({ ...item }));
      setDrafts((current) => ({
        ...current,
        [intentId]: {
          intent_id: intentId,
          locale: session?.intent?.performance_locale || session?.locale || 'zh-CN',
          performance_context: session?.intent?.performance_context
            || buildPerformanceContext(items, performanceContextDisclaimer),
          performance_items: items,
        },
      }));
      return;
    }
    void generateDraft(intentId);
  }, [drafts, errors, generateDraft, language, loading, performanceContextDisclaimer, session?.intent, setDrafts]);

  const continueToPerformance = () => {
    if (!selectedIntentId) return;
    setView('performance');
    setErrors((current) => ({ ...current, [selectedIntentId]: null }));
  };

  useEffect(() => {
    const intentIds = Object.keys(requestControllers.current);
    intentIds.forEach((intentId) => {
      requestControllers.current[intentId]?.abort();
      requestVersions.current[intentId] = (requestVersions.current[intentId] || 0) + 1;
    });
    requestControllers.current = {};
    setLoading({});
    setErrors({});
  }, [language]);

  useEffect(() => {
    if (view !== 'performance' || !selectedIntentId || !session) return;
    loadPerformanceDraft(selectedIntentId);
  }, [loadPerformanceDraft, selectedIntentId, session, view]);

  useEffect(() => () => {
    Object.values(requestControllers.current).forEach((controller) => {
      controller?.abort();
    });
  }, []);

  const updateDraftItem = (index: number, value: string) => {
    if (!selectedIntentId || isEditorLocked) return;
    const intentId = selectedIntentId;
    edited.current[intentId] = true;
    setErrors((current) => (
      current[intentId]
        ? { ...current, [intentId]: null }
        : current
    ));
    setDrafts((current) => {
      const draft = current[intentId] || emptyDraft(intentId, language);
      const items = draft.performance_items.map((item, itemIndex) => (
        itemIndex === index
          ? {
            ...item,
            current_performance: value,
            generation_reason: null,
          }
          : item
      ));
      return {
        ...current,
        [intentId]: {
          ...draft,
          performance_context: buildPerformanceContext(items, performanceContextDisclaimer),
          performance_items: items,
        },
      };
    });
  };

  const submit = async () => {
    if (
      !selectedIntentId
      || !hasCompleteDraft
      || !goalsMatch(currentItems, currentDraft?.locale || language)
      || isSubmittingRef.current
    ) return;
    const intentId = selectedIntentId;
    const performanceContext = buildPerformanceContext(currentItems, performanceContextDisclaimer);
    const submittedItems = currentItems.map((item) => ({
      ...item,
      generation_reason: item.generation_reason?.trim() || null,
    }));
    isSubmittingRef.current = true;
    setIsSubmitting(true);
    setErrors((current) => ({ ...current, [intentId]: null }));
    try {
      await confirmIntent(performanceContext, submittedItems);
      navigate('/app/simulation');
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [intentId]: userFacingErrorMessage(
          error,
          undefined,
          translate(
            '员工表现保存失败，已保留当前修改，请稍后重试。',
            'Employee performance could not be saved. Your edits have been kept; please try again later.',
          ),
        ),
      }));
    } finally {
      isSubmittingRef.current = false;
      setIsSubmitting(false);
    }
  };

  const visibleRows: IntentGoalPerformanceItem[] = hasDraft
    ? currentItems
    : performanceSectionTitles(language).map((title) => ({
      goal: title,
      current_performance: '',
      generation_reason: null,
    }));

  return (
    <section id="screen-intent" className="screen active">
      <div className="page-intro">
        <h1>{view === 'performance'
          ? translate('员工表现', 'Employee performance')
          : translate('沟通意图', 'Conversation intent')}</h1>
      </div>
      <div className={`split-layout narrow-right intent-layout-five${view === 'performance' ? ' intent-layout-performance' : ''}${view === 'performance' && isGoalsPanelOpen ? ' intent-layout-goals-open' : ''}`}>
        <section className="soft-card intent-card intent-card-five">
          {view === 'select' ? (
            <div className="intent-stage intent-stage-select">
              <div className="intent-options intent-options-five">
                {options.intents.map((item, index) => {
                  const selected = item.id === selectedIntentId;
                  return (
                    <button
                      key={item.id}
                      className={`intent-option intent-option-compact ${selected ? 'selected' : ''}`}
                      type="button"
                      onClick={() => setSelectedIntentId(item.id)}
                    >
                      <span className="option-icon">{selected ? '✓' : String(index + 1).padStart(2, '0')}</span>
                      <span className="option-content">
                        <h3>{intentName(item.id, item.name, language)}</h3>
                        {intentDescription(item.id, item.description, language) && (
                          <p>{intentDescription(item.id, item.description, language)}</p>
                        )}
                      </span>
                      <span className="option-radio" />
                    </button>
                  );
                })}
              </div>
              <div className={'intent-card-action' + (intentWarning ? ' has-warning' : '')}>
                {intentWarning && eligibilityMessage && (
                  <div id="intent-selection-warning" className="intent-eligibility-warning" role="status">
                    <AlertTriangle aria-hidden="true" />
                    <p>{eligibilityMessage}</p>
                  </div>
                )}
                <button
                  className="btn btn-primary btn-wide"
                  type="button"
                  onClick={continueToPerformance}
                  disabled={!selectedIntentId}
                  aria-describedby={intentWarning ? 'intent-selection-warning' : undefined}
                >
                  {intentWarning
                    ? translate('仍然选择并继续', 'Continue with this selection')
                    : translate('确认沟通意图', 'Confirm conversation intent')}
                </button>
              </div>
            </div>
          ) : (
            <div className="intent-stage intent-performance-stage">
              <header className="intent-performance-head">
                <button
                  type="button"
                  className="intent-back-button"
                  onClick={() => setView('select')}
                  aria-label={translate('返回沟通意图选择', 'Back to conversation-intent selection')}
                  title={translate('返回沟通意图选择', 'Back to conversation-intent selection')}
                >
                  <ArrowLeft aria-hidden="true" />
                </button>
                <div>
                  <span>{translate('当前沟通意图', 'Current conversation intent')}</span>
                  <h2>{intent
                    ? intentName(intent.id, intent.name, language)
                    : translate('待选择', 'Not selected')}</h2>
                </div>
                <button
                  type="button"
                  className="intent-goals-toggle"
                  onClick={() => setIsGoalsPanelOpen((current) => !current)}
                  aria-controls="intent-employee-goals-panel"
                  aria-expanded={isGoalsPanelOpen}
                  aria-label={isGoalsPanelOpen
                    ? translate('收起当前员工目标', 'Collapse current employee goals')
                    : translate('展开当前员工目标', 'Expand current employee goals')}
                  title={isGoalsPanelOpen
                    ? translate('收起当前员工目标', 'Collapse current employee goals')
                    : translate('展开当前员工目标', 'Expand current employee goals')}
                >
                  {isGoalsPanelOpen
                    ? <PanelRightClose aria-hidden="true" />
                    : <PanelRightOpen aria-hidden="true" />}
                </button>
              </header>

              <div
                className="intent-goal-performance-table"
                role="table"
                aria-label={translate('员工表现编辑', 'Edit employee performance')}
              >
                <div className="intent-goal-performance-header" role="row">
                  <span role="columnheader">{translate('分析维度', 'Analysis dimension')}</span>
                  <span className="intent-editable-column-header" role="columnheader">
                    {translate('模拟完成情况', 'Simulated completion status')}
                    <small>
                      <PencilLine aria-hidden="true" />
                      {isGenerating
                        ? translate('生成后修改', 'Edit after generation')
                        : isSubmitting
                          ? translate('正在保存', 'Saving')
                          : translate('点击文字修改', 'Click the text to edit')}
                    </small>
                  </span>
                </div>

                <div className="intent-goal-performance-body" role="rowgroup">
                  {visibleRows.map((item, index) => {
                    return (
                      <div className="intent-goal-performance-row" role="row" key={`${index}-${item.goal}`}>
                        <div className="intent-goal-cell" role="cell">
                          <p>{performanceTitle(item.goal)}</p>
                        </div>
                        <div className="intent-current-performance-cell" role="cell">
                          {isGenerating && !item.current_performance.trim() ? (
                            <div className="intent-performance-line-skeleton" role="status" aria-label={translateTemplate(
                              '正在生成“{goal}”',
                              'Generating “{goal}”',
                              { goal: performanceTitle(item.goal) },
                            )}>
                              <span />
                              <span />
                            </div>
                          ) : (
                            <PerformanceLineEditor
                              value={item.current_performance}
                              onChange={(value) => updateDraftItem(index, value)}
                              placeholder={translateTemplate(
                                '填写{goal}，综合全部目标而非逐项目标拆分。',
                                'Describe {goal} across all goals rather than item by item.',
                                { goal: performanceTitle(item.goal) },
                              )}
                              label={translateTemplate(
                                '{goal}的可编辑内容',
                                'Editable content for {goal}',
                                { goal: performanceTitle(item.goal) },
                              )}
                              readOnly={isEditorLocked}
                            />
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              {currentError && (
                <div className="intent-draft-error" role="alert">
                  <AlertTriangle aria-hidden="true" />
                  <span>{currentError}</span>
                </div>
              )}

              <div className="intent-performance-actions">
                <button
                  className="btn btn-primary"
                  type="button"
                  onClick={submit}
                  disabled={!hasCompleteDraft || isEditorLocked}
                  aria-busy={isSubmitting}
                >
                  {translate('确认并进入人格与诉求', 'Confirm and continue to persona and motives')}
                </button>
              </div>
            </div>
          )}
        </section>
        {view === 'performance' && (
          <aside
            id="intent-employee-goals-panel"
            className="intent-goals-panel"
            aria-label={translate('当前员工目标', 'Current employee goals')}
            hidden={!isGoalsPanelOpen}
          >
            <EmployeeProfileCard
              title={translate('当前员工目标', 'Current employee goals')}
              rows={employeeGoalRows}
              emptyText={translate('暂无员工目标信息。', 'No employee goals are available.')}
              hideRowLabelKeys={['GO']}
              goalSource={profileGoalSource(displayedProfile)}
              className="intent-goals-card"
            />
          </aside>
        )}
        {view === 'select' && (
          <EmployeeProfileCard
            title={translate('员工档案', 'Employee profile')}
            rows={employeeRows}
            emptyText={translate('暂无员工档案信息。', 'No employee profile information is available.')}
            contextTitle={translate('本次沟通', 'This conversation')}
            contextRows={[[
              'IN',
              translate('沟通意图', 'Conversation intent'),
              intent
                ? intentName(intent.id, intent.name, language)
                : translate('待选择', 'Not selected'),
            ]]}
            className="intent-detail-card profile-dossier-grid"
          >
            {canReturnToPerformance && (
              <section
                className="intent-profile-performance-summary"
                aria-labelledby="intent-profile-performance-title"
              >
                <div className="intent-profile-performance-summary-head">
                  <h3 id="intent-profile-performance-title">{translate('员工表现', 'Employee performance')}</h3>
                  <button
                    className="intent-performance-return-button"
                    type="button"
                    onClick={continueToPerformance}
                    aria-label={translate('返回员工当前表现', 'Return to current employee performance')}
                    title={translate('返回员工当前表现', 'Return to current employee performance')}
                  >
                    <ArrowRight aria-hidden="true" />
                  </button>
                </div>
                {profilePerformanceItems.length > 0 ? (
                  <div className="intent-profile-performance-list">
                    {profilePerformanceItems.map((item) => (
                      <article key={item.goal}>
                        <h4>{performanceTitle(item.goal)}</h4>
                        <p>{item.current_performance}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="intent-profile-performance-status" role={isGenerating ? 'status' : undefined}>
                    {isGenerating
                      ? translate('正在生成员工表现...', 'Generating employee performance...')
                      : translate('员工表现尚未生成。', 'Employee performance has not been generated yet.')}
                  </p>
                )}
              </section>
            )}
          </EmployeeProfileCard>
        )}
      </div>
    </section>
  );
}
