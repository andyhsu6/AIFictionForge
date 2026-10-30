import { App as AntdApp } from 'antd';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RefreshDiffModal from './RefreshDiffModal';
import RefreshEstimateModal from './RefreshEstimateModal';
import { getTaskStatus } from '../services/backgroundTaskService';
import { profileRefreshApi } from '../services/profileRefreshService';
import { mapTaskStatusMessage } from '../services/errorMapper';
import i18n from '../i18n';

vi.mock('../services/profileRefreshService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/profileRefreshService')>();
  return {
    ...actual,
    profileRefreshApi: {
      estimate: vi.fn(),
      preview: vi.fn(),
      apply: vi.fn(),
    },
  };
});

vi.mock('../services/backgroundTaskService', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/backgroundTaskService')>();
  return { ...actual, getTaskStatus: vi.fn() };
});

vi.mock('../services/errorMapper', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/errorMapper')>();
  return { ...actual, mapTaskStatusMessage: vi.fn(() => 'Preview task failed with a useful reason') };
});

vi.mock('../store/hooks', () => ({
  useCharacterSync: () => ({ refreshCharacters: vi.fn() }),
}));

const plan = {
  plan_version: 1,
  mode: 'full' as const,
  from_chapter: 1,
  through_chapter: 12,
  created_at: '2026-10-30T00:00:00Z',
  summary: { add: 1, change: 1, preserved: 1, rejected: 1, same: 1, entities: 1, no_mention: 0, skipped_batches: 0 },
  entities: [{
    id: 'e1', name: 'Test Person', kind: 'character', fingerprint: 'fp',
    fields: [
      { field: 'background', old: 'Old background', new: 'New background', action: 'change' as const },
      { field: 'personality', old: 'Edited personality', new: 'Suggested personality', action: 'preserved' as const },
      { field: 'current_state', old: 'Same state', new: 'Same state', action: 'same' as const },
      { field: 'appearance', old: 'Kept appearance', new: '', action: 'preserved' as const },
    ],
    facts_delta: null,
    stats: { quote_not_found: 2 },
  }],
};

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage('zh');
});

afterEach(cleanup);

describe('profile refresh modals', () => {
  it('renders the diff summary, change row, and existing rejection reason labels', () => {
    render(<AntdApp><RefreshDiffModal open plan={plan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    expect(screen.getByText('新增 1')).toBeTruthy();
    expect(screen.getByText('Old background → New background')).toBeTruthy();
    expect(screen.getByText('Edited personality')).toBeTruthy();
    expect(screen.getByText('AI 建议：Suggested personality（未写入）')).toBeTruthy();
    expect(screen.getByText('Same state')).toBeTruthy();
    expect(screen.queryByText('Kept appearance → （空）')).toBeNull();
    fireEvent.click(screen.getByText('丢弃原因'));
    expect(screen.getByText('引文未命中 2')).toBeTruthy();
  });

  it('debounces estimate requests when changing mode and input values', async () => {
    vi.mocked(profileRefreshApi.estimate).mockResolvedValue({
      mode: 'incremental', through_chapter: 12, entities_total: 3, candidates: 2,
      no_mention: 1, chunks: 1, calls_min: 1, calls_max: 3,
    });
    render(<AntdApp><RefreshEstimateModal open projectId="project-1" onClose={vi.fn()} /></AntdApp>);
    expect(profileRefreshApi.estimate).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText('候选实体')).toBeTruthy());
    expect(screen.getByText('1–3')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('全量'));
    expect(profileRefreshApi.estimate).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(profileRefreshApi.estimate).toHaveBeenLastCalledWith('project-1', 'full', 1), { timeout: 1500 });
  });

  it('reads the plan from the detailed preview task result', async () => {
    vi.mocked(profileRefreshApi.estimate).mockResolvedValue({
      mode: 'incremental', through_chapter: 12, entities_total: 3, candidates: 2,
      no_mention: 1, chunks: 1, calls_min: 1, calls_max: 3,
    });
    vi.mocked(profileRefreshApi.preview).mockResolvedValue({ id: 'task-1', task_type: 'profile_preview' });
    vi.mocked(getTaskStatus).mockResolvedValue({
      id: 'task-1', task_type: 'profile_preview', project_id: 'project-1', status: 'completed',
      task_result: { plan }, progress: 100,
    } as never);
    render(<AntdApp><RefreshEstimateModal open projectId="project-1" onClose={vi.fn()} /></AntdApp>);
    await waitFor(() => expect(screen.getByText('候选实体')).toBeTruthy());
    fireEvent.click(await screen.findByText('生成差异预览'));
    expect(await screen.findByText('档案差异预览')).toBeTruthy();
    expect(screen.getByText('Old background → New background')).toBeTruthy();
  });

  it('shows batch progress from preview task details', async () => {
    vi.mocked(profileRefreshApi.estimate).mockResolvedValue({
      mode: 'incremental', through_chapter: 12, entities_total: 3, candidates: 2,
      no_mention: 1, chunks: 1, calls_min: 1, calls_max: 3,
    });
    vi.mocked(profileRefreshApi.preview).mockResolvedValue({ id: 'task-2', task_type: 'profile_preview' });
    vi.mocked(getTaskStatus).mockResolvedValue({
      id: 'task-2', task_type: 'profile_preview', project_id: 'project-1', status: 'running',
      task_result: null, progress: 40, progress_details: { stage: 'profile_previewing', batches_done: 2, batches_total: 5 },
    } as never);
    render(<AntdApp><RefreshEstimateModal open projectId="project-1" onClose={vi.fn()} /></AntdApp>);
    await waitFor(() => expect(screen.getByText('候选实体')).toBeTruthy());
    fireEvent.click(await screen.findByText('生成差异预览'));
    expect(await screen.findByText('第 2/5 批')).toBeTruthy();
    expect(getTaskStatus).toHaveBeenCalledWith('task-2');

  });

  it('uses the localized task detail message for a failed preview task', async () => {
    vi.mocked(profileRefreshApi.estimate).mockResolvedValue({
      mode: 'incremental', through_chapter: 12, entities_total: 3, candidates: 2,
      no_mention: 1, chunks: 1, calls_min: 1, calls_max: 3,
    });
    vi.mocked(profileRefreshApi.preview).mockResolvedValue({ id: 'task-3', task_type: 'profile_preview' });
    vi.mocked(getTaskStatus).mockResolvedValue({
      id: 'task-3', task_type: 'profile_preview', project_id: 'project-1', status: 'failed',
      task_result: null, progress: 40, status_message: 'failed',
    } as never);
    render(<AntdApp><RefreshEstimateModal open projectId="project-1" onClose={vi.fn()} /></AntdApp>);
    await waitFor(() => expect(screen.getByText('候选实体')).toBeTruthy());
    fireEvent.click(await screen.findByText('生成差异预览'));
    await waitFor(() => expect(mapTaskStatusMessage).toHaveBeenCalledWith(expect.objectContaining({ status_message: 'failed' })));
    expect(screen.getByText('Preview task failed with a useful reason')).toBeTruthy();
  });
});
