import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import EmployeeProfileCard from '../../components/EmployeeProfileCard';
import { useWorkflow } from '../../context/WorkflowContext';
import { useLanguage } from '../../i18n/LanguageContext';
import {
  intentName,
  motiveDescription,
  motiveName,
} from '../../i18n/businessLabels';
import type { MotiveOption, SessionLocale } from '../../types/domain';
import { dossierEmployeeProfileRows, safeList } from '../../utils/format';
import {
  fromPersonalitySliderValue,
  PERSONALITY_DIMENSIONS,
  type PersonalityDimensionCopy,
  personalitySliderText,
  toPersonalitySliderValue,
} from '../../utils/personalitySlider';

const PERSONALITY_COPY_ENGLISH = {
  openness: {
    key: 'openness',
    label: 'Open to trying new approaches',
    low: 'Prefers familiar approaches',
    mid: 'Willing to try some new approaches',
    high: 'Very willing to try new approaches',
  },
  conscientiousness: {
    key: 'conscientiousness',
    label: 'Works with a plan',
    low: 'Often adjusts while working',
    mid: 'Generally follows a plan',
    high: 'Plans ahead and follows through carefully',
  },
  extraversion: {
    key: 'extraversion',
    label: 'Proactively expresses ideas',
    low: 'Usually listens more and speaks less',
    mid: 'Shares ideas when needed',
    high: 'Frequently volunteers ideas',
  },
  agreeableness: {
    key: 'agreeableness',
    label: 'Willing to consult with others',
    low: 'Prefers to follow their own approach',
    mid: 'Consults with others',
    high: 'Actively seeks input and works on solutions together',
  },
  neuroticism: {
    key: 'neuroticism',
    label: 'Stays steady under pressure',
    low: 'More likely to feel tense or worried',
    mid: 'May feel tense under high pressure',
    high: 'Generally remains calm under pressure',
  },
} as const;

const PROFILE_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '工号': 'Employee ID',
  '姓名': 'Name',
  '岗位': 'Role',
  '部门': 'Department',
  '职级': 'Level',
  '当前绩效评级': 'Current performance rating',
  '汇报关系': 'Reporting line',
};

function motiveText(motive: MotiveOption | null | undefined, language: SessionLocale) {
  if (!motive) return '';
  const description = motiveDescription(motive.id, motive.description, language);
  if (language !== 'zh-CN') return description;
  const examples = safeList<string>(motive.examples).slice(0, 2).join(' / ');
  return [description, examples].filter(Boolean).join('；');
}

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

