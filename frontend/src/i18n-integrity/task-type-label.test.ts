/**
 * 任务类型标签守卫（PR-2a Task 7）。
 *
 * 防的具体回归：`getTaskTypeLabel` 的 `default: return taskType` 会把后端原始的
 * `task_type` 字符串（如 `agent_plan`）直接画给用户 —— 未登录、切到 en、或后端
 * 新增一个 task_type 时都会发生，且**不会**让 `tsc -b` 变红（switch 对 string 输入
 * 永远类型合法），vitest 里也没有任何用例碰过这条分支。
 *
 * 因此这里读源码而不是渲染组件：jsdom 渲染要走到那条分支才有意义，而面板只在
 * store 里有任务时才挂载；源码断言与本仓 `types/agent-task-type.test.ts`、
 * i18n-integrity 其余套件同一手法。
 *
 * 值域清单为前端侧字面量（I6 约定）：后端 pytest 不读 `../frontend`（纯后端环境
 * 只装 `backend/`），这里同样不读 `../backend`，两侧各存一份字面量、改一侧即红。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function readSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');
}

/** `task_resources.py` 的 `TASK_TYPE_RESOURCES` 全部键（字面量，含 agent_plan）。 */
const EXPECTED_TASK_TYPES = [
  'outline_new',
  'outline_continue',
  'outline_expand',
  'outline_batch_expand',
  'chapter_generate',
  'chapter_batch',
  'chapter_regenerate',
  'chapter_partial_regenerate',
  'chapter_analysis',
  'character_generate',
  'organization_generate',
  'career_generate',
  'wizard',
  'agent_plan',
] as const;

/** 从 getTaskTypeLabel 的函数体里抠出 case 标签与它 return 的 t() 键。 */
function labelSwitchCases(source: string): Map<string, string> {
  const body = source.match(/const getTaskTypeLabel = [\s\S]*?\n  \};/);
  if (!body) throw new Error('FloatingTaskPanel.tsx 里找不到 getTaskTypeLabel');
  const cases = new Map<string, string>();
  for (const one of body[0].matchAll(/case '([^']+)':\s*return t\('([^']+)'\)/g)) {
    cases.set(one[1], one[2]);
  }
  return cases;
}

function locale(namespace: string): Record<string, string> {
  return JSON.parse(readSource(`../locales/${namespace}/floatingTaskPanel.json`));
}

describe('FloatingTaskPanel task type labels', () => {
  const source = readSource('../components/FloatingTaskPanel.tsx');
  const cases = labelSwitchCases(source);
  const zh = locale('zh');
  const en = locale('en');

  it('pins the task type value domain literal-for-literal', () => {
    expect(EXPECTED_TASK_TYPES.length).toBe(14);
    expect(new Set(EXPECTED_TASK_TYPES).size).toBe(EXPECTED_TASK_TYPES.length);
  });

  it('has an explicit label branch for every task type (no raw task_type leak)', () => {
    const missing = EXPECTED_TASK_TYPES.filter((taskType) => !cases.has(taskType));
    expect(missing, `这些 task_type 会掉进 default 分支、把原始字符串画给用户: ${missing.join(', ')}`).toEqual([]);
  });

  it('localizes agent_plan in both locales with real copy', () => {
    const key = cases.get('agent_plan');
    expect(key, 'agent_plan 分支必须 return 一个 t() 键').toBeTruthy();
    // en 里夹中文、或两侧都塞同一个中文串，parity 门不一定拦得住，这里自己拦。
    expect(zh[key as string]).toBe('创作计划');
    const enValue = en[key as string];
    expect(enValue, 'en 留空会挂 CI parity 门').toBeTruthy();
    expect(enValue).not.toMatch(/[㐀-鿿]/);
    expect(enValue).not.toContain(key as string);
  });

  it('every label key used by the switch exists in zh and en', () => {
    for (const [taskType, key] of cases) {
      expect(zh[key], `${taskType} 的 zh 键 ${key} 缺失`).toBeTruthy();
      expect(en[key], `${taskType} 的 en 键 ${key} 缺失`).toBeTruthy();
    }
  });
});
