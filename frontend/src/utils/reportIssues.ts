import type { CoachBetterPhrase, CoachTaskResult } from '../types/domain';

export const OPENING_ISSUE_TITLES = [
  '沟通基调',
  '绩效结果对齐',
] as const;

export const OUTPUT_EXPECTATIONS_ISSUE_TITLES = [
  '目标方向的正确性',
  '结果的突破性',
  '高绩效文化',
] as const;

export const EMOTION_ISSUE_TITLES = [
  '认真倾听，理解情绪',
  '共情总结',
  '共同探索',
] as const;

export const DEVELOPMENT_PLAN_ISSUE_TITLES = [
  '岗位等级要求',
  'Career Elements',
] as const;

const MISSING_FIXED_DIMENSION_JUDGMENT = '历史报告未记录该子维度的判断。';

export interface ReportIssueItem {
  text: string;
  sourceIndex: number;
  title?: string;
  titleIndex?: number;
  isPlaceholder?: boolean;
}

export interface ReportImprovementRow {
  sourceIndex: number;
  title?: string;
  titleIndex?: number;
  issue?: ReportIssueItem;
  phrase?: CoachBetterPhrase;
  phraseSourceIndex?: number;
}

export interface ReportImprovementEmptyCopy {
  finding: string;
  suggestion: string;
}

interface FixedIssueDimension {
  id: string;
  title: string;
}

const FIXED_ISSUE_DIMENSIONS_BY_TASK: Readonly<Record<string, readonly FixedIssueDimension[]>> = {
  opening_evaluation: [
    { id: 'communication_tone', title: OPENING_ISSUE_TITLES[0] },
    { id: 'performance_result_alignment', title: OPENING_ISSUE_TITLES[1] },
  ],
  output_expectations_evaluation: [
    { id: 'goal_direction_correctness', title: OUTPUT_EXPECTATIONS_ISSUE_TITLES[0] },
    { id: 'result_breakthrough', title: OUTPUT_EXPECTATIONS_ISSUE_TITLES[1] },
    { id: 'high_performance_culture', title: OUTPUT_EXPECTATIONS_ISSUE_TITLES[2] },
  ],
  emotion_evaluation: [
    { id: 'listening_and_emotion_understanding', title: EMOTION_ISSUE_TITLES[0] },
    { id: 'empathic_summary', title: EMOTION_ISSUE_TITLES[1] },
    { id: 'joint_exploration', title: EMOTION_ISSUE_TITLES[2] },
  ],
  development_plan_evaluation: [
    { id: 'job_level_requirements', title: DEVELOPMENT_PLAN_ISSUE_TITLES[0] },
    { id: 'career_elements', title: DEVELOPMENT_PLAN_ISSUE_TITLES[1] },
  ],
};

function normalizedIssueItems(result: CoachTaskResult): Array<Omit<ReportIssueItem, 'title'>> {
  return (result.improvement_points || []).map((item, sourceIndex) => {
    const text = item.trim().replace(/结果的突破型/g, '结果的突破性');
    return { text, sourceIndex };
  }).filter((item) => Boolean(item.text));
}

function fixedIssueDimensions(result: CoachTaskResult): readonly FixedIssueDimension[] | undefined {
  if (result.status !== 'success') return undefined;
  const dimensions = FIXED_ISSUE_DIMENSIONS_BY_TASK[result.task_id];
  if (result.task_id !== 'development_plan_evaluation' || !dimensions) {
    return dimensions;
  }
  const declaredIds = new Set(
    (result.better_phrases || [])
      .map((phrase) => phrase.diagnostic_dimension_id?.trim())
      .filter((dimensionId): dimensionId is string => Boolean(dimensionId)),
  );
  const applicable = dimensions.filter(({ id }) => declaredIds.has(id));
  return applicable.length ? applicable : undefined;
}

export function reportImprovementEmptyCopy(
  result: CoachTaskResult,
  displayScore: number | null | undefined,
): ReportImprovementEmptyCopy {
  if (result.status === 'failed') {
    return {
      finding: '该维度评估失败，未生成改进内容。',
      suggestion: '请重新生成报告后查看具体建议。',
    };
  }
  if (result.status === 'insufficient_information') {
    return {
      finding: '当前对话信息不足，尚无法形成改进判断。',
      suggestion: '补充完成相关对话后再生成具体建议。',
    };
  }
  if (typeof displayScore === 'number' && displayScore <= 3) {
    return {
      finding: `该维度评分为 ${displayScore} 分，但评估结果未返回与评分对应的改进问题。`,
      suggestion: '请重新生成该维度评估，以获取可核验的具体建议。',
    };
  }
  if (displayScore === 5) {
    return {
      finding: '本轮评分为 5 分，未返回明确改进问题。',
      suggestion: '本轮未返回对应的具体建议。',
    };
  }
  if (displayScore === 4) {
    return {
      finding: '本轮评分为 4 分，未返回有经理原话支持的明确改进问题。',
      suggestion: '本轮未返回对应的具体建议。',
    };
  }
  return {
    finding: '该维度评估结果未返回分数或明确改进问题。',
    suggestion: '请重新生成该维度评估后查看具体建议。',
  };
}

