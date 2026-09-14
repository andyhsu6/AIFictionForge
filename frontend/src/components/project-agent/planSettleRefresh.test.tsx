// Panel-level wiring for PR-3: a settled agent_plan event must make the panel
// re-read the conversation (no typing required), must not double-request, and
// must never touch business data. projectAgentApi is fully mocked, so a call
// count here is exactly one HTTP request.
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import ProjectAgentPanel from './ProjectAgentPanel';
import { eventBus, EventNames } from '../../store/eventBus';
import { projectAgentApi } from '../../services/api';
import i18n from '../../i18n';
import type { AgentConversationDetail } from '../../types';

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    projectAgentApi: {
      listConversations: vi.fn(() => Promise.resolve([{ id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active', last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00' }])),
      createConversation: vi.fn(),
      getConversation: vi.fn(),
      deleteConversation: vi.fn(),
      confirmToolCall: vi.fn(),
      rejectToolCall: vi.fn(),
      approvePlan: vi.fn(),
      cancelPlan: vi.fn(),
      chatStream: vi.fn(() => new Promise<void>(() => {})),
    },
  };
});

const detail = (): AgentConversationDetail => ({
  id: 'conv-1',
  user_id: 'u',
  project_id: 'proj-1',
  title: 't',
  status: 'active',
  last_message_at: '2026-09-13T00:00:00',
  created_at: '2026-09-13T00:00:00',
  updated_at: '2026-09-13T00:00:00',
  messages: [{ id: 'm-1', conversation_id: 'conv-1', role: 'assistant', content: 'done', created_at: '2026-09-13T00:00:00' }],
  tool_calls: [],
  execution_steps: [],
});

const runningDetail = (): AgentConversationDetail => ({
  ...detail(),
  tool_calls: [{
    id: 'tc-plan',
    conversation_id: 'conv-1',
    tool_name: 'propose_plan',
    arguments: { objective: 'plan objective', steps: [{ id: 's1', tool: 'start_project_task', action: 'analyze_chapter', arguments: {} }] },
    risk_level: 2,
    requires_confirmation: true,
    status: 'executing',
    created_at: '2026-09-13T00:00:00',
  }],
  execution_steps: [{
    id: 'step-1',
    conversation_id: 'conv-1',
    assistant_message_id: 'm-1',
    sequence: 1,
    step_type: 'tool',
    category: 'analysis',
    title: 'Analyze chapter 1',
    content: 'step body',
    status: 'completed',
    created_at: '2026-09-13T00:00:00',
    updated_at: '2026-09-13T00:00:00',
  }],
});

const renderPanel = () => render(
  <MemoryRouter>
    <AntdApp>
      <ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} />
    </AntdApp>
  </MemoryRouter>,
);

