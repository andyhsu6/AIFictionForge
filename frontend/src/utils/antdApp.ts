/**
 * antd App singleton bridge.
 *
 * antd v5 recommends context-aware `App.useApp()` modal/message/notification
 * over the static APIs, which ignore ConfigProvider theme/locale.
 * React components use `App.useApp()` directly; this module serves NON-component
 * call sites (services/, utils/) that cannot use hooks. The real instance is
 * injected once from the React tree (AntdAppBridge in ThemeProvider).
 */
import { App } from 'antd';

type AppApi = ReturnType<typeof App.useApp>;

let appApi: AppApi | null = null;

/** Injected from the React side after mount. */
export const setAntdApp = (api: AppApi): void => {
  appApi = api;
};

const createProxy = <K extends keyof AppApi>(key: K): AppApi[K] =>
  new Proxy({} as AppApi[K], {
    get(_target, prop) {
      if (!appApi) {
        // Pre-injection call (should not happen: bridge mounts before user interaction).
        // eslint-disable-next-line no-console
        console.warn(`[antdApp] App not ready - ${String(key)}.${String(prop)} ignored`);
        return () => undefined;
      }
      const value = (appApi[key] as unknown as Record<string | symbol, unknown>)[prop];
      return typeof value === 'function' ? value.bind(appApi[key]) : value;
    },
  });

export const antdMessage = createProxy('message');
export const antdNotification = createProxy('notification');
export const antdModal = createProxy('modal');

export const antdApp = {
  modal: antdModal,
  message: antdMessage,
  notification: antdNotification,
  setApp: setAntdApp,
};
