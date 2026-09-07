/**
 * todo 26 (f) + (j): SSE / background-task error contract and the raw-field
 * display policy (task 14a).
 *
 * Contract under test (services/errorMapper.ts):
 *  - any payload WITH a code renders localized text only: `errors:<code>`
 *    template for known codes, localized generic text for unknown ones —
 *    the backend raw text/detail NEVER reaches the display string;
 *  - a payload with NO code is a legacy row: detail/status_message passes
 *    through verbatim (the only detail escape);
 *  - `raw`/`error_raw`/`message_raw` are diagnostics-only: they feed
 *    getErrorDiagnostic() (debug surfaces) but never the display string;
 *  - FloatingTaskPanel surfaces the diagnostic ONLY behind import.meta.env.DEV
 *    (asserted at unit level on the component source).
 *
 * Uses the real app i18n instance in EN (the node:test orphan errorMapper
 * suite covers the zh side; no duplication of its cases here beyond the
 * en-locale contract anchors).
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { beforeAll, afterEach, describe, expect, it, vi } from 'vitest';
import i18n from '../i18n';
import {
  getErrorDiagnostic,
  mapErrorPayload,
  mapSSEError,
  mapSSEProgressMessage,
  mapTaskStatusMessage,
} from '../services/errorMapper';
import { getProjectTasks, pollTaskUntilComplete } from '../services/backgroundTaskService';
import { resolveImportWarningText } from '../utils/importWarnings';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('mapErrorPayload (display policy anchors)', () => {
  it('known code + params -> localized template with interpolation', () => {
    expect(
      mapErrorPayload({
        code: 'conflict.chapter_order_exists',
        params: { order_index: 3 },
        detail: '原始后端 detail',
        status: 409,
      })
    ).toBe('Chapter 3 already exists and cannot be created again');
  });

  it('known code: backend detail is NOT interpolated (diagnostic only)', () => {
    expect(mapErrorPayload({ code: 'auth.unauthorized', detail: '未登录', status: 401 })).toBe('Not logged in');
  });

  it('unregistered code -> localized GENERIC text, detail never leaks', () => {
    const shown = mapErrorPayload({ code: 'weird.some_code', detail: '原始后端文案' });
    expect(shown).toBe('Unknown error');
    expect(shown).not.toContain('原始后端文案');
  });

  it('no code (legacy row) -> detail verbatim — the only detail escape', () => {
    expect(mapErrorPayload({ detail: 'legacy raw text' })).toBe('legacy raw text');
  });
});

describe('mapSSEError', () => {
  it('error_code + error_params -> localized template; error_raw ignored', () => {
    expect(
      mapSSEError({
        error: 'raw failure text',
        error_code: 'internal.generation_failed',
        error_params: { error: 'boom' },
        error_raw: 'raw failure text',
      })
    ).toBe('Generation failed: boom');
  });

  it('unregistered error_code -> generic text, raw never displayed', () => {
    const shown = mapSSEError({ error: 'raw failure text', error_code: 'weird.code' });
    expect(shown).toBe('Unknown error');
    expect(shown).not.toContain('raw failure text');
  });

  it('no error_code (legacy event) -> error text passthrough', () => {
    expect(mapSSEError({ error: 'legacy stream failure' })).toBe('legacy stream failure');
  });
});

describe('mapSSEProgressMessage', () => {
  it('message_code + params -> localized template', () => {
    expect(
      mapSSEProgressMessage({
        message: 'raw progress',
        message_code: 'task.batch_status_generating',
        message_params: { chapter_number: 2, current: 3, total: 5 },
      })
    ).toBe('Generating chapter 2 (3/5)');
  });

  it('unregistered message_code -> localized generic, raw never displayed', () => {
    const shown = mapSSEProgressMessage({ message: 'raw progress text', message_code: 'nope.nope' });
    expect(shown).toBe('Unknown error');
    expect(shown).not.toContain('raw progress text');
  });

  it('no message_code (legacy event) -> raw message passthrough', () => {
    expect(mapSSEProgressMessage({ message: 'legacy progress text' })).toBe('legacy progress text');
  });
});

describe('mapTaskStatusMessage', () => {
  it('status_code + params -> localized template', () => {
    expect(
      mapTaskStatusMessage({
        status_message: 'raw status',
        status_code: 'conflict.chapter_order_exists',
        status_params: { order_index: 7 },
      })
    ).toBe('Chapter 7 already exists and cannot be created again');
  });

  it('NULL status_code (legacy row) -> status_message verbatim', () => {
    expect(mapTaskStatusMessage({ status_message: '旧版任务消息', status_code: null })).toBe('旧版任务消息');
  });

  it('unregistered status_code -> generic; raw/error_message never displayed', () => {
    const shown = mapTaskStatusMessage({ status_message: 'raw status', status_code: 'weird.task_code' });
    expect(shown).toBe('Unknown error');
  });

  // issue #27: book-import polling rows now carry status_code/status_params
  // (backend BookImportTaskStatusResponse); these use the real import.task.*
  // locale entries.
  it('book-import polling row: import.task.* code -> localized template', () => {
    expect(
      mapTaskStatusMessage({
        status_message: '正在初始化AI服务...',
        status_code: 'import.task.initAiService',
        status_params: null,
      })
    ).toBe('Initializing AI service...');
  });

  it('book-import polling row: import.task.* code + params -> interpolated template', () => {
    expect(
      mapTaskStatusMessage({
        status_message: '已处理末5章 5/5 个章节结构...',
        status_code: 'import.task.chapterStructuresTail',
        status_params: { chapters: 5, index: 5, total: 5 },
      })
    ).toBe('Processed 5/5 chapter structures (last 5 chapters)...');
  });
});

describe('resolveImportWarningText (book-import preview warnings)', () => {
  // Runtime book-data params (chapter titles etc.) stay original in the
  // localized output — they are values, not keys.
  it('registered warning code + params -> errors-ns template with interpolation', () => {
    expect(
      resolveImportWarningText(i18n.t, {
        code: 'import.warning.duplicateTitles',
        message: '检测到重复章节标题「开端」（出现 2 次）',
        level: 'warning',
        params: { title: '开端', occurrences: 2 },
      })
    ).toBe('Duplicate chapter title "开端" detected 2 times');
  });

  it('registered warning code + multiple params -> all interpolated', () => {
    expect(
      resolveImportWarningText(i18n.t, {
        code: 'import.warning.filteredChaptersTail',
        message: '已按解析配置仅保留末5章 5 章用于导入（原始识别 8 章）',
        level: 'info',
        params: { kept: 5, detected: 8 },
      })
    ).toBe('Kept only the last 5 chapters for import per the parsing configuration (8 chapters originally detected)');
  });

  it('unregistered/unknown warning code -> raw backend message verbatim (defaultValue fallback)', () => {
    expect(
      resolveImportWarningText(i18n.t, {
        code: 'import.warning.notRegisteredAnywhere',
        message: '原始告警文案保持原样',
        level: 'warning',
        params: { title: 'ignored' },
      })
    ).toBe('原始告警文案保持原样');
  });
});

describe('backgroundTaskService mapping over real fetch payloads', () => {
  const TASK_BASE = { id: 't1', task_type: 'chapter_generation', project_id: 'p1', progress: 100, progress_details: null, task_result: null, retry_count: 0, cancel_requested: false, created_at: null, started_at: null, completed_at: null, updated_at: null, affected_resources: [], can_cancel: false, can_delete: false, error_message: null } as const;

  it('pollTaskUntilComplete failed row: onError gets the LOCALIZED status (raw not displayed)', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          ...TASK_BASE,
          status: 'failed',
          status_message: 'RAW backend diagnostic',
          status_code: 'conflict.chapter_order_exists',
          status_params: { order_index: 4 },
          error_message: 'RAW backend diagnostic',
        }),
        { status: 200 }
      )
    );
    vi.stubGlobal('fetch', fetchMock);

    const onError = vi.fn();
    pollTaskUntilComplete('t1', () => {}, () => {}, onError, 60_000);
    await vi.advanceTimersByTimeAsync(1);

    expect(onError).toHaveBeenCalledTimes(1);
    const [message] = onError.mock.calls[0] as [string, unknown];
    expect(message).toBe('Chapter 4 already exists and cannot be created again');
    expect(message).not.toContain('RAW backend diagnostic');
    vi.useRealTimers();
  });

  it('pollTaskUntilComplete legacy failed row (NULL code): status_message passthrough', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ ...TASK_BASE, status: 'failed', status_message: 'legacy failure text', status_code: null }), { status: 200 })
      )
    );
    const onError = vi.fn();
    pollTaskUntilComplete('t1', () => {}, () => {}, onError, 60_000);
    await vi.advanceTimersByTimeAsync(1);
    expect(onError).toHaveBeenCalledWith('legacy failure text', expect.anything());
    vi.useRealTimers();
  });

  it('failing task API call: error message = action prefix + mapped envelope (raw excluded)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: '原始错误', code: 'auth.unauthorized', params: null, raw: 'RAW diag' }), { status: 401 })
      )
    );
    await expect(getProjectTasks('p1')).rejects.toThrow('Failed to fetch task list: Not logged in');
    await expect(getProjectTasks('p1')).rejects.not.toThrow(/原始错误|RAW diag/);
  });
});

describe('todo 26 (j) raw contract', () => {
  it('getErrorDiagnostic prefers raw, falls back to detail, empty raw falls through', () => {
    expect(getErrorDiagnostic({ raw: 'RAW', detail: 'detail' })).toBe('RAW');
    expect(getErrorDiagnostic({ raw: '', detail: 'detail' })).toBe('detail');
    expect(getErrorDiagnostic({ raw: null, detail: null })).toBe('');
  });

  it('FloatingTaskPanel renders the diagnostic ONLY behind import.meta.env.DEV', () => {
    const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'components', 'FloatingTaskPanel.tsx'), 'utf8');
    expect(source).toMatch(/import\.meta\.env\.DEV\s*&&\s*diagnostic\s*&&\s*diagnostic\s*!==\s*display/);
  });
});
