import type { AgentToolCall } from '../../types';

export const PLAN_TOOL_NAME = 'propose_plan';
export const PLAN_TASK_TYPE = 'agent_plan';
/** 上限 = 批准闸门，不是展示裁剪：批准路径的 validate_plan 拒绝 >12 步
 *  （agent_plan_schema.py:14,115）；运行器的 30 步（agent_plan_runner.py:58）只是
 *  runner 自身护栏。超过 12 步的 payload 一律不解析，否则卡片会给出一个点下去
 *  必然被后端拒收的批准入口。 */
export const PLAN_MAX_STEPS = 12;

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

export type SettleRefreshDecision = 'reload' | 'defer' | 'list-only' | 'ignore';

/**
 * 计划完成事件是否要刷会话、怎么刷。
 * 抽成纯函数的原因：这条判定同时承担三件事——
 *  1) 与 ProjectDetail 的 SETTLED 监听分工（非 agent_plan 一律 ignore，业务数据刷新只归它）；
 *  2) 会话归属（事件带的 conversationId 与当前会话不同 ⇒ 只刷列表，不覆盖视图）；
 *  3) 撞上 send() 乐观占位时延后刷新。
 */
export function decideSettleRefresh(input: {
  taskType?: string | null;
  eventProjectId?: string | null;
  currentProjectId: string;
  eventConversationId?: string | null;
  activeConversationId?: string;
  sending: boolean;
}): SettleRefreshDecision {
  if (input.taskType !== PLAN_TASK_TYPE) return 'ignore';
  if (input.eventProjectId && input.eventProjectId !== input.currentProjectId) return 'ignore';
  if (input.eventConversationId && input.activeConversationId
    && input.eventConversationId !== input.activeConversationId) return 'list-only';
  const target = input.eventConversationId || input.activeConversationId;
  if (!target) return 'ignore';
  return input.sending ? 'defer' : 'reload';
}

/** 计划轮询的唯一判据：计划工具调用仍在 executing（runner 收尾后会变 executed/failed）。 */
export function shouldPollRunningPlan(
  toolCalls: Array<Pick<AgentToolCall, 'tool_name' | 'status'>>,
): boolean {
  return toolCalls.some(toolCall => isPlanToolCall(toolCall as AgentToolCall) && toolCall.status === 'executing');
}

/**
 * 该 role=tool 消息是否属于某份计划的收尾摘要。
 * 两个信号取或：内容里的 tool 名，或 tool_call_id 指向一个 propose_plan 记录。
 * 不能只信 tool_call_id：provider 未回传 id 时它会回退成 AgentToolCall.id（§0 的坑）。
 * 名字信号必须覆盖 PR-2c 实际写出的哨兵 `plan_run_summary`（agent_plan_runner.py:68/581），
 * 否则 provider id 与 record id 不同时会漏认收尾摘要。
 */
export function isPlanSummaryMessage(input: {
  tool_call_id?: string;
  parsedToolName?: unknown;
}, planToolCallIds: Set<string>): boolean {
  if (typeof input.parsedToolName === 'string'
    && (input.parsedToolName === PLAN_TOOL_NAME
      || input.parsedToolName === 'plan_summary'
      || input.parsedToolName === 'plan_run_summary')) return true;
  return Boolean(input.tool_call_id && planToolCallIds.has(input.tool_call_id));
}

/** 计划卡上需要标注"会覆盖既有结果"的 action 集合（协议标识，禁止本地化）。
 *  判据来源：架构 §6 保持确认（risk 2）列表里的覆盖型动作。 */
export const PLAN_OVERWRITE_ACTIONS: ReadonlySet<string> = new Set([
  'analyze_chapter',
  'regenerate_chapter',
  'partial_regenerate_chapter',
  'batch_generate_chapters',
  'replace_chapter_text',
]);
