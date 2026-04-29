import { createContext } from 'react';
import type { ResolvedThemeMode } from './themeConfig';
import type { ThemeMode } from './themeStorage';

export interface ThemeContextValue {
  mode: ThemeMode;
  resolvedMode: ResolvedThemeMode;
  setMode: (mode: ThemeMode) => void;
}

export const ThemeModeContext = createContext<ThemeContextValue | undefined>(undefined);
