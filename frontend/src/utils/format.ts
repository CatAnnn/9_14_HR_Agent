import type { EmployeeProfile, EmployeeRecord, StepKey } from '../types/domain';

export const STEP_KEYS: StepKey[] = ['profile', 'intent', 'simulation', 'guidance', 'rehearsal', 'report'];

export const STEP_LABELS: Record<StepKey, string> = {
  profile: 'Profile',
  intent: 'Intent',
  simulation: 'Persona',
  guidance: 'Guidance',
  rehearsal: 'Rehearsal',
  report: 'Report',
};

export const DEFAULT_PROFILE_TEXT = '';

export function safeList<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter(Boolean) as T[]) : [];
}

const PROFILE_DISPLAY_PLACEHOLDERS = new Set([
  '',
  '-',
  '--',
  '—',
  'n/a',
  'na',
  'none',
  'null',
  'undefined',
  'unknown',
  'not provided',
  '[]',
  '{}',
  '[object object]',
  '未知',
  '未提供',
  '暂无',
]);

const PROFILE_DISPLAY_KEYS = ['description', 'name', 'text', 'status', 'value', 'label'] as const;
const PROFILE_LIST_PREFIX = /^\s*(?:(?:\d{1,3}[.)、](?!\d)|[a-z][.)、])\s*|[•·▪◦‣●○■□◆◇✓✔]+\s*|[-–—]+(?!\d)\s*)/iu;

function parseProfileDisplayValue(value: string): unknown {
  const text = value.trim();
  if (!text || !/^(?:\[.*\]|\{.*\}|".*")$/s.test(text)) return value;
  try {
    return JSON.parse(text);
  } catch {
    return value;
  }
}

