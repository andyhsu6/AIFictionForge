// Behavioural guard for issue #257: the Stop button must (a) abort the local
// stream and (b) call the server-side cancel endpoint. Aborting alone leaves the
// backend running when the disconnect never reaches ASGI (proxy / background tab),
// which is exactly the original 20-minute "cannot cancel" report.
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
      cancelTurn: vi.fn(),
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

type StreamCallbacks = {
  onConversation?: (data: { conversation_id: string; title: string }) => void;
  onCancelled?: (content: string) => void;
};

function renderPanel() {
  return render(
    <MemoryRouter>
      <AntdApp>
        <ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} />
      </AntdApp>
    </MemoryRouter>,
  );
}

async function startTurn(view: ReturnType<typeof renderPanel>) {
  await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalled());
  fireEvent.change(view.getByRole('textbox'), { target: { value: '帮我查一下资料' } });
  fireEvent.click(view.getByTestId('composer-send'));
  await waitFor(() => expect(projectAgentApi.chatStream).toHaveBeenCalledTimes(1));
}

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(async () => {
  vi.clearAllMocks();
  vi.mocked(projectAgentApi.listConversations).mockResolvedValue([
    { ...baseConversation, messages: undefined, tool_calls: undefined, execution_steps: undefined } as never,
  ]);
  vi.mocked(projectAgentApi.getConversation).mockResolvedValue(baseConversation as never);
  vi.mocked(projectAgentApi.cancelTurn).mockResolvedValue({
    conversation_id: 'conv-1', cancelled: true, settled: true, repaired_steps: 0, message: '已请求停止本次执行',
  });
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
});

describe('stop a running agent turn', () => {
  it('calls the server cancel endpoint and aborts the local stream', async () => {
    vi.mocked(projectAgentApi.chatStream).mockImplementation(
      (_projectId, _payload, callbacks, signal) => {
        (callbacks as StreamCallbacks).onConversation?.({ conversation_id: 'conv-1', title: 't' });
        return new Promise<void>((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
          });
        });
      },
    );

    const view = renderPanel();
    await startTurn(view);

    fireEvent.click(view.getByText('停止'));

    await waitFor(() => expect(projectAgentApi.cancelTurn).toHaveBeenCalledWith('proj-1', 'conv-1'));
    // 本地流已掐断：停止按钮随 sending=false 消失（说明 finally 已经跑过）。
    await waitFor(() => expect(view.queryByText('停止')).toBeNull());
    // 关键验收：abort + 取消接口落定后必须重读会话，用持久化状态替换本地占位，
    // 而不是把 running 旧行留在界面上继续转圈。
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(2));
    expect(view.getByText('older reply')).toBeTruthy();
  });

  it('re-syncs the conversation when the server has not settled the stop yet', async () => {
    vi.mocked(projectAgentApi.cancelTurn).mockResolvedValue({
      conversation_id: 'conv-1', cancelled: true, settled: false, repaired_steps: 0,
      message: '已请求停止本次执行',
    });
    vi.mocked(projectAgentApi.chatStream).mockImplementation(
      (_projectId, _payload, callbacks, signal) => {
        (callbacks as StreamCallbacks).onConversation?.({ conversation_id: 'conv-1', title: 't' });
        return new Promise<void>((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
          });
        });
      },
    );

    const view = renderPanel();
    await startTurn(view);

    fireEvent.click(view.getByText('停止'));

    // 第一次 reload（catch 路径）之后服务端可能仍在后台收尾；settled=false 必须
    // 触发一次延迟复查，否则会停在读到 running 旧行的那一帧。
    await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(2));
    await waitFor(
      () => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(3),
      { timeout: 4000 },
    );
  });

  it('shows the server-side stop reason when the stream reports cancellation', async () => {
    let streamCallbacks: StreamCallbacks | undefined;
    vi.mocked(projectAgentApi.chatStream).mockImplementation(
      (_projectId, _payload, callbacks) => {
        streamCallbacks = callbacks as StreamCallbacks;
        streamCallbacks.onConversation?.({ conversation_id: 'conv-1', title: 't' });
        return new Promise<void>(() => {});
      },
    );

    const view = renderPanel();
    await startTurn(view);

    const reason = '本次执行已超过 5 分钟上限，已自动停止。';
    streamCallbacks?.onCancelled?.(reason);

    await waitFor(() => expect(view.getByText(reason)).toBeTruthy());
  });
});
