# AIFictionForge 项目约定

## 项目定位
- 项目使用 Git 协作开发，远程 PR 用于重大改动评审。

## 提交规则
- **重大改动必须提交 PR**：涉及核心架构、数据库 schema、鉴权/安全、跨模块接口或影响用户数据/体验的大改动，不得直接提交或推送主干；应创建 feature 分支、推送远程并提交 PR，评审通过后再合并。
- 不确定是否属于重大改动时，先询问用户确认。
- **分支命名规范**：分支名必须**描述改动内容**，格式 `<type>/<简述>`（如 `fix/524-streaming-timeout`、`feat/chunk-first-principle`、`docs/issue-sync-convention`），type 用 `fix`/`feat`/`docs`/`refactor`/`chore` 等；**禁止**使用无信息量的通用名（如 `local-works`、`dev`、`test`、`work`）。
- **PR 命名规范**：PR 标题与分支名都要反映改动内容，标题格式 `<type>: <改动摘要>`（如 `fix: 拆书 524 超时修复`）。
- **协作语言规范（国际化）**：issue 与 PR 的**标题默认英文**（面向 OpenAI 开源项目申请与国际贡献者），正文可中英双语（英文为主，中文作补充说明）。示例标题：`[Bug] 524 timeout when generating characters in book import`、`fix: streaming JSON generation in call_with_json_retry`。

## Issues 同步规范（全流程自动同步）
- **需求（feature）**：需求明确后在 `andyhsu6/AIFictionForge` 创建 issue（`[需求]` 前缀，label `enhancement`），描述背景、期望功能与实现思路；计划确定后把计划要点同步到 issue 评论区；实施中每个 commit 引用 issue 编号（如 `fix: ... closes #21` 或 `feat(...): ... refs #21`）；验收确认后更新 issue 状态（验收结果/PR 链接）并关闭。
- **Bug**：发现 bug 时先创建 issue（`[Bug]` 前缀，label `bug`）记录复现步骤、预期/实际行为与环境；修复提交时在 commit message 中引用 issue 编号（如 `fix: ... closes #9`），完成后关闭 issue。
- 批量/历史同步：按功能主题合并建 issue，颗粒度到功能模块而非单个提交。
- 使用仓库内的中文模板：`.github/ISSUE_TEMPLATE/bug_report.md`、`feature_request.md`。

## 文档目录约定
- 规划产物默认存 `.omo/`（plans/reports/drafts，已 gitignore）；执行时把对外可见的规划文档同步一份到 `docs/`。
- 文档文件名避免中文，使用英文或双语文件名，便于 CI/工具链/IDE 直接阅读。

## 原文数据脱敏（版权与隐私）
- **禁止**将用户导入的书/文档原文、章节正文摘录、角色人名、世界观专名等原文数据推送到 GitHub（仓库公开，且可能涉及版权）。
- 涉及原文内容的验收证据（截图、报告、摘要、角色名单）一律存本地 `.omo/`/`docs/`（gitignore），**不入库、不上传**。
- issue/PR 正文与评论中提及验收时，用**描述性措辞**替代具体原文数据（如"真实书籍 re-import"、"protagonist"、"角色名 redacted"），不出现书名、人名、原文片段。
- 本规范为硬约束：任何 issue/PR/commit/文档中出现原文数据，视为违规，需立即脱敏修正。规范来源与历史处置见 issue #23（`docs: source-text data desensitization convention for public GitHub`）。

## 服务管理（aistoryforge.sh）
项目根目录提供 `aistoryforge.sh` 服务管理脚本，管理后端（uvicorn:8008）与前端（vite:5173）：

```bash
cd /Users/andyhsu/codehouse/AIFictionForge

./aistoryforge.sh status              # 查看两个服务状态（PID + 健康检查）
./aistoryforge.sh start               # 启动全部（后端 + 前端）
./aistoryforge.sh stop                # 停止全部
./aistoryforge.sh restart             # 重启全部
./aistoryforge.sh restart backend     # 仅重启后端
./aistoryforge.sh start frontend      # 仅启动前端
./aistoryforge.sh stop backend        # 仅停止后端
```

- 日志：后端 `/tmp/aistoryforge-backend.log`、前端 `/tmp/aistoryforge-frontend.log`
- 后端健康检查：`curl http://localhost:8008/health` 应返回 `{"status":"ok"}`

## 环境约定
- **端口**：后端 8008（`.env` 的 `APP_PORT`；8000 被本机 SillyTavern 占用）、前端 5173（Vite dev，代理指向 8008）
- **数据库**：SQLite `backend/data/mumuai_novel.db`（本地开发不用 PostgreSQL）
- **后端启动**：必须从项目根目录以 `PYTHONPATH=backend` 启动（否则 pydantic 读不到根目录 `.env`）；venv 在 `backend/.venv`（Python 3.12）
- **前端**：`frontend/` 下 `npm run dev`；`vite.config.ts` 代理 `/api` 与 `/generated-assets` 到 8008
- **测试**：`cd backend && .venv/bin/python -m pytest tests/ -v`（pytest 仅装在 venv，不进 requirements.txt）
- **Embedding 模型**：已缓存于 `backend/embedding/onnx/`，启动自动加载

## 工作偏好
- **不要反复询问**：服务管理（启动/停止/重启/状态）、本地提交、常规运维操作直接执行，不要每次征求同意。
- 涉及删除数据、修改 `.env`、改动数据库 schema 等破坏性操作时仍需确认。

## 超长内容处理约定（拆分优先原则）
- **默认拆分多次处理**：任何输入/输出过大过长（书/章节/提示词/上下文/JSON 输出等）时，默认优先考虑**拆分多次传输/处理**（分批、分段、多轮喂入），而不是截断、丢弃或整体失败。
- **截断/丢弃需论证**：只有拆分多次处理会带来明确负面影响（如破坏语义完整性、引入一致性偏差、成本不可接受、延迟不可容忍）时，才允许降级为截断/摘要/丢弃，且必须说明理由。
- **拆分方案优先保全信息**：拆分的目的是保留全部信息；截断/摘要意味着信息损失，属于最后手段。
- **超大内容（书/长文档）**：按"模型窗口 + 内容总量"综合判断注入/处理策略（全量 → 尾部加权 → 拆分多次传输），单章过长同样适用拆分，不做单章硬截断。
- 实现前先评估拆分的负面影响（轮次增加带来的成本/延迟/上下文一致性），评估结果记录在对应 issue。
