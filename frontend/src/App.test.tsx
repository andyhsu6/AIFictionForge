// App smoke test: render the real <App /> under jsdom and prove the i18n
// language switch works end to end.
//
// Network policy: the app must be deterministic with zero network access, so
// the whole services/api layer is mocked. The /login route is chosen because
// it renders without ProtectedRoute's session machinery; Login itself probes
// auth on mount (getCurrentUser -> getAuthConfig) and both mocks resolve the
// "unauthenticated + local auth enabled" path, which leaves the login form
// rendered with no backend behind it.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';
import { ThemeProvider } from './theme/ThemeProvider';
import i18n from './i18n';

vi.mock('./services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./services/api')>();
  return {
    ...actual,
    authApi: {
      ...actual.authApi,
      getCurrentUser: vi.fn(() => Promise.reject(new Error('offline in tests'))),
      getAuthConfig: vi.fn(() =>
        Promise.resolve({
          local_auth_enabled: true,
          linuxdo_enabled: false,
          email_auth_enabled: false,
          email_register_enabled: false,
        })
      ),
    },
  };
});

describe('App (smoke)', () => {
  // Composition root mirrors main.tsx: <ThemeProvider><App /></ThemeProvider>.
  // ThemeSwitch (rendered on the login page) needs the ThemeModeContext.
  const renderApp = () =>
    render(
      <ThemeProvider>
        <App />
      </ThemeProvider>
    );

  beforeEach(() => {
    // LanguageDetector reads/caches `lng` in localStorage — start every test
    // from a clean slate so language state is deterministic.
    window.localStorage.clear();
    window.history.pushState({}, '', '/login');
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders the /login route without crashing', async () => {
    renderApp();

    // AppFooter always renders these static strings on /login
    expect(await screen.findByText('AIFictionForge')).toBeInTheDocument();
    expect(screen.getByText('GPL v3.0')).toBeInTheDocument();

    // Login falls back to the mocked auth config and renders the local
    // login form (username + password inputs)
    await waitFor(() => {
      expect(document.querySelector('input[type="password"]')).not.toBeNull();
    });
  });

  it('switches language via i18n and flips known UI strings', async () => {
    renderApp();

    await act(async () => {
      await i18n.changeLanguage('en');
    });
    expect(i18n.language).toBe('en');
    // common:back is a real, fully translated key pair (zh: 返回 / en: Back)
    expect(i18n.t('common:back')).toBe('Back');

    await act(async () => {
      await i18n.changeLanguage('zh');
    });
    expect(i18n.language).toBe('zh');
    expect(i18n.t('common:back')).toBe('返回');
  });
});
