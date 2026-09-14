# AIFictionForge (灵创) 📚✨

<div align="center">

**[中文](/README.zh-CN.md) | English**

![Version](https://img.shields.io/badge/version-1.5.4-blue.svg)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-green.svg)
![React](https://img.shields.io/badge/react-18.3.1-blue.svg)
![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)

**An AI-powered intelligent novel creation assistant**

[Features](#-features) • [Model Requirements](#-model-requirements) • [Quick Start](#-quick-start) • [Configuration](#%EF%B8%8F-configuration) • [Project Structure](#-project-structure)

</div>

---

> This project is forked from [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel).
> It is a localized, de-upstreamed derivative built on the upstream source, not an original standalone project.
> Thanks to the original author and all contributors. The code is licensed under GPL-3.0, see [LICENSE](LICENSE).

---

## ⚠️ Breaking change: a model with a ≥900,000 token context window is now required

**Version note: applies from the first release after `v1.5.4`.** The last tagged release (`v1.5.4`, still current in the badge above) accepted smaller models and silently degraded them; the release after it does not.

- Every AI feature requires a backing model with a context window of at least **900,000 tokens**. Below that floor the model is refused — when you save it **and** on every request that would actually dispatch it, including a per-request model override.
- **The tiered degradation is gone.** There is no "128K mode" that quietly swaps whole-book injection for recent-chapter summaries plus retrieval. There is also **no implicit fallback model**: an account with no configured model gets `validation.ai_model_not_configured` instead of a system-chosen default.
- **Existing accounts are not migrated silently.** If you had a smaller model configured, your next AI request is refused with `validation.ai_model_below_minimum`, and the app points you at Settings (a sticky notice on streaming/background paths, the gate form inline when you are already there). That form shows three numbers from the cached verdict alone — the measured window, the window you declared, and the budget actually adopted, next to the 900,000-token floor itself — so you can re-select and re-verify a qualifying model even if your gateway is unreachable at that moment.
- **`DEFAULT_MODEL` is no longer read by the backend.** The setting was removed; the variable is ignored if it is still present in your `.env` or `docker-compose.yml`. Models are configured per account inside the app.
- A fresh install therefore has **no usable AI model until you add one** in Settings.

The floor is enforced as well as the probe can measure it, which is **not** the same as "it can never be bypassed" — see [Model Requirements](#-model-requirements) for what the probe measures and the two blind spots it still has.

---

## ✨ Features

- 🤖 **Multiple AI Providers** - OpenAI, Gemini, Claude and any OpenAI-compatible endpoint (protocol compatibility only; the model you configure still needs a ≥900,000 token context window, see Model Requirements)
- 📝 **Smart Wizard** - AI automatically generates outlines, characters, and world settings
- 👥 **Character Management** - Visual management of character relationships and organization structures
- 📖 **Chapter Editing** - Create, edit, regenerate, and polish chapters
- 🌐 **Worldbuilding** - Build complete story backgrounds
- 🔐 **Multiple Login Methods** - LinuxDO OAuth or local account login
- 💾 **PostgreSQL** - Production-grade database with multi-user data isolation
- 🐳 **Docker Deployment** - One-click startup, out of the box

## 📸 Project Preview

<details>

<summary>Multiple images ahead</summary>

<div align="center">

### Login Screen
![Login Screen](images/1.png)

![Login Screen](images/1-1.png)

### Main Interface
![Main Interface](images/2.png)

![Main Interface (Dark)](images/2-1.png)

### Project Management
![Project Management](images/3.png)

![Project Management](images/3-1.png)

</div>

</details>

## 📋 TODO List

### ✅ Completed Features

- [x] **Inspiration Mode** - Generate creative inspiration and ideas
- [x] **Custom Writing Styles** - Define your own AI writing styles
- [x] **Data Import / Export** - Import and export project data
- [x] **Prompt Editing UI** - Visually edit Prompt templates
- [x] **Chapter Word Limits** - Users can set the target length for generated chapters
- [x] **Chain-of-Thought & Chapter Relationship Graph** - Visualize logical relationships between chapters
- [x] **One-Click Rewrite from Analysis** - Regenerate content based on analysis suggestions
- [x] **Linux DO Auto Account Creation** - OAuth login automatically creates an account
- [x] **Career & Rank System** - Customizable career and level systems, supporting cultivation realms, magic levels, and more
- [x] **Character / Organization Card Import & Export** - Export character and organization cards individually for cross-project data sharing
- [x] **Foreshadowing Management** - Intelligently track plot foreshadowing, remind about unrecovered threads, and visualize the foreshadowing timeline
- [x] **Book Deconstruction** - One-click book breakdown and analysis

### 📝 Planned Features

......

## 🧭 Plan Runner: Concurrency, Timing and Interruption Semantics

Once a plan is approved, the server runs its steps back to back without
asking again. That changes how the app behaves under load, so the limits
below are documented as product behaviour, not as implementation detail.

**Concurrency**

- All model calls share one process-wide semaphore of
  `max_concurrent_requests = 5`. Plans issue calls in the same queue as
  interactive chat, so a running plan can make unrelated generations
  slower. Slowdown — not an error — is the visible symptom.
- One plan per user runs at a time; steps inside a plan run strictly in
  order.
- Per-user background work is served by a single worker. A plan that runs
  for tens of minutes holds that worker, so a manual generation you start
  meanwhile waits until the plan finishes (worst case = total plan time).
- Completed steps log their dispatch latency and how many model calls
  queued while they ran (`/tmp` backend log, one line per completed step;
  the failed-step path records its timing in the plan's `step_results`
  without a per-step log, and a run that recorded any step also logs one
  closing summary line). `/health` reports `plans_running`, the global
  count of plans in `running` state (approved-but-not-started plans are
  not counted, and no user or project identifiers are ever exposed).
  Each probe runs one extra COUNT filtered by
  `task_type='agent_plan' AND status='running'`; `background_tasks` has no
  index on that column pair, so the count scans the table. That is
  immaterial at current row counts, and adding an index is out of scope
  for this PR.

**Timing**

- Between two steps the runner waits `agent_plan_step_grace_seconds`
  (default `3.0`). Chapter analysis relies on SQLite WAL making a written
  row visible to other sessions, and an automated "write, then immediately
  launch the next step" chain turns that rare race into the normal path.
  Do not tune this to 0 unless you moved off SQLite.

**Restart**

- A server restart marks in-flight plans as failed and writes a localized
  interruption note on the plan row, surfaced through the
  `progress.agent_plan_interrupted` status code: how many steps finished,
  that the results are not final, and that the plan has to be started
  again.
- **Plans are never re-sent automatically.** Chapter analysis overwrites
  existing analysis results, story memories and foreshadowing; JSON
  import and consistency repair are not idempotent either. Replaying them
  unattended would gamble with your data.

**Cancellation — what actually stops**

- Stopping a plan stops the plan: remaining steps are not started, and the
  step counter is recorded.
- **A chapter-analysis sub-task already in flight is not cascaded and
  keeps running to completion.** That task type exposes no cancellation
  channel today; the plan records it under `uncancellable_sub_tasks`
  instead of pretending it stopped. Sub-tasks that do support cancellation
  are cancelled.
- A cancelled or interrupted plan produces no closing summary, and no
  further LLM call is billed for it.

## 🧠 Model Requirements

**This project requires a large-context model — a context window of at least 900,000 tokens.**

Almost every core feature feeds book-scale content into the prompt: chapter generation can inject the whole book, book deconstruction parses an entire imported text, and the creative assistant carries long project history plus tool results on top of that. Below the floor the failure mode is **silent**, not slow — the assistant drops your earliest instructions once its history budget is full, and a batch-analysis summary can report on only part of the book while still sounding complete. That kind of quality regression cannot be recovered after the fact, so we treat the window as a floor rather than a performance preference.

| Context window | Status |
|---|---|
| ≥ 900,000 tokens | ✅ Supported |
| Measured below the floor | ❌ Not supported — the save is rejected, and every request that would dispatch that model is refused |
| Probe could not reach a verdict | ❌ Also rejected — unknown counts as unqualified, until you declare `context_window_tokens ≥ 900,000` in the settings form |

There is no middle tier and no checkbox that waves a verdict through: a model the app has **measured** below the floor stays rejected even if you declare a larger window. The declaration field is an exit for the "probe could not tell" case only.

There is **no implicit default model** either. An account with no configured model does not silently inherit one: AI features stop with `validation.ai_model_not_configured` and send you to Settings. The retired `DEFAULT_MODEL` environment variable is not read by the backend — configure the model per account in the app, where the window is actually measured.

### What the app measures

Verdicts are cached per **(provider, base URL, model name)** triple. Whenever the model a request is about to use has no verdict on file — a first configuration, or a change to any of those three — the app probes your endpoint and waits for the result before running:

1. **Metadata**: `GET /models/<id>`, reading a context-length field if your gateway exposes one (zero tokens).
2. **Server-side bound**: a minimal prompt with `max_tokens` set to the floor, streamed, relying on the provider's own upper-bound validation — accepting that budget is evidence of a window at or above the floor (≈zero tokens; the stream is cut after the first chunk). A rejection counts as evidence of a *smaller* window only when the gateway itself states a context or prompt bound below the floor. A refusal that merely rejects an oversized request without naming such a number — including one that is only about the **output** cap — is recorded as "could not decide", not as "too small", because being rejected at exactly the floor cannot exclude a window of exactly the floor. "Could not decide" is the state the declaration field exists for.

A stored verdict is reused **indefinitely** — there is no automatic re-check, periodic or otherwise. The settings form has a manual **Re-check** that measures and caches without blocking; rejecting is the save/dispatch gate's job, and only a change to one of the three triple parts or that manual action re-probes. A third tier (fill near 1M tokens with a needle and read it back) is the only thing that separates "the endpoint accepted the request" from "the model really read the window"; **it is deliberately not wired up in this iteration**, because it would cost ≈1M input tokens per user. The form shows three numbers side by side — measured, declared, and the budget actually adopted — next to the floor itself, so you can see which one decided.

Tier 2 records the probe scale it **accepted**, not the model's real window, and the whole-book budget is that recorded number × 0.6. A model whose real window is 1,000,000 tokens therefore now records 900,000 and budgets 540,000 characters instead of 600,000 — a 10% smaller injection. The direction is safe: the app injects less of your book than the model could hold, never more.

A first probe can fail — an unreachable gateway, a key that is momentarily invalid — and "could not decide" is not a conclusion. So when the model a request is about to dispatch has no conclusion, or only that non-conclusion, the app probes again and waits (at most once per minute per triple, so a gateway outage cannot turn every request into an outbound probe). **If that dispatch-time re-probe still cannot decide, the result is not stored**: `context_window_gate` in `GET /settings` stays `null`, the card shows "not measured", and the AI request is refused under the "unknown counts as unqualified" rule. That is more honest than recording a measurement that never happened; it does mean the card can read "not measured" while AI is refused, which is expected — a manual Re-check, or a retry once the gateway is reachable, resolves it.

Eligibility is **not** decided by a hardcoded model list. A built-in registry only hints which probe size to start from; it can never qualify or disqualify a model. **You choose the model, and you declare what your own endpoint gives you — you are the authority on it, not our table.**

> **Blind spot 1 — a silent-truncating gateway can pass.** Plenty of compatibility relays do not error on an oversized request, they just cut it down. Such an endpoint answers tier 2 as if it accepted the probe scale and is recorded as qualified. With tier 3 unwired there is no defence, so a model that passed the gate can still lose the beginning of your book. If output quality is worse than the reported window suggests, suspect the gateway, not the model name.

> **Blind spot 2 — a gateway that swaps or downgrades your model, and a stale verdict that is never re-checked.** Verdicts are cached per (provider, base URL, model name) triple and are **not** refreshed automatically — not on a UTC schedule, not on dispatch. Requests keep being admitted under the stale verdict until you change one of the three triple parts or run the manual Re-check; a gateway-side downgrade therefore stays invisible until you do.

> **What even a correct verdict cannot tell you**: accepting the probe scale is not the same as *using* the window well. Long-range consistency in the middle of a novel stays a per-model property, so judge a new model on your own text before committing to it.

> Supporting an API protocol is not the same as meeting the window requirement — the integrations listed in Tech Stack (OpenAI/Claude/Gemini SDKs) describe transport compatibility only. Eligibility is decided by the probed or declared window of the specific model you configure.

## 💻 Hardware Requirements

### Minimum (Personal Use / Development)

| Component | Requirement |
|------|------|
| **CPU** | 2 cores |
| **Memory** | 2 GB RAM |
| **Storage** | 10 GB free space |
| **Network** | Stable internet connection (for AI API calls) |

### Recommended (Small Team / Production)

| Component | Requirement |
|------|------|
| **CPU** | 4 cores |
| **Memory** | 8 GB RAM |
| **Storage** | 20 GB SSD |
| **Network** | Stable internet connection |

### High Concurrency (80-150 Users)

| Component | Requirement |
|------|------|
| **CPU** | 8 cores |
| **Memory** | 16 GB RAM |
| **Storage** | 50 GB+ SSD |
| **Network** | High-bandwidth connection |

> **📌 Notes**
> - **Embedding model**: ~400 MB disk space, loaded into memory at runtime
> - **PostgreSQL**: default config uses 256 MB shared_buffers and 1 GB effective_cache_size
> - **Docker deployment**: reserve an additional 1-2 GB of memory for the container runtime
> - This project primarily depends on external AI APIs (OpenAI/Claude/Gemini); no local GPU is required

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose (optional, not required for local development)
- At least one AI service API Key (OpenAI/Gemini/Claude/DeepSeek-compatible) — **for a model with a ≥900,000 token context window**, see Model Requirements above

### Docker Compose Deployment

```bash
# 1. Get the source code (skip if you already have it locally)
# If forking from upstream, first clone the upstream repository:
git clone https://github.com/xiamuceer-j/MuMuAINovel.git
cd MuMuAINovel
# Then merge the changes from this derivative into your copy

# 2. Configure environment variables (required)
cp backend/.env.example .env
# Edit the .env file and fill in the required config (API Key, database password, etc.)

# 3. Make sure the required files are in place
# ⚠️ Important: ensure the following files exist
# - .env (config file, must be mounted into the container)
# - backend/scripts/init_postgres.sql (database initialization script)

# 4. Start the services
docker-compose up -d

# 5. Access the app
# Open your browser and visit http://localhost:8008
```

> **📌 Notes**
>
> 1. **`.env` file mounting**: `docker-compose.yml` automatically mounts `.env` into the container; make sure the file exists
> 2. **Database initialization**: `init_postgres.sql` runs automatically on first startup to install the required PostgreSQL extensions
> 3. **Build from source**: this project does not ship prebuilt images; build it yourself with `docker-compose build`. Place the embedding model files under `backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/`

### Local Development / Building from Source

#### Preparation

```bash
# ⚠️ Important: before running from source, prepare the embedding model files
# The model is large (~400MB) and must be placed in the following directory:
# backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/
#
# 📥 How to get it: it is downloaded automatically from the official Hugging Face
# repository on first startup, or you can download it manually from
# https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

#### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure the .env file
cp .env.example .env
# Edit .env and fill in the required configuration

# Start PostgreSQL (via Docker)
docker run -d --name postgres \
  -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=aistoryforge \
  -p 5432:5432 \
  postgres:18-alpine

# Start the backend
python -m uvicorn app.main:app --host localhost --port 8008 --reload
```

#### Frontend

```bash
cd frontend
npm install
npm run dev  # development mode
npm run build  # production build
```

> **📌 Port notes**: the frontend dev server runs on **5173** (Vite, proxies `/api` -> `http://localhost:8008`); the backend listens on **8008**. After `npm run build`, the built frontend is served by the backend as a single-port entry on **8008**.

## ⚙️ Configuration

### Required Configuration

Create the `.env` file:

```bash
# PostgreSQL database (required)
DATABASE_URL=postgresql+asyncpg://aistoryforge:your_password@postgres:5432/aistoryforge
POSTGRES_PASSWORD=your_secure_password

# AI services
OPENAI_API_KEY=your_openai_key
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_AI_PROVIDER=openai
# DEFAULT_MODEL has been RETIRED: the backend no longer reads this variable, and
# there is no system default model to fall back to. Configure the model per
# account in the app (Settings), where its real context window is probed.
# The model must have a >=900,000 token context window — see "Model Requirements" above.
# Whole-book context injection scales with the window you configure; for
# long-output tasks like book deconstruction, streaming is recommended.

# Local account login
LOCAL_AUTH_ENABLED=true
LOCAL_AUTH_USERNAME=admin
LOCAL_AUTH_PASSWORD=your_password
```

### Optional Configuration

```bash
# LinuxDO OAuth
LINUXDO_CLIENT_ID=your_client_id
LINUXDO_CLIENT_SECRET=your_client_secret
LINUXDO_REDIRECT_URI=http://localhost:8008/api/auth/callback
# LinuxDO login-specific proxy (optional, only affects OAuth token and user info requests)
LINUXDO_PROXY_URL=http://127.0.0.1:7890

# PostgreSQL connection pool (high-concurrency tuning)
DATABASE_POOL_SIZE=30
DATABASE_MAX_OVERFLOW=20

# Plan runner: seconds to wait between steps (default 3.0; keep it unless you moved off SQLite)
AGENT_PLAN_STEP_GRACE_SECONDS=3

# Session Cookie Secure flag
# Defaults to true, suitable for HTTPS deployments; if you access over HTTP and the
# browser does not persist the login cookie, set it to false
SESSION_COOKIE_SECURE=true

# Local / Docker internal LLM (off by default to keep SSRF protection)
# ALLOW_PRIVATE_AI_ENDPOINTS=true
# ALLOWED_AI_HOSTS=host.docker.internal,127.0.0.1
```

> **🔐 Cookie Secure Notes**
>
> - HTTPS deployment: keep `SESSION_COOKIE_SECURE=true`; the browser will only send the login cookie over HTTPS.
> - HTTP deployment: if the browser does not save the cookie after login, set `SESSION_COOKIE_SECURE=false` in `.env`, then restart the backend or the Docker container.
>
> **🌐 LinuxDO-Specific Proxy Notes**
>
> - If only the LinuxDO authorization login is unreachable on the current network, prefer configuring `LINUXDO_PROXY_URL` rather than a global `HTTP_PROXY` / `HTTPS_PROXY`.
> - `LINUXDO_PROXY_URL` is used only for LinuxDO OAuth token exchange and user-info requests; it does not affect AI services, SMTP, database, or other network calls.
> - Common example: `LINUXDO_PROXY_URL=http://127.0.0.1:7890`. Inside a Docker container, use the host's address on the Docker network instead of the container's `127.0.0.1`.
> - The example above is an HTTP proxy; if you need a SOCKS proxy, first make sure the httpx SOCKS support dependency is installed in your runtime environment.
>
> **🖥️ Local / Docker Internal LLM Notes**
>
> - By default, `localhost`, `127.0.0.1`, private IPs, and hostnames that resolve to internal networks (e.g. `host.docker.internal`) are rejected to reduce SSRF risk.
> - If your AI service runs on a local Ollama / llama.cpp, or a Docker container needs to reach a model on the host, set `ALLOW_PRIVATE_AI_ENDPOINTS=true` in `.env`, or add the allowed hostnames to `ALLOWED_AI_HOSTS`.
> - Even with local access enabled, link-local addresses (such as the cloud metadata endpoint `169.254.169.254`) are still rejected.
> - MCP plugin URLs are not affected by this switch and always go through strict public-internet validation.

### Relay API Configuration

Supports any OpenAI-compatible relay / proxy service:

```bash
# New API example
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_BASE_URL=https://api.new-api.com/v1

# Other relay services
OPENAI_BASE_URL=https://your-proxy-service.com/v1
```

## 🐳 Docker Deployment Details

### Service Architecture

- **postgres**: PostgreSQL 18 database
  - Port: 5432
  - Data persistence: `postgres_data` volume
  - Init script: `backend/scripts/init_postgres.sql` (mounted automatically)
  - Tuned for 80-150 concurrent users

- **aistoryforge**: main application service
  - Port: 8008
  - Log directory: `./logs`
  - Config mount: `.env` file
  - Automatically waits for the database to be ready
  - Health check: runs every 30 seconds

### Key Files

| File | Description | Required |
|------|------|---------|
| `.env` | Environment config (API Key, database password, etc.) | ✅ Required |
| `docker-compose.yml` | Service orchestration config | ✅ Required |
| `backend/scripts/init_postgres.sql` | PostgreSQL extension installation script | ✅ Auto-mounted |
| `backend/embedding/models--*/` | Embedding model files | ⚠️ Needed for self-built images |

### Common Commands

```bash
# Build and start the services
docker-compose build
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f

# Stop the services
docker-compose down

# Restart the services
docker-compose restart

# Check resource usage
docker stats
```

### Data Persistence

- `./postgres_data` - PostgreSQL database files
- `./logs` - Application log files

### Port Configuration

Change the port mapping in `docker-compose.yml`:

```yaml
ports:
  - "8800:8008"  # host:container
```

## 📁 Project Structure

```
AIFictionForge/
├── backend/                 # Backend service
│   ├── app/
│   │   ├── api/            # API routes
│   │   ├── models/         # Data models
│   │   ├── services/       # Business logic
│   │   ├── middleware/     # Middleware
│   │   ├── database.py     # Database connection
│   │   └── main.py         # Application entry point
│   ├── scripts/            # Utility scripts
│   └── requirements.txt    # Python dependencies
├── frontend/               # Frontend app
│   ├── src/
│   │   ├── pages/         # Page components
│   │   ├── components/    # Shared components
│   │   ├── services/      # API services
│   │   └── store/         # State management
│   └── package.json
├── docker-compose.yml      # Docker Compose configuration
├── Dockerfile             # Docker image build
└── README.md
```

## 🛠️ Tech Stack

**Backend**: FastAPI • PostgreSQL • SQLAlchemy • OpenAI/Claude/Gemini SDK

**Frontend**: React 18 • TypeScript • Ant Design • Zustand • Vite

## 📖 Usage Guide

1. **Sign in** - use a local account or a LinuxDO account
2. **Create a project** - choose "Create with Wizard"
3. **AI generation** - enter basic info and the AI automatically generates the outline and characters
4. **Edit & refine** - manage character relationships, generate and edit chapters

### API Documentation

- Swagger UI: `http://localhost:8008/docs`
- ReDoc: `http://localhost:8008/redoc`

## 📝 License

This project is licensed under the [GNU General Public License v3.0](LICENSE)

**What GPL v3 means:**
- ✅ Free to use, modify, and distribute
- ✅ Can be used for commercial purposes
- 📝 Modified versions must be open source
- 📝 Original author's copyright must be preserved
- 📝 Derivative works must be licensed under GPL v3

## 🙏 Acknowledgments

- Upstream project [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) and all its contributors
- [FastAPI](https://fastapi.tiangolo.com/) - Python web framework
- [React](https://react.dev/) - Frontend framework
- [Ant Design](https://ant.design/) - UI component library
- [PostgreSQL](https://www.postgresql.org/) - Database