function cleanProfileDisplayText(value: string): string {
  return value
    .normalize('NFC')
    .replace(/\\u([0-9a-f]{4})/gi, (_match: string, code: string) => String.fromCharCode(Number.parseInt(code, 16)))
    .replace(/\\r\\n|\\n|\\r/g, '\n')
    .replace(/\\t/g, ' ')
    .replace(/&(?:nbsp|#160);/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&quot;/gi, '"')
    .replace(/&(?:apos|#39);/gi, "'")
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff\ufffd]/g, '')
    .split('\n')
    .map((line) => line.replace(PROFILE_LIST_PREFIX, '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join('\n');
}

function profileDisplayItems(value: unknown, depth = 0): string[] {
  if (value == null || depth > 3) return [];
  if (Array.isArray(value)) {
    return value.flatMap((item) => profileDisplayItems(item, depth + 1));
  }
  if (typeof value === 'string') {
    const parsed = parseProfileDisplayValue(value);
    if (parsed !== value) return profileDisplayItems(parsed, depth + 1);
    const text = cleanProfileDisplayText(value);
    return text ? text.split('\n') : [];
  }
  if (typeof value === 'number') return Number.isFinite(value) ? [String(value)] : [];
  if (typeof value === 'boolean') return [value ? '是' : '否'];
  if (typeof value === 'object') {
    const objectValue = value as Record<string, unknown>;
    for (const key of PROFILE_DISPLAY_KEYS) {
      const items = profileDisplayItems(objectValue[key], depth + 1);
      if (items.length) return items;
    }
  }
  return [];
}

export function profileValueToText(value: unknown): string {
  const seen = new Set<string>();
  return profileDisplayItems(value)
    .filter((item) => {
      const normalized = item.toLocaleLowerCase();
      if (PROFILE_DISPLAY_PLACEHOLDERS.has(normalized) || /^[?？]+$/.test(item)) return false;
      if (seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    })
    .join('\n');
}

const SUPPLEMENTAL_INFO_PREFIX = /^额外提供的信息\s*[：:]\s*/u;
const LABELED_PROFILE_LINE = /^([^：:\n]{1,32})\s*[：:]\s*(.*)$/u;
const PROFILE_LABEL_ALIASES: Readonly<Record<string, ReadonlyArray<string>>> = {
  关键目标: ['目标', '目标信息', '关键目标信息'],
};

function normalizeComparableProfileText(value: string): string {
  return value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[\s:：,，、;；/|。.!！?？"'“”‘’()[\]（）-]+/gu, '');
}

export function supplementalInfoToText(
  value: unknown,
  structuredRows: ReadonlyArray<readonly [string, string, string]> = [],
): string {
  const seen = new Set<string>();
  const structuredLabels = new Set<string>();
  const structuredValues = new Set<string>();

  structuredRows.forEach(([, label, rowValue]) => {
    structuredLabels.add(normalizeComparableProfileText(label));
    (PROFILE_LABEL_ALIASES[label] || []).forEach((alias) => {
      structuredLabels.add(normalizeComparableProfileText(alias));
    });

    const valueLines = rowValue.split('\n').map((line) => line.trim()).filter(Boolean);
    valueLines.forEach((line) => structuredValues.add(normalizeComparableProfileText(line)));
    if (valueLines.length > 1) {
      structuredValues.add(normalizeComparableProfileText(valueLines.join(' / ')));
    }
  });

  return profileValueToText(value)
    .split('\n')
    .map((line) => line.replace(SUPPLEMENTAL_INFO_PREFIX, '').trim())
    .filter((line) => {
      const normalized = normalizeComparableProfileText(line);
      if (!line || seen.has(normalized)) return false;
      if (structuredValues.has(normalized)) return false;

      const labeledLine = line.match(LABELED_PROFILE_LINE);
      const repeatsStructuredField = labeledLine
        && structuredLabels.has(normalizeComparableProfileText(labeledLine[1]))
        && structuredValues.has(normalizeComparableProfileText(labeledLine[2]));
      if (repeatsStructuredField) return false;

      seen.add(normalized);
      return true;
    })
    .join('\n');
}

export function valueToText(value: unknown): string {
  if (value == null) return '';
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const obj = item as Record<string, unknown>;
          return String(obj.description ?? obj.name ?? obj.text ?? '').trim();
        }
        return String(item).trim();
      })
      .filter(Boolean)
      .join(' / ');
  }
  if (typeof value === 'object') return '';
  return String(value).trim();
}

export function profileFromEmployeeRecord(record: EmployeeRecord | null): EmployeeProfile | null {
  if (!record) return null;
  const p = record.profile || {};
  return {
    employee_id: record.employee_id ?? p.employee_id,
    name: record.name ?? p.name,
    employee_alias: p.employee_alias ?? record.employee_alias,
    role: p.role ?? record.role,
    department: p.department ?? record.department,
    level: p.level,
    reporting_line: p.reporting_line ?? (record.manager ? `汇报给 ${record.manager}` : null),
    performance_rating: p.performance_rating,
    tcl: p.tcl,
    review_cycle: p.review_cycle,
    conversation_topic: p.conversation_topic,
    key_goals: p.key_goals ?? [],
    facts: p.facts ?? [],
    past_ratings: p.past_ratings ?? [],
    historical_feedback: p.historical_feedback ?? [],
    management_actions: p.management_actions ?? [],
    employee_status_summary: p.employee_status_summary,
    sensitive_constraints: p.sensitive_constraints ?? {},
    source_profile_text: p.source_profile_text ?? record.profile_text ?? null,
    supplemental_info: p.supplemental_info ?? null,
  };
}

export function employeeRecordText(record: EmployeeRecord | null): string {
  if (!record) return '';
  if (record.profile_text && String(record.profile_text).trim()) return String(record.profile_text).trim();
  const p = profileFromEmployeeRecord(record) || {};
  const rows: Array<[string, unknown]> = [
    ['工号', record.employee_id],
    ['姓名', record.name],
    ['员工代称', p.employee_alias],
    ['岗位', p.role],
    ['部门', p.department],
    ['职级', p.level],
    ['汇报关系', p.reporting_line],
    ['当前绩效评级', p.performance_rating],
    ['TCL', p.tcl],
    ['考核周期', p.review_cycle],
    ['本次谈话主题', p.conversation_topic],
    ['关键目标', p.key_goals],
    ['事实', p.facts],
    ['历史反馈', p.historical_feedback],
    ['管理动作', p.management_actions],
    ['员工状态', p.employee_status_summary],
  ];
  return rows
    .map(([label, value]) => [label, valueToText(value)] as const)
    .filter(([, value]) => value)
    .map(([label, value]) => `${label}：${value}`)
    .join('\n');
}

export function composeProfileText(_selectedEmployee: EmployeeRecord | null, manualText: string): string {
  return manualText.trim();
}

function compactPerformanceValue(profile: EmployeeProfile) {
  const rating = profileValueToText(profile.performance_rating);
  const tcl = profileValueToText(profile.tcl);
  return [rating ? `评级 ${rating}` : '', tcl ? `TCL ${tcl}` : '']
    .filter(Boolean)
    .join(' / ');
}

export function dossierEmployeeProfileRows(
  profile: EmployeeProfile | null,
): Array<[string, string, string]> {
  if (!profile) return [];
  const displayName = profileValueToText(profile.name)
    || profileValueToText(profile.employee_alias)
    || '—';
  return [
    ['NM', '姓名', displayName],
    ['DP', '部门', profileValueToText(profile.department) || '—'],
    ['LV', '职级', profileValueToText(profile.level) || '—'],
    ['PF', '绩效', compactPerformanceValue(profile) || '—'],
    ['RL', '岗位', profileValueToText(profile.role) || '—'],
  ];
}

export function profileRows(profile: EmployeeProfile | null): Array<[string, string, string]> {
  if (!profile) return [];
  const p = profile;
  const sensitive = Object.values(p.sensitive_constraints || {})
    .map((x) => (x && typeof x === 'object' ? (x as { status?: string }).status : x))
    .filter(Boolean);
  const rows: Array<[string, string, unknown]> = [
    ['#', '工号', p.employee_id],
    ['NM', '姓名', p.name],
    ['AL', '员工代称', p.employee_alias],
    ['RL', '岗位', p.role],
    ['▦', '部门', p.department],
    ['LV', '职级', p.level],
    ['↗', '汇报关系', p.reporting_line],
    ['◎', '当前绩效评级', p.performance_rating],
    ['TC', 'TCL', p.tcl],
    ['□', '考核周期', p.review_cycle],
    ['◇', '本次谈话主题', p.conversation_topic],
    ['GO', '关键目标', p.key_goals],
    ['✓', '完成情况', p.facts],
    ['↺', '历史反馈', p.historical_feedback],
    ['MA', '管理动作', p.management_actions],
    ['ST', '员工状态', p.employee_status_summary],
    ['SC', '敏感约束', sensitive],
    ['SI', '额外提供的信息', p.supplemental_info],
  ];
  return rows
    .map(([icon, label, value]) => [icon, label, profileValueToText(value)] as [string, string, string])
    .filter(([, , value]) => Boolean(value));
}

export function inferStepFromState(stage?: string): StepKey {
  if (stage === 'report_ready') return 'report';
  if (stage === 'rehearsal') return 'rehearsal';
  if (stage === 'guidance_ready') return 'rehearsal';
  if (stage === 'setup_ready') return 'guidance';
  if (stage === 'profile_ready') return 'intent';
  return 'profile';
}

export function getApiBase(): string {
  const injected = window.__HR_AGENT_API_BASE;
  const env = import.meta.env.VITE_API_BASE as string | undefined;
  const fallback = '/api/v1';
  return (injected || env || fallback).replace(/\/$/, '');
}

export function getWsApiBase(): string {
  const url = new URL(getApiBase(), window.location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString().replace(/\/$/, '');
}
