import type { EmployeeProfile, IntentOption } from '../types/domain';

export type IntentEligibilityStatus =
  | 'not_selected'
  | 'unconfigured'
  | 'allowed'
  | 'mismatch'
  | 'unknown';

export interface IntentEligibilityEvaluation {
  status: IntentEligibilityStatus;
  allowed: boolean;
  performanceRating: number | null;
  tcl: string | null;
}

const PERFORMANCE_RATING_LINE = /^\s*(?:Performance Rating \(A Group\)|当前绩效评级)\s*:\s*(.*?)\s*$/i;
const TCL_LINE = /^\s*TCL(?:\s*\(SLx\))?\s*:\s*(.*?)\s*$/i;
const PERFORMANCE_TCL = /(?:^|[,;|/])\s*Performance\s*[:=]?\s*(#|\+{1,4})(?=\s*(?:[,;|/]|$))/i;

export function normalizePerformanceRating(value: unknown): number | null {
  if (typeof value === 'number') {
    return Number.isInteger(value) && value >= 1 && value <= 5 ? value : null;
  }
  if (typeof value !== 'string') return null;

  const match = normalizeText(value).match(/^([1-5])(?:\.0+)?$/);
  return match ? Number(match[1]) : null;
}

export function normalizeTcl(value: unknown): string | null {
  if (value == null) return null;
  const text = normalizeText(value);
  const compact = text.replace(/\s+/g, '');
  if (compact === '#' || /^\+{1,4}$/.test(compact)) return compact;

  return text.match(PERFORMANCE_TCL)?.[1] || null;
}

export function evaluateIntentEligibility(
  profile: EmployeeProfile | null,
  intent: IntentOption | null,
): IntentEligibilityEvaluation {
  const performanceRating = profileRating(profile);
  const tcl = profileTcl(profile);

  if (!intent) {
    return {
      status: 'not_selected',
      allowed: false,
      performanceRating,
      tcl,
    };
  }

  const rule = intent.eligibility;
  const ratings = rule?.performance_ratings || [];
  const tclValues = rule?.tcl_values || [];
  if (!rule || (!ratings.length && !tclValues.length)) {
    return {
      status: 'unconfigured',
      allowed: true,
      performanceRating,
      tcl,
    };
  }

  if (performanceRating == null && tcl == null) {
    return {
      status: 'unknown',
      allowed: true,
      performanceRating: null,
      tcl: null,
    };
  }

  const ratingMatches = performanceRating != null && ratings.includes(performanceRating);
  const tclMatches = tcl != null && tclValues.includes(tcl);
  if (ratingMatches || tclMatches) {
    return {
      status: 'allowed',
      allowed: true,
      performanceRating,
      tcl,
    };
  }

  return {
    status: 'mismatch',
    allowed: true,
    performanceRating,
    tcl,
  };
}

function profileRating(profile: EmployeeProfile | null): number | null {
  if (!profile) return null;
  return normalizePerformanceRating(profile.performance_rating)
    ?? normalizePerformanceRating(extractSourceValue(profile.source_profile_text, PERFORMANCE_RATING_LINE));
}

function profileTcl(profile: EmployeeProfile | null): string | null {
  if (!profile) return null;
  return normalizeTcl(profile.tcl)
    ?? normalizeTcl(extractSourceValue(profile.source_profile_text, TCL_LINE));
}

function extractSourceValue(source: string | null | undefined, pattern: RegExp): string | null {
  if (!source) return null;
  for (const line of normalizeText(source).split(/\r?\n/)) {
    const match = line.match(pattern);
    if (match) return match[1].trim() || null;
  }
  return null;
}

function normalizeText(value: unknown): string {
  return String(value).normalize('NFKC').trim();
}
