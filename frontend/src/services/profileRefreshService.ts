import api from './api';

export type RefreshMode = 'incremental' | 'full';
export type RefreshAction = 'add' | 'change' | 'preserved' | 'rejected' | 'same';

export interface RefreshEstimate {
  mode: RefreshMode;
  through_chapter: number;
  entities_total: number;
  candidates: number;
  no_mention: number;
  chunks: number;
  calls_min: number;
  calls_max: number;
}

export interface RefreshField {
  field: string;
  old: string | null;
  new: string | null;
  action: RefreshAction;
}

export interface RefreshEntity {
  id: string;
  name: string;
  kind: string;
  fingerprint: string;
  fields: RefreshField[];
  facts_delta: unknown;
  stats: Record<string, number>;
}

export interface RefreshPlan {
  plan_version: number;
  mode: RefreshMode;
  from_chapter: number;
  through_chapter: number;
  created_at: string;
  summary: {
    add: number;
    change: number;
    preserved: number;
    rejected: number;
    same: number;
    entities: number;
    no_mention: number;
    skipped_batches: number;
  };
  entities: RefreshEntity[];
}

export interface RefreshApplyResult {
  applied: number;
  fields_written: number;
  stale: Array<{ id: string; name: string }>;
}

export interface RefreshTaskResponse {
  id?: string;
  task_id?: string;
  task_type: string;
}

export const profileRefreshApi = {
  estimate: (projectId: string, mode: RefreshMode, fromChapter: number) =>
    api.get<unknown, RefreshEstimate>(`/${projectId}/characters/refresh-estimate`, {
      params: { mode, from_chapter: fromChapter },
    }),
  preview: (projectId: string, mode: RefreshMode, fromChapter: number) =>
    api.post<unknown, RefreshTaskResponse>(`/${projectId}/characters/refresh-preview`, {
      mode,
      from_chapter: fromChapter,
    }),
  apply: (projectId: string, previewTaskId: string) =>
    api.post<unknown, RefreshApplyResult>(`/${projectId}/characters/refresh-apply`, {
      preview_task_id: previewTaskId,
    }),
};