export default function SimulationStep() {
  const navigate = useNavigate();
  const { language, translate } = useLanguage();
  const [primaryOpen, setPrimaryOpen] = useState(false);
  const [secondaryOpen, setSecondaryOpen] = useState(false);
  const primaryDropdownRef = useRef<HTMLDivElement>(null);
  const secondaryDropdownRef = useRef<HTMLDivElement>(null);
  const {
    options,
    selectedIntentId,
    selectedPersonality,
    selectedPrimaryMotiveId,
    selectedSecondaryMotiveIds,
    displayedProfile,
    updatePersonalityDimension,
    setSelectedPrimaryMotiveId,
    setSelectedSecondaryMotiveIds,
    confirmSimulation,
  } = useWorkflow();

  const intent = options.intents.find((item) => item.id === selectedIntentId);
  const primaryMotive = options.motives.find((item) => item.id === selectedPrimaryMotiveId);
  const secondaryMotives = selectedSecondaryMotiveIds
    .map((id) => options.motives.find((item) => item.id === id))
    .filter(Boolean) as MotiveOption[];
  const employeeRows = dossierEmployeeProfileRows(displayedProfile).map(([key, label, value]) => ([
    key,
    translate(label, PROFILE_LABELS_ENGLISH[label]),
    value,
  ] as ReturnType<typeof dossierEmployeeProfileRows>[number]));
  const canSubmit = Boolean(
    primaryMotive
    && selectedSecondaryMotiveIds.length <= 2
    && secondaryMotives.length === selectedSecondaryMotiveIds.length
    && !selectedSecondaryMotiveIds.includes(primaryMotive.id)
    && new Set(selectedSecondaryMotiveIds).size === selectedSecondaryMotiveIds.length
  );
  const secondarySelectionText = secondaryMotives.length
    ? secondaryMotives.map((item) => motiveName(item.id, item.name, language)).join(' / ')
    : translate('未选择（最多选择两项）', 'None selected (choose up to two)');

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!primaryDropdownRef.current?.contains(target)) setPrimaryOpen(false);
      if (!secondaryDropdownRef.current?.contains(target)) setSecondaryOpen(false);
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setPrimaryOpen(false);
      setSecondaryOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, []);

  const submit = async () => {
    await confirmSimulation();
    navigate('/app/guidance');
  };

  const toggleSecondaryMotive = (id: string) => {
    if (!id || id === selectedPrimaryMotiveId) return;
    setSelectedSecondaryMotiveIds((current) => {
      if (current.includes(id)) {
        return current.filter((item) => item !== id);
      }
      if (current.length >= 2) return current;
      return [...current, id];
    });
  };

  return (
    <section id="screen-simulation" className="screen active">
      <div className="page-intro">
        <h1>{translate('人格与诉求', 'Persona and motives')}</h1>
      </div>
      <div className="split-layout narrow-right simulation-layout">
        <section className="soft-card simulation-card">
          <div className="panel-section-head motive-heading motive-heading-first"><div><h2>{translate('诉求组合', 'Motive combination')}</h2></div></div>
          <div className="motive-select-panel">
            <div className={`motive-select-field motive-primary-select${primaryOpen ? ' is-open' : ''}`} ref={primaryDropdownRef}>
              <span>{translate('员工的主诉求', 'Employee primary motive')}</span>
              <div className="motive-dropdown-control">
                <button
                  type="button"
                  className={`motive-multiselect-trigger motive-primary-trigger${primaryMotive ? ' is-filled' : ''}`}
                  aria-haspopup="listbox"
                  aria-expanded={primaryOpen}
                  aria-controls="primary-motive-options"
                  onClick={() => {
                    setPrimaryOpen((open) => !open);
                    setSecondaryOpen(false);
                  }}
                >
                  <strong>{primaryMotive
                    ? motiveName(primaryMotive.id, primaryMotive.name, language)
                    : translate('未选择（单选）', 'Not selected (choose one)')}</strong>
                  <i aria-hidden="true" />
                </button>
                {primaryOpen && (
                  <div id="primary-motive-options" className="motive-multiselect-menu motive-primary-menu" role="listbox" aria-label={translate('员工的主诉求', 'Employee primary motive')}>
                    {options.motives.map((item) => {
                      const selected = item.id === selectedPrimaryMotiveId;
                      return (
                        <button
                          key={item.id}
                          type="button"
                          className={'motive-multiselect-option' + (selected ? ' selected' : '')}
                          role="option"
                          aria-selected={selected}
                          onClick={() => {
                            setSelectedPrimaryMotiveId(item.id);
                            setPrimaryOpen(false);
                          }}
                        >
                          <span>
                            <strong>{motiveName(item.id, item.name, language)}</strong>
                            <small>{motiveText(item, language)}</small>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>

            <div className={`motive-select-field motive-multiselect${secondaryOpen ? ' is-open' : ''}`} ref={secondaryDropdownRef}>
              <span>{translate('员工的辅诉求', 'Employee secondary motives')}</span>
              <div className="motive-dropdown-control">
                <button
                  type="button"
                  className={`motive-multiselect-trigger${secondaryMotives.length ? ' is-filled' : ''}`}
                  aria-haspopup="listbox"
                  aria-expanded={secondaryOpen}
                  aria-controls="secondary-motive-options"
                  onClick={() => {
                    setSecondaryOpen((open) => !open);
                    setPrimaryOpen(false);
                  }}
                >
                  <span className="motive-multiselect-copy">
                    <strong>{secondarySelectionText}</strong>
                  </span>
                  <i aria-hidden="true" />
                </button>
                {secondaryOpen && (
                  <div id="secondary-motive-options" className="motive-multiselect-menu" role="listbox" aria-label={translate(
                    '员工的辅诉求，可不选，最多选择两个',
                    'Employee secondary motives; optional, choose up to two',
                  )}>
                    {options.motives.map((item) => {
                      const checked = selectedSecondaryMotiveIds.includes(item.id);
                      const disabled = item.id === selectedPrimaryMotiveId || (!checked && selectedSecondaryMotiveIds.length >= 2);
                      return (
                        <button
                          key={item.id}
                          type="button"
                          className={'motive-multiselect-option' + (checked ? ' selected' : '')}
                          role="option"
                          aria-selected={checked}
                          disabled={disabled}
                          onClick={() => toggleSecondaryMotive(item.id)}
                        >
                          <span>
                            <strong>{motiveName(item.id, item.name, language)}</strong>
                            <small>{motiveText(item, language)}</small>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="panel-section-head persona-heading"><div><h2>{translate('人格倾向', 'Personality tendencies')}</h2></div></div>
          <div className="big-five-panel" aria-label={translate('人格倾向设置', 'Personality tendency settings')}>
            {PERSONALITY_DIMENSIONS.map((dimension) => {
              const value = toPersonalitySliderValue(dimension.key, selectedPersonality[dimension.key]);
              const copy = localizedPersonalityCopy(dimension, translate);
              return (
                <div
                  key={dimension.key}
                  className="personality-slider"
                  style={{ '--personality-value': `${value}%` } as CSSProperties}
                >
                  <div className="slider-head">
                    <strong>{copy.label}</strong>
                  </div>
                  <label className="persona-range-label">
                    <span className="sr-only">{copy.label}</span>
                    <input
                      type="range"
                      min="0"
                      max="100"
                      step="1"
                      value={value}
                      onChange={(event) => updatePersonalityDimension(
                        dimension.key,
                        fromPersonalitySliderValue(dimension.key, Number(event.target.value)),
                      )}
                      aria-label={copy.label}
                      aria-valuetext={personalitySliderText(value, copy)}
                    />
                  </label>
                  <div className="personality-scale-ends" aria-hidden="true">
                    <span>{copy.low}</span>
                    <span>{copy.high}</span>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="simulation-card-action"><button className="btn btn-primary btn-wide" onClick={submit} disabled={!canSubmit}>{translate('确认人格与诉求', 'Confirm persona and motives')}</button></div>
        </section>

        <EmployeeProfileCard
          title={translate('员工档案', 'Employee profile')}
          rows={employeeRows}
          emptyText={translate('暂无员工档案信息。', 'No employee profile information is available.')}
          contextTitle={translate('沟通与诉求', 'Conversation and motives')}
          className="profile-dossier-grid"
          contextRows={[
            ['IN', translate('沟通意图', 'Conversation intent'), intent
              ? intentName(intent.id, intent.name, language)
              : translate('已确认', 'Confirmed')],
            ['M1', translate('员工的主诉求', 'Employee primary motive'), primaryMotive
              ? motiveName(primaryMotive.id, primaryMotive.name, language)
              : translate('待选择', 'Not selected')],
            ['M2', translate('员工的辅诉求', 'Employee secondary motives'), secondaryMotives
              .map((item) => motiveName(item.id, item.name, language)).join(' / ')
              || translate('未选择', 'None selected')],
          ]}
        >
          <div className="profile-detail-block">
            <h3>{translate('人格倾向', 'Personality tendencies')}</h3>
            <p>{PERSONALITY_DIMENSIONS.map((item) => {
              const copy = localizedPersonalityCopy(item, translate);
              return `${copy.label}: ${personalitySliderText(toPersonalitySliderValue(item.key, selectedPersonality[item.key]), copy)}`;
            }).join(' / ')}</p>
          </div>
        </EmployeeProfileCard>
      </div>
    </section>
  );
}
