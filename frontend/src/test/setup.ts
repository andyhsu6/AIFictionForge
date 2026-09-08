// Global test setup for vitest (jsdom).
// 1. jest-dom matchers on vitest's expect (toBeInTheDocument, etc.)
// 2. jsdom shims that antd v5 needs at render time (matchMedia / ResizeObserver)
import '@testing-library/jest-dom/vitest';

if (typeof window !== 'undefined') {
  // antd Grid.useBreakpoint / responsive observers call window.matchMedia,
  // which jsdom does not implement. Stub it: no media query ever matches,
  // which yields the desktop layout deterministically.
  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {}, // deprecated API, still probed by antd
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }

  // rc-resize-observer (used by antd Tabs/Overflow) requires a global
  // ResizeObserver; jsdom ships none. defineProperty avoids TS narrowing the
  // property to `never` inside the `!('ResizeObserver' in window)` branch.
  if (!('ResizeObserver' in window)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    Object.defineProperty(window, 'ResizeObserver', {
      value: ResizeObserverStub,
      configurable: true,
      writable: true,
    });
  }

  // Node >= 22 exposes a global `localStorage` accessor that returns
  // undefined unless the process is launched with --localstorage-file. Under
  // vitest's jsdom environment window === globalThis, so Node's accessor
  // shadows jsdom's working implementation. Replace it with an in-memory
  // Storage shim so i18next's detector cache and app code behave like a
  // real browser.
  if (typeof window.localStorage === 'undefined') {
    const store = new Map<string, string>();
    const storage: Storage = {
      get length() {
        return store.size;
      },
      clear: () => store.clear(),
      getItem: (key: string) => (store.has(key) ? store.get(key)! : null),
      key: (index: number) => Array.from(store.keys())[index] ?? null,
      removeItem: (key: string) => {
        store.delete(key);
      },
      setItem: (key: string, value: string) => {
        store.set(key, String(value));
      },
    };
    Object.defineProperty(window, 'localStorage', {
      value: storage,
      configurable: true,
      writable: true,
    });
  }
}
