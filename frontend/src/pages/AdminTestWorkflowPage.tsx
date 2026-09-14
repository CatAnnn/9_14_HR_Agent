import {
  ArrowLeft,
  BarChart3,
  FlaskConical,
  MessageSquareText,
  Plus,
  RotateCcw,
  Trash2,
} from 'lucide-react';
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { BoschSupergraphic } from '../components/BoschSupergraphic';
import { Brand } from '../components/Brand';
import { LanguageSelector } from '../components/LanguageSelector';
import { RouteRedirect } from '../components/RouteLoading';
import {
  intentName,
  motiveDescription,
  motiveName,
} from '../i18n/businessLabels';
import { useLanguage, type AppLanguage } from '../i18n/LanguageContext';
import { useAuthStore } from '../store/authStore';
import type {
  AdminTestConversationTurn,
  AdminTestWorkflowDestination,
  BigFivePersonality,
  SetupOptions,
} from '../types/domain';
import { ADMIN_TEST_SESSION_STORAGE_KEY } from '../utils/adminTestWorkflow';
import { userFacingErrorMessage } from '../utils/displayText';
import {
  fromPersonalitySliderValue,
  PERSONALITY_DIMENSIONS,
  type PersonalityDimensionCopy,
  personalitySliderText,
  toPersonalitySliderValue,
} from '../utils/personalitySlider';
import { removeWorkflowSessionDrafts } from '../utils/workflowDraftStorage';

type DraftTurn = AdminTestConversationTurn & { id: number };
type AdminTestFeedback =
  | { kind: 'validation'; code: 'conversation_required' }
  | { kind: 'request'; cause: unknown; fallback: 'config_load' | 'session_create' };

function adminTestFeedbackMessage(
  feedback: AdminTestFeedback | null,
  translate: (source: string, english?: string) => string,
): string {
  if (!feedback) return '';
  if (feedback.kind === 'validation') {
    return translate(
      '直接生成复盘至少需要一条管理者发言和一条员工发言。',
      'Generating a review directly requires at least one Manager turn and one Employee turn.',
    );
  }
  const fallback = feedback.fallback === 'config_load'
    ? translate('测试配置载入失败。', 'The test configuration failed to load.')
    : translate('测试会话创建失败。', 'The test session could not be created.');
  return userFacingErrorMessage(feedback.cause, undefined, fallback);
}

const DEFAULT_BIG_FIVE: BigFivePersonality = {
  openness: 50,
  conscientiousness: 50,
  extraversion: 50,
  agreeableness: 50,
  neuroticism: 50,
};

const SAMPLE_CONVERSATION: DraftTurn[] = [
  { id: 1, speaker: 'manager', text: '今天想和你回顾本周期的绩效结果，也一起明确接下来的改进重点。' },
  { id: 2, speaker: 'employee', text: '我对这个结果有些意外，之前没有听到这么明确的反馈。' },
  { id: 3, speaker: 'manager', text: '先不用纠结过程，结果没有达到要求是比较明确的。' },
  { id: 4, speaker: 'employee', text: '但目标中途调整过，我希望这些变化也能被考虑。' },
  { id: 5, speaker: 'manager', text: '你负责的交付有两次延期，跨团队问题也没有及时升级，这些都影响了最终结果。' },
  { id: 6, speaker: 'employee', text: '我理解有延期，但我不清楚你希望我接下来具体做到什么程度。' },
  { id: 7, speaker: 'manager', text: '未来两个月请按周同步风险，并在关键节点前完成方案评审，我们每两周复盘一次。' },
  { id: 8, speaker: 'employee', text: '可以，我希望我们把衡量标准和需要的支持也一起写清楚。' },
];

