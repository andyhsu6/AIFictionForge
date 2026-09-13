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
 * 值域逐字一致仍靠人工同步（改后端映射时同步 `src/types/index.ts`），不跨语言读文件；
 * 但同步不再"无防护"：两侧各存一份同一清单的字面量用例（I6），改一侧忘另一侧就会红。
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

/**
 * 值域清单（评审 I6）：后端 `app/services/task_resources.py` 的
 * `AGENT_TASK_ACTION_TYPES` 的 value 集合，逐个写成字面量。
 *
 * 为什么光有上面三条"存在性"断言不够：把联合类型的 11 个成员**全部改名**，
 * `tsc -b` 仍然 exit 0、本文件旧用例仍然全绿 —— `api.ts` 只引用类型名
 * （`task_type?: AgentTaskType`），从不引用成员，所以联合内部怎么改都编译通过。
 * 能拦住的只有"集合相等"。后端 `test_agent_task_types_all_resolve_to_a_known_resource_mapping`
 * 里另有同一份字面量清单：后端 pytest 不读前端文件（纯后端环境只装 backend/，
 * 评审 F3 已裁定），所以清单在两侧各存一份，改一侧必须同步另一侧。
 */
const EXPECTED_MEMBERS = [
  'outline_new',
  'outline_expand',
  'outline_batch_expand',
  'chapter_generate',
  'chapter_batch',
  'chapter_analysis',
  'chapter_regenerate',
  'chapter_partial_regenerate',
  'character_generate',
  'organization_generate',
  'career_generate',
] as const;

describe('AgentTaskType union (PR-1 tool_executed contract)', () => {
  const parsed = agentTaskTypeMembers(readSource('./index.ts'));

  it('pins the value domain literal-for-literal (I6)', () => {
    // 清单自己先自证非空且无重复：否则下面的集合相等会被一份空清单"通过"。
    expect(EXPECTED_MEMBERS.length).toBe(11);
    expect(new Set(EXPECTED_MEMBERS).size).toBe(EXPECTED_MEMBERS.length);
    expect(new Set(parsed.members)).toEqual(new Set<string>(EXPECTED_MEMBERS));
  });

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
