// Falsifiable pair pinning the plain-text rule for tool messages:
//   * a plan summary (role=tool) must never reach MarkdownRenderer, no matter
//     what the server-authored text contains;
//   * an assistant message with the SAME payload must still render markdown,
//     which proves the negative assertion is about the tool path and not a
//     broken renderer / broken mock.
import { App as AntdApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { cleanup, render, waitFor } from '@testing-library/react';
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

// Neutral fixture only (AGENTS.md source-text rule): markdown + HTML payload,
// no real book text, no names.
const HOSTILE = '## heading\n**bold** text\n<img src="x" onerror="window.__pwned=1">';

const conversationWith = (role: 'tool' | 'assistant') => ({
  id: 'conv-1', user_id: 'u', project_id: 'proj-1', title: 't', status: 'active',
  last_message_at: '2026-09-13T00:00:00', created_at: '2026-09-13T00:00:00', updated_at: '2026-09-13T00:00:00',
  messages: role === 'tool'
    ? [{
        id: 'm-tool', conversation_id: 'conv-1', role: 'tool', tool_call_id: 'tc-plan',
        content: JSON.stringify({ tool: 'propose_plan', result: HOSTILE }),
        created_at: '2026-09-13T00:00:00',
      }]
    : [{
        id: 'm-assistant', conversation_id: 'conv-1', role: 'assistant', content: HOSTILE,
        created_at: '2026-09-13T00:00:00',
      }],
  tool_calls: role === 'tool' ? [{
    id: 'tc-plan', conversation_id: 'conv-1', tool_name: 'propose_plan',
    arguments: { objective: 'plan objective', steps: [{ id: 's1', tool: 'start_project_task', action: 'analyze_chapter', arguments: {} }] },
    risk_level: 2, requires_confirmation: true, status: 'executed',
    result: { entity_id: 'plan-1', task_type: 'agent_plan' }, created_at: '2026-09-13T00:00:00',
  }] : [],
  execution_steps: [],
});

const renderPanel = async () => {
  const view = render(<MemoryRouter><AntdApp><ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} /></AntdApp></MemoryRouter>);
  await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalled());
  return view;
};

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
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
});

describe('plan summary rendering', () => {
  it('renders a tool-role summary as text nodes only', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(conversationWith('tool') as never);
    const view = await renderPanel();

    const summary = await view.findByTestId('plan-summary-text');
    expect(summary.querySelector('h1, h2, h3, strong, em, img')).toBeNull();
    expect(summary.querySelector('p')).toBeNull();
    expect(summary.textContent).toContain('## heading');
    expect(summary.textContent).toContain('**bold**');
    expect(summary.textContent).toContain('<img src="x" onerror="window.__pwned=1">');
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // 纯文本路径 ⇒ 唯一的子节点类型是文本（可能有换行产生的多个文本节点）
    expect(Array.from(summary.childNodes).every(node => node.nodeType === 3)).toBe(true);
  });

  it('keeps the summary visible without a click', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(conversationWith('tool') as never);
    const view = await renderPanel();
    const summary = await view.findByTestId('plan-summary-text');
    expect(summary).toBeVisible();
  });

  it('still renders markdown for assistant messages (control for the assertion above)', async () => {
    vi.mocked(projectAgentApi.getConversation).mockResolvedValue(conversationWith('assistant') as never);
    const view = await renderPanel();
    await waitFor(() => expect(view.container.querySelector('h2, h1, h3')).toBeTruthy());
    expect(view.container.querySelector('strong')).toBeTruthy();
  });
});
