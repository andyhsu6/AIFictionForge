# AIFictionForge Frontend

The web UI for AIFictionForge: React 18 + TypeScript, built with Vite and Ant Design.

The dev server runs on **5173** and proxies `/api` and `/generated-assets` to the backend on `http://localhost:8008`.

## Commands

```bash
npm install    # install dependencies
npm run dev    # dev server on 5173 (HMR); needs the backend running on 8008
npm run build  # production build, output to backend/static/
```

`npm run build` writes into `backend/static/`, so the backend can serve the app as a single-port entry on **8008**. See the root [README](../README.md) for the full setup, and [CONTRIBUTING.md](../CONTRIBUTING.md) for development conventions.

## Layout

- `src/pages/` - route/page components
- `src/components/` - shared components
- `src/services/` - API clients
- `src/store/` - state management (Zustand)
- `src/locales/` - zh/en UI strings
