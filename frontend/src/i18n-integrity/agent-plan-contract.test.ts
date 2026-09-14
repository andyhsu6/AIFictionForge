import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');
}

const panelEmitSource = readSource('../components/FloatingTaskPanel.tsx');

const panelSource = readSource('../components/project-agent/ProjectAgentPanel.tsx');

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
