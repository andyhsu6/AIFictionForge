/**
 * todo 26 (g): antd + dayjs locale switching.
 *
 * ThemeProvider maps i18next resolvedLanguage -> antd ConfigProvider locale
 * pack (zh -> zh_CN, en -> en_US) and -> the dayjs global locale (zh -> zh-cn,
 * en -> en). The stable observable seam is antd's DatePicker placeholder,
 * which ConfigProvider derives from the active locale pack, plus
 * dayjs.locale() itself.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import dayjs from 'dayjs';
import { DatePicker } from 'antd';
import i18n from '../i18n';
import { ThemeProvider } from '../theme/ThemeProvider';

const renderWithinTheme = () =>
  render(
    <ThemeProvider>
      <DatePicker data-testid="dp" />
    </ThemeProvider>
  );

afterEach(() => {
  cleanup();
});

describe('ThemeProvider locale switching (todo 26g)', () => {
  it('en -> antd en_US (DatePicker placeholder) + dayjs locale "en"', async () => {
    await i18n.changeLanguage('en');
    renderWithinTheme();
    await waitFor(() => expect(screen.getByTestId('dp')).toBeInTheDocument());
    expect(await screen.findByPlaceholderText('Select date')).toBeInTheDocument();
    await waitFor(() => expect(dayjs.locale()).toBe('en'));
  });

  it('zh -> antd zh_CN (DatePicker placeholder) + dayjs locale "zh-cn"', async () => {
    await i18n.changeLanguage('zh');
    renderWithinTheme();
    await waitFor(() => expect(screen.getByTestId('dp')).toBeInTheDocument());
    expect(await screen.findByPlaceholderText('请选择日期')).toBeInTheDocument();
    await waitFor(() => expect(dayjs.locale()).toBe('zh-cn'));
    // Switching back must flip the mapping again (dynamic sync, not mount-once).
    await i18n.changeLanguage('en');
    expect(await screen.findByPlaceholderText('Select date')).toBeInTheDocument();
    await waitFor(() => expect(dayjs.locale()).toBe('en'));
  });
});
