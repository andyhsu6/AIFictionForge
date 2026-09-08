# Contributing to AIFictionForge

Thank you for your interest in contributing! AIFictionForge (灵创) is an AI-powered novel creation assistant built with a FastAPI backend (Python 3.12) and a React 18 + Vite frontend. This project is a derivative of [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel), licensed under GPL-3.0.

**Language policy (国际化):** issue and PR **titles default to English**; bodies may be bilingual (English primary, Chinese as a supplement). Documentation filenames should avoid Chinese characters so CI and tooling can read them. The UI itself supports two locales: `zh` (product default) and `en`.

## Ways to contribute

- Report bugs or propose features via the issue templates (`.github/ISSUE_TEMPLATE/`).
- Improve translations (`en` locale is the one that most needs help).
- Fix bugs or implement features via pull requests.
- Improve documentation (English or Chinese).

## Development setup

Local development uses **SQLite** (`backend/data/mumuai_novel.db`); PostgreSQL is only used for Docker deployments.

### Backend (FastAPI, port 8008)

```bash
# Python 3.12 virtualenv lives at backend/.venv
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

The backend **must be started from the repository root with `PYTHONPATH=backend`**, otherwise pydantic cannot read the root `.env`:

```bash
PYTHONPATH=backend backend/.venv/bin/python -m uvicorn app.main:app --port 8008
```

Configure `.env` at the repo root (copy from `.env.example` if present). `APP_PORT` defaults to 8008.

### Frontend (Vite dev server, port 5173)

```bash
cd frontend
npm install
npm run dev
```

`vite.config.ts` proxies `/api` and `/generated-assets` to `http://localhost:8008`.

### Service management script

The repo root ships `./aistoryforge.sh`, which manages both services:

```bash
./aistoryforge.sh status              # PIDs + health check for both services
./aistoryforge.sh start               # start backend + frontend
./aistoryforge.sh stop                # stop all
./aistoryforge.sh restart             # restart all
./aistoryforge.sh restart backend     # restart backend only
./aistoryforge.sh start frontend      # start frontend only
```

- Logs: `/tmp/aistoryforge-backend.log` and `/tmp/aistoryforge-frontend.log`.
- Backend health check: `curl http://localhost:8008/health` should return `{"status":"ok"}`.

### Running tests

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
```

(pytest is installed in the venv only and is intentionally not listed in `requirements.txt`.)

## Internationalization (i18n) contributions

All user-facing UI strings go through the translation files — **never hard-code strings in components**.

- Locale files: `frontend/src/locales/zh/<namespace>.json` and `frontend/src/locales/en/<namespace>.json`. `zh` is the primary (product default) language where keys are seeded.
- New namespaces must be registered in `frontend/src/i18n/resources.ts`.
- Extraction/config lives in `frontend/i18next.config.ts` (i18next CLI); TypeScript types are generated into `src/types/i18next.d.ts`.
- **Every new UI key must be added to both `zh` and `en` locale files.** A key-completeness check in CI fails the build when the two locales drift apart.
- When translating, keep placeholders (e.g. `{{count}}`) and interpolation syntax intact.

## Git workflow

### Branches

Branch names must describe the change: `<type>/<short-description>`, e.g. `fix/524-streaming-timeout`, `feat/chunk-first-principle`. Types: `fix`, `feat`, `docs`, `refactor`, `chore`. Generic names (`dev`, `test`, `work`, `local-works`) are not accepted.

### Commits

Use conventional-commit style messages and reference the tracked issue:

```
fix: streaming JSON generation in call_with_json_retry closes #9
feat(skill-chat): skill management page refs #21
```

### When is a PR required?

Any **major change** — core architecture, database schema, auth/security, cross-module interfaces, or anything affecting user data or user experience — must go through a feature branch, a pushed branch, and a PR for review. It must never be committed directly to the main branch. If you are unsure whether your change qualifies, open a PR (or ask in the issue) first.

### Issue tracking

- Bugs: use the Bug report template; the fix commit references the issue (`closes #N`) and the issue is closed once the fix is verified.
- Features: use the Feature request template; plan highlights are posted to the issue comments once agreed, commits reference the issue, and the issue is closed after acceptance.
- Batch/historical sync: group related work by functional module rather than one issue per commit.

## Data privacy hard rule (source-text desensitization)

The repository is public and user-imported books may be copyrighted. **Never post to GitHub**: original text from imported books/documents, chapter excerpts, character names, or world-building proper nouns.

- In issues, PRs, commits, and docs, use descriptive wording instead (e.g. "a real-book re-import", "protagonist", "character names redacted").
- Acceptance evidence containing original text (screenshots, reports, summaries) stays in local, gitignored directories (`.omo/`, `docs/`).

## PR etiquette

- Keep PRs focused: one logical change per PR.
- Title format: `<type>: <summary>` (English, e.g. `fix: streaming timeout in chapter generation`).
- Fill in the PR template (summary, changes, test plan, checklist).
- Link the related issue; describe how you tested the change.
- Make sure no secrets, `.env` contents, or user/source text data appear in the diff.
- Update the docs/CHANGELOG when the change affects users.

## Reporting security issues

Please do **not** open public issues for security vulnerabilities. See [SECURITY.md](SECURITY.md) for private reporting instructions.

## Code of Conduct

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).
