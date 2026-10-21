// Running-plan progress + the stop door. Two behaviours worth pinning:
//  - the stop button calls the plan-specific endpoint, never the generic
//    task cancel (which would freeze the plan row before final step counts);
//  - runner steps that carry no assistant_message_id still render, otherwise a
//    plan looks frozen even though the server is writing steps.
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectAgentPanel from './ProjectAgentPanel';
import { projectAgentApi } from '../../services/api';
import i18n from '../../i18n';

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    projectAgentApi: {
      listConversations: vi.fn(() => Promise.resolve([])),
      createConversation: vi.fn(),
      getConversation: vi.fn(),
      deleteConversation: vi.fn(),
      confirmToolCall: vi.fn(),
      rejectToolCall: vi.fn(),
      approvePlan: vi.fn(),
      cancelPlan: vi.fn(() => Promise.resolve({ plan_task_id: 'plan-1', status: 'cancelling', message: 'ok' })),
      chatStream: vi.fn(),
    },
  };
});

const runningPlan = {
  id: 'tc-plan',
  conversation_id: 'conv-1',
  message_id: 'm-assistant',
  tool_name: 'propose_plan',
  arguments: {
    objective: 'plan objective',
    steps: [
      { id: 's1', tool: 'start_project_task', action: 'analyze_chapter', arguments: {} },
      { id: 's2', tool: 'start_project_task', action: 'generate_chapter', arguments: {} },
    ],
  },
  risk_level: 2,
  requires_confirmation: true,
  status: 'executing',
  result: { entity_id: 'plan-1', task_type: 'agent_plan' },
  created_at: '2026-09-13T00:00:00',
};

const orphanStep = {
  id: 'step-1',
  conversation_id: 'conv-1',
  sequence: 1,
  step_type: 'tool',
  category: 'project',
  title: 'analyze chapter one',
  content: 'running',
  status: 'running',
  created_at: '2026-09-13T00:00:00',
  updated_at: '2026-09-13T00:00:00',
};

const planExecutionStep = (overrides: Partial<typeof orphanStep & { tool_call_id: string }> = {}) => ({
  ...orphanStep,
  ...overrides,
});

beforeAll(() => {
  // jsdom ships no scrollIntoView; the panel auto-scrolls on every message/step
  // update (same shim as planSettleRefresh.test.tsx).
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(async () => {
  vi.clearAllMocks();
  vi.mocked(projectAgentApi.listConversations).mockResolvedValue([
    { id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active', last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00' },
  ]);
  vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
    id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
    last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
    messages: [{ id: 'm-assistant', conversation_id: 'conv-1', role: 'assistant', content: '', created_at: '2026-09-13T00:00:00' }],
    tool_calls: [runningPlan],
    execution_steps: [orphanStep],
  });
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
});

describe('running plan progress', () => {
  it('shows the stop door and cancels through the plan endpoint', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(view.queryByTestId('plan-stop-button')).toBeTruthy());
    fireEvent.click(view.getByTestId('plan-stop-button'));
    // The trigger itself matches the name regex too; the OK button is the match
    // without the panel-owned testid.
    const confirm = await waitFor(() => {
      const okButtons = screen.getAllByRole('button', { name: /停止|OK|确定/ })
        .filter(button => !button.hasAttribute('data-testid'));
      expect(okButtons).toHaveLength(1);
      return okButtons[0];
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(projectAgentApi.cancelPlan).toHaveBeenCalledWith('proj-1', 'plan-1'));
  });

  it('renders runner steps that carry no assistant_message_id', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(view.queryByTestId('plan-steps')).toBeTruthy());
    expect(view.getByTestId('plan-steps').textContent).toContain('analyze chapter one');
  });

  it('shows no plan progress block for a non-plan executing tool call', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
      ...(
        await projectAgentApi.getConversation('proj-1', 'conv-1')
      ),
      tool_calls: [{ ...runningPlan, tool_name: 'start_project_task' }],
      execution_steps: [],
    });
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalled());
    expect(view.queryByTestId('plan-stop-button')).toBeNull();
  });

  it('renders an orphan runner step exactly once across the whole panel', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(view.queryByTestId('plan-steps')).toBeTruthy());
    expect(view.getAllByText('analyze chapter one')).toHaveLength(1);
  });

  it('shows only the running plan steps, not failed steps from an older plan', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
      id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
      last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
      messages: [{ id: 'm-assistant', conversation_id: 'conv-1', role: 'assistant', content: '', created_at: '2026-09-13T00:00:00' }],
      tool_calls: [runningPlan],
      execution_steps: [
        planExecutionStep({ id: 'old-step', tool_call_id: 'tc-old', title: '计划第 1/12 步：manage_organization', status: 'failed' }),
        planExecutionStep({ id: 'current-step', tool_call_id: 'tc-plan', title: '本次计划步骤', status: 'running' }),
      ],
    });

    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    const steps = await waitFor(() => view.getByTestId('plan-steps'));
    expect(steps.textContent).toContain('本次计划步骤');
    expect(steps.textContent).not.toContain('manage_organization');
  });

  it('counts completed steps from the current plan only', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
      id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
      last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
      messages: [{ id: 'm-assistant', conversation_id: 'conv-1', role: 'assistant', content: '', created_at: '2026-09-13T00:00:00' }],
      tool_calls: [runningPlan],
      execution_steps: [
        planExecutionStep({ id: 'current-completed', tool_call_id: 'tc-plan', title: '已完成步骤', status: 'completed' }),
        planExecutionStep({ id: 'current-running', tool_call_id: 'tc-plan', title: '执行中步骤', status: 'running' }),
        planExecutionStep({ id: 'old-failed', tool_call_id: 'tc-old', title: '历史失败步骤', status: 'failed' }),
      ],
    });

    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(view.getByText('第 1 / 2 步')).toBeTruthy());
    expect(view.queryByText('第 2 / 2 步')).toBeNull();
  });
});
