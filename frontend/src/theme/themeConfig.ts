import type { ThemeConfig } from 'antd';
import { theme } from 'antd';
import type { ThemeMode } from './themeStorage';

export type ResolvedThemeMode = Exclude<ThemeMode, 'system'>;

const sharedToken: ThemeConfig['token'] = {
  colorPrimary: '#4D8088',
  borderRadius: 8,
  wireframe: false,
  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif",
};

const sharedComponents: ThemeConfig['components'] = {
  Button: {
    borderRadius: 8,
    controlHeight: 36,
  },
  Card: {
    borderRadiusLG: 12,
  },
  Tooltip: {
    colorBgSpotlight: sharedToken.colorPrimary,
  },
};

const lightThemeConfig: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    ...sharedToken,
    colorBgBase: '#F8F6F1',
    colorTextBase: '#2B2B2B',
    colorBgLayout: '#F8F6F1',
    colorBgContainer: '#FFFFFF',
  },
  components: {
    ...sharedComponents,
    Layout: {
      bodyBg: '#F8F6F1',
      headerBg: '#FFFFFF',
      siderBg: '#FFFFFF',
    },
  },
};

const darkThemeConfig: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    ...sharedToken,
    colorBgBase: '#141414',
    colorTextBase: '#f5f5f5',
  },
  components: {
    ...sharedComponents,
    Layout: {
      bodyBg: '#0f1115',
      headerBg: '#141414',
      siderBg: '#141414',
    },
  },
};

export const getThemeConfig = (mode: ResolvedThemeMode): ThemeConfig => {
  return mode === 'dark' ? darkThemeConfig : lightThemeConfig;
};