describe('ProjectAgentPanel plan settlement refresh', () => {
  beforeAll(() => {
    // jsdom ships no scrollIntoView; the panel auto-scrolls on every message/step
    // update, so mounting without this shim throws inside a passive effect before
    // any assertion runs (same patch as project-agent-risk-copy.test.tsx:133).
    Element.prototype.scrollIntoView = () => {};
  });

  beforeEach(async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(detail());
    await i18n.changeLanguage('zh');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('reloads the conversation when an agent_plan settles', async () => {
    renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
      projectId: 'proj-1', taskId: 'plan-1', conversationId: 'conv-1', taskType: 'agent_plan',
      resources: ['chapters'], task: { id: 'plan-1', status: 'completed' },
    });

    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    expect(projectAgentApi.getConversation).toHaveBeenCalledWith('proj-1', 'conv-1');
  });

  it('collapses two settlements arriving during one reload into two requests total', async () => {
    // 去重语义：在途期间的事件只置一个 trailing 标记，不打第二炮，也不排队第三炮。
    let release: () => void = () => {};
    vi.mocked(projectAgentApi.getConversation).mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve(detail());
    }));
    renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    const payload = {
      projectId: 'proj-1', taskId: 'plan-1', conversationId: 'conv-1', taskType: 'agent_plan',
      resources: ['chapters'], task: { id: 'plan-1', status: 'completed' },
    };
    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, payload);
    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, payload);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    release();
    await new Promise(resolve => { setTimeout(resolve, 0); });
    expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(2);

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, payload);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(3));
  });

  it('ignores settled events for non-plan task types', async () => {
    renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
      projectId: 'proj-1', taskId: 't-2', conversationId: 'conv-1', taskType: 'chapter_regenerate',
      resources: ['chapters'], task: { id: 't-2', status: 'completed' },
    });
    await new Promise(resolve => { setTimeout(resolve, 30); });
    expect(projectAgentApi.getConversation).not.toHaveBeenCalled();
  });

  it('does not fetch another conversation but still refreshes the list', async () => {
    renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();
    vi.mocked(projectAgentApi.listConversations).mockClear();

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
      projectId: 'proj-1', taskId: 'plan-2', conversationId: 'conv-2', taskType: 'agent_plan',
      resources: ['chapters'], task: { id: 'plan-2', status: 'completed' },
    });

    await waitFor(() => expect(projectAgentApi.listConversations).toHaveBeenCalled());
    expect(projectAgentApi.getConversation).not.toHaveBeenCalled();
  });

  it('unsubscribes on unmount so a late event cannot refetch', async () => {
    const view = renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();
    view.unmount();

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
      projectId: 'proj-1', taskId: 'plan-1', conversationId: 'conv-1', taskType: 'agent_plan',
      resources: [], task: { id: 'plan-1', status: 'completed' },
    });
    await new Promise(resolve => { setTimeout(resolve, 30); });
    expect(projectAgentApi.getConversation).not.toHaveBeenCalled();
  });

  it('polls the conversation every 3s while the plan is executing', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
        ...detail(),
        tool_calls: [{
          id: 'tc-plan',
          conversation_id: 'conv-1',
          tool_name: 'propose_plan',
          arguments: { objective: 'plan objective', steps: [{ id: 's1', tool: 'start_project_task', action: 'analyze_chapter', arguments: {} }] },
          risk_level: 2,
          requires_confirmation: true,
          status: 'executing',
          created_at: '2026-09-13T00:00:00',
        }],
      });
      renderPanel();
      await vi.waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
      vi.mocked(projectAgentApi.getConversation).mockClear();

      await vi.advanceTimersByTimeAsync(3000);
      expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(3000);
      expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(2);
    } finally {
      act(() => { vi.useRealTimers(); });
    }
  });

  it('stops polling once no plan is executing', async () => {
    vi.useFakeTimers();
    try {
      renderPanel();
      await vi.waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
      vi.mocked(projectAgentApi.getConversation).mockClear();

      await vi.advanceTimersByTimeAsync(9000);
      expect(projectAgentApi.getConversation).not.toHaveBeenCalled();
    } finally {
      act(() => { vi.useRealTimers(); });
    }
  });

  it('keeps the transcript and an expanded process panel intact across a poll tick', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(projectAgentApi.getConversation).mockResolvedValue(runningDetail());
      renderPanel();
      await vi.waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
      vi.mocked(projectAgentApi.getConversation).mockClear();

      fireEvent.click(screen.getByText('思考与调用过程'));
      expect(screen.getByText('Analyze chapter 1')).toBeVisible();

      let releasePoll: (value: AgentConversationDetail) => void = () => {};
      vi.mocked(projectAgentApi.getConversation).mockImplementationOnce(
        () => new Promise(resolve => { releasePoll = resolve; })
      );

      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(document.querySelector('.ant-spin')).toBeNull();
      expect(screen.getByText('Analyze chapter 1')).toBeVisible();

      await act(async () => {
        releasePoll(runningDetail());
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(document.querySelector('.ant-spin')).toBeNull();
      expect(screen.getByText('Analyze chapter 1')).toBeVisible();
    } finally {
      act(() => { vi.useRealTimers(); });
    }
  });

  it('stops polling once a polled snapshot shows the plan left the executing state', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(projectAgentApi.getConversation)
        .mockResolvedValueOnce(runningDetail())
        .mockResolvedValue(detail());
      renderPanel();
      await vi.waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
      vi.mocked(projectAgentApi.getConversation).mockClear();

      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1);

      await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
      expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1);
    } finally {
      act(() => { vi.useRealTimers(); });
    }
  });

  it('defers a settle during streaming and reloads once the turn ends', async () => {
    let releaseStream: () => void = () => {};
    vi.mocked(projectAgentApi.chatStream).mockImplementation(() => new Promise<void>(resolve => {
      releaseStream = () => resolve();
    }));
    renderPanel();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    fireEvent.change(screen.getByPlaceholderText('询问或修改当前项目……'), { target: { value: '继续' } });
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    await waitFor(() => expect(projectAgentApi.chatStream).toHaveBeenCalledTimes(1));

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, {
      projectId: 'proj-1', taskId: 'plan-1', conversationId: 'conv-1', taskType: 'agent_plan',
      resources: [], task: { id: 'plan-1', status: 'completed' },
    });
    await new Promise(resolve => { setTimeout(resolve, 30); });
    expect(projectAgentApi.getConversation).not.toHaveBeenCalled();

    releaseStream();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalled());
  });
});
