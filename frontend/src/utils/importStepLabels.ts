import type { BookImportStepFailure } from '../types';

/**
 * `never[]` params make any i18next TFunction structurally assignable (same
 * seam as services/errorMapper.ts / utils/importWarnings.ts); the dynamic key
 * is cast at the single call site below.
 */
type StepLabelTranslator = (...args: never[]) => unknown;

/**
 * 拆书失败步骤标签本地化（issue #33 Category 1）。
 *
 * 后端 `step_name` 是稳定英文键（world_building / career_system / characters /
 * relationship_extraction），`step_label` 仍随包下发中文原文——仅作未知步骤键的
 * 兜底，绝不作为 en 显示文案。已知步骤走 bookImport ns 的 `progress.stepLabel.*`
 * 模板；未知/新增步骤回退 `step_label`（保证不显示空白）。
 */
const STEP_LABEL_KEYS: Record<string, string> = {
  world_building: 'progress.stepLabel.world_building',
  career_system: 'progress.stepLabel.career_system',
  characters: 'progress.stepLabel.characters',
  relationship_extraction: 'progress.stepLabel.relationship_extraction',
};

export function resolveStepLabel(
  t: StepLabelTranslator,
  failure: Pick<BookImportStepFailure, 'step_name' | 'step_label'>,
): string {
  const key = STEP_LABEL_KEYS[failure.step_name];
  if (!key) return failure.step_label;
  const translate = t as unknown as (
    key: string,
    options: Record<string, unknown>,
  ) => unknown;
  return String(translate(key, { ns: 'bookImport', defaultValue: failure.step_label }));
}