export function buildReportImprovementRows(
  result: CoachTaskResult | undefined,
): ReportImprovementRow[] {
  if (!result) return [];

  const issues = normalizedIssueItems(result);
  const phrases = (result.better_phrases || [])
    .map((phrase, sourceIndex) => ({ phrase, sourceIndex }))
    .filter(({ phrase }) => Boolean(
      phrase.original?.trim()
      || phrase.suggestion?.trim()
      || phrase.reason?.trim(),
    ));
  const fixedDimensions = fixedIssueDimensions(result);
  if (fixedDimensions) {
    const issuesBySourceIndex = new Map(
      issues.map((issue) => [issue.sourceIndex, issue]),
    );
    const phrasesBySourceIndex = new Map(
      phrases.map(({ phrase, sourceIndex }) => [sourceIndex, phrase]),
    );
    const sourceIndexes = [...new Set([
      ...issuesBySourceIndex.keys(),
      ...phrasesBySourceIndex.keys(),
    ])].sort((left, right) => left - right);
    const hasDeclaredDimension = phrases.some(
      ({ phrase }) => Boolean(phrase.diagnostic_dimension_id?.trim()),
    );
    const useLegacyPositions = !hasDeclaredDimension
      && sourceIndexes.length === fixedDimensions.length
      && sourceIndexes.every((sourceIndex, index) => sourceIndex === index);
    const hasSparseLegacyContent = !hasDeclaredDimension
      && sourceIndexes.length > 0
      && !useLegacyPositions;
    const fixedIndexById = new Map(
      fixedDimensions.map(({ id }, index) => [id, index]),
    );
    const assigned = new Map<number, ReportImprovementRow[]>();
    const unclassified: ReportImprovementRow[] = [];

    for (const sourceIndex of sourceIndexes) {
      const phrase = phrasesBySourceIndex.get(sourceIndex);
      const declaredId = phrase?.diagnostic_dimension_id?.trim();
      const fixedIndex = declaredId
        ? fixedIndexById.get(declaredId)
        : useLegacyPositions
          ? sourceIndex
          : undefined;
      const row: ReportImprovementRow = {
        sourceIndex,
        issue: issuesBySourceIndex.get(sourceIndex),
        phrase,
        phraseSourceIndex: phrase ? sourceIndex : undefined,
      };
      if (fixedIndex === undefined) {
        unclassified.push(row);
      } else {
        const dimensionRows = assigned.get(fixedIndex) || [];
        dimensionRows.push(row);
        assigned.set(fixedIndex, dimensionRows);
      }
    }

    const fixedRows = fixedDimensions.flatMap<ReportImprovementRow>(
      ({ title }, fixedIndex): ReportImprovementRow[] => {
        const rows = assigned.get(fixedIndex) || [];
        if (!rows.length) {
          return hasSparseLegacyContent
            ? [{
                sourceIndex: fixedIndex,
                title,
                issue: {
                  text: MISSING_FIXED_DIMENSION_JUDGMENT,
                  sourceIndex: fixedIndex,
                  title,
                  isPlaceholder: true,
                },
              }]
            : [];
        }

        return rows.map((row) => {
          const issue = row.issue
            ? { ...row.issue, title }
            : {
                text: MISSING_FIXED_DIMENSION_JUDGMENT,
                sourceIndex: row.sourceIndex,
                title,
                isPlaceholder: true,
              };
          return {
            sourceIndex: fixedIndex,
            title,
            issue,
            phrase: row.phrase,
            phraseSourceIndex: row.phraseSourceIndex,
          };
        });
      },
    );
    const unclassifiedTitle = result.task_id === 'emotion_evaluation'
      ? '当前需要解决的问题'
      : '未标明子维度的改进项';
    const unclassifiedRows = unclassified.map((row, index) => {
      const title = unclassifiedTitle;
      const titleIndex = unclassified.length === 1 ? undefined : index + 1;
      return {
        ...row,
        sourceIndex: fixedDimensions.length + index,
        title,
        titleIndex,
        issue: row.issue ? { ...row.issue, title, titleIndex } : undefined,
      };
    });
    return [...fixedRows, ...unclassifiedRows];
  }

  const issuesBySourceIndex = new Map(
    issues.map((issue) => [issue.sourceIndex, issue]),
  );
  const phrasesBySourceIndex = new Map(
    phrases.map(({ phrase, sourceIndex }) => [sourceIndex, phrase]),
  );
  const sourceIndexes = new Set([
    ...issuesBySourceIndex.keys(),
    ...phrasesBySourceIndex.keys(),
  ]);

  return [...sourceIndexes]
    .sort((left, right) => left - right)
    .map((sourceIndex) => ({
      sourceIndex,
      issue: issuesBySourceIndex.get(sourceIndex),
      phrase: phrasesBySourceIndex.get(sourceIndex),
      phraseSourceIndex: phrasesBySourceIndex.has(sourceIndex)
        ? sourceIndex
        : undefined,
    }));
}
