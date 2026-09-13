/**
 * PR-1（评审 F3）：`AgentTaskType` 联合类型的存在性契约。
 *
 * 为什么放在前端：这个联合类型最初由一条后端 pytest 用正则读
 * `frontend/src/types/index.ts` 做跨语言 parity——纯后端环境（Docker 镜像只 COPY
 * `backend/`）里没有 `../frontend`，那条用例会在这样的环境里直接失败，已删除。
 * 拆分后的双侧契约：
 *  - 本文件：前端侧钉「联合类型存在、非空、无重复成员、且真的被 onToolExecuted 用到」；
 *  - backend/tests/test_project_agent_inline_task.py
 *    ::test_agent_task_types_all_resolve_to_a_known_resource_mapping：
 *    后端侧钉「`AGENT_TASK_ACTION_TYPES` 的每个 value 都有资源映射」。
 * 值域逐字一致靠人工同步（改后端映射时同步 `src/types/index.ts`），不再跨语言读文件。
 *
 * 读源码而不是 import：类型在运行时被擦除，import 侧只能由 `tsc -b` 保证，
 * 断言联合类型「形状」必须看文本（与本仓 i18n-integrity 套件的源码断言同一手法）。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');
}

function agentTaskTypeMembers(source: string): { declared: boolean; members: string[] } {
  const declaration = source.match(/export type AgentTaskType =([^;]*);/);
  if (!declaration) return { declared: false, members: [] };
  return { declared: true, members: [...declaration[1].matchAll(/'([^']*)'/g)].map((member) => member[1]) };
}

describe('AgentTaskType union (PR-1 tool_executed contract)', () => {
  const parsed = agentTaskTypeMembers(readSource('./index.ts'));

  it('is declared in src/types/index.ts with a non-empty union', () => {
    expect(parsed.declared, 'src/types/index.ts 必须声明 export type AgentTaskType').toBe(true);
    expect(parsed.members.length).toBeGreaterThan(0);
  });

  it('has no duplicate members', () => {
    expect(new Set(parsed.members).size).toBe(parsed.members.length);
  });

  it('types the onToolExecuted payload instead of a loose string', () => {
    const apiSource = readSource('../services/api.ts');
    expect(apiSource).toMatch(/import type \{[\s\S]*?AgentTaskType[\s\S]*?\} from '\.\.\/types'/);
    expect(apiSource).toMatch(/task_type\?: AgentTaskType;/);
  });
});
