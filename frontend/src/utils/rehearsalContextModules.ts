export const REHEARSAL_CONTEXT_MODULES = [
  { id: 'context', label: '会话信息' },
  { id: 'employee', label: '员工信息' },
  { id: 'goals', label: '员工目标' },
  { id: 'performance', label: '员工表现' },
  { id: 'guidance', label: '谈前指导' },
] as const;

export type RehearsalContextModuleId = typeof REHEARSAL_CONTEXT_MODULES[number]['id'];
export type RehearsalContextModulePosition = 'before' | 'active' | 'after';

export function getRehearsalContextModulePosition(
  index: number,
  activeIndex: number,
): RehearsalContextModulePosition {
  if (index < activeIndex) return 'before';
  if (index === activeIndex) return 'active';
  return 'after';
}
