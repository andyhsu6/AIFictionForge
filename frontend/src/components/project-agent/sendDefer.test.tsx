// Behavioural guard for "refresh must be deferred while a turn streams".
// We drive the real send() path with a chatStream we resolve by hand, then fire
// a plan settlement mid-flight. The optimistic placeholder (two local messages)
// must survive until the stream ends; only then may the panel re-read the
// conversation and clobber it.
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectAgentPanel from './ProjectAgentPanel';
import { eventBus, EventNames } from '../../store/eventBus';
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
      cancelPlan: vi.fn(),
      chatStream: vi.fn(),
    },
  };
});

const baseConversation = {
  id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
  last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
  messages: [{ id: 'm-old', conversation_id: 'conv-1', role: 'assistant', content: 'older reply', created_at: '2026-09-13T00:00:00' }],
  tool_calls: [],
  execution_steps: [],
};

let resolveStream: () => void = () => {};

const settlePayload = {
  projectId: 'proj-1', taskId: 'plan-1', conversationId: 'conv-1', taskType: 'agent_plan',
  resources: ['chapters'], task: { id: 'plan-1', status: 'completed' },
};

beforeAll(() => {
  // jsdom ships no scrollIntoView; the panel auto-scrolls on every message/step
  // update (same shim as planSettleRefresh.test.tsx).
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(async () => {
  vi.clearAllMocks();
  vi.mocked(projectAgentApi.listConversations).mockResolvedValue([
    { ...baseConversation, messages: undefined, tool_calls: undefined, execution_steps: undefined } as never,
  ]);
  vi.mocked(projectAgentApi.getConversation).mockResolvedValue(baseConversation as never);
  vi.mocked(projectAgentApi.chatStream).mockImplementation(() => new Promise<void>(resolve => {
    resolveStream = resolve;
  }));
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
});

describe('send / settle interleaving', () => {
  it('does not reload the conversation while a turn is streaming', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    fireEvent.change(view.getByRole('textbox'), { target: { value: 'next request' } });
    fireEvent.click(view.getByTestId('composer-send'));
    await waitFor(() => expect(projectAgentApi.chatStream).toHaveBeenCalledTimes(1));

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, settlePayload);
    await new Promise(resolve => { setTimeout(resolve, 30); });
    // 乐观占位仍在：服务端快照只有一条 older reply，占位期间 DOM 里应看到两条消息文本
    expect(view.getByTestId('composer-send')).toBeDisabled();
    expect(projectAgentApi.getConversation).not.toHaveBeenCalled();

    resolveStream();
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    expect(view.queryByText('next request')).toBeNull(); // 快照已替换占位
  });

  it('reuses the same send callback so the settle subscription is not re-registered', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    const textbox = view.getByRole('textbox');
    fireEvent.change(textbox, { target: { value: 'a' } });
    fireEvent.change(textbox, { target: { value: 'ab' } });
    vi.mocked(projectAgentApi.getConversation).mockClear();

    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, settlePayload);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    expect(projectAgentApi.getConversation).toHaveBeenCalledWith('proj-1', 'conv-1');
  });

  it('keeps the plan association when switching projects', async () => {
    const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));
    vi.mocked(projectAgentApi.getConversation).mockClear();

    view.rerender(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-2" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
    await waitFor(() => expect(projectAgentApi.listConversations).toHaveBeenLastCalledWith('proj-2'));
    eventBus.emit(EventNames.BACKGROUND_TASK_SETTLED, settlePayload); // proj-1 的计划在新项目视图下完成
    await new Promise(resolve => { setTimeout(resolve, 30); });
    expect(projectAgentApi.getConversation).not.toHaveBeenCalledWith('proj-1', 'conv-1');
  });
});
