import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { PropsWithChildren } from 'react';
import { getThemeConfig, type ResolvedThemeMode } from './themeConfig';
import { ThemeModeContext } from './themeContext';
import { getStoredThemeMode, setStoredThemeMode, type ThemeMode } from './themeStorage';

const getSystemResolvedMode = (): ResolvedThemeMode => {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return 'light';
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
};

const getResolvedMode = (themeMode: ThemeMode, currentSystemMode: ResolvedThemeMode): ResolvedThemeMode => {
  return themeMode === 'system' ? currentSystemMode : themeMode;
};

const hexToRgba = (hexColor: string, alpha: number): string => {
  const hex = hexColor.replace('#', '').trim();

  if (/^[\da-fA-F]{3}$/.test(hex)) {
    const [r, g, b] = hex.split('');
    return `rgba(${parseInt(`${r}${r}`, 16)}, ${parseInt(`${g}${g}`, 16)}, ${parseInt(`${b}${b}`, 16)}, ${alpha})`;
  }

  if (/^[\da-fA-F]{6}$/.test(hex)) {
    return `rgba(${parseInt(hex.slice(0, 2), 16)}, ${parseInt(hex.slice(2, 4), 16)}, ${parseInt(hex.slice(4, 6), 16)}, ${alpha})`;
  }

  return `rgba(136, 77, 92, ${alpha})`;
};

export const ThemeProvider = ({ children }: PropsWithChildren) => {
  const [mode, setModeState] = useState<ThemeMode>(() => getStoredThemeMode());
  const [systemMode, setSystemMode] = useState<ResolvedThemeMode>(() => getSystemResolvedMode());
  const transitionCleanupRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return;
    }

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = (event: MediaQueryListEvent) => {
      setSystemMode(event.matches ? 'dark' : 'light');
    };

    setSystemMode(mediaQuery.matches ? 'dark' : 'light');

    if (typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleChange);
      return () => mediaQuery.removeEventListener('change', handleChange);
    }

    mediaQuery.addListener(handleChange);
    return () => mediaQuery.removeListener(handleChange);
  }, []);

  const resolvedMode: ResolvedThemeMode = getResolvedMode(mode, systemMode);
  const themeConfig = useMemo(() => getThemeConfig(resolvedMode), [resolvedMode]);

  const setMode = useCallback((nextMode: ThemeMode) => {
    if (nextMode === mode) {
      return;
    }

    const nextResolvedMode = getResolvedMode(nextMode, systemMode);
    const applyMode = () => {
      setModeState(nextMode);
      setStoredThemeMode(nextMode);
    };

    if (typeof document === 'undefined') {
      applyMode();
      return;
    }

    const root = document.documentElement;
    const docWithViewTransition = document as Document & {
      startViewTransition?: (callback: () => void) => { finished: Promise<void> };
    };

    if (!docWithViewTransition.startViewTransition || nextResolvedMode === resolvedMode) {
      applyMode();
      return;
    }

    root.setAttribute('data-theme-transition', nextResolvedMode === 'dark' ? 'to-dark' : 'to-light');

    try {
      docWithViewTransition.startViewTransition(() => {
        applyMode();
      }).finished.finally(() => {
        if (transitionCleanupRef.current !== null) {
          window.clearTimeout(transitionCleanupRef.current);
        }
        transitionCleanupRef.current = window.setTimeout(() => {
          root.removeAttribute('data-theme-transition');
          transitionCleanupRef.current = null;
        }, 50);
      });
    } catch {
      root.removeAttribute('data-theme-transition');
      applyMode();
    }
  }, [mode, resolvedMode, systemMode]);

  useLayoutEffect(() => {
    if (typeof document === 'undefined') {
      return;
    }

    const root = document.documentElement;
    root.setAttribute('data-theme-mode', mode);
    root.setAttribute('data-theme-resolved', resolvedMode);
    root.style.colorScheme = resolvedMode;

    const tooltipBg = themeConfig.token?.colorPrimary ?? '#884d5c';
    root.style.setProperty('--app-tooltip-bg', tooltipBg);
    root.style.setProperty('--app-tooltip-shadow', hexToRgba(tooltipBg, 0.3));
  }, [mode, resolvedMode, themeConfig]);

  useEffect(() => {
    return () => {
      if (transitionCleanupRef.current !== null) {
        window.clearTimeout(transitionCleanupRef.current);
      }
    };
  }, []);

  const contextValue = useMemo(
    () => ({
      mode,
      resolvedMode,
      setMode,
    }),
    [mode, resolvedMode, setMode],
  );

  return (
    <ThemeModeContext.Provider value={contextValue}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          ...themeConfig,
          cssVar: true,
        }}
      >
        {children}
      </ConfigProvider>
    </ThemeModeContext.Provider>
  );
};
