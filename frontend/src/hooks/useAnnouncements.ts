import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { announcementApi } from '../services/api';
import type { Announcement } from '../types';

const READ_IDS_KEY = 'mumu_announcements_read_ids';
const LAST_SYNC_KEY = 'mumu_announcements_last_sync_at';
const DEFAULT_SYNC_INTERVAL = 5 * 60 * 1000;

const loadReadIds = (): string[] => {
  try {
    const raw = localStorage.getItem(READ_IDS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
  } catch (error) {
    console.warn('读取公告已读缓存失败:', error);
    return [];
  }
};

const saveReadIds = (ids: string[]) => {
  try {
    localStorage.setItem(READ_IDS_KEY, JSON.stringify(Array.from(new Set(ids))));
  } catch (error) {
    console.warn('保存公告已读缓存失败:', error);
  }
};

const loadLastSyncAt = (): string | undefined => {
  try {
    return localStorage.getItem(LAST_SYNC_KEY) || undefined;
  } catch (error) {
    console.warn('读取公告同步缓存失败:', error);
    return undefined;
  }
};

const saveLastSyncAt = (value?: string | null) => {
  if (!value) return;
  try {
    localStorage.setItem(LAST_SYNC_KEY, value);
  } catch (error) {
    console.warn('保存公告同步缓存失败:', error);
  }
};

const sortAnnouncements = (items: Announcement[]) => {
  return [...items].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    const aTime = new Date(a.publish_at || a.created_at || a.updated_at || 0).getTime();
    const bTime = new Date(b.publish_at || b.created_at || b.updated_at || 0).getTime();
    return bTime - aTime;
  });
};

const mergeAnnouncements = (current: Announcement[], incoming: Announcement[]) => {
  const map = new Map<string, Announcement>();
  current.forEach(item => map.set(item.id, item));
  incoming.forEach(item => map.set(item.id, item));
  return sortAnnouncements(Array.from(map.values()));
};

export function useAnnouncements(syncInterval: number = DEFAULT_SYNC_INTERVAL) {
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [loading, setLoading] = useState(false);
  const [readIds, setReadIds] = useState<string[]>(() => loadReadIds());
  const lastSyncAtRef = useRef<string | undefined>(loadLastSyncAt());
  const syncingRef = useRef(false);

  const hasUnread = useMemo(
    () => announcements.some(item => !readIds.includes(item.id)),
    [announcements, readIds],
  );

  const refresh = useCallback(async (options?: { full?: boolean }) => {
    if (syncingRef.current) return;
    syncingRef.current = true;
    setLoading(true);

    try {
      const response = options?.full
        ? await announcementApi.list({ page: 1, limit: 50 })
        : await announcementApi.sync({ since: lastSyncAtRef.current, limit: 50 });

      const items = response.data?.items || [];
      setAnnouncements(prev => options?.full ? sortAnnouncements(items) : mergeAnnouncements(prev, items));

      const latest = response.data?.latest_updated_at || response.data?.server_time;
      if (latest) {
        lastSyncAtRef.current = latest;
        saveLastSyncAt(latest);
      }
    } catch (error) {
      console.warn('同步公告失败:', error);
    } finally {
      syncingRef.current = false;
      setLoading(false);
    }
  }, []);

  const markAllRead = useCallback(() => {
    const nextIds = Array.from(new Set([...readIds, ...announcements.map(item => item.id)]));
    setReadIds(nextIds);
    saveReadIds(nextIds);
  }, [announcements, readIds]);

  useEffect(() => {
    void refresh({ full: !lastSyncAtRef.current });

    const timer = window.setInterval(() => {
      void refresh();
    }, syncInterval);

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        void refresh();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [refresh, syncInterval]);

  return {
    announcements,
    loading,
    hasUnread,
    refresh,
    markAllRead,
  };
}
