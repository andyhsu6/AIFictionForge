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
      { field: 'background', old: 'Current background text', new: 'Discarded AI background text', action: 'rejected' as const },
    ],
    facts_delta: null,
    stats: { quote_not_found: 2 },
  }],
};

const selectablePlan = {
  plan_version: 1,
  mode: 'full' as const,
  from_chapter: 1,
  through_chapter: 12,
  created_at: '2026-10-30T00:00:00Z',
  summary: { add: 1, change: 2, preserved: 1, rejected: 0, same: 1, entities: 2, no_mention: 0, skipped_batches: 0 },
  entities: [
    {
      id: 'e1', name: 'Entity One', kind: 'character', fingerprint: 'fp1',
      fields: [
        { field: 'background', old: 'Old background', new: 'New background', action: 'change' as const },
        { field: 'personality', old: 'Old personality', new: 'New personality', action: 'change' as const },
        { field: 'appearance', old: 'Edited appearance', new: 'Suggested appearance', action: 'preserved' as const },
      ],
      facts_delta: null,
      stats: {},
    },
    {
      id: 'e2', name: 'Entity Two', kind: 'organization', fingerprint: 'fp2',
      fields: [
        { field: 'location', old: null, new: 'New location', action: 'add' as const },
        { field: 'motto', old: 'Same motto', new: 'Same motto', action: 'same' as const },
      ],
      facts_delta: null,
      stats: {},
    },
  ],
};

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage('zh');
});

afterEach(cleanup);

