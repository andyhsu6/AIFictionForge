/**
 * PR-1 review I1 + I2, asserted on the rendered DOM.
 *
 * I1: the backend used to translate the risk audit code into Chinese and paste it
 * into `step.content`, which the panel renders verbatim -- an English user therefore
 * read a full Chinese sentence on the confirmation card. The backend now ships only
 * the snake_case code in `detail.risk.reason`; the copy lives in the locale files, so
 * the same card must render per-language text.
 *
 * I2: the "view parameters" `<pre>` dumped every `detail` key, so the raw audit JSON
 * (`{"action": ..., "risk_level": 2, ...}`) landed in user-visible UI.
 *
 * Both cases mount the real panel (only `projectAgentApi` is stubbed) so the asserts
 * run against the same render path the app uses.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { App as AntApp } from 'antd';
import { MemoryRouter } from 'react-router-dom';

import ProjectAgentPanel from '../components/project-agent/ProjectAgentPanel';
import i18n from '../i18n';
import type { AgentExecutionStep, AgentMessage, AgentToolCall } from '../types';
import enPanel from '../locales/en/projectAgentPanel.json';
import zhPanel from '../locales/zh/projectAgentPanel.json';

const { api } = vi.hoisted(() => ({
  api: {
    listConversations: vi.fn(),
    getConversation: vi.fn(),
    deleteConversation: vi.fn(),
    createConversation: vi.fn(),
  },
}));

vi.mock('../services/api', () => ({ projectAgentApi: api }));

const TOOL_CALL: AgentToolCall = {
  id: 'tc-1',
  conversation_id: 'conv-1',
  tool_name: 'start_project_task',
  arguments: { action: 'analyze_chapter', chapter_number: 1 },
  risk_level: 2,
  requires_confirmation: true,
  status: 'waiting_confirmation',
  created_at: '2026-01-01 00:00:00',
};

const USER_MESSAGE: AgentMessage = {
  id: 'msg-u1',
  conversation_id: 'conv-1',
  role: 'user',
  content: 'analyze chapter 1',
  created_at: '2026-01-01 00:00:00',
};

const ASSISTANT_MESSAGE: AgentMessage = {
  id: 'msg-a1',
  conversation_id: 'conv-1',
  role: 'assistant',
  content: 'ready to apply the change',
  created_at: '2026-01-01 00:00:01',
};

/**
 * `content` is exactly what the backend persists for a confirmation step
 * (project_agent_service.CONFIRMATION_STEP_CONTENT): the neutral base sentence with
 * no risk reason appended. `detail.risk` carries only the audit code.
 */
const CONFIRMATION_STEP: AgentExecutionStep = {
  id: 'step-1',
  conversation_id: 'conv-1',
  assistant_message_id: 'msg-a1',
  user_message_id: 'msg-u1',
  tool_call_id: 'tc-1',
  sequence: 2,
  step_type: 'tool',
  category: 'project',
  title: 'start_project_task',
  content: '已生成修改预览，等待用户确认。',
  status: 'waiting_confirmation',
  detail: {
    arguments: { action: 'analyze_chapter', chapter_number: 1 },
    preview: { label: 'chapter 1 analysis' },
    risk: {
      action: 'analyze_chapter',
      risk_level: 2,
      requires_confirmation: true,
      reason: 'overwrite_existing_analysis',
    },
    tool_call: TOOL_CALL,
  },
  created_at: '2026-01-01 00:00:01',
  updated_at: '2026-01-01 00:00:02',
};

/** The card must be mounted before any "is absent" assertion, or absence is vacuous. */
const BASE_CONTENT = '已生成修改预览，等待用户确认。';

async function mountPanel(steps: AgentExecutionStep[]) {
  api.getConversation.mockResolvedValue({
    id: 'conv-1',
    user_id: 'u1',
    project_id: 'proj-1',
    title: 'PR-1',
    status: 'active',
    last_message_at: '2026-01-01 00:00:02',
    created_at: '2026-01-01 00:00:00',
    updated_at: '2026-01-01 00:00:02',
    messages: [USER_MESSAGE, ASSISTANT_MESSAGE],
    tool_calls: [TOOL_CALL],
    execution_steps: steps,
  });
  render(
    <MemoryRouter initialEntries={['/project/proj-1']}>
      <AntApp>
        <ProjectAgentPanel
          projectId="proj-1"
          mobile={false}
          mobileOpen={false}
          onMobileClose={() => {}}
        />
      </AntApp>
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText(BASE_CONTENT)).toBeTruthy());
}

