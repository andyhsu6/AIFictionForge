# AIFictionForge（灵创）📚✨

<div align="center">

**English | [中文](/README.zh-CN.md)**

**基于 AI 的智能小说创作助手**

[特性](#-特性) • [模型要求](#-模型要求) • [快速开始](#-快速开始) • [配置说明](#%EF%B8%8F-配置说明) • [项目结构](#-项目结构)

</div>

---

> 本项目 fork 自 [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel)。
> 这是基于上游源码的本地化与去上游化衍生版本，不是独立原创项目。
> 感谢原作者与所有贡献者。代码遵循 GPL-3.0，详见 [LICENSE](LICENSE)。

---

## 🧠 模型要求

- 所有 AI 功能都要求底座模型的上下文窗口**至少 900,000 token**。低于这条下限的模型，保存时会被拒绝，每一次真正要派发该模型的请求也会被拒。
- **没有隐式兜底模型**，也没有「128K 模式」之类的分级降级。全新安装在**设置**里添加模型之前没有任何可用的 AI 模型，窗口会在那里针对你自己的端点实测。
- `DEFAULT_MODEL` 已废弃，后端不再读取。请在应用内按账户配置模型。
- 窗口的实测方式与探测仍然存在的盲区，见 [docs/model-requirements.md](docs/model-requirements.md)。

---

## ✨ 特性

- 🤖 **多 AI 服务商**：支持 OpenAI、Gemini、Claude 及任意 OpenAI 兼容端点（仅指协议兼容；所配置的模型仍需具备 >=900,000 token 上下文窗口）。
- 🧙 **项目创建向导**：AI 根据简短描述生成大纲、角色和世界观。
- 📖 **章节工作流**：创建、编辑、整章或局部重新生成、润色，并可设置目标字数。
- 📚 **拆书**：导入既有文本并逐章分析。
- 🧵 **伏笔追踪**：追踪剧情伏笔、提醒未回收线索，并提供伏笔时间线。
- 👥 **角色与组织**：角色和组织管理、关系图谱，以及可自定义的职业等级体系（修仙境界、魔法等级等）。
- 🌐 **世界观设定**：构建完整的故事背景与设定。
- 💡 **灵感模式**：生成创作灵感和方向。
- ✍️ **自定义写作风格**：定义并复用你自己的 AI 写作风格。
- 🧩 **提示词模板编辑**：在界面中可视化编辑提示词模板。
- 🧠 **故事记忆与一致性**：按项目维护长期故事记忆，并提供数据一致性检查与修复。
- 🛠️ **Skills 与 MCP 插件**：内置写作技能（扫描、分析、写作、去 AI 味）与 MCP 插件管理。
- 🧭 **计划执行器**：批准多步计划后由服务端连续执行。详见[计划执行器语义](docs/plan-runner-semantics.md)。
- 🔍 **记忆搜索**：按语义检索项目的故事记忆。
- 🌍 **国际化**：zh/en 界面，`content_language` 控制 AI 输出语言。
- 🔐 **登录**：本地账户或 LinuxDO OAuth（自动创建账号）。
- 📦 **导入 / 导出**：项目数据，以及角色和组织卡片，支持跨项目共享。
- 🐳 **部署**：Docker Compose，或由后端以单端口 8008 提供构建产物。

## 📸 项目预览

<details>
<summary>多图预警</summary>

| 登录 | 主界面 | 项目管理 |
|:--:|:--:|:--:|
| ![登录](images/1.png) | ![主界面](images/2.png) | ![项目管理](images/3.png) |
| ![登录（备选）](images/1-1.png) | ![主界面（暗色）](images/2-1.png) | ![项目管理（备选）](images/3-1.png) |

</details>

## 💻 硬件配置要求

| 场景 | CPU | 内存 | 存储 | 网络 |
|------|------|------|------|------|
| 最低配置（个人使用 / 开发） | 2 核 | 2 GB RAM | 10 GB 可用空间 | 稳定互联网连接（用于调用 AI API） |
| 推荐配置（小型团队 / 生产） | 4 核 | 8 GB RAM | 20 GB SSD | 稳定互联网连接 |
| 高并发配置（80-150 用户） | 8 核 | 16 GB RAM | 50 GB+ SSD | 高带宽连接 |

> **📌 说明**
> - **Embedding 模型**：约 400 MB 磁盘空间，运行时加载到内存。
> - 不需要本地 GPU；本项目依赖外部 AI API。

## 🚀 快速开始

**前置要求**：Docker 和 Docker Compose（可选，本地开发无需），以及至少一个 AI 服务的 API Key（OpenAI / Gemini / Claude / 任意 OpenAI 兼容中转），且该模型需具备 >=900,000 token 上下文窗口。

### Docker Compose 部署

```bash
git clone https://github.com/andyhsu6/AIFictionForge.git && cd AIFictionForge
cp backend/.env.example .env          # 然后填入必要配置
docker-compose up -d                  # 然后访问 http://localhost:8008
```

`docker-compose.yml` 会自动把 `.env` 挂载进容器，`backend/scripts/init_postgres.sql` 会在首次启动时执行并安装所需的 PostgreSQL 扩展。本项目不提供预构建镜像：请用 `docker-compose build` 自行构建，并将 embedding 模型文件放到 `backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/`。

### 本地开发 / 从源码构建

先准备 embedding 模型（约 400 MB，放在 `backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/`）。首次启动时会从 Hugging Face 自动下载，也可手动从 <https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2> 获取。

```bash
# 后端
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # 然后编辑
docker run -d --name postgres -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=aistoryforge -p 5432:5432 postgres:18-alpine
python -m uvicorn app.main:app --host localhost --port 8008 --reload

# 前端
cd ../frontend
npm install
npm run dev    # 开发模式
npm run build  # 生产构建
```

> **📌 端口说明**：前端开发服务器运行在 **5173**（Vite，代理 `/api` 与 `/generated-assets` 到 `http://localhost:8008`）；后端监听 **8008**。执行 `npm run build` 后，构建产物由后端以单端口 **8008** 入口提供访问。

## ⚙️ 配置说明

`.env` 必需项：

```bash
DATABASE_URL=postgresql+asyncpg://aistoryforge:your_password@postgres:5432/aistoryforge
POSTGRES_PASSWORD=your_secure_password
OPENAI_API_KEY=your_openai_key
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_AI_PROVIDER=openai
LOCAL_AUTH_ENABLED=true
LOCAL_AUTH_USERNAME=admin
LOCAL_AUTH_PASSWORD=your_password
# DEFAULT_MODEL 已废弃：后端不再读取该变量，系统也不提供任何默认模型。
# 请在应用内按账户配置具备 >=900,000 token 窗口的模型（设置页）。
```

任何 OpenAI 兼容中转都可用：把 `OPENAI_BASE_URL` 指向中转地址，并把密钥填到 `OPENAI_API_KEY`。完整清单（含小米 MiMo 适配）见 [`backend/.env.example`](backend/.env.example)。

常用可选项：

```bash
LINUXDO_CLIENT_ID=your_client_id             # LinuxDO OAuth
LINUXDO_CLIENT_SECRET=your_client_secret
LINUXDO_REDIRECT_URI=http://localhost:8008/api/auth/callback
LINUXDO_PROXY_URL=http://127.0.0.1:7890      # 仅 OAuth 专用代理
AGENT_PLAN_STEP_GRACE_SECONDS=3              # 计划执行器步间等待
SESSION_COOKIE_SECURE=true                   # HTTP 部署登录 Cookie 不保存时设为 false
# ALLOW_PRIVATE_AI_ENDPOINTS=true            # 本地 / Docker 内网 LLM
# ALLOWED_AI_HOSTS=host.docker.internal,127.0.0.1
```

> **📌 说明**：HTTPS 部署保持 `SESSION_COOKIE_SECURE=true`（HTTP 部署下浏览器不保存登录 Cookie 时设为 `false`）。如果只有 LinuxDO 登录不可达，优先设置 `LINUXDO_PROXY_URL`，不要配置全局 `HTTP_PROXY` / `HTTPS_PROXY`；它不影响 AI、SMTP 和数据库调用，Docker 容器内需使用宿主机在 Docker 网络中的地址。本地和 Docker 内网 LLM 端点默认被拒（SSRF 防护），可用 `ALLOW_PRIVATE_AI_ENDPOINTS=true` 开启，或把主机名加入 `ALLOWED_AI_HOSTS`；链路本地地址仍然会被拒绝。

## 📁 项目结构

```
AIFictionForge/
├── backend/                 # FastAPI 服务
│   ├── app/                # api/, models/, services/, skills/, middleware/
│   ├── embedding/          # Embedding 模型文件
│   └── requirements.txt
├── frontend/               # React + TypeScript 应用
│   ├── src/                # pages/, components/, services/, store/
│   └── package.json
├── docs/                   # 文档
├── docker-compose.yml
├── Dockerfile
└── README.md
```

## 🛠️ 技术栈

**后端**：FastAPI • PostgreSQL • SQLAlchemy • OpenAI / Claude / Gemini SDK

**前端**：React 18 • TypeScript • Ant Design • Zustand • Vite

## 📖 使用指南

1. **登录系统**：使用本地账户或 LinuxDO 账户。
2. **创建项目**：选择「使用向导创建」，让 AI 起草大纲、角色和世界观。
3. **完善设定**：管理角色、组织、关系、职业等级和世界观。
4. **生成与编辑章节**：生成、重新生成或润色章节，并设置目标字数。
5. **分析**：运行拆书、章节分析和一致性检查，追踪伏笔。
6. **扩展**：安装写作技能、接入 MCP 插件，或为代理执行器批准多步计划。

### API 文档

- Swagger UI：`http://localhost:8008/docs`
- ReDoc：`http://localhost:8008/redoc`

## 📝 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)。

**GPL v3 意味着**：可自由使用、修改和分发；可用于商业目的；修改版本与衍生作品必须以 GPL v3 保持开源，并保留原作者版权。

## 🙏 致谢

- 上游项目 [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 及所有贡献者
- [FastAPI](https://fastapi.tiangolo.com/) - Python Web 框架
- [React](https://react.dev/) - 前端框架
- [Ant Design](https://ant.design/) - UI 组件库
- [PostgreSQL](https://www.postgresql.org/) - 数据库

## 📚 更多文档

- [CONTRIBUTING.md](CONTRIBUTING.md) - 如何参与贡献
- [docs/i18n.md](docs/i18n.md) - 国际化指南
- [docs/model-requirements.md](docs/model-requirements.md) - 上下文窗口实测与盲区
- [docs/plan-runner-semantics.md](docs/plan-runner-semantics.md) - 计划执行器的并发、时序与中断
