import type { EmployeeProfile } from '../types/domain';

export interface ProfileGoalPoint {
  marker: string | null;
  markerKind: 'number' | 'bullet' | 'none';
  text: string;
}

export interface ProfileGoalGroup {
  title: string | null;
  points: ProfileGoalPoint[];
}

export interface ProfileGoalDimension {
  title: string;
  groups: ProfileGoalGroup[];
}

interface RawGoalLine {
  text: string;
  dimensionHint: boolean;
  groupHint: boolean;
  pointHint: boolean;
  compactSummary: boolean;
}

interface GoalEntry extends ProfileGoalPoint {
  displayText: string;
  dimensionHint: boolean;
  groupHint: boolean;
  pointHint: boolean;
}

interface MutableGoalGroup extends ProfileGoalGroup {
  kind: 'default' | 'heading' | 'numbered';
}

interface MutableGoalDimension extends ProfileGoalDimension {
  strong: boolean;
  groups: MutableGoalGroup[];
}

const GOAL_VALUE_KEYS = ['description', 'name', 'text', 'value', 'label'] as const;
const EMPTY_GOAL_VALUES = new Set([
  '', '-', '--', '—', 'n/a', 'na', 'none', 'null', 'undefined', 'unknown',
  'not provided', '未知', '未提供', '暂无',
]);
const NUMBERED_PREFIX = /^((?:\d{1,3}[a-z]?|[a-z])[.)、），,])\s*(.+)$/iu;
const BULLET_PREFIX = /^([-*•·▪◦‣●○■□◆◇✓✔])\s*(.+)$/u;
const CHINESE_DIMENSION_PREFIX = /^[一二三四五六七八九十百]+[、.．]\s*\S/u;
const CHINESE_DIMENSION_WITH_DETAIL = /^([一二三四五六七八九十百]+[、.．]\s*[^:：]{2,48})\s*[:：]\s*(\S[\s\S]*)$/u;
const QUARTER_DIMENSION = /^Q[1-4]\s*20\d{2}\b/iu;
const CORE_THEME_DIMENSION = /^20\d{2}\s+Core\s+Theme\s*:/iu;
const EXPLICIT_DIMENSION = /(?:\b(?:KPI\s+Goals?|Key\s+Results?|Competence\s+development\s+goal)\b|(?:目标|goals?)\s*[:：]?\s*$)/iu;
const ACTION_SENTENCE_PREFIX = /^(?:enable|finish|try|create|lead|work|use|introduce|co-?work|conduct|follow|promote|define|deliver|track|review|coordinate|establish|submit|ensure|draft|increase|enhance|optimi[sz]e|provide|continue|set|fully|no\b|responsible|develop|complete|drive|support|implement|design|build|take|share|contribute|[a-z]+ing\b)/iu;
const SOURCE_GOAL_BLOCK = /(?:^|\n)(?:Goal|关键目标)\s*[：:]\s*([\s\S]*?)(?=\n(?:Current Career Elements|Career Elements|Talent Pool|TCL \(SLx\)|Performance Rating \(A Group\)|History ASR Rating|事实|历史绩效结果|历史反馈|已采取管理行动)\s*[：:]|$)/iu;
const GROUP_LABEL_PATTERN = /(Action\s*Plan\s*\/\s*目标行动计划|Bottom\s*\/\s*保底值|Meet\s*Expectation\s*\/\s*达标值|Challenging\s*\/\s*挑战值)/giu;
const COMPACT_NUMBERED_MARKER = /(^|\s)(\d{1,3}[.)、）])\s*(?=\S)/gu;
const GOAL_TABLE_HEADER = /^Goal\s*\(\s*Results\s*\|\s*Learning\s*\|\s*Collaboration\s*\)\s*(?:Description)?$/iu;
const FLATTENED_GOAL_TABLE_HEADERS = [
  'goal/目标',
  'actionplan/目标行动计划',
  'bottom/保底值',
  'meetexpectation/达标值',
  'challenging/挑战值',
] as const;
const LONG_DIMENSION_MIN_CHARS = 180;
const LONG_DIMENSION_BOUNDARIES = [
  /\s+(?=\d{1,3}[a-z]\)\s)/iu,
  /(?=:\s*-\s+(?:I|We|To)\b)/iu,
  /\s+(?=-\s+(?:I|We|To)\b)/iu,
  /\s+(?=Our\s)/iu,
] as const;

