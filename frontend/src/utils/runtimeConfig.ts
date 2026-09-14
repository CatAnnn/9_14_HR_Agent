const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);
const FALSE_VALUES = new Set(['0', 'false', 'no', 'off']);

export type AgentIntroductionVariant = 'classic' | 'journey';

export function parseRuntimeBoolean(value: unknown, fallback: boolean): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') {
    if (value === 1) return true;
    if (value === 0) return false;
    return fallback;
  }
  if (typeof value !== 'string') return fallback;

  const normalized = value.trim().toLowerCase();
  if (TRUE_VALUES.has(normalized)) return true;
  if (FALSE_VALUES.has(normalized)) return false;
  return fallback;
}

export function isWorkflowPageGuideEnabled(): boolean {
  const configured = typeof window === 'undefined'
    ? undefined
    : window.__HR_AGENT_RUNTIME_CONFIG__?.workflowPageGuideEnabled;
  return parseRuntimeBoolean(configured, true);
}

export function isWorkflowPageGuideAlwaysShow(): boolean {
  const configured = typeof window === 'undefined'
    ? undefined
    : window.__HR_AGENT_RUNTIME_CONFIG__?.workflowPageGuideAlwaysShow;
  return parseRuntimeBoolean(configured, false);
}

export function parseAgentIntroductionVariant(value: unknown): AgentIntroductionVariant {
  if (typeof value !== 'string') return 'classic';
  const normalized = value.trim().toLowerCase();
  return normalized === 'journey' ? 'journey' : 'classic';
}

export function getAgentIntroductionVariant(): AgentIntroductionVariant {
  const configured = typeof window === 'undefined'
    ? undefined
    : window.__HR_AGENT_RUNTIME_CONFIG__?.agentIntroductionVariant;
  return parseAgentIntroductionVariant(configured);
}
