import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { App } from 'antd';
import React from 'react';
import { useChapterSync } from './hooks';
import { useStore } from './index';
import '../i18n';

function createSSEStream(events: Array<Record<string, unknown>>): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const evt of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(evt)}\n\n`));
      }
      controller.close();
    },
  });
}

const wrapper: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <App>{children}</App>
);

describe('useChapterSync.generateChapterContentStream', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    useStore.setState({
      currentProject: { id: 'proj-1', title: 'Test' } as never,
      chapters: [],
    });
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('rejects instead of swallowing SSE error events', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        createSSEStream([
          { type: 'progress', message: '开始生成章节...', progress: 0, status: 'processing' },
          {
            type: 'error',
            error: '第58章生成被截断（正文 0 字 / 目标 4000 字）',
            code: 500,
            error_code: 'task.failed',
          },
        ]),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
      )
    );

    const { result } = renderHook(() => useChapterSync(), { wrapper });

    await expect(
      result.current.generateChapterContentStream('ch-58', undefined, 1, 4000)
    ).rejects.toThrow();
  });

  it('rejects when stream closes with empty content and no result', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(
        createSSEStream([
          { type: 'progress', message: '开始生成章节...', progress: 0, status: 'processing' },
        ]),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
      )
    );

    const { result } = renderHook(() => useChapterSync(), { wrapper });

    await expect(
      result.current.generateChapterContentStream('ch-58', undefined, 1, 4000)
    ).rejects.toThrow();
  });

  it('resolves with content and analysis_task_id on valid stream', async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/generate-stream')) {
        return new Response(
          createSSEStream([
            { type: 'chunk', content: '第一章正文' },
            {
              type: 'result',
              data: { word_count: 5, analysis_task_id: 'task-1' },
            },
            { type: 'done' },
          ]),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
        );
      }
      return new Response(JSON.stringify({ total: 0, items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;

    const { result } = renderHook(() => useChapterSync(), { wrapper });
    const res = await result.current.generateChapterContentStream('ch-1', undefined, 1, 4000);

    expect(res.content).toBe('第一章正文');
    expect(res.analysis_task_id).toBe('task-1');
  });
});