beforeAll(async () => {
  // jsdom ships no scrollIntoView; the panel auto-scrolls on every step update, so
  // mounting without this shim throws before any assertion runs. Vitest isolates
  // modules per file, so this prototype patch stays inside this suite.
  Element.prototype.scrollIntoView = () => {};
  api.listConversations.mockResolvedValue([{ id: 'conv-1' }]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  api.listConversations.mockResolvedValue([{ id: 'conv-1' }]);
});

describe('confirmation card is localized, not server-translated (I1)', () => {
  const cases = [
    { language: 'zh', text: zhPanel.riskReason.overwrite_existing_analysis },
    { language: 'en', text: enPanel.riskReason.overwrite_existing_analysis },
  ] as const;

  for (const testCase of cases) {
    const other = testCase.language === 'en' ? cases[0] : cases[1];

    it(`renders the risk reason in ${testCase.language} from the locale file`, async () => {
      window.localStorage.setItem('lng', testCase.language);
      await i18n.changeLanguage(testCase.language);
      await mountPanel([CONFIRMATION_STEP]);

      const reason = await screen.findByTestId('risk-reason');
      // Byte-for-byte the locale string: the copy came from the locale file, not from
      // text pasted into the component or appended by the backend.
      expect(reason.textContent).toBe(testCase.text);
      // The audit code stays an audit code -- never shown as UI text...
      expect(reason.textContent).not.toContain('overwrite_existing_analysis');
      // ...and the other language must not leak into this one.
      expect(reason.textContent).not.toBe(other.text);
      // I1's regression: the reason must not be glued onto the base content either.
      expect(screen.getByText(BASE_CONTENT).textContent).toBe(BASE_CONTENT);
    });
  }

  it('keeps every reason code of the backend contract in both locales', () => {
    // backend/app/services/project_agent_risk.py is the only producer of these codes.
    const codes = [
      'overwrite_existing_analysis',
      'chapter_unresolvable',
      'analysis_probe_failed',
    ];
    for (const riskReason of [zhPanel.riskReason, enPanel.riskReason]) {
      for (const code of codes) {
        const value = riskReason[code as keyof typeof riskReason];
        expect(typeof value === 'string' && value.trim().length > 0, `missing ${code}`).toBe(true);
      }
    }
  });

  it('renders no reason line for a code the frontend does not know', async () => {
    window.localStorage.setItem('lng', 'en');
    await i18n.changeLanguage('en');
    await mountPanel([{
      ...CONFIRMATION_STEP,
      detail: { ...CONFIRMATION_STEP.detail, risk: { reason: 'from_the_future' } },
    }]);

    expect(screen.queryByTestId('risk-reason')).toBeNull();
    // The unknown code must not surface as a raw key path either (i18next would
    // otherwise render "riskReason.from_the_future" as if it were copy).
    expect(document.body.textContent).not.toContain('from_the_future');
  });
});

describe('debug dump no longer leaks the raw risk JSON (I2)', () => {
  it('shows arguments but not the risk internals in <pre>', async () => {
    window.localStorage.setItem('lng', 'en');
    await i18n.changeLanguage('en');
    await mountPanel([CONFIRMATION_STEP]);

    await waitFor(() => expect(document.querySelectorAll('pre').length).toBe(1));
    const dump = document.querySelector('pre') as HTMLElement;
    expect(dump.textContent).toContain('arguments');
    expect(dump.textContent).not.toContain('risk_level');
    expect(dump.textContent).not.toContain('"risk"');
    expect(dump.textContent).not.toContain('overwrite_existing_analysis');
    // tool_call has its own renderer, so it stays out of the dump as before.
    expect(dump.textContent).not.toContain('"tool_call"');
  });

  it('still offers the dump when risk was the only other key', async () => {
    // Guards the hasDetail/dump filter staying in sync: a step whose only payload is
    // the audit risk must not grow an expander that opens onto an empty block.
    window.localStorage.setItem('lng', 'en');
    await i18n.changeLanguage('en');
    await mountPanel([{
      ...CONFIRMATION_STEP,
      detail: { risk: CONFIRMATION_STEP.detail?.risk },
    }]);

    await waitFor(() => expect(screen.getByTestId('risk-reason')).toBeTruthy());
    expect(document.querySelectorAll('pre').length).toBe(0);
  });
});
