import { Segmented, Tooltip } from 'antd';
import { BulbOutlined, MoonOutlined, DesktopOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useThemeMode } from '../theme/useThemeMode';
import type { ThemeMode } from '../theme/themeStorage';
import type { ReactNode } from 'react';

interface ThemeSwitchProps {
  size?: 'small' | 'middle' | 'large';
  block?: boolean;
}

export default function ThemeSwitch({ size = 'middle', block = false }: ThemeSwitchProps) {
  const { t } = useTranslation();
  const { mode, setMode } = useThemeMode();

  const options: Array<{ value: ThemeMode; label: ReactNode }> = [
    {
      value: 'light',
      label: (
        <Tooltip title={t('theme.light')}>
          <BulbOutlined />
        </Tooltip>
      ),
    },
    {
      value: 'dark',
      label: (
        <Tooltip title={t('theme.dark')}>
          <MoonOutlined />
        </Tooltip>
      ),
    },
    {
      value: 'system',
      label: (
        <Tooltip title={t('theme.system')}>
          <DesktopOutlined />
        </Tooltip>
      ),
    },
  ];

  return (
    <Segmented
      size={size}
      value={mode}
      onChange={(value) => setMode(value as ThemeMode)}
      options={options}
      block={block}
    />
  );
}
