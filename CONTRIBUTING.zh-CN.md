# 参与贡献 AIFictionForge（CONTRIBUTING 中文版）

感谢你有兴趣参与贡献！AIFictionForge（灵创）是一个 AI 辅助小说创作工具，后端为 FastAPI（Python 3.12），前端为 React 18 + Vite。本项目衍生自 [MuMuAINovel](https://github.com/xiamuceer-j/MuMuAINovel)，采用 GPL-3.0 许可证。

**协作语言规范（国际化）：** issue 与 PR 的**标题默认英文**；正文可中英双语（英文为主，中文作补充）。文档文件名避免使用中文，便于 CI 与工具链直接阅读。UI 支持两种语言：`zh`（产品默认语言）与 `en`。

## 贡献方式

- 通过 issue 模板（`.github/ISSUE_TEMPLATE/`）报告 bug 或提出功能需求。
- 完善翻译（`en` 语言包最需要帮助）。
- 通过 Pull Request 修复 bug 或实现功能。
- 改进文档（英文或中文）。

## 开发环境搭建

本地开发使用 **SQLite**（`backend/data/mumuai_novel.db`）；PostgreSQL 仅用于 Docker 部署。

### 后端（FastAPI，端口 8008）

```bash
# Python 3.12 虚拟环境位于 backend/.venv
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

后端**必须从仓库根目录以 `PYTHONPATH=backend` 启动**，否则 pydantic 读不到根目录 `.env`：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m uvicorn app.main:app --port 8008
```

配置仓库根目录的 `.env`（如有 `.env.example` 可复制）。`APP_PORT` 默认 8008。

### 前端（Vite dev server，端口 5173）

```bash
cd frontend
npm install
npm run dev
```

`vite.config.ts` 将 `/api` 与 `/generated-assets` 代理到 `http://localhost:8008`。

### 服务管理脚本

仓库根目录提供 `./aistoryforge.sh`，管理前后端两个服务：

```bash
./aistoryforge.sh status              # 查看两个服务状态（PID + 健康检查）
./aistoryforge.sh start               # 启动全部（后端 + 前端）
./aistoryforge.sh stop                # 停止全部
./aistoryforge.sh restart             # 重启全部
./aistoryforge.sh restart backend     # 仅重启后端
./aistoryforge.sh start frontend      # 仅启动前端
```

- 日志：`/tmp/aistoryforge-backend.log` 与 `/tmp/aistoryforge-frontend.log`。
- 后端健康检查：`curl http://localhost:8008/health` 应返回 `{"status":"ok"}`。

### 运行测试

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
```

（pytest 仅安装在 venv 中，刻意不写入 `requirements.txt`。）

## 国际化（i18n）贡献规范

所有面向用户的 UI 文案都必须走翻译文件——**禁止在组件中硬编码字符串**。

- 语言包：`frontend/src/locales/zh/<namespace>.json` 与 `frontend/src/locales/en/<namespace>.json`。`zh` 是主语言（产品默认），新键先在 `zh` 中落地。
- 新增 namespace 必须在 `frontend/src/i18n/resources.ts` 中注册。
- 提取/配置位于 `frontend/i18next.config.ts`（i18next CLI）；TypeScript 类型生成到 `src/types/i18next.d.ts`。
- **每个新增 UI 键必须同时添加到 `zh` 与 `en` 两个语言包。** CI 中的键完整性检查会在两份语言包不同步时使构建失败。
- 翻译时请保持占位符（如 `{{count}}`）与插值语法不变。

## Git 工作流

### 分支

分支名必须描述改动内容：`<type>/<简述>`，如 `fix/524-streaming-timeout`、`feat/chunk-first-principle`。type 取 `fix`、`feat`、`docs`、`refactor`、`chore`。禁止使用无信息量的通用名（`dev`、`test`、`work`、`local-works` 等）。

### 提交

使用 conventional commit 风格的提交信息，并引用对应 issue：

```
fix: streaming JSON generation in call_with_json_retry closes #9
feat(skill-chat): skill management page refs #21
```

### 什么改动必须走 PR？

任何**重大改动**——核心架构、数据库 schema、鉴权/安全、跨模块接口，或影响用户数据/体验的改动——都必须创建 feature 分支、推送远程并提交 PR 评审，不得直接提交到主干。不确定是否属于重大改动时，先提 PR（或在 issue 中询问）确认。

### Issue 跟踪

- Bug：使用 Bug report 模板；修复提交引用 issue（`closes #N`），验证通过后关闭。
- 功能需求：使用 Feature request 模板；计划确定后把要点同步到 issue 评论区，提交时引用 issue，验收后关闭。
- 批量/历史同步：按功能模块合并建 issue，而不是每个 commit 一个 issue。

## 数据隐私硬约束（原文数据脱敏）

仓库是公开的，用户导入的书可能涉及版权。**禁止推送到 GitHub**：用户导入的书/文档原文、章节正文摘录、角色人名、世界观专名。

- 在 issue、PR、commit 与文档中使用描述性措辞替代（如"a real-book re-import"、"protagonist"、"character names redacted"）。
- 含原文内容的验收证据（截图、报告、摘要）一律存放在本地已 gitignore 的目录（`.omo/`、`docs/`）。

## PR 礼仪

- 保持 PR 聚焦：一个 PR 一个逻辑改动。
- 标题格式：`<type>: <摘要>`（英文，如 `fix: streaming timeout in chapter generation`）。
- 填写 PR 模板（摘要、改动、测试计划、检查清单）。
- 关联相关 issue；说明改动如何测试。
- 确认 diff 中不含密钥、`.env` 内容或用户/原文数据。
- 影响用户可见行为的改动请同步更新文档与 CHANGELOG。

## 报告安全问题

请**不要**通过公开 issue 报告安全漏洞。私下报告方式见 [SECURITY.md](SECURITY.md)。

## 行为准则

参与贡献即表示你同意遵守[行为准则](CODE_OF_CONDUCT.md)。
