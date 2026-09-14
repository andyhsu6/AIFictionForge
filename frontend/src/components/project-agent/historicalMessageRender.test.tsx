// 永久转圈缺陷回归护栏：历史 assistant 消息 content=="" 且无工具调用时是终态，
// 不是本轮流式 —— 它必须不渲染 spinner、不渲染多余文案；只有工具调用的历史轮
// 必须渲染工具调用文案；唯一允许转圈的是当前发送中的乐观占位消息。
// 夹具一律中性占位，不含任何原文数据（AGENTS.md 脱敏硬约束）。
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
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
      cancelPlan: vi.fn(),
      chatStream: vi.fn(),
    },
  };
});

const TOOL_CALLS = JSON.stringify([
  { id: 'call-0', type: 'function', function: { name: 'read_project_overview', arguments: '{}' } },
  { id: 'call-1', type: 'function', function: { name: 'read_project_overview', arguments: '{}' } },
  { id: 'call-2', type: 'function', function: { name: 'read_project_overview', arguments: '{}' } },
  { id: 'call-3', type: 'function', function: { name: 'read_project_overview', arguments: '{}' } },
]);

const baseConversation = {
  id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
  last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
  messages: [
    {
      id: 'm-tools', conversation_id: 'conv-1', role: 'assistant', content: '',
      tool_calls: TOOL_CALLS, created_at: '2026-09-13T00:00:00',
    },
    {
      id: 'm-empty', conversation_id: 'conv-1', role: 'assistant', content: '',
      created_at: '2026-09-13T00:00:01',
    },
  ],
  tool_calls: [],
  execution_steps: [],
};

const toolLabel = () => i18n.t('preparingToolCalls', { ns: 'projectAgentPanel', count: 4 });

const renderPanel = async () => {
  const view = render(
    <MemoryRouter>
      <AntdApp>
        <ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} />
      </AntdApp>
    </MemoryRouter>,
  );
  await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalled());
  return view;
};

beforeAll(() => {
  // jsdom ships no scrollIntoView; the panel auto-scrolls on every message/step update.
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(async () => {
  vi.clearAllMocks();
  vi.mocked(projectAgentApi.listConversations).mockResolvedValue([
    { id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active', last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00' } as never,
  ]);
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
});

describe('historical assistant message rendering', () => {
  it('renders no spinner and no text for an empty historical message', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(baseConversation as never);
    const view = await renderPanel();

    // 工具轮落地 = 历史已渲染完成（离屏 loading 态已退出）。
    await view.findByText(toolLabel());
    const emptyMessage = view.getByTestId('agent-message-m-empty');
    expect(emptyMessage.querySelector('.ant-spin')).toBeNull();
    expect(emptyMessage.textContent).toBe('');
  });

  it('renders the tool-call label when tool_calls is present', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(baseConversation as never);
    const view = await renderPanel();

    const label = await view.findByText(toolLabel());
    expect(label.textContent).toBe(toolLabel());
    // 只有工具调用的历史轮不再是 loading 态。
    expect(view.container.querySelectorAll('.ant-spin').length).toBe(0);
  });

  it('keeps the spinner for the in-flight streaming turn', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue({
      ...baseConversation, messages: [], execution_steps: [],
    } as never);
    vi.mocked(projectAgentApi.chatStream).mockImplementation(() => new Promise<void>(() => {}));

    const view = await renderPanel();
    fireEvent.change(view.getByRole('textbox'), { target: { value: 'go' } });
    fireEvent.click(view.getByTestId('composer-send'));
    await waitFor(() => expect(projectAgentApi.chatStream).toHaveBeenCalledTimes(1));

    // 乐观用户消息已渲染 ⇒ 历史 loading 态已结束，剩下的 spin 只可能是本轮占位。
    await view.findByText('go');
    await waitFor(() => expect(view.container.querySelectorAll('.ant-spin')).toHaveLength(1));
  });
});