const SAMPLE_CONVERSATION_ENGLISH: DraftTurn[] = [
  { id: 1, speaker: 'manager', text: 'I would like to review your performance results for this cycle and clarify the improvement priorities ahead.' },
  { id: 2, speaker: 'employee', text: 'I am surprised by the result because I had not received feedback this explicit before.' },
  { id: 3, speaker: 'manager', text: 'Let us not dwell on the process. It is clear that the result did not meet expectations.' },
  { id: 4, speaker: 'employee', text: 'The goals changed during the cycle, and I would like those changes to be considered as well.' },
  { id: 5, speaker: 'manager', text: 'Two of your deliverables were late, and cross-team issues were not escalated promptly. Both affected the final result.' },
  { id: 6, speaker: 'employee', text: 'I understand there were delays, but I am not clear about the exact standard you expect me to meet next.' },
  { id: 7, speaker: 'manager', text: 'For the next two months, please provide weekly risk updates and complete solution reviews before key milestones. We will review progress every two weeks.' },
  { id: 8, speaker: 'employee', text: 'That works for me. I would also like us to document the success measures and the support I will need.' },
];

const SAMPLE_CONVERSATION_GERMAN: DraftTurn[] = [
  { id: 1, speaker: 'manager', text: 'Ich möchte die Leistungsergebnisse dieses Zyklus mit Ihnen besprechen und die nächsten Verbesserungsschwerpunkte klären.' },
  { id: 2, speaker: 'employee', text: 'Das Ergebnis überrascht mich, weil ich zuvor kein so eindeutiges Feedback erhalten hatte.' },
  { id: 3, speaker: 'manager', text: 'Lassen Sie uns nicht zu lange beim Prozess bleiben. Das Ergebnis hat die Erwartungen eindeutig nicht erfüllt.' },
  { id: 4, speaker: 'employee', text: 'Die Ziele wurden während des Zyklus geändert. Ich möchte, dass diese Änderungen ebenfalls berücksichtigt werden.' },
  { id: 5, speaker: 'manager', text: 'Zwei Ihrer Liefertermine wurden nicht eingehalten, und teamübergreifende Probleme wurden nicht rechtzeitig eskaliert. Beides hat das Endergebnis beeinflusst.' },
  { id: 6, speaker: 'employee', text: 'Ich verstehe, dass es Verzögerungen gab, aber mir ist noch nicht klar, welchen konkreten Standard ich künftig erfüllen soll.' },
  { id: 7, speaker: 'manager', text: 'Bitte melden Sie in den nächsten zwei Monaten Risiken wöchentlich und schließen Sie die Lösungsprüfung vor wichtigen Meilensteinen ab. Wir prüfen den Fortschritt alle zwei Wochen.' },
  { id: 8, speaker: 'employee', text: 'Damit kann ich arbeiten. Ich möchte außerdem die Erfolgskriterien und die erforderliche Unterstützung gemeinsam festhalten.' },
];

const SAMPLE_CONVERSATION_JAPANESE: DraftTurn[] = [
  { id: 1, speaker: 'manager', text: '今期のパフォーマンス結果を振り返り、今後の改善ポイントを明確にしたいと思います。' },
  { id: 2, speaker: 'employee', text: 'これほど明確なフィードバックはこれまで受けていなかったので、この結果には少し驚いています。' },
  { id: 3, speaker: 'manager', text: 'まずプロセスの議論は置いておきましょう。結果が期待水準に達していないことは明確です。' },
  { id: 4, speaker: 'employee', text: 'ただ、期中に目標が変更されています。その変化も考慮してほしいです。' },
  { id: 5, speaker: 'manager', text: '担当した成果物で2回の遅延があり、部門横断の問題も適時にエスカレーションされませんでした。どちらも最終結果に影響しています。' },
  { id: 6, speaker: 'employee', text: '遅延があったことは理解していますが、今後どの水準まで達成することを求められているのかが明確ではありません。' },
  { id: 7, speaker: 'manager', text: '今後2か月はリスクを週次で共有し、重要なマイルストーンの前に案のレビューを完了してください。進捗は2週間ごとに振り返ります。' },
  { id: 8, speaker: 'employee', text: 'わかりました。成功基準と必要な支援についても、一緒に明文化したいです。' },
];

const SAMPLE_CONVERSATIONS_BY_LANGUAGE: Readonly<Record<AppLanguage, readonly DraftTurn[]>> = {
  'zh-CN': SAMPLE_CONVERSATION,
  en: SAMPLE_CONVERSATION_ENGLISH,
  de: SAMPLE_CONVERSATION_GERMAN,
  ja: SAMPLE_CONVERSATION_JAPANESE,
};

