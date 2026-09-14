import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import FloatingTaskPanel from '../components/FloatingTaskPanel';
import i18n from '../i18n';
import { eventBus, EventNames } from '../store/eventBus';
import { getProjectTasks, type TaskStatus } from '../services/backgroundTaskService';

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');
}

const panelEmitSource = readSource('../components/FloatingTaskPanel.tsx');

const panelSource = readSource('../components/project-agent/ProjectAgentPanel.tsx');

const apiSource = readSource('../services/api.ts');

const typesSource = readSource('../types/index.ts');

describe('background task settled payload contract', () => {
  it('carries conversation attribution for plan tasks', () => {
    expect(panelEmitSource).toContain('conversationId: task.conversation_id ?? null');
    expect(panelEmitSource).toContain('taskType: task.task_type');
  });

  it('keeps task_input out of the panel-facing payload', () => {
    // 计划卡数据源是 AgentToolCall.arguments，不依赖 task_input；
    // 面板若开始读 task_input，说明后端最小透出被绕过。
    expect(panelSource).not.toContain('task_input');
  });
});

describe('plan progress contract', () => {
  it('renders runner steps that are not attached to any message', () => {
    // PR-2b 的 _insert_step 允许 assistant_message_id 为空 ⇒ 面板必须有自己的挂载点，
    // 否则计划执行步骤会落库但永远不显示。
    expect(panelSource).toContain('plan-steps');
    expect(panelSource).toContain('plan-stop-button');
  });
});

// Task 2 dropped these panel wiring assertions; the behavioural halves live in
// planProgress.test.tsx (stop door) and sendDefer.test.tsx (result attribution).
describe('plan panel wiring contract', () => {
  it('routes the stop door through the plan-specific cancel endpoint', () => {
    expect(panelSource).toContain('projectAgentApi.cancelPlan');
  });

  it('consumes the result SSE event as authoritative conversation attribution', () => {
    expect(panelSource).toContain('onResult:');
    expect(panelSource).toContain('resultConversationIdRef');
  });
});

describe('agent plan api contract', () => {
  it('exposes approve-plan keyed by the tool call id', () => {
    expect(apiSource).toContain('`/projects/${projectId}/agent/tool-calls/${toolCallId}/approve-plan`');
  });

  it('exposes a plan-specific cancel endpoint', () => {
    expect(apiSource).toContain('`/projects/${projectId}/agent/plans/${planTaskId}/cancel`');
  });

  it('sends selected_step_ids (the AgentPlanApprovalRequest field name)', () => {
    expect(apiSource).toContain('selected_step_ids');
  });
});

describe('agent plan approve result contract', () => {
  it('carries exactly the backend AgentPlanApprovalResponse fields', () => {
    const block = typesSource.match(/export interface AgentPlanApproveResult \{([^}]*)\}/)?.[1] ?? '';
    expect(block).toContain('tool_call_id');
    expect(block).toContain('plan_task_id');
    expect(block).toContain('status');
    expect(block).toContain('steps_total');
    expect(block).not.toMatch(/\bsteps\b/);
    expect(block).not.toMatch(/\bmessage\b/);
    const fields = block.split('\n').map(line => line.trim()).filter(line => line.length > 0);
    expect(fields).toHaveLength(4);
  });
});

// Behavioral guard for the settled-emit payload: the substring assertions above
// were proven vacuous (moving the emitted keys into a comment still passed), so
// mount the real panel against a stubbed task list and inspect the emitted args.
const { taskApi } = vi.hoisted(() => ({
  taskApi: {
    getProjectTasks: vi.fn(),
    getTaskStatus: vi.fn(),
    cancelTask: vi.fn(),
    cancelBatchTask: vi.fn(),
    deleteTask: vi.fn(),
    clearProjectTasks: vi.fn(),
  },
}));

vi.mock('../services/backgroundTaskService', () => taskApi);

function planTask(overrides: Partial<TaskStatus> = {}): TaskStatus {
  return {
    id: 'plan-task-1',
    task_type: 'agent_plan',
    project_id: 'proj-1',
    conversation_id: 'conv-9',
    status: 'completed',
    progress: 100,
    status_message: null,
    progress_details: null,
    error_message: null,
    task_result: null,
    retry_count: 0,
    cancel_requested: false,
    created_at: '2026-09-14T00:00:00',
    started_at: '2026-09-14T00:00:01',
    completed_at: '2026-09-14T00:00:02',
    updated_at: '2026-09-14T00:00:03',
    affected_resources: [],
    can_cancel: false,
    can_delete: true,
    ...overrides,
  };
}

function mountFloatingPanel(autoRefreshInterval = 3000) {
  return render(
    createElement(
      AntApp,
      null,
      createElement(FloatingTaskPanel, { projectId: 'proj-1', autoRefreshInterval }),
    ),
  );
}

describe('settled emit behavior carries conversation attribution', () => {
  beforeAll(async () => {
    await i18n.changeLanguage('zh');
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.mocked(getProjectTasks).mockReset();
  });

  it('emits conversationId + taskType when a watched plan task settles', async () => {
    const task = planTask();
    vi.mocked(getProjectTasks).mockResolvedValue({ items: [task] });
    const emitSpy = vi.spyOn(eventBus, 'emit');

    mountFloatingPanel();
    await waitFor(() => expect(getProjectTasks).toHaveBeenCalledTimes(1));

    // Register the task as watched, exactly like BACKGROUND_TASK_CREATED does at runtime.
    act(() => {
      eventBus.emit(EventNames.BACKGROUND_TASK_CREATED, { projectId: 'proj-1', taskId: task.id });
    });

    await waitFor(() => {
      expect(emitSpy).toHaveBeenCalledWith(
        EventNames.BACKGROUND_TASK_SETTLED,
        expect.objectContaining({ conversationId: 'conv-9', taskType: 'agent_plan' }),
      );
    });
  });

  it('emits the same attribution after a running -> completed transition', async () => {
    const running = planTask({ status: 'running', progress: 40 });
    const completed = planTask({ status: 'completed', progress: 100 });
    vi.mocked(getProjectTasks)
      .mockResolvedValueOnce({ items: [running] })
      .mockResolvedValue({ items: [completed] });
    const emitSpy = vi.spyOn(eventBus, 'emit');

    mountFloatingPanel(25);

    await waitFor(() => {
      expect(emitSpy).toHaveBeenCalledWith(
        EventNames.BACKGROUND_TASK_SETTLED,
        expect.objectContaining({ conversationId: 'conv-9', taskType: 'agent_plan' }),
      );
    }, { timeout: 2000 });
  });
});