const PROFILE_GOAL_SYSTEM_LABELS_ENGLISH: Readonly<Record<string, string>> = {
  '关键目标': 'Key goals',
  '目标说明': 'Goal description',
  'Action Plan / 目标行动计划': 'Action plan',
  'Bottom / 保底值': 'Minimum target',
  'Meet Expectation / 达标值': 'Expected target',
  'Challenging / 挑战值': 'Stretch target',
};

export function profileGoalSystemLabelEnglish(value: string): string | undefined {
  return PROFILE_GOAL_SYSTEM_LABELS_ENGLISH[value];
}

function decodeGoalText(value: string): string {
  return value
    .normalize('NFKC')
    .replace(/\\u([0-9a-f]{4})/gi, (_match: string, code: string) => (
      String.fromCharCode(Number.parseInt(code, 16))
    ))
    .replace(/\\r\\n|\\n|\\r/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\r\n?/g, '\n')
    .replace(/<br\s*\/?\s*>/giu, '\n')
    .replace(/<\/(?:p|li|div|h[1-6])\s*>/giu, '\n')
    .replace(/<\/?[a-z][^>\n]*>/giu, ' ')
    .replace(/&nbsp;|&#160;/giu, ' ')
    .replace(/&amp;/giu, '&')
    .replace(/&lt;/giu, '<')
    .replace(/&gt;/giu, '>');
}

function normalizeGoalLineText(value: string): string {
  let text = value
    .replace(/[\u200B-\u200D\u2060\uFEFF]/gu, '')
    .replace(/^[ \t]*#{1,6}[ \t]+/u, '')
    .replace(/^[ \t]*>[ \t]?/u, '')
    .replace(/\[([^\]]+)\]\([^\s)]+(?:\s+["'][^"']*["'])?\)/gu, '$1')
    .replace(/\*\*([^*]+)\*\*/gu, '$1')
    .replace(/__([^_]+)__/gu, '$1')
    .replace(/[\u00A0\u3000]/gu, ' ')
    .replace(/[ \t]+/gu, ' ')
    .trim();

  const wrappers: Array<[string, string]> = [
    ['"', '"'],
    ['“', '”'],
    ["'", "'"],
    ['‘', '’'],
  ];
  let changed = true;
  while (changed && text.length >= 2) {
    changed = false;
    for (const [opening, closing] of wrappers) {
      if (text.startsWith(opening) && text.endsWith(closing)) {
        text = text.slice(opening.length, -closing.length).trim();
        changed = true;
        break;
      }
    }
  }

  return text
    .replace(/^["“”]+(?=\S)/u, '')
    .replace(/["“”]+$/u, '')
    .replace(/[ \t]+/gu, ' ')
    .trim();
}

function lineKey(value: string): string {
  return value.normalize('NFKC').replace(/\s+/gu, ' ').trim().toLocaleLowerCase();
}

function canonicalGroupLabel(value: string): string {
  const normalized = value.replace(/\s+/gu, '').toLocaleLowerCase();
  if (normalized.startsWith('actionplan/')) return 'Action Plan / 目标行动计划';
  if (normalized.startsWith('bottom/')) return 'Bottom / 保底值';
  if (normalized.startsWith('meetexpectation/')) return 'Meet Expectation / 达标值';
  return 'Challenging / 挑战值';
}

function createRawGoalLine(
  text: string,
  options: Partial<Omit<RawGoalLine, 'text'>> = {},
): RawGoalLine | null {
  const normalized = normalizeGoalLineText(text);
  if (!normalized) return null;
  const emptyKey = normalized.toLocaleLowerCase();
  if (
    EMPTY_GOAL_VALUES.has(emptyKey)
    || /^[?？]+$/u.test(normalized)
    || /^```/u.test(normalized)
    || /^\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?$/u.test(normalized)
    || GOAL_TABLE_HEADER.test(normalized)
    || /^Description$/iu.test(normalized)
  ) return null;
  return {
    text: normalized,
    dimensionHint: options.dimensionHint ?? false,
    groupHint: options.groupHint ?? false,
    pointHint: options.pointHint ?? false,
    compactSummary: options.compactSummary ?? false,
  };
}

function splitBulletContent(value: string, pointHint = true): RawGoalLine[] {
  const normalized = normalizeGoalLineText(value)
    .replace(/\s*([•●▪◦‣])\s*/gu, '\n$1 ')
    .trim();
  if (!normalized) return [];
  return normalized
    .split('\n')
    .map((part) => createRawGoalLine(part, { pointHint }))
    .filter((line): line is RawGoalLine => line !== null);
}

function splitGroupLabels(text: string, dimensionHint: boolean): RawGoalLine[] | null {
  const matches = Array.from(text.matchAll(GROUP_LABEL_PATTERN));
  if (!matches.length) return null;

  const lines: RawGoalLine[] = [];
  const prefix = text.slice(0, matches[0].index).trim();
  const prefixLine = createRawGoalLine(prefix, { dimensionHint });
  if (prefixLine) lines.push(prefixLine);

  matches.forEach((match, index) => {
    const labelLine = createRawGoalLine(canonicalGroupLabel(match[0]), { groupHint: true });
    if (labelLine) lines.push(labelLine);
    const bodyStart = (match.index ?? 0) + match[0].length;
    const bodyEnd = matches[index + 1]?.index ?? text.length;
    lines.push(...splitBulletContent(text.slice(bodyStart, bodyEnd), true));
  });
  return lines;
}

function splitCompactNumberedLine(text: string, dimensionHint: boolean): RawGoalLine[] | null {
  const starts = Array.from(text.matchAll(COMPACT_NUMBERED_MARKER))
    .map((match) => (match.index ?? 0) + match[1].length);
  if (starts.length < 2 || starts[0] !== 0) return null;

  const lines: RawGoalLine[] = [];
  if (dimensionHint) {
    const title = createRawGoalLine('关键目标', { dimensionHint: true });
    if (title) lines.push(title);
  }
  starts.forEach((start, index) => {
    const part = text.slice(start, starts[index + 1] ?? text.length);
    const line = createRawGoalLine(part, { compactSummary: true });
    if (line) lines.push(line);
  });
  return lines;
}

function splitLongDimensionLine(text: string, dimensionHint: boolean): RawGoalLine[] | null {
  if (!dimensionHint || Array.from(text).length < LONG_DIMENSION_MIN_CHARS) return null;

  const boundaryIndexes = LONG_DIMENSION_BOUNDARIES
    .map((pattern) => text.search(pattern))
    .filter((index) => index >= 18);
  if (!boundaryIndexes.length) return null;

  const boundary = Math.min(...boundaryIndexes);
  const title = createRawGoalLine(cleanHeading(text.slice(0, boundary)), { dimensionHint: true });
  const body = text.slice(boundary).replace(/^:\s*/u, '').trim();
  if (!title || !body) return null;

  const details = body
    .replace(/\s+(?=\d{1,3}[a-z]\)\s)/giu, '\n')
    .replace(/\s+-\s+(?=(?:I|We|To)\b)/giu, '\n- ')
    .split('\n')
    .flatMap((part) => splitBulletContent(part, true));
  return details.length ? [title, ...details] : null;
}

function expandPhysicalGoalLine(rawLine: string): RawGoalLine[] {
  // One trailing tab is the legacy dimension marker. Repeated trailing tabs
  // come from empty spreadsheet columns and must not create empty dimensions.
  const trailingWhitespace = rawLine.match(/[ \t]*$/u)?.[0] ?? '';
  const trailingTabCount = trailingWhitespace.match(/\t/gu)?.length ?? 0;
  const dimensionHint = trailingTabCount === 1;
  const normalized = normalizeGoalLineText(rawLine.replace(/\t/gu, ' '));
  if (!normalized) return [];

  const grouped = splitGroupLabels(normalized, dimensionHint);
  if (grouped) return grouped;

  const compactNumbered = splitCompactNumberedLine(normalized, dimensionHint);
  if (compactNumbered) return compactNumbered;

  const longDimension = splitLongDimensionLine(normalized, dimensionHint);
  if (longDimension) return longDimension;

  const chineseDimension = normalized.match(CHINESE_DIMENSION_WITH_DETAIL);
  if (chineseDimension) {
    return [
      createRawGoalLine(chineseDimension[1], { dimensionHint: true }),
      createRawGoalLine(chineseDimension[2], { pointHint: true }),
    ].filter((line): line is RawGoalLine => line !== null);
  }

  const line = createRawGoalLine(normalized, { dimensionHint });
  return line ? [line] : [];
}

function normalizeRawGoalLines(lines: RawGoalLine[]): RawGoalLine[] {
  const detailedKeys = new Set(
    lines.filter((line) => !line.compactSummary).map((line) => lineKey(line.text)),
  );
  const withoutDuplicatedSummary = lines.filter((line) => (
    !line.compactSummary || !detailedKeys.has(lineKey(line.text))
  ));
  return withoutDuplicatedSummary.filter((line, index) => {
    const previous = withoutDuplicatedSummary[index - 1];
    return !previous
      || lineKey(previous.text) !== lineKey(line.text)
      || previous.dimensionHint !== line.dimensionHint
      || previous.groupHint !== line.groupHint;
  });
}

function rawGoalLines(value: unknown, depth = 0): RawGoalLine[] {
  if (value == null || depth > 4) return [];
  if (Array.isArray(value)) {
    return normalizeRawGoalLines(value.flatMap((item) => rawGoalLines(item, depth + 1)));
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (/^(?:\[.*\]|\{.*\}|".*")$/s.test(trimmed)) {
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        if (parsed !== value) return rawGoalLines(parsed, depth + 1);
      } catch {
        // A legacy goal can resemble JSON while still being ordinary text.
      }
    }
    return normalizeRawGoalLines(
      decodeGoalText(value).split('\n').flatMap(expandPhysicalGoalLine),
    );
  }
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of GOAL_VALUE_KEYS) {
      const lines = rawGoalLines(record[key], depth + 1);
      if (lines.length) return lines;
    }
  }
  return [];
}

function toGoalEntry(line: RawGoalLine): GoalEntry {
  const numbered = line.text.match(NUMBERED_PREFIX);
  if (numbered) {
    return {
      marker: numbered[1],
      markerKind: 'number',
      text: numbered[2].trim(),
      displayText: line.text,
      dimensionHint: line.dimensionHint,
      groupHint: line.groupHint,
      pointHint: line.pointHint,
    };
  }
  const bullet = line.text.match(BULLET_PREFIX);
  if (bullet) {
    return {
      marker: bullet[1],
      markerKind: 'bullet',
      text: bullet[2].trim(),
      displayText: line.text,
      dimensionHint: line.dimensionHint,
      groupHint: line.groupHint,
      pointHint: line.pointHint,
    };
  }
  return {
    marker: null,
    markerKind: 'none',
    text: line.text,
    displayText: line.text,
    dimensionHint: line.dimensionHint,
    groupHint: line.groupHint,
    pointHint: line.pointHint,
  };
}

function isStrongDimension(entry: GoalEntry): boolean {
  return entry.dimensionHint
    || CHINESE_DIMENSION_PREFIX.test(entry.displayText)
    || QUARTER_DIMENSION.test(entry.displayText)
    || CORE_THEME_DIMENSION.test(entry.displayText)
    || EXPLICIT_DIMENSION.test(entry.displayText);
}

function isShortTitle(text: string, maximumLatinWords = 7): boolean {
  if (/[。！？!?；;]$/u.test(text)) return false;
  if (/\p{Script=Han}/u.test(text)) return Array.from(text).length <= 28;
  const words = text.replace(/[.。]$/u, '').split(/\s+/u).filter(Boolean);
  return words.length <= maximumLatinWords;
}

function dimensionPointCount(dimension: MutableGoalDimension): number {
  return dimension.groups.reduce((total, group) => total + group.points.length, 0);
}

function isLikelyHeading(
  entry: GoalEntry,
  next: GoalEntry | undefined,
  dimension: MutableGoalDimension,
): boolean {
  if (entry.markerKind !== 'none' || entry.pointHint || entry.groupHint) return false;

  const pointCount = dimensionPointCount(dimension);
  if (dimension.strong && next && isStrongDimension(next) && pointCount === 0) return false;
  if (next?.markerKind !== 'none') return true;
  if (/^upskill\b/iu.test(entry.text)) return true;
  if (!isShortTitle(entry.text, 10)) return false;
  if (next && !isShortTitle(next.text, 12)) return true;
  if (!ACTION_SENTENCE_PREFIX.test(entry.text)) return true;
  return pointCount >= 2 && isShortTitle(entry.text, 5);
}

function isBulletGroupHeading(entry: GoalEntry, next: GoalEntry | undefined): boolean {
  return entry.markerKind === 'bullet'
    && /[:：]$/u.test(entry.text)
    && next?.markerKind === 'bullet'
    && isShortTitle(entry.text.replace(/[:：]$/u, ''), 8);
}

function cleanHeading(value: string): string {
  return value.replace(/\s*[:：]\s*$/u, '').trim();
}

function extractGoalBlock(sourceProfileText: string | null | undefined): string | null {
  if (!sourceProfileText) return null;
  const match = decodeGoalText(sourceProfileText).match(SOURCE_GOAL_BLOCK);
  return match?.[1]?.trim() || null;
}

function goalTableHeaderKey(value: string): string {
  return normalizeGoalLineText(value).replace(/\s+/gu, '').toLocaleLowerCase();
}

function stripSpreadsheetTextEnvelope(value: string): string {
  const text = value.trim();
  const wrappers: Array<[string, string]> = [
    ['"', '"'],
    ['“', '”'],
    ["'", "'"],
    ['‘', '’'],
  ];
  for (const [opening, closing] of wrappers) {
    if (text.startsWith(opening) && text.endsWith(closing)) {
      return text.slice(opening.length, -closing.length).trim();
    }
  }
  return text;
}

function goalTableCellLines(value: string): string[] {
  return decodeGoalText(value)
    .split('\n')
    .map(normalizeGoalLineText)
    .filter((line) => Boolean(line) && !EMPTY_GOAL_VALUES.has(line.toLocaleLowerCase()));
}

function goalTablePoints(value: string): ProfileGoalPoint[] {
  return goalTableCellLines(value).map((text) => {
    const line = createRawGoalLine(text, { pointHint: true });
    if (!line) return null;
    const entry = toGoalEntry(line);
    return {
      marker: entry.marker,
      markerKind: entry.markerKind,
      text: entry.text,
    };
  }).filter((point): point is ProfileGoalPoint => point !== null);
}

interface FlattenedGoalTableRow {
  goal: string;
  actionPlan: string;
  bottom: string;
  meetExpectation: string;
  challenging: string;
}

function parseFlattenedGoalTable(value: unknown): ProfileGoalDimension[] | null {
  if (typeof value !== 'string') return null;

  const decoded = stripSpreadsheetTextEnvelope(decodeGoalText(value));
  const headerEnd = decoded.indexOf('\n');
  if (headerEnd < 0) return null;

  const headers = decoded.slice(0, headerEnd).split('\t').map(goalTableHeaderKey);
  if (
    headers.length !== FLATTENED_GOAL_TABLE_HEADERS.length
    || !headers.every((header, index) => header === FLATTENED_GOAL_TABLE_HEADERS[index])
  ) return null;

  const cells = decoded.slice(headerEnd + 1).trim().split('\t');
  if (cells.length < FLATTENED_GOAL_TABLE_HEADERS.length) return null;

  const rows: FlattenedGoalTableRow[] = [];
  let currentGoal = cells[0];
  let cursor = 1;
  while (cursor + 3 < cells.length) {
    const actionPlan = cells[cursor];
    const bottom = cells[cursor + 1];
    const meetExpectation = cells[cursor + 2];
    const challengeAndNextGoal = cells[cursor + 3];
    const hasNextRow = cursor + 4 < cells.length;
    let challenging = challengeAndNextGoal;
    let nextGoal = '';

    if (hasNextRow) {
      const combinedLines = goalTableCellLines(challengeAndNextGoal);
      const thresholdLineCount = Math.max(
        1,
        goalTableCellLines(bottom).length,
        goalTableCellLines(meetExpectation).length,
      );
      if (combinedLines.length <= thresholdLineCount) return null;
      challenging = combinedLines.slice(0, thresholdLineCount).join('\n');
      nextGoal = combinedLines.slice(thresholdLineCount).join('\n');
    }

    if (!goalTableCellLines(currentGoal).length) return null;
    rows.push({ goal: currentGoal, actionPlan, bottom, meetExpectation, challenging });
    currentGoal = nextGoal;
    cursor += 4;
  }

  if (cursor !== cells.length || !rows.length) return null;

  return rows.map((row) => {
    const goalLines = goalTableCellLines(row.goal);
    const groups: ProfileGoalGroup[] = [];
    const addGroup = (title: string, content: string) => {
      const points = goalTablePoints(content);
      if (points.length) groups.push({ title, points });
    };

    if (goalLines.length > 1) {
      addGroup('目标说明', goalLines.slice(1).join('\n'));
    }
    addGroup('Action Plan / 目标行动计划', row.actionPlan);
    addGroup('Bottom / 保底值', row.bottom);
    addGroup('Meet Expectation / 达标值', row.meetExpectation);
    addGroup('Challenging / 挑战值', row.challenging);

    return {
      title: goalLines[0],
      groups,
    };
  });
}

export function profileGoalSource(profile: EmployeeProfile | null | undefined): unknown {
  if (!profile) return null;
  const sourceBlock = extractGoalBlock(profile.source_profile_text);
  return sourceBlock && rawGoalLines(sourceBlock).length
    ? sourceBlock
    : profile.key_goals;
}

export function parseProfileGoalDimensions(value: unknown): ProfileGoalDimension[] {
  const tableDimensions = parseFlattenedGoalTable(value);
  if (tableDimensions) return tableDimensions;

  const entries = rawGoalLines(value).map(toGoalEntry);
  if (!entries.length) return [];

  const dimensions: MutableGoalDimension[] = [];
  let dimension: MutableGoalDimension | null = null;
  let group: MutableGoalGroup | null = null;

  const startDimensionTitle = (title: string, strong: boolean) => {
    dimension = { title: title.trim(), groups: [], strong };
    dimensions.push(dimension);
    group = null;
  };

  const startDimension = (entry: GoalEntry, strong: boolean) => {
    startDimensionTitle(entry.displayText, strong);
  };

  const ensureDimension = () => {
    if (!dimension) startDimensionTitle('关键目标', true);
  };

  const startGroup = (
    title: string | null,
    kind: MutableGoalGroup['kind'],
  ) => {
    ensureDimension();
    group = { title, points: [], kind };
    dimension?.groups.push(group);
  };

  const addPoint = (entry: GoalEntry) => {
    ensureDimension();
    if (!group) startGroup(null, 'default');
    group?.points.push({
      marker: entry.marker,
      markerKind: entry.markerKind,
      text: entry.text,
    });
  };

  entries.forEach((entry, index) => {
    const next = entries[index + 1];
    if (entry.groupHint) {
      startGroup(cleanHeading(entry.text), 'heading');
      return;
    }
    if (isStrongDimension(entry)) {
      startDimension(entry, true);
      return;
    }
    if (!dimension) {
      if (entry.markerKind !== 'none' || entry.pointHint) addPoint(entry);
      else startDimension(entry, false);
      return;
    }
    if (isBulletGroupHeading(entry, next)) {
      startGroup(cleanHeading(entry.text), 'heading');
      return;
    }
    if (isLikelyHeading(entry, next, dimension)) {
      if (dimension.strong) startGroup(entry.displayText, 'heading');
      else startDimension(entry, false);
      return;
    }
    if (entry.markerKind === 'number' && next?.markerKind === 'bullet') {
      startGroup(entry.displayText, 'numbered');
      return;
    }
    if (entry.markerKind === 'number' && group?.kind === 'numbered') group = null;
    addPoint(entry);
  });

  return dimensions
    .filter(({ title }) => Boolean(title.trim()))
    .map(({ title, groups }) => ({
      title,
      groups: groups
        .map(({ title: groupTitle, points }) => ({
          title: groupTitle,
          points: points.filter((point) => Boolean(point.text.trim())),
        }))
        .filter(({ title: groupTitle, points }) => Boolean(groupTitle) || points.length > 0),
    }));
}