function sampleConversation(language: AppLanguage): DraftTurn[] {
  return SAMPLE_CONVERSATIONS_BY_LANGUAGE[language].map((turn) => ({ ...turn }));
}

const PERSONALITY_COPY_ENGLISH = {
  openness: { label: 'Open to trying new approaches', low: 'Prefers familiar approaches', mid: 'Willing to try some new approaches', high: 'Very willing to try new approaches' },
  conscientiousness: { label: 'Works with a plan', low: 'Often adjusts while working', mid: 'Generally follows a plan', high: 'Plans ahead and follows through carefully' },
  extraversion: { label: 'Proactively expresses ideas', low: 'Usually listens more and speaks less', mid: 'Shares ideas when needed', high: 'Frequently volunteers ideas' },
  agreeableness: { label: 'Willing to consult with others', low: 'Prefers to follow their own approach', mid: 'Consults with others', high: 'Actively seeks input and works on solutions together' },
  neuroticism: { label: 'Stays steady under pressure', low: 'More likely to feel tense or worried', mid: 'May feel tense under high pressure', high: 'Generally remains calm under pressure' },
} as const;

function localizedPersonalityCopy(
  dimension: PersonalityDimensionCopy,
  translate: (source: string, english?: string) => string,
): PersonalityDimensionCopy {
  const english = PERSONALITY_COPY_ENGLISH[dimension.key];
  return {
    key: dimension.key,
    label: translate(dimension.label, english.label),
    low: translate(dimension.low, english.low),
    mid: translate(dimension.mid, english.mid),
    high: translate(dimension.high, english.high),
  };
}

function motiveSelection(options: SetupOptions, intentId: string) {
  const recommendation = options.motive_recommendations?.[intentId]
    || options.default_motive_recommendation;
  const validIds = new Set(options.motives.map((item) => item.id));
  const primary = recommendation?.primary_motive_id
    && validIds.has(recommendation.primary_motive_id)
    ? recommendation.primary_motive_id
    : options.motives[0]?.id || '';
  const recommendedSecondary = (recommendation?.secondary_motive_ids || [])
    .filter((id) => validIds.has(id) && id !== primary)
    .slice(0, 2);
  const secondary = recommendedSecondary.length
    ? recommendedSecondary
    : options.motives.filter((item) => item.id !== primary).slice(0, 1).map((item) => item.id);
  return { primary, secondary };
}

