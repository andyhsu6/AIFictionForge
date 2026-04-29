const SIDEBAR_COLLAPSED_STORAGE_KEY = 'mumu_sidebar_collapsed';

export const getStoredSidebarCollapsed = (): boolean => {
  if (typeof window === 'undefined') {
    return false;
  }

  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1';
  } catch (error) {
    console.warn('读取侧边栏状态失败:', error);
    return false;
  }
};

export const setStoredSidebarCollapsed = (collapsed: boolean): void => {
  if (typeof window === 'undefined') {
    return;
  }

  try {
    localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, collapsed ? '1' : '0');
  } catch (error) {
    console.warn('保存侧边栏状态失败:', error);
  }
};

export const getSidebarCollapsedStorageKey = (): string => SIDEBAR_COLLAPSED_STORAGE_KEY;
