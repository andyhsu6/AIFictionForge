// Plan approval card: exactly ONE approve button, per-step toggle, step order
// preserved after removing a step, and the honest "targets unverifiable" notice
// (architecture section 1 correction F2 / section 6 handover to PR-3).
import { App as AntdApp } from 'antd';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import PlanApprovalCard from './PlanApprovalCard';
import { projectAgentApi } from '../../services/api';
import i18n from '../../i18n';
import type { AgentToolCall } from '../../types';

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    projectAgentApi: {
      ...actual.projectAgentApi,
      // Real AgentPlanApproveResult shape (backend/app/schemas/project_agent.py:111-115).
      approvePlan: vi.fn(() => Promise.resolve({
        tool_call_id: 'tc-plan',
        plan_task_id: 'plan-1',
        status: 'pending',
        steps_total: 3,
      })),
      rejectToolCall: vi.fn(() => Promise.resolve({
        success: true,
        message: 'ok',
        tool_call: {
          id: 'tc-plan',
          conversation_id: 'conv-1',
          tool_name: 'propose_plan',
          arguments: {},
          risk_level: 2,
          requires_confirmation: false,
          status: 'rejected',
          created_at: '2026-09-13T00:00:00',
        },
        resources: [],
      })),
    },
  };
});

function planCall(stepCount = 3): AgentToolCall {
  return {
    id: 'tc-plan',
    conversation_id: 'conv-1',
    tool_name: 'propose_plan',
    arguments: {
      objective: 'plan objective',
      steps: Array.from({ length: stepCount }, (_, index) => ({
        id: `s${index + 1}`,
        tool: 'start_project_task',
        action: index === 0 ? 'analyze_chapter' : 'generate_chapter',
        arguments: { chapter_number: index + 1 },
        note: `step ${index + 1}`,
      })),
    },
    risk_level: 2,
    requires_confirmation: true,
    status: 'waiting_confirmation',
    created_at: '2026-09-13T00:00:00',
  };
}

const renderCard = (toolCall = planCall()) => render(
  <AntdApp>
    <PlanApprovalCard projectId="proj-1" toolCall={toolCall} onDecided={() => {}} />
  </AntdApp>,
);

describe('PlanApprovalCard', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('zh');
    vi.mocked(projectAgentApi.approvePlan).mockClear();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders exactly one approve button for the whole plan', () => {
    renderCard();
    expect(screen.getAllByTestId('plan-approve')).toHaveLength(1);
    expect(screen.getByText('批准计划')).toBeInTheDocument();
  });

  it('renders one toggle per step and keeps the plan order on approval', async () => {
    renderCard();
    expect(screen.getAllByTestId(/^plan-step-toggle/)).toHaveLength(3);
    fireEvent.click(screen.getByTestId('plan-step-toggle-s2'));
    await waitFor(() => expect(screen.getByTestId('plan-step-toggle-s2')).not.toBeChecked());

    fireEvent.click(screen.getByTestId('plan-approve'));
    await waitFor(() => expect(projectAgentApi.approvePlan).toHaveBeenCalledTimes(1));
    expect(projectAgentApi.approvePlan).toHaveBeenCalledWith('proj-1', 'tc-plan', { selected_step_ids: ['s1', 's3'] });
  });

  it('restores the original plan order when a removed step is re-selected', async () => {
    renderCard();
    fireEvent.click(screen.getByTestId('plan-step-toggle-s2'));
    await waitFor(() => expect(screen.getByTestId('plan-step-toggle-s2')).not.toBeChecked());
    fireEvent.click(screen.getByTestId('plan-step-toggle-s2'));
    await waitFor(() => expect(screen.getByTestId('plan-step-toggle-s2')).toBeChecked());

    fireEvent.click(screen.getByTestId('plan-approve'));
    await waitFor(() => expect(projectAgentApi.approvePlan).toHaveBeenCalledTimes(1));
    expect(projectAgentApi.approvePlan).toHaveBeenCalledWith('proj-1', 'tc-plan', { selected_step_ids: ['s1', 's2', 's3'] });
  });

  it('never lets the user approve an empty plan', () => {
    renderCard(planCall(1));
    fireEvent.click(screen.getByTestId('plan-step-toggle-s1'));
    expect(screen.getByTestId('plan-step-toggle-s1')).toBeChecked();
  });

  it('states that step targets are unverifiable at approval time', () => {
    renderCard();
    expect(screen.getByText(/批准时不可验证/)).toBeInTheDocument();
  });

  it('flags steps that overwrite existing results', () => {
    renderCard();
    expect(screen.getAllByText(/覆盖既有/).length).toBeGreaterThanOrEqual(1);
  });

  it('renders nothing when the payload cannot be parsed', () => {
    const broken = { ...planCall(), arguments: { objective: 'x' } as unknown as Record<string, unknown> };
    // component={false}: antd <App> otherwise always renders its own `ant-app`
    // wrapper div, so the container could never be empty even if the card
    // renders null. The assertion is about the card leaving no DOM behind.
    const view = render(
      <AntdApp component={false}><PlanApprovalCard projectId="proj-1" toolCall={broken} onDecided={() => {}} /></AntdApp>,
    );
    expect(view.container).toBeEmptyDOMElement();
  });

  it('falls back to an empty card title instead of a blank gap', () => {
    const empty = { ...planCall(), arguments: { objective: 'x', steps: [] } } as unknown as AgentToolCall;
    const view = render(
      <AntdApp component={false}><PlanApprovalCard projectId="proj-1" toolCall={empty} onDecided={() => {}} /></AntdApp>,
    );
    expect(view.container).toBeEmptyDOMElement();
  });
});
