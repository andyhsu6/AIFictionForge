// Plan card view-model tests. Pure functions only: no React, no network.
// Guards the three decisions PR-3 depends on:
//   1. only `propose_plan` tool calls render a plan card,
//   2. the plan task id is read from result.{entity_id,task_type} (PR-1 contract),
//   3. progress is derived from BackgroundTask.progress_details, never guessed.
import { describe, expect, it } from 'vitest';

import {
  decideSettleRefresh,
  isPlanToolCall,
  parsePlanPayload,
  planTaskIdOf,
  readPlanProgress,
  toggleStepSelection,
  type AgentPlanStep,
} from './planCardModel';
import type { AgentToolCall } from '../../types';

const step = (id: string, action: string): AgentPlanStep => ({
  id,
  tool: 'start_project_task',
  action,
  arguments: { chapter_number: 1 },
  note: `${action} note`,
});

function planToolCall(overrides: Partial<AgentToolCall> = {}): AgentToolCall {
  return {
    id: 'tc-1',
    conversation_id: 'conv-1',
    tool_name: 'propose_plan',
    arguments: { objective: 'plan objective', steps: [step('s1', 'analyze_chapter'), step('s2', 'generate_chapter')] },
    risk_level: 2,
    requires_confirmation: true,
    status: 'waiting_confirmation',
    created_at: '2026-09-13T00:00:00',
    ...overrides,
  };
}

describe('isPlanToolCall', () => {
  it('accepts only propose_plan', () => {
    expect(isPlanToolCall(planToolCall())).toBe(true);
    expect(isPlanToolCall(planToolCall({ tool_name: 'start_project_task' }))).toBe(false);
  });
});

describe('parsePlanPayload', () => {
  it('reads objective and steps', () => {
    const payload = parsePlanPayload(planToolCall());
    expect(payload?.objective).toBe('plan objective');
    expect(payload?.steps.map(item => item.id)).toEqual(['s1', 's2']);
  });

  it('accepts a JSON string payload from the TEXT column', () => {
    const raw = JSON.stringify({ objective: 'x', steps: [step('s1', 'analyze_chapter')] });
    const payload = parsePlanPayload(planToolCall({ arguments: raw as unknown as Record<string, unknown> }));
    expect(payload?.steps).toHaveLength(1);
  });

  it('returns null for malformed steps instead of rendering an empty card', () => {
    expect(parsePlanPayload(planToolCall({ arguments: { objective: 'x' } }))).toBeNull();
    expect(parsePlanPayload(planToolCall({ arguments: { objective: 'x', steps: [{ id: 's1' }] } }))).toBeNull();
    expect(parsePlanPayload(planToolCall({ arguments: 'not json' as unknown as Record<string, unknown> }))).toBeNull();
  });

  it('accepts a step without action (read-only tool) and stores null', () => {
    const payload = parsePlanPayload(planToolCall({
      arguments: { objective: 'x', steps: [{ id: 's1', tool: 'get_project_overview' }] },
    }));
    expect(payload?.steps).toHaveLength(1);
    expect(payload?.steps[0]?.action).toBeNull();
  });

  it('accepts an explicit null action', () => {
    const payload = parsePlanPayload(planToolCall({
      arguments: { objective: 'x', steps: [{ id: 's1', tool: 'list_chapters', action: null }] },
    }));
    expect(payload?.steps).toHaveLength(1);
    expect(payload?.steps[0]?.action).toBeNull();
  });

  it('rejects a step whose tool is missing or empty (no action fallback)', () => {
    expect(parsePlanPayload(planToolCall({
      arguments: { objective: 'x', steps: [{ id: 's1', action: 'analyze_chapter' }] },
    }))).toBeNull();
    expect(parsePlanPayload(planToolCall({
      arguments: { objective: 'x', steps: [{ id: 's1', tool: '', action: 'analyze_chapter' }] },
    }))).toBeNull();
  });

  it('rejects duplicate step ids', () => {
    expect(parsePlanPayload(planToolCall({
      arguments: {
        objective: 'x',
        steps: [step('s1', 'analyze_chapter'), step('s1', 'generate_chapter')],
      },
    }))).toBeNull();
  });
});

describe('planTaskIdOf', () => {
  it('returns entity_id only when task_type is agent_plan', () => {
    expect(planTaskIdOf(planToolCall({
      status: 'executed',
      result: { entity_id: 'plan-9', task_type: 'agent_plan' },
    }))).toBe('plan-9');
    expect(planTaskIdOf(planToolCall({ result: { entity_id: 'other-9', task_type: 'outline_new' } }))).toBeUndefined();
    expect(planTaskIdOf(planToolCall({ result: { entity_id: 'no-type' } }))).toBeUndefined();
    expect(planTaskIdOf(planToolCall())).toBeUndefined();
  });
});

