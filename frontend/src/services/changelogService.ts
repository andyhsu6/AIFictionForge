/**
 * 本地更新日志服务
 * 从后端 /api/changelog 读取本地 CHANGELOG.md 内容
 */

export interface LocalChangelogEntry {
  id: string;
  date: string;
  message: string;
}

export async function fetchChangelog(): Promise<LocalChangelogEntry[]> {
  const response = await fetch('/api/changelog', { cache: 'no-cache' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  return data.entries || [];
}

const CHANGELOG_SEEN_KEY = 'changelog_seen_in_session';

export function hasSeenChangelogInSession(): boolean {
  return sessionStorage.getItem(CHANGELOG_SEEN_KEY) === '1';
}

export function markChangelogSeenInSession(): void {
  sessionStorage.setItem(CHANGELOG_SEEN_KEY, '1');
}