describe('profile refresh modals', () => {
  it('renders the diff summary, change summary, and existing rejection reason labels', () => {
    render(<AntdApp><RefreshDiffModal open plan={plan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    expect(screen.getByText('新增 1')).toBeTruthy();
    // 变更行折叠态只显示变化片段，不再拼「旧 → 新」整段
    expect(screen.getByText('− Old background')).toBeTruthy();
    expect(screen.getByText('＋ New background')).toBeTruthy();
    expect(screen.getByText('AI 建议（已丢弃）：Discarded AI background text')).toBeTruthy();
    expect(screen.getByText('Edited personality')).toBeTruthy();
    expect(screen.getByText('AI 建议：Suggested personality（未写入）')).toBeTruthy();
    expect(screen.getByText('Same state')).toBeTruthy();
    expect(screen.queryByText('Kept appearance → （空）')).toBeNull();
    // Pristine plans carry no skipped batches, so no warning banner appears.
    expect(screen.queryByText(/批未完成/)).toBeNull();
    fireEvent.click(screen.getByText('丢弃原因'));
    expect(screen.getByText('引文未命中 2')).toBeTruthy();
  });

  it('expands a change row into old/new blocks with full values', () => {
    render(<AntdApp><RefreshDiffModal open plan={plan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    fireEvent.click(screen.getByText('− Old background'));
    expect(screen.getByText('旧值')).toBeTruthy();
    expect(screen.getByText('新值')).toBeTruthy();
    expect(screen.getByText('Old background')).toBeTruthy();
    expect(screen.getByText('New background')).toBeTruthy();
  });

  it('summarizes wholesale rewrites by character counts and reveals full values on expand', () => {
    const rewritePlan = {
      ...plan,
      summary: { ...plan.summary, change: 1, rejected: 0 },
      entities: [{
        ...plan.entities[0],
        id: 'e2',
        fields: [{
          field: 'personality',
          old: '她自幼在温暖的家庭里长大，性格温和，喜欢读书，也很在意别人的评价。',
          new: '北境佣兵出身，沉默寡言，擅长追踪与潜行，几乎不信任任何人。',
          action: 'change' as const,
        }],
        stats: {},
      }],
    };
    render(<AntdApp><RefreshDiffModal open plan={rewritePlan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    expect(screen.getByText(/整段重写（旧 \d+ 字 → 新 \d+ 字）/)).toBeTruthy();
    fireEvent.click(screen.getByText(/整段重写/));
    expect(screen.getByText('旧值')).toBeTruthy();
    expect(screen.getByText('新值')).toBeTruthy();
    expect(screen.getByText(/她自幼在温暖的家庭里长大/)).toBeTruthy();
    expect(screen.getByText(/北境佣兵出身/)).toBeTruthy();
  });

  it('labels rejected rows as discarded AI suggestions instead of a change arrow', () => {
    render(<AntdApp><RefreshDiffModal open plan={plan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    fireEvent.click(screen.getByText('AI 建议（已丢弃）：Discarded AI background text'));
    expect(screen.getByText('当前值')).toBeTruthy();
    expect(screen.getByText('Current background text')).toBeTruthy();
    expect(screen.getByText('AI 建议（已丢弃）')).toBeTruthy();
  });

  it('warns about skipped batches with sorted chapter numbers and localized reason labels', () => {
    const planWithSkipped = {
      ...plan,
      summary: { ...plan.summary, skipped_batches: 2 },
      skipped: [
        { chapter_numbers: [5, 3], code: 'ai.timeout' },
        { chapter_numbers: [7], code: 'ai.unknown_code' },
      ],
    };
    render(<AntdApp><RefreshDiffModal open plan={planWithSkipped} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    expect(
      screen.getByText('有 2 批未完成（第 3、5、7 章）：模型网关超时、ai.unknown_code。差异仅覆盖其余章节，可重新预览补齐。'),
    ).toBeTruthy();
    expect(screen.getByText('新增 1')).toBeTruthy();
  });

  it('keeps older plans without a skipped field working (count-only fallback)', () => {
    const countOnlyPlan = { ...plan, summary: { ...plan.summary, skipped_batches: 1 } };
    render(<AntdApp><RefreshDiffModal open plan={countOnlyPlan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} /></AntdApp>);
    expect(
      screen.getByText('有 1 批未完成（第 — 章）：—。差异仅覆盖其余章节，可重新预览补齐。'),
    ).toBeTruthy();
    expect(screen.getByText('＋ New background')).toBeTruthy();
  });

  it('shows merged fact count only when the apply result includes merged facts', () => {
    const view = render(
      <AntdApp>
        <RefreshDiffModal
          open
          plan={plan}
          applying={false}
          result={{ applied: 2, fields_written: 3, stale: [], facts_merged: 2 }}
          onCancel={vi.fn()}
          onApply={vi.fn()}
        />
      </AntdApp>,
    );

    expect(screen.getByText('并合入 2 条档案事实')).toBeTruthy();

    view.rerender(
      <AntdApp>
        <RefreshDiffModal
          open
          plan={plan}
          applying={false}
          result={{ applied: 2, fields_written: 3, stale: [], facts_merged: 0 }}
          onCancel={vi.fn()}
          onApply={vi.fn()}
        />
      </AntdApp>,
    );

    expect(screen.queryByText(/并合入/)).toBeNull();
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
    expect(screen.getByText('＋ New background')).toBeTruthy();
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
    // antd message 是异步挂载的：CI 上比本地慢一拍，这里必须用 findByText 等它出现。
    expect(await screen.findByText('Preview task failed with a useful reason')).toBeTruthy();
  });
});

describe('profile refresh field selection', () => {
  it('defaults to every add/change field and only offers checkboxes for those rows', () => {
    const onApply = vi.fn();
    render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={onApply} />
      </AntdApp>,
    );

    expect(screen.getByLabelText('选择 Entity One 的全部字段')).toBeTruthy();
    expect(screen.getByLabelText('选择 背景')).toBeTruthy();
    expect(screen.getByLabelText('选择 性格')).toBeTruthy();
    expect(screen.getByLabelText('选择 所在地')).toBeTruthy();
    // preserved / same 行不可应用，也不出现勾选框。
    expect(screen.queryByLabelText('选择 外貌')).toBeNull();
    expect(screen.queryByLabelText('选择 口号')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '应用选中的 3 项' }));
    expect(onApply).toHaveBeenCalledWith([
      { entity_id: 'e1', fields: ['background', 'personality'] },
      { entity_id: 'e2', fields: ['location'] },
    ]);
  });

  it('only applies the remaining fields after unchecking one field row', () => {
    const onApply = vi.fn();
    render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={onApply} />
      </AntdApp>,
    );

    fireEvent.click(screen.getByLabelText('选择 性格'));
    fireEvent.click(screen.getByRole('button', { name: '应用选中的 2 项' }));
    expect(onApply).toHaveBeenCalledWith([
      { entity_id: 'e1', fields: ['background'] },
      { entity_id: 'e2', fields: ['location'] },
    ]);
  });

  it('keeps the row collapsed when clicking its checkbox', () => {
    render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} />
      </AntdApp>,
    );

    fireEvent.click(screen.getByLabelText('选择 背景'));
    expect(screen.queryByText('旧值')).toBeNull();
    expect(screen.queryByText('新值')).toBeNull();
  });

  it('uses a three-state entity checkbox to select and clear a whole entity', () => {
    render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} />
      </AntdApp>,
    );

    const entityCheckbox = screen.getByLabelText('选择 Entity One 的全部字段');
    fireEvent.click(screen.getByLabelText('选择 性格'));
    expect(entityCheckbox.closest('.ant-checkbox')?.className).toContain('ant-checkbox-indeterminate');

    fireEvent.click(entityCheckbox);
    expect(screen.getByRole('button', { name: '应用选中的 3 项' })).toBeTruthy();

    fireEvent.click(entityCheckbox);
    expect(screen.getByRole('button', { name: '应用选中的 1 项' })).toBeTruthy();
  });

  it('disables apply and never calls onApply when every field is unchecked', () => {
    const onApply = vi.fn();
    render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={onApply} />
      </AntdApp>,
    );

    fireEvent.click(screen.getByLabelText('选择 Entity One 的全部字段'));
    fireEvent.click(screen.getByLabelText('选择 Entity Two 的全部字段'));

    const applyButton = screen.getByRole('button', { name: '应用选中的 0 项' }) as HTMLButtonElement;
    expect(applyButton.disabled).toBe(true);
    fireEvent.click(applyButton);
    expect(onApply).not.toHaveBeenCalled();
  });

  it('resets to select-all when a new plan arrives', () => {
    const view = render(
      <AntdApp>
        <RefreshDiffModal open plan={selectablePlan} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} />
      </AntdApp>,
    );

    fireEvent.click(screen.getByLabelText('选择 性格'));
    expect(screen.getByRole('button', { name: '应用选中的 2 项' })).toBeTruthy();

    view.rerender(
      <AntdApp>
        <RefreshDiffModal open plan={{ ...selectablePlan }} applying={false} result={null} onCancel={vi.fn()} onApply={vi.fn()} />
      </AntdApp>,
    );
    expect(screen.getByRole('button', { name: '应用选中的 3 项' })).toBeTruthy();
  });

  it('passes the selected fields from the estimate modal to the apply API', async () => {
    vi.mocked(profileRefreshApi.estimate).mockResolvedValue({
      mode: 'incremental', through_chapter: 12, entities_total: 3, candidates: 2,
      no_mention: 1, chunks: 1, calls_min: 1, calls_max: 3,
    });
    vi.mocked(profileRefreshApi.preview).mockResolvedValue({ id: 'task-4', task_type: 'profile_preview' });
    vi.mocked(getTaskStatus).mockResolvedValue({
      id: 'task-4', task_type: 'profile_preview', project_id: 'project-1', status: 'completed',
      task_result: { plan }, progress: 100,
    } as never);
    vi.mocked(profileRefreshApi.apply).mockResolvedValue({ applied: 1, fields_written: 1, stale: [], facts_merged: 0 });

    render(<AntdApp><RefreshEstimateModal open projectId="project-1" onClose={vi.fn()} /></AntdApp>);
    await waitFor(() => expect(screen.getByText('候选实体')).toBeTruthy());
    fireEvent.click(await screen.findByText('生成差异预览'));
    expect(await screen.findByText('档案差异预览')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '应用选中的 1 项' }));
    await waitFor(() => expect(profileRefreshApi.apply).toHaveBeenCalledWith(
      'project-1',
      'task-4',
      [{ entity_id: 'e1', fields: ['background'] }],
    ));
  });
});
