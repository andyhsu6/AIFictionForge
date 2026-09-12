# AIFictionForge（灵创）📚✨

<div align="center">

**English | [中文](/README.zh-CN.md)**

![Version](https://img.shields.io/badge/version-1.5.4-blue.svg)
![Python](https://img.shields.io/badge/python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109.0-green.svg)
![React](https://img.shields.io/badge/react-18.3.1-blue.svg)
![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)

**基于 AI 的智能小说创作助手**

[特性](#-特性) • [模型要求](#-模型要求) • [快速开始](#-快速开始) • [配置说明](#%EF%B8%8F-配置说明) • [项目结构](#-项目结构)

</div>

---

> 本项目 fork 自 [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel)。
> 这是基于上游源码的本地化与去上游化衍生版本，不是独立原创项目。
> 感谢原作者与所有贡献者。代码遵循 GPL-3.0，详见 [LICENSE](LICENSE)。

---

## ⚠️ 破坏性变更：现在要求模型具备 ≥1M token 的上下文窗口

**版本说明：自 `v1.5.4` 之后的第一个发布版本生效。** 最近一个已打标签的版本（`v1.5.4`，也就是顶部徽章显示的版本）还接受更小的模型并对其做静默分级降级；它之后的版本不再接受。

- 所有 AI 功能都要求底座模型的上下文窗口至少 **1,000,000 tokens**。低于这条下限的模型会被拒绝——保存时拒，**每一次真正要把该模型发出去的请求**也拒，包括逐次请求传入的模型覆盖。
- **分级降级已删除。** 不再有「128K 模式」把全书注入悄悄换成最近章节摘要 + 记忆检索；也**没有隐式兜底模型**：未配置模型的账户会拿到 `validation.ai_model_not_configured`，而不会被系统塞一个默认值。
- **存量账户不会被静默迁移。** 原先配置着较小模型的用户，下一次 AI 请求会以 `validation.ai_model_below_minimum` 被拒，应用会把他们引导到设置页（流式与后台任务路径给出常驻提示，已在设置页时则直接内联显示门禁表单）。该表单只凭缓存结论就能同屏显示三个数——实测窗口 / 你声明的窗口 / 系统下限——因此即使此刻网关不可达，也能当场看清需要重新选择模型。
- **`DEFAULT_MODEL` 环境变量后端已不再读取。** 对应的配置项已删除；你的 `.env` 或 `docker-compose.yml` 里若还留着它，会被忽略。模型改为在应用内按账户配置。
- 因此全新安装在你**于设置里添加模型之前没有任何可用的 AI 模型**。

这条下限会尽探测所能地强制执行，但这**不等于「任何时刻都不会被绕过」**——探测具体测了什么、仍有哪两个盲区，见下文[「模型要求」](#-模型要求)。

---

## ✨ 特性

- 🤖 **多 AI 服务商** - 支持 OpenAI、Gemini、Claude 及任意 OpenAI 兼容端点（仅指协议兼容；所配置的模型仍需具备 ≥1M token 上下文窗口，见「模型要求」）
- 📝 **智能向导** - AI 自动生成大纲、角色和世界观
- 👥 **角色管理** - 人物关系、组织架构可视化管理
- 📖 **章节编辑** - 支持创建、编辑、重新生成和润色
- 🌐 **世界观设定** - 构建完整的故事背景
- 🔐 **多种登录** - LinuxDO OAuth 或本地账户登录
- 💾 **PostgreSQL** - 生产级数据库，多用户数据隔离
- 🐳 **Docker 部署** - 一键启动，开箱即用

## 📸 项目预览

<details>

<summary>多图预警</summary>

<div align="center">

### 登录界面
![登录界面](images/1.png)

![登录界面](images/1-1.png)

### 主界面
![主界面](images/2.png)

![主界面（暗色）](images/2-1.png)

### 项目管理
![项目管理](images/3.png)

![项目管理](images/3-1.png)

</div>

</details>

## 📋 TODO List

### ✅ 已完成功能

- [x] **灵感模式** - 创作灵感和点子生成
- [x] **自定义写作风格** - 支持自定义 AI 写作风格
- [x] **数据导入导出** - 项目数据的导入导出
- [x] **Prompt 调整界面** - 可视化编辑 Prompt 模板
- [x] **章节字数限制** - 用户可设置生成字数
- [x] **思维链与章节关系图谱** - 可视化章节逻辑关系
- [x] **根据分析一键重写** - 根据分析建议重新生成
- [x] **Linux DO 自动创建账号** - OAuth 登录自动生成账号
- [x] **职业等级体系** - 自定义职业和等级系统，支持修仙境界、魔法等级等多种体系
- [x] **角色/组织卡片导入导出** - 单独导出角色和组织卡片，支持跨项目数据共享
- [x] **伏笔管理** - 智能追踪剧情伏笔，提醒未回收线索，可视化伏笔时间线
- [x] **拆书功能** - 一键拆书

### 📝 规划中功能

......

## 🧠 模型要求

**本项目要求使用大上下文模型——上下文窗口至少 1M token。**

几乎所有核心功能都要把书本体量的内容灌进 prompt：章节生成可注入全书、拆书要解析整本导入文本、创作助手还要在此基础上携带很长的项目历史与工具结果。窗口不足时的失效形态是**静默的，而不是变慢**——助手的历史预算装满后会丢掉你最早的指令，批量分析的收尾总结可能只覆盖了一部分章节却听起来很完整。这类质量退化事后无法挽回，所以我们把窗口当作底线而非性能偏好。

| 上下文窗口 | 支持状态 |
|---|---|
| ≥ 1M token | ✅ 支持 |
| 实测低于 1M | ❌ 不支持 —— 保存即被拒，且每一次真正派发该模型的请求都会被拒 |
| 探测无法定论 | ❌ 同样不支持 —— 未知即不合格，直到你在设置表单里把 `context_window_tokens` 声明为 ≥1M |

没有中间档，也没有任何「勾一下放行」的开关：被**实测**出低于 1M 的模型，即使你声明更大的窗口也照样拒绝。声明框只服务「探测判不出」这一种情况。

系统也**没有隐式默认模型**。未配置模型的账户不会被系统塞一个默认值：AI 功能直接以 `validation.ai_model_not_configured` 停下并引导你去设置页。已废弃的 `DEFAULT_MODEL` 环境变量后端不再读取——请在应用内按账户配置模型，窗口是在那里实测的。

### 应用实测的是什么

结论按 **(provider, base URL, 模型名)** 三元组缓存。只要本次要派发的模型在这个三元组上从未有过结论——首次配置，或三者中任一发生变更——应用会先探测你的端点并**等结果出来**再执行：

1. **元数据档**：`GET /models/<id>`，读取网关暴露的窗口字段（0 token，多数网关不提供）。
2. **服务端上界档**：极小 prompt + 把 `max_tokens` 设成 1M 下限并流式发送，靠服务端自己的上界校验判定——接受这个输出预算就是「窗口 ≥1M」的证据，被上界类错误拒绝就是「更小」的证据（≈0 token；拿到首个分片即断开）。

已有结论会一路用到 UTC 自然日翻页为止：跨天后的首个 AI 请求或保存会在**后台**复测同样这两档，且**不等待其结果**，因此那一次请求仍按旧结论执行。设置表单另有手动「重新检测」，它只测不拦（并把结论落缓存），拒绝由保存/派发门禁负责。第三档（把输入填到接近 1M 再回读一枚 needle）才是区分「网关接受了请求」与「模型真读进去了 1M」的唯一手段，**本期刻意未接线**，因为它每个用户就要花掉 ≈1M 输入 token。表单会把三个数并排显示——实测窗口 / 你填写的窗口 / 系统下限——让采用哪个数一目了然。

是否合格**不**由写死的模型清单决定。内置登记表只用来提示从哪个刻度开始探测，它永远不能给模型开合格证、也不能判模型不合格。**模型由你选，端点给了多大窗口由你声明——你的端点你说了算，不是一张表说了算。**

> **盲区一：静默截断型网关可以通过前两档。** 相当多兼容网关对超量请求不报错，而是直接截断。这类端点会像接受了 1M 那样回答第 2 档，于是被判为合格。第 3 档未接线，这条没有防线，所以过了门禁的模型仍可能读不到你书的开头。若产出质量明显差于所报告的窗口，请怀疑网关而不是模型名。

> **盲区二：两次复测之间网关换模型或降配。** 结论按三元组缓存，最多每个 UTC 自然日复测一次且在后台进行，所以在新结论落地前，请求会一直沿用那条过期结论放行。

> **即使判定正确也测不出的部分**：能吃下 1M token ≠ 能把这 1M 用好。长篇小说中段的前后一致性仍取决于具体模型，换新模型前建议先用自己的文本试一轮。

> **支持某个 API 协议 ≠ 满足窗口要求**：技术栈一节列出的 OpenAI/Claude/Gemini SDK 只说明传输层兼容，是否合格由你所配置模型的实测（或显式声明）窗口决定。

## 💻 硬件配置要求

### 最低配置（个人使用/开发环境）

| 组件 | 要求 |
|------|------|
| **CPU** | 2 核 |
| **内存** | 2 GB RAM |
| **存储** | 10 GB 可用空间 |
| **网络** | 稳定互联网连接（用于调用 AI API） |

### 推荐配置（小型团队/生产环境）

| 组件 | 要求 |
|------|------|
| **CPU** | 4 核 |
| **内存** | 8 GB RAM |
| **存储** | 20 GB SSD |
| **网络** | 稳定互联网连接 |

### 高并发配置（80-150 用户）

| 组件 | 要求 |
|------|------|
| **CPU** | 8 核 |
| **内存** | 16 GB RAM |
| **存储** | 50 GB+ SSD |
| **网络** | 高带宽连接 |

> **📌 说明**
> - **Embedding 模型**：约 400 MB 磁盘空间，运行时加载到内存
> - **PostgreSQL**：默认配置使用 256 MB shared_buffers，1 GB effective_cache_size
> - **Docker 部署**：建议预留额外 1-2 GB 内存给容器运行时
> - 本项目主要依赖外部 AI API（OpenAI/Claude/Gemini），不需要本地 GPU

## 🚀 快速开始

### 前置要求

- Docker 和 Docker Compose（可选，本地开发无需）
- 至少一个 AI 服务的 API Key（OpenAI/Gemini/Claude/DeepSeek 兼容），**且该模型需具备 ≥1M token 上下文窗口**，见上文「模型要求」

### Docker Compose 部署

```bash
# 1. 获取源码（本地已有源码则跳过）
# 若从上游 fork，先克隆上游仓库：
git clone https://github.com/xiamuceer-j/MuMuAINovel.git
cd MuMuAINovel
# 然后将本衍生版本的修改合并到你的副本中

# 2. 配置环境变量（必需）
cp backend/.env.example .env
# 编辑 .env 文件，填入必要配置（API Key、数据库密码等）

# 3. 确保文件准备完整
# ⚠️ 重要：确保以下文件存在
# - .env（配置文件，必需挂载到容器）
# - backend/scripts/init_postgres.sql（数据库初始化脚本）

# 4. 启动服务
docker-compose up -d

# 5. 访问应用
# 打开浏览器访问 http://localhost:8008
```

> **📌 注意事项**
>
> 1. **`.env` 文件挂载**: `docker-compose.yml` 会自动将 `.env` 挂载到容器，确保文件存在
> 2. **数据库初始化**: `init_postgres.sql` 会在首次启动时自动执行，安装必要的 PostgreSQL 扩展
> 3. **自行构建**: 本项目不提供预构建镜像，请使用 `docker-compose build` 从源码自行构建；Embedding 模型文件需放置到 `backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/`

### 本地开发 / 从源码构建

#### 前置准备

```bash
# ⚠️ 重要：从源码运行前，需要先准备 embedding 模型文件
# 模型文件较大（约 400MB），需放置到以下目录：
# backend/embedding/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2/
#
# 📥 获取方式：首次启动时会从 Hugging Face 官方仓库自动下载，或手动下载
# https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

#### 后端

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 配置 .env 文件
cp .env.example .env
# 编辑 .env 填入必要配置

# 启动 PostgreSQL（可使用 Docker）
docker run -d --name postgres \
  -e POSTGRES_PASSWORD=your_password \
  -e POSTGRES_DB=aistoryforge \
  -p 5432:5432 \
  postgres:18-alpine

# 启动后端
python -m uvicorn app.main:app --host localhost --port 8008 --reload
```

#### 前端

```bash
cd frontend
npm install
npm run dev  # 开发模式
npm run build  # 生产构建
```

> **📌 端口说明**：前端开发服务器运行在 **5173**（Vite，代理 `/api` -> `http://localhost:8008`）；后端监听 **8008**。执行 `npm run build` 后，构建产物由后端以单端口 **8008** 入口直接提供访问。

## ⚙️ 配置说明

### 必需配置

创建 `.env` 文件：

```bash
# PostgreSQL 数据库（必需）
DATABASE_URL=postgresql+asyncpg://aistoryforge:your_password@postgres:5432/aistoryforge
POSTGRES_PASSWORD=your_secure_password

# AI 服务
OPENAI_API_KEY=your_openai_key
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_AI_PROVIDER=openai
# DEFAULT_MODEL 已废弃：后端不再读取该变量，系统也不提供任何默认模型。
# 请在应用内按账户配置模型，窗口是在那里实测的；该模型必须具备 ≥1M token
# 上下文窗口，详见上文「模型要求」。
# 全书上下文注入量随所配置窗口伸缩；拆书等长输出任务建议配合流式。

# 本地账户登录
LOCAL_AUTH_ENABLED=true
LOCAL_AUTH_USERNAME=admin
LOCAL_AUTH_PASSWORD=your_password
```

### 可选配置

```bash
# LinuxDO OAuth
LINUXDO_CLIENT_ID=your_client_id
LINUXDO_CLIENT_SECRET=your_client_secret
LINUXDO_REDIRECT_URI=http://localhost:8008/api/auth/callback
# LinuxDO 登录专用代理（可选，仅影响 OAuth token 与用户信息请求）
LINUXDO_PROXY_URL=http://127.0.0.1:7890

# PostgreSQL 连接池（高并发优化）
DATABASE_POOL_SIZE=30
DATABASE_MAX_OVERFLOW=20

# 会话 Cookie Secure 标记
# 默认 true，适合 HTTPS 部署；如果使用 HTTP 访问并且浏览器不保存登录 Cookie，可设为 false
SESSION_COOKIE_SECURE=true

# 本地 / Docker 内网 LLM（默认关闭，保持 SSRF 防护）
# ALLOW_PRIVATE_AI_ENDPOINTS=true
# ALLOWED_AI_HOSTS=host.docker.internal,127.0.0.1
```

> **🔐 Cookie Secure 说明**
>
> - HTTPS 部署：建议保持 `SESSION_COOKIE_SECURE=true`，浏览器只会通过 HTTPS 发送登录 Cookie。
> - HTTP 部署：如果登录后浏览器没有保存 Cookie，请在 `.env` 中设置 `SESSION_COOKIE_SECURE=false`，然后重启后端或 Docker 容器。
>
> **🌐 LinuxDO 专用代理说明**
>
> - 如果只有 LinuxDO 授权登录在当前网络不可达，优先配置 `LINUXDO_PROXY_URL`，不要配置全局 `HTTP_PROXY` / `HTTPS_PROXY`。
> - `LINUXDO_PROXY_URL` 只会用于 LinuxDO OAuth 的 token 交换和用户信息请求，不影响 AI 服务、SMTP、数据库等其他网络调用。
> - 常见示例：`LINUXDO_PROXY_URL=http://127.0.0.1:7890`；Docker 容器内访问宿主机代理时通常需要使用宿主机在 Docker 网络中的地址，而不是容器内的 `127.0.0.1`。
> - 当前示例按 HTTP 代理配置；如果需要 SOCKS 代理，请先确保运行环境安装了 httpx 的 SOCKS 支持依赖。
>
> **🖥️ 本地 / Docker 内网 LLM 说明**
>
> - 默认会拒绝 `localhost`、`127.0.0.1`、私网 IP 以及解析到内网的主机名（例如 `host.docker.internal`），用于降低 SSRF 风险。
> - 如果 AI 服务跑在本机 Ollama / llama.cpp，或 Docker 容器需要访问宿主机上的模型，请在 `.env` 中设置 `ALLOW_PRIVATE_AI_ENDPOINTS=true`，或把允许的主机名写入 `ALLOWED_AI_HOSTS`。
> - 即使开启本地放行，链路本地地址（如云厂商元数据 `169.254.169.254`）仍然会被拒绝。
> - MCP 插件 URL 不受该开关影响，继续走严格的公网校验。

### 中转 API 配置

支持所有 OpenAI 兼容格式的中转服务：

```bash
# New API 示例
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_BASE_URL=https://api.new-api.com/v1

# 其他中转服务
OPENAI_BASE_URL=https://your-proxy-service.com/v1
```

## 🐳 Docker 部署详情

### 服务架构

- **postgres**: PostgreSQL 18 数据库
  - 端口: 5432
  - 数据持久化: `postgres_data` volume
  - 初始化脚本: `backend/scripts/init_postgres.sql`（自动挂载）
  - 优化配置: 支持 80-150 并发用户

- **aistoryforge**: 主应用服务
  - 端口: 8008
  - 日志目录: `./logs`
  - 配置挂载: `.env` 文件
  - 自动等待数据库就绪
  - 健康检查: 每 30 秒检测一次

### 重要文件说明

| 文件 | 说明 | 是否必需 |
|------|------|---------|
| `.env` | 环境配置（API Key、数据库密码等） | ✅ 必需 |
| `docker-compose.yml` | 服务编排配置 | ✅ 必需 |
| `backend/scripts/init_postgres.sql` | PostgreSQL 扩展安装脚本 | ✅ 自动挂载 |
| `backend/embedding/models--*/` | Embedding 模型文件 | ⚠️ 自建需要 |

### 常用命令

```bash
# 构建并启动服务
docker-compose build
docker-compose up -d

# 查看状态
docker-compose ps

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down

# 重启服务
docker-compose restart

# 查看资源使用
docker stats
```

### 数据持久化

- `./postgres_data` - PostgreSQL 数据库文件
- `./logs` - 应用日志文件

### 端口配置

修改 `docker-compose.yml` 中的端口映射：

```yaml
ports:
  - "8800:8008"  # 宿主机:容器
```

## 📁 项目结构

```
AIFictionForge/
├── backend/                 # 后端服务
│   ├── app/
│   │   ├── api/            # API 路由
│   │   ├── models/         # 数据模型
│   │   ├── services/       # 业务逻辑
│   │   ├── middleware/     # 中间件
│   │   ├── database.py     # 数据库连接
│   │   └── main.py         # 应用入口
│   ├── scripts/            # 工具脚本
│   └── requirements.txt    # Python 依赖
├── frontend/               # 前端应用
│   ├── src/
│   │   ├── pages/         # 页面组件
│   │   ├── components/    # 通用组件
│   │   ├── services/      # API 服务
│   │   └── store/         # 状态管理
│   └── package.json
├── docker-compose.yml      # Docker Compose 配置
├── Dockerfile             # Docker 镜像构建
└── README.md
```

## 🛠️ 技术栈

**后端**: FastAPI • PostgreSQL • SQLAlchemy • OpenAI/Claude/Gemini SDK

**前端**: React 18 • TypeScript • Ant Design • Zustand • Vite

## 📖 使用指南

1. **登录系统** - 使用本地账户或 LinuxDO 账户
2. **创建项目** - 选择"使用向导创建"
3. **AI 生成** - 输入基本信息，AI 自动生成大纲和角色
4. **编辑完善** - 管理角色关系，生成和编辑章节

### API 文档

- Swagger UI: `http://localhost:8008/docs`
- ReDoc: `http://localhost:8008/redoc`

## 📝 许可证

本项目采用 [GNU General Public License v3.0](LICENSE)

**GPL v3 意味着：**
- ✅ 可自由使用、修改和分发
- ✅ 可用于商业目的
- 📝 必须开源修改版本
- 📝 必须保留原作者版权
- 📝 衍生作品必须使用 GPL v3 协议

## 🙏 致谢

- 上游项目 [xiamuceer-j/MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel) 及所有贡献者
- [FastAPI](https://fastapi.tiangolo.com/) - Python Web 框架
- [React](https://react.dev/) - 前端框架
- [Ant Design](https://ant.design/) - UI 组件库
- [PostgreSQL](https://www.postgresql.org/) - 数据库
