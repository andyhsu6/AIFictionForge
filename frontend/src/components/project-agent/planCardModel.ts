import type { AgentToolCall } from '../../types';

export const PLAN_TOOL_NAME = 'propose_plan';
export const PLAN_TASK_TYPE = 'agent_plan';
/** 展示裁剪上限，对齐 runner 的 MAX_PLAN_STEPS（agent_plan_runner.py:58）；
 *  批准闸门是 schema 的 12 步上限（agent_plan_schema.py:14,115），前端不做授权判定。 */
export const PLAN_MAX_STEPS = 30;

export interface AgentPlanStep {
  id: string;
  tool: string;
  action: string | null;
  arguments: Record<string, unknown>;
  note?: string;
}

export interface AgentPlanPayload {
  objective: string;
  steps: AgentPlanStep[];
}

/** 计划行 progress_details 的读取视图（键名必须与 PR-2b 写方 `_details()` 逐字一致；
 *  读不到键 = 形状漂移缺陷，不是可接受的默认值——缺键归零只会让进度恒为 0/0）。 */
export interface PlanProgressView {
  completed: number;
  total: number;
  failedAtStep: number | null;
  running: boolean;
  settled: boolean;
  subTaskIds: string[];
  uncancellableSubTaskIds: string[];
}

export function isPlanToolCall(toolCall: AgentToolCall): boolean {
  return toolCall.tool_name === PLAN_TOOL_NAME;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    try {
      return asRecord(JSON.parse(value));
    } catch {
      return null;
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) return value as Record<string, unknown>;
  return null;
}

/**
 * 从 `propose_plan` 的 arguments 还原计划。
 * 任何一步形状不合就整体返回 null：宁可不出卡片，也不给用户一张「看起来能批准、
 * 批准后端就报错」的残缺计划卡。
 */
export function parsePlanPayload(toolCall: AgentToolCall): AgentPlanPayload | null {
  if (!isPlanToolCall(toolCall)) return null;
  const source = asRecord(toolCall.arguments);
  if (!source) return null;
  const objective = typeof source.objective === 'string' ? source.objective : '';
  const steps = source.steps;
  if (!Array.isArray(steps) || steps.length === 0 || steps.length > PLAN_MAX_STEPS) return null;
  const parsed: AgentPlanStep[] = [];
  const seenIds = new Set<string>();
  for (const item of steps) {
    const entry = asRecord(item);
    if (!entry || typeof entry.id !== 'string' || !entry.id) return null;
    // id 重复 = 后端 validate_plan 会拒收（agent_plan_schema.py:124-126）。
    if (seenIds.has(entry.id)) return null;
    seenIds.add(entry.id);
    // 后端 schema 只 required [id, tool]；action 可空（仅 start_project_task 强制 action）。
    if (typeof entry.tool !== 'string' || !entry.tool) return null;
    parsed.push({
      id: entry.id,
      tool: entry.tool,
      action: typeof entry.action === 'string' && entry.action ? entry.action : null,
      arguments: asRecord(entry.arguments) || {},
      note: typeof entry.note === 'string' ? entry.note : undefined,
    });
  }
  return { objective, steps: parsed };
}

/**
 * 批准后的计划任务行 id（进度/取消/轮询锚点）。
 * 必须同时校验 task_type：三张任务表的 id 无跨表唯一性，只读 entity_id 会认错任务。
 */
export function planTaskIdOf(toolCall: AgentToolCall): string | undefined {
  const result = asRecord(toolCall.result);
  if (!result) return undefined;
  if (result.task_type !== PLAN_TASK_TYPE) return undefined;
  return typeof result.entity_id === 'string' && result.entity_id ? result.entity_id : undefined;
}

function toIdArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.length > 0);
}

function toCount(value: unknown): number {
  const num = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(num) && num >= 0 ? Math.floor(num) : 0;
}

export function readPlanProgress(task: {
  status: string;
  progress_details?: Record<string, unknown> | null;
}): PlanProgressView {
  // 键名的唯一权威 = PR-2b 写方 runner 的 `_details()`（.omo/plans/plan-a-pr2b.md:1036-1052）：
  // 顶层只有 steps_total / steps_done / failed_at_step，取消信息**嵌套**在 cancel 下。
  // 顶层不存在 completed / total / sub_task_ids / uncancellable_sub_task_ids，
  // 读错键不会报错、只会让进度恒为 0/0。
  const details = asRecord(task.progress_details) || {};
  const cancel = asRecord(details.cancel) || {};
  const failedAt = details.failed_at_step;
  const failedAtStep = typeof failedAt === 'number' && failedAt > 0 ? failedAt : null;
  return {
    completed: toCount(details.steps_done),
    total: toCount(details.steps_total),
    failedAtStep,
    running: task.status === 'running',
    settled: task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled',
    // subTaskIds = 已被级联取消的子任务（cancel.cancelled_sub_tasks）
    subTaskIds: toIdArray(cancel.cancelled_sub_tasks),
    uncancellableSubTaskIds: toIdArray(cancel.uncancellable_sub_tasks),
  };
}

/**
 * 勾选/取消单步。三条不变量：
 *  - 至少保留 1 步（空计划批准服务端会报错，且语义上等于「拒绝」，走拒绝按钮）；
 *  - 组件调用必须传第三参（完整 `payload.steps.map(s => s.id)`）：结果按原计划 steps
 *    顺序，不因点选顺序变化（删除后序号引用仍指向前序步骤）；
 *  - 2 参调用时第三参默认 = selected，没有原始顺序可依，新选步骤前置，保证
 *    「取消后再选回最后一步」不是无操作（plan 测试第 2 例的期望形状）。
 */
export function toggleStepSelection(selected: string[], stepId: string, allStepIds: string[] = selected): string[] {
  if (selected.includes(stepId)) {
    if (selected.length === 1) return selected;
    return selected.filter(item => item !== stepId);
  }
  const order = allStepIds.includes(stepId) ? allStepIds : [stepId, ...allStepIds];
  return order.filter(item => item === stepId || selected.includes(item));
}
