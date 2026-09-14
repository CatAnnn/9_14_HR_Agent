import { memo } from 'react';
import {
  ClipboardList,
  Crosshair,
  FileText,
  MessageSquare,
  PanelLeft,
  UserRound,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { AccountActions } from './AccountActions';
import { STATIC_ASSETS } from '../config/staticAssets';
import type { SessionState, StepKey } from '../types/domain';
import { STEP_KEYS } from '../utils/format';
import { isStepUnlocked } from '../utils/workflowSteps';
import { useLanguage } from '../i18n/LanguageContext';

const stepIcons: Record<StepKey, LucideIcon> = {
  profile: UserRound,
  intent: Crosshair,
  simulation: UsersRound,
  guidance: ClipboardList,
  rehearsal: MessageSquare,
  report: FileText,
};

const stepLabels: Record<StepKey, readonly [string, string]> = {
  profile: ['员工信息', 'Employee profile'],
  intent: ['沟通意图', 'Conversation intent'],
  simulation: ['人格与诉求', 'Persona and motives'],
  guidance: ['谈前指导', 'Preparation guidance'],
  rehearsal: ['多轮预演', 'Rehearsal'],
  report: ['复盘报告', 'Review report'],
};

interface StepNavProps {
  current: StepKey;
  session: SessionState | null;
  collapsed: boolean;
  intentGate?: boolean;
  reportAccessible: boolean;
  onToggle: () => void;
  onPreload: (step: StepKey) => void;
}

export const StepNav = memo(function StepNav({
  current,
  session,
  collapsed,
  intentGate,
  reportAccessible,
  onToggle,
  onPreload,
}: StepNavProps) {
  const navigate = useNavigate();
  const { translate } = useLanguage();
  const currentIndex = STEP_KEYS.indexOf(current);
  const simulationIndex = STEP_KEYS.indexOf('simulation');

  return (
    <aside className={`app-sidebar${collapsed ? ' is-collapsed' : ''}`} aria-label={translate('工作台导航')}>
      <div className="workspace-toolbar">
        <div className="sidebar-brand-switch">
          <button
            className="sidebar-brand-mark"
            type="button"
            onClick={() => navigate('/')}
            aria-label={translate('返回主页')}
            title={collapsed ? undefined : translate('返回主页')}
          >
            <img
              className="sidebar-brand-image"
              src={STATIC_ASSETS.companyLogo}
              alt=""
              aria-hidden="true"
              width={160}
              height={160}
              draggable={false}
            />
          </button>
          <button
            className={`sidebar-toggle${collapsed ? ' is-open-control' : ''}`}
            type="button"
            onClick={onToggle}
            aria-label={translate(collapsed ? '展开侧边栏' : '收起侧边栏')}
            aria-expanded={!collapsed}
            data-tooltip={collapsed ? translate('展开侧边栏') : undefined}
            title={collapsed ? undefined : translate('收起侧边栏')}
          >
            <PanelLeft aria-hidden="true" />
          </button>
        </div>
      </div>

      <nav className="stepper" aria-label={translate('流程步骤')}>
        {STEP_KEYS.map((step, index) => {
          const blockedByIntentGate = intentGate === false && index >= simulationIndex;
          const canVisit = !blockedByIntentGate && (
            index <= currentIndex || isStepUnlocked(step, session, reportAccessible)
          );
          const [sourceLabel, englishLabel] = stepLabels[step];
          const displayLabel = translate(sourceLabel, englishLabel);
          const StepIcon = stepIcons[step];
          return (
            <button
              key={step}
              type="button"
              className={`step-item ${step === current ? 'is-current' : ''} ${index < currentIndex ? 'is-complete' : ''}`}
              aria-current={step === current ? 'step' : undefined}
              aria-label={displayLabel}
              disabled={!canVisit}
              onPointerEnter={() => canVisit && onPreload(step)}
              onPointerDown={() => canVisit && onPreload(step)}
              onFocus={() => canVisit && onPreload(step)}
              onClick={() => canVisit && navigate(`/app/${step}`)}
            >
              <span className="step-icon"><StepIcon aria-hidden="true" /></span>
              <span className="step-copy" aria-hidden={collapsed || undefined}>
                <strong>{displayLabel}</strong>
              </span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        <AccountActions compact={collapsed} variant="sidebar" />
      </div>
    </aside>
  );
});