describe('readPlanProgress', () => {
  // ⚠️ 夹具的**唯一来源** = PR-2b 写方 runner 的 `_details()` 返回字面量
  // （.omo/plans/plan-a-pr2b.md:1036-1052）。派工时先把那段代码逐字读一遍、按其键名与
  // 嵌套层次构造夹具；**禁止**再手写一份"看起来合理"的形状——手写夹具会让假绿的
  // 读方也一起变绿（本条即上一版 N2 缺陷的成因）。
  it('derives steps_done/steps_total and terminal flags from progress_details', () => {
    expect(readPlanProgress({
      status: 'running',
      progress_details: {
        stage: 'running', message: '进行中', outcome: 'running',
        steps_total: 5, steps_done: 2, failed_at_step: null,
        cancel: { requested: false, reason: null, cancelled_sub_tasks: ['a', 'b'], uncancellable_sub_tasks: [] },
        step_results: [],
      },
    })).toMatchObject({ completed: 2, total: 5, failedAtStep: null, running: true, settled: false, subTaskIds: ['a', 'b'] });
  });

  it('treats cancelled/failed as settled and keeps failed_at_step', () => {
    expect(readPlanProgress({
      status: 'failed',
      progress_details: {
        stage: 'failed', message: '第 2 步失败', outcome: 'failed',
        steps_total: 3, steps_done: 1, failed_at_step: 2,
        cancel: { requested: false, reason: null, cancelled_sub_tasks: [], uncancellable_sub_tasks: ['c'] },
        step_results: [],
      },
    })).toMatchObject({ completed: 1, total: 3, failedAtStep: 2, settled: true, running: false, uncancellableSubTaskIds: ['c'] });
  });

  it('defaults to zeros on a task without progress details', () => {
    expect(readPlanProgress({ status: 'pending', progress_details: null }))
      .toMatchObject({ completed: 0, total: 0, failedAtStep: null, running: false, settled: false, subTaskIds: [] });
  });
});

describe('toggleStepSelection', () => {
  it('keeps the last selected id when unselecting to a single step plan', () => {
    expect(toggleStepSelection(['s1', 's2'], 's1')).toEqual(['s2']);
    expect(toggleStepSelection(['s2'], 's1')).toEqual(['s1', 's2']);
    expect(toggleStepSelection(['s1'], 's1')).toEqual(['s1']);
  });

  it('restores the original plan order when the component passes allStepIds', () => {
    const all = ['s1', 's2', 's3'];
    // Click order must not leak into the result: the plan order decides.
    expect(toggleStepSelection(['s3'], 's2', all)).toEqual(['s2', 's3']);
    expect(toggleStepSelection(['s1', 's3'], 's2', all)).toEqual(['s1', 's2', 's3']);
    expect(toggleStepSelection(['s1', 's2', 's3'], 's2', all)).toEqual(['s1', 's3']);
  });
});

describe('decideSettleRefresh', () => {
  const base = {
    currentProjectId: 'proj-1',
    activeConversationId: 'conv-1',
    sending: false,
  };

  it('ignores every non-plan task so ProjectDetail stays the only business refresher', () => {
    expect(decideSettleRefresh({ ...base, taskType: 'outline_new', eventProjectId: 'proj-1' })).toBe('ignore');
    expect(decideSettleRefresh({ ...base, taskType: undefined, eventProjectId: 'proj-1' })).toBe('ignore');
  });

  it('ignores events from another project', () => {
    expect(decideSettleRefresh({ ...base, taskType: 'agent_plan', eventProjectId: 'proj-2' })).toBe('ignore');
  });

  it('only refreshes the list when the plan belongs to another conversation', () => {
    expect(decideSettleRefresh({
      ...base, taskType: 'agent_plan', eventProjectId: 'proj-1', eventConversationId: 'conv-2',
    })).toBe('list-only');
  });

  it('defers while a turn is being streamed so the optimistic placeholder survives', () => {
    expect(decideSettleRefresh({
      ...base, taskType: 'agent_plan', eventProjectId: 'proj-1', eventConversationId: 'conv-1', sending: true,
    })).toBe('defer');
  });

  it('reloads the active conversation once streaming is idle', () => {
    expect(decideSettleRefresh({
      ...base, taskType: 'agent_plan', eventProjectId: 'proj-1', eventConversationId: 'conv-1',
    })).toBe('reload');
    // 老任务行没有 conversation_id（透出前创建的历史任务）时按当前会话处理
    expect(decideSettleRefresh({
      ...base, taskType: 'agent_plan', eventProjectId: 'proj-1', eventConversationId: null,
    })).toBe('reload');
  });

  it('ignores when there is nothing to reload', () => {
    expect(decideSettleRefresh({
      ...base, activeConversationId: undefined, taskType: 'agent_plan', eventProjectId: 'proj-1', eventConversationId: null,
    })).toBe('ignore');
  });
});