export default function AdminTestWorkflowPage() {
  const navigate = useNavigate();
  const { language, translate, translateTemplate } = useLanguage();
  const { user } = useAuthStore();
  const [options, setOptions] = useState<SetupOptions | null>(null);
  const [intentId, setIntentId] = useState('');
  const [primaryMotiveId, setPrimaryMotiveId] = useState('');
  const [secondaryMotiveIds, setSecondaryMotiveIds] = useState<string[]>([]);
  const [personality, setPersonality] = useState<BigFivePersonality>(DEFAULT_BIG_FIVE);
  const [conversation, setConversation] = useState<DraftTurn[]>(() => sampleConversation(language));
  const [busy, setBusy] = useState<AdminTestWorkflowDestination | null>(null);
  const [feedback, setFeedback] = useState<AdminTestFeedback | null>(null);
  const error = adminTestFeedbackMessage(feedback, translate);

  useEffect(() => {
    if (user?.role !== 'admin') return;
    void api.setupOptions()
      .then((loaded) => {
        const initialIntentId = loaded.default_intent || loaded.intents[0]?.id || '';
        const motives = motiveSelection(loaded, initialIntentId);
        setOptions(loaded);
        setIntentId(initialIntentId);
        setPrimaryMotiveId(motives.primary);
        setSecondaryMotiveIds(motives.secondary);
        setPersonality(loaded.default_big_five || DEFAULT_BIG_FIVE);
      })
      .catch((reason) => setFeedback({
        kind: 'request',
        cause: reason,
        fallback: 'config_load',
      }));
  }, [user?.role]);

  const intent = options?.intents.find((item) => item.id === intentId);
  const selectedPrimaryMotive = options?.motives.find((item) => item.id === primaryMotiveId);
  const reportConversationValid = useMemo(() => {
    const valid = conversation.filter((turn) => turn.text.trim());
    return valid.some((turn) => turn.speaker === 'manager')
      && valid.some((turn) => turn.speaker === 'employee');
  }, [conversation]);
  const selectionValid = Boolean(
    options
    && intentId
    && primaryMotiveId
    && secondaryMotiveIds.length <= 2
    && !secondaryMotiveIds.includes(primaryMotiveId),
  );

  if (user?.role !== 'admin') return <RouteRedirect to="/" />;

  const selectIntent = (nextIntentId: string) => {
    if (!options) return;
    const motives = motiveSelection(options, nextIntentId);
    setIntentId(nextIntentId);
    setPrimaryMotiveId(motives.primary);
    setSecondaryMotiveIds(motives.secondary);
  };

  const selectPrimaryMotive = (nextPrimaryId: string) => {
    if (!options) return;
    const remaining = secondaryMotiveIds.filter((id) => id !== nextPrimaryId);
    if (!remaining.length) {
      const replacement = options.motives.find((item) => item.id !== nextPrimaryId)?.id;
      if (replacement) remaining.push(replacement);
    }
    setPrimaryMotiveId(nextPrimaryId);
    setSecondaryMotiveIds(remaining.slice(0, 2));
  };

  const toggleSecondaryMotive = (motiveId: string) => {
    if (motiveId === primaryMotiveId) return;
    setSecondaryMotiveIds((current) => {
      if (current.includes(motiveId)) return current.filter((id) => id !== motiveId);
      if (current.length >= 2) return current;
      return [...current, motiveId];
    });
  };

  const updateTurn = (id: number, patch: Partial<AdminTestConversationTurn>) => {
    setConversation((current) => current.map((turn) => turn.id === id ? { ...turn, ...patch } : turn));
  };

  const addTurn = () => {
    setConversation((current) => [
      ...current,
      {
        id: Math.max(0, ...current.map((turn) => turn.id)) + 1,
        speaker: current[current.length - 1]?.speaker === 'manager' ? 'employee' : 'manager',
        text: '',
      },
    ]);
  };

  const createTestWorkflow = async (destination: AdminTestWorkflowDestination) => {
    if (!selectionValid) return;
    if (destination === 'report' && !reportConversationValid) {
      setFeedback({ kind: 'validation', code: 'conversation_required' });
      return;
    }
    setBusy(destination);
    setFeedback(null);
    try {
      let previousSessionId: string | null = null;
      try {
        previousSessionId = window.localStorage.getItem(ADMIN_TEST_SESSION_STORAGE_KEY);
      } catch {
        // A blocked read must not turn draft cleanup into a workflow creation failure.
      }
      const created = await api.createAdminTestWorkflow({
        destination,
        intent_id: intentId,
        personality,
        primary_motive_id: primaryMotiveId,
        secondary_motive_ids: secondaryMotiveIds,
        conversation: destination === 'report'
          ? conversation
            .filter((turn) => turn.text.trim())
            .map(({ speaker, text }) => ({ speaker, text: text.trim() }))
          : [],
      });
      window.localStorage.setItem(ADMIN_TEST_SESSION_STORAGE_KEY, created.session_id);
      if (previousSessionId && previousSessionId !== created.session_id) {
        let draftStorage: Storage | null = null;
        try {
          draftStorage = window.sessionStorage;
        } catch {
          // The new test session remains usable when draft storage is unavailable.
        }
        removeWorkflowSessionDrafts(
          draftStorage,
          ADMIN_TEST_SESSION_STORAGE_KEY,
          previousSessionId,
        );
      }
      navigate(`/admin/test-workflow/${destination}`);
    } catch (reason) {
      setFeedback({
        kind: 'request',
        cause: reason,
        fallback: 'session_create',
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="admin-test-page">
      <BoschSupergraphic />
      <header className="admin-test-header">
        <Brand />
        <div>
          <span>{translate('管理员测试通道', 'Administrator test channel')}</span>
          <h1>{translate('流程测试配置', 'Workflow test configuration')}</h1>
        </div>
        <div className="admin-test-header-actions">
          <LanguageSelector className="btn btn-secondary" compact />
          <button className="btn btn-secondary" type="button" onClick={() => navigate('/admin')}>
            <ArrowLeft size={17} aria-hidden="true" />
            {translate('返回管理页面', 'Back to administration')}
          </button>
        </div>
      </header>

      <section className="admin-test-config" aria-busy={!options || Boolean(busy)}>
        <div className="admin-test-intro">
          <div className="admin-test-intro-icon" aria-hidden="true"><FlaskConical size={23} /></div>
          <div>
            <h2>{translate('构造独立测试会话', 'Create an isolated test session')}</h2>
            <p>{translate(
              '系统自动生成符合所选意图规则的模拟员工档案。该会话只归当前管理员所有，不会覆盖正常工作会话。',
              'The system creates a synthetic employee profile that follows the selected intent. This session belongs only to the current administrator and does not overwrite a normal workflow session.',
            )}</p>
          </div>
        </div>

        {error && <div className="auth-error admin-test-error" role="alert">{error}</div>}

        <section className="admin-test-section admin-test-intent-section">
          <div className="admin-test-section-heading">
            <span>01</span>
            <div><h2>{translate('沟通意图', 'Conversation intent')}</h2><p>{translate('选择本次测试采用的业务情景。', 'Choose the business scenario for this test.')}</p></div>
          </div>
          <div className="admin-test-intents" role="radiogroup" aria-label={translate('沟通意图', 'Conversation intent')}>
            {(options?.intents || []).map((item) => (
              <button
                key={item.id}
                type="button"
                role="radio"
                aria-checked={item.id === intentId}
                className={item.id === intentId ? 'selected' : ''}
                onClick={() => selectIntent(item.id)}
              >
                <strong>{intentName(item.id, item.name, language)}</strong>
                <span>{translate('业务测试情景', 'Business test scenario')}</span>
              </button>
            ))}
          </div>
        </section>

        <div className="admin-test-two-column">
          <section className="admin-test-section">
            <div className="admin-test-section-heading">
              <span>02</span>
              <div><h2>{translate('员工诉求', 'Employee motives')}</h2><p>{translate('设置一项主诉求；辅诉求可不选，最多两项。', 'Select one primary motive and up to two optional secondary motives.')}</p></div>
            </div>
            <label className="admin-test-select-field">
              <span>{translate('员工的主诉求', 'Employee primary motive')}</span>
              <select value={primaryMotiveId} onChange={(event) => selectPrimaryMotive(event.target.value)}>
                {(options?.motives || []).map((item) => (
                  <option key={item.id} value={item.id}>{motiveName(item.id, item.name, language)}</option>
                ))}
              </select>
              {selectedPrimaryMotive?.description && <small>{motiveDescription(selectedPrimaryMotive.id, selectedPrimaryMotive.description, language)}</small>}
            </label>
            <div className="admin-test-secondary-motives">
              <span>{translate('员工的辅诉求（可选）', 'Employee secondary motives (optional)')}</span>
              <div>
                {(options?.motives || []).map((item) => {
                  const checked = secondaryMotiveIds.includes(item.id);
                  const disabled = item.id === primaryMotiveId || (!checked && secondaryMotiveIds.length >= 2);
                  return (
                    <label key={item.id} className={checked ? 'selected' : ''}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled}
                        onChange={() => toggleSecondaryMotive(item.id)}
                      />
                      <span>{motiveName(item.id, item.name, language)}</span>
                    </label>
                  );
                })}
              </div>
            </div>
          </section>

          <section className="admin-test-section">
            <div className="admin-test-section-heading">
              <span>03</span>
              <div><h2>{translate('大五人格', 'Big Five personality')}</h2></div>
            </div>
            <div className="admin-test-personality">
              {PERSONALITY_DIMENSIONS.map((dimension) => {
                const value = toPersonalitySliderValue(dimension.key, personality[dimension.key]);
                const copy = localizedPersonalityCopy(dimension, translate);
                return (
                  <label
                    key={dimension.key}
                    className="admin-test-personality-row"
                    style={{ '--personality-value': `${value}%` } as CSSProperties}
                  >
                    <span><strong>{copy.label}</strong><small>{personalitySliderText(value, copy)}</small></span>
                    <span className="admin-test-personality-control">
                      <input
                        type="range"
                        min="0"
                        max="100"
                        step="1"
                        value={value}
                        onChange={(event) => setPersonality((current) => ({
                          ...current,
                          [dimension.key]: fromPersonalitySliderValue(dimension.key, Number(event.target.value)),
                        }))}
                        aria-label={copy.label}
                        aria-valuetext={personalitySliderText(value, copy)}
                      />
                      <span className="personality-scale-ends" aria-hidden="true">
                        <span>{copy.low}</span>
                        <span>{copy.high}</span>
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          </section>
        </div>

        <section className="admin-test-section admin-test-transcript-section">
          <div className="admin-test-section-heading admin-test-transcript-heading">
            <span>04</span>
            <div>
              <h2>{translate('复盘测试对话', 'Review-test conversation')}</h2>
              <p>{translate('仅“直接生成复盘”使用。预置内容是模拟样例，可按测试目标修改。', 'Used only when generating a review directly. Edit the synthetic sample to match the test objective.')}</p>
            </div>
            <button className="btn btn-secondary" type="button" onClick={() => setConversation(
              sampleConversation(language),
            )}>
              <RotateCcw size={16} aria-hidden="true" />
              {translate('恢复样例', 'Restore sample')}
            </button>
          </div>
          <div className="admin-test-turns">
            {conversation.map((turn, index) => (
              <div className="admin-test-turn" key={turn.id}>
                <div className="admin-test-speaker-control" role="group" aria-label={translateTemplate('第 {turn} 轮发言人', 'Speaker for turn {turn}', { turn: index + 1 })}>
                  <button
                    type="button"
                    className={turn.speaker === 'manager' ? 'active' : ''}
                    onClick={() => updateTurn(turn.id, { speaker: 'manager' })}
                  >{translate('管理者', 'Manager')}</button>
                  <button
                    type="button"
                    className={turn.speaker === 'employee' ? 'active' : ''}
                    onClick={() => updateTurn(turn.id, { speaker: 'employee' })}
                  >{translate('员工', 'Employee')}</button>
                </div>
                <textarea
                  value={turn.text}
                  rows={2}
                  aria-label={translateTemplate('第 {turn} 轮内容', 'Content of turn {turn}', { turn: index + 1 })}
                  onChange={(event) => updateTurn(turn.id, { text: event.target.value })}
                />
                <button
                  className="icon-button"
                  type="button"
                  title={translate('删除该轮', 'Delete this turn')}
                  aria-label={translateTemplate('删除第 {turn} 轮', 'Delete turn {turn}', { turn: index + 1 })}
                  onClick={() => setConversation((current) => current.filter((item) => item.id !== turn.id))}
                >
                  <Trash2 size={17} />
                </button>
              </div>
            ))}
          </div>
          <button className="admin-test-add-turn" type="button" onClick={addTurn}>
            <Plus size={17} aria-hidden="true" />
            {translate('增加一轮', 'Add turn')}
          </button>
        </section>

        <footer className="admin-test-actions">
          <div>
            <strong>{intentName(intent?.id, intent?.name || translate('等待配置', 'Waiting for configuration'), language)}</strong>
            <span>{translate('将创建一条新的管理员测试会话', 'A new administrator test session will be created.')}</span>
          </div>
          <button
            className="btn btn-secondary"
            type="button"
            disabled={!selectionValid || Boolean(busy)}
            onClick={() => void createTestWorkflow('report')}
          >
            <BarChart3 size={18} aria-hidden="true" />
            {busy === 'report' ? translate('正在创建', 'Creating') : translate('直接生成复盘', 'Generate review directly')}
          </button>
          <button
            className="btn btn-primary"
            type="button"
            disabled={!selectionValid || Boolean(busy)}
            onClick={() => void createTestWorkflow('rehearsal')}
          >
            <MessageSquareText size={18} aria-hidden="true" />
            {busy === 'rehearsal' ? translate('正在创建', 'Creating') : translate('进入多轮预演', 'Start rehearsal')}
          </button>
        </footer>
      </section>
    </main>
  );
}
