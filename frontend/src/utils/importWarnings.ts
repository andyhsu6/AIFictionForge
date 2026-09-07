import type { BookImportWarning } from '../types';

/**
 * `never[]` params make any i18next TFunction structurally assignable (same
 * seam as services/errorMapper.ts); the dynamic key is cast at the single
 * call site below.
 */
type WarningTranslator = (...args: never[]) => unknown;

/**
 * 拆书预览告警文案解析（issue #27 Phase 3）。
 *
 * 展示策略与 errorMapper.mapErrorPayload 刻意不同——告警明细必须保留后端
 * 原文的可读性，因此不走"未知码渲染通用文案"的 mapErrorPayload 通道：
 * - 码已注册（errors ns 存在 `import.warning.*` 模板）→ 本地化模板 + params 插值；
 * - 码未注册/未知 → 原始后端 message 兜底（i18next defaultValue 机制）。
 *
 * 跨命名空间：bookImport 页面的 t 绑定 bookImport ns，告警码在 errors ns，
 * 由 options.ns 显式指向 errors（i18next 运行时支持，fixed t 亦生效）。
 */
export function resolveImportWarningText(
  t: WarningTranslator,
  warning: Pick<BookImportWarning, 'code' | 'message'> &
    Pick<Partial<BookImportWarning>, 'params'>,
): string {
  const translate = t as unknown as (
    key: string,
    options: Record<string, unknown>,
  ) => unknown;
  const translated = translate(warning.code, {
    ns: 'errors',
    ...(warning.params || {}),
    defaultValue: warning.message,
  });
  return String(translated);
}
