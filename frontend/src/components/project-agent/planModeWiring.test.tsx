// Behavioural guard for the plan-mode wiring: the panel must send the user's
// choice to the backend, otherwise the model is never offered propose_plan even
// when the switch is on — or is forced into a plan when it is off. The panel
// drives the REAL chatStream, so the assertion is on the actual HTTP request
// body, not on a source substring.
import { App as AntdApp } from 'antd';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import ProjectAgentPanel from './ProjectAgentPanel';
import { projectAgentApi } from '../../services/api';
import i18n from '../../i18n';

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api')>();
  return {
    ...actual,
    projectAgentApi: {
      ...actual.projectAgentApi,
      listConversations: vi.fn(() => Promise.resolve([])),
      createConversation: vi.fn(),
      getConversation: vi.fn(),
      deleteConversation: vi.fn(),
      confirmToolCall: vi.fn(),
      rejectToolCall: vi.fn(),
      approvePlan: vi.fn(),
      cancelPlan: vi.fn(),
    },
  };
});

const conversation = {
  id: 'conv-1',
  user_id: 'u',
  project_id: 'proj-1',
  title: 't',
  status: 'active',
  last_message_at: '2026-09-13T00:00:00',
  created_at: '2026-09-13T00:00:00',
  updated_at: '2026-09-13T00:00:00',
  messages: [],
  tool_calls: [],
  execution_steps: [],
};

async function sendOneTurn(): Promise<Record<string, unknown>> {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    body: { getReader: () => ({ read: async () => ({ done: true, value: undefined }) }) },
  } as unknown as Response);
  vi.stubGlobal('fetch', fetchMock);

  const view = render(
    <MemoryRouter>
      <AntdApp>
        <ProjectAgentPanel projectId="proj-1" mobile={false} mobileOpen={false} onMobileClose={() => {}} />
      </AntdApp>
    </MemoryRouter>,
  );
  await waitFor(() => expect(projectAgentApi.getConversation).toHaveBeenCalledTimes(1));

  fireEvent.change(view.getByRole('textbox'), { target: { value: 'plan a three-step task' } });
  fireEvent.click(view.getByTestId('composer-send'));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(async () => {
  localStorage.clear();
  vi.mocked(projectAgentApi.listConversations).mockResolvedValue([conversation] as never);
  vi.mocked(projectAgentApi.getConversation).mockResolvedValue(conversation as never);
  await i18n.changeLanguage('zh');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('agent turn plan mode wiring', () => {
  it('defaults to plan_mode: false so an opinion request is not turned into a plan', async () => {
    const body = await sendOneTurn();
    expect(body.plan_mode).toBe(false);
    expect(body.message).toBe('plan a three-step task');
  });

  it('sends plan_mode: true when the user has switched it on', async () => {
    localStorage.setItem('project-agent-plan-mode', 'true');
    const body = await sendOneTurn();
    expect(body.plan_mode).toBe(true);
  });
});
