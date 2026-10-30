import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
const post = vi.fn();

vi.mock('./api', () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import { profileRefreshApi } from './profileRefreshService';

/**
 * 路径守卫：axios 实例的 baseURL 是 `/api`，而三个端点挂在 `/api/projects/{id}/characters/*` 下。
 * 之前写成 `/{id}/characters/*` 时，浏览器实际请求 `/api/{id}/...` → 404「API 路径不存在」，
 * 组件测试不会发现（全部 mock 掉了网络），只有在真浏览器里点一下才会暴露。
 */
describe('profileRefreshApi request paths', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it('targets /projects/<id>/characters/* for all three calls', () => {
    profileRefreshApi.estimate('p1', 'full', 3);
    expect(get).toHaveBeenCalledWith('/projects/p1/characters/refresh-estimate', {
      params: { mode: 'full', from_chapter: 3 },
    });

    profileRefreshApi.preview('p1', 'incremental', 1);
    expect(post).toHaveBeenCalledWith('/projects/p1/characters/refresh-preview', {
      mode: 'incremental',
      from_chapter: 1,
    });

    profileRefreshApi.apply('p1', 'task-1');
    expect(post).toHaveBeenCalledWith('/projects/p1/characters/refresh-apply', {
      preview_task_id: 'task-1',
    });
  });
});
