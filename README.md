# AIFictionForge (灵创) 📚✨

<div align="center">

**[中文](/README.zh-CN.md) | English**

![Version](https://img.shields.io/badge/version-1.5.4-blue.svg)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-green.svg)
![React](https://img.shields.io/badge/react-18.3.1-blue.svg)
![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)

**An AI-powered intelligent novel creation assistant**

[Features](#-features) • [Quick Start](#-quick-start) • [Configuration](#%EF%B8%8F-configuration) • [Project Structure](#-project-structure)

</div>

---

> This project is forked from [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel).
> It is a localized, de-upstreamed derivative built on the upstream source, not an original standalone project.
> Thanks to the original author and all contributors. The code is licensed under GPL-3.0, see [LICENSE](LICENSE).

---

## ✨ Features

- 🤖 **Multiple AI Models** - Supports major providers including OpenAI, Gemini, and Claude
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
- At least one AI service API Key (OpenAI/Gemini/Claude)

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
# Open your browser and visit http://localhost:8000
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
python -m uvicorn app.main:app --host localhost --port 8000 --reload
```

#### Frontend

```bash
cd frontend
npm install
npm run dev  # development mode
npm run build  # production build
```

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
# ⭐ Recommended: large-context (≥1M token) models such as DeepSeek V4 Flash / V3, Gemini 2.0 Pro
# The system tiers context injection by the model's context window: 1M models can feed
# the whole book during chapter generation, while 128K models automatically fall back to
# recent-chapter summaries plus memory retrieval. For long-output tasks like book
# deconstruction, streaming is recommended.
DEFAULT_MODEL=gpt-4o-mini

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
LINUXDO_REDIRECT_URI=http://localhost:8000/api/auth/callback
# LinuxDO login-specific proxy (optional, only affects OAuth token and user info requests)
LINUXDO_PROXY_URL=http://127.0.0.1:7890

# PostgreSQL connection pool (high-concurrency tuning)
DATABASE_POOL_SIZE=30
DATABASE_MAX_OVERFLOW=20

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
  - Port: 8000
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
  - "8800:8000"  # host:container
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

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

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
