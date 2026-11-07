# AIFictionForge 项目约定

## 项目定位
- 项目使用 Git 协作开发，远程 PR 用于重大改动评审。

## 提交规则
- **重大改动必须提交 PR**：涉及核心架构、数据库 schema、鉴权/安全、跨模块接口或影响用户数据/体验的大改动，不得直接提交或推送主干；应创建 feature 分支、推送远程并提交 PR，评审通过后再合并。
- 不确定是否属于重大改动时，先询问用户确认。
- **分支命名规范**：分支名必须**描述改动内容**，格式 `<type>/<简述>`（如 `fix/524-streaming-timeout`、`feat/chunk-first-principle`、`docs/issue-sync-convention`），type 用 `fix`/`feat`/`docs`/`refactor`/`chore` 等；**禁止**使用无信息量的通用名（如 `local-works`、`dev`、`test`、`work`）。
- **PR 命名规范**：PR 标题与分支名都要反映改动内容，标题格式 `<type>: <改动摘要>`（如 `fix: 拆书 524 超时修复`）。
- **协作语言规范（国际化）**：issue 与 PR 的**标题默认英文**（面向国际贡献者），正文可中英双语（英文为主，中文作补充说明）。示例标题：`[Bug] 524 timeout when generating characters in book import`、`fix: streaming JSON generation in call_with_json_retry`。

## Issues / PR 同步规范（四同步点，其余不额外同步）

生命周期固定为**四个同步点**；**commit 只引用编号（`refs #N`），不写 issue/PR 评论**；评审与验收结论只写 PR 评论或打 label，**不用 issue 状态表达阶段**。

1. **计划定稿 → 建 issue 并落计划**
   - 需求明确且计划定稿后，在 `andyhsu6/AIFictionForge` 建 issue（`[Feature]` 前缀，label `enhancement` + `status:planned`），写背景、期望功能、实现思路与**验收标准**；计划要点同步为 issue 评论（计划文档默认在 `docs/handoffs/` 不入库，issue 评论是计划在 GitHub 侧的唯一载体）。
   - “计划定稿”判定：有可执行的任务拆分 + 可核对的验收标准；未定稿不得开 PR。
   - **Bug**：发现即建 issue（`[Bug]` 前缀，label `bug`），记录复现步骤、预期/实际行为与环境。
2. **开工 → Draft PR（条件触发）**
   - 涉及**多会话/多天工作**或属于重大改动（核心架构、数据库 schema、鉴权/安全、跨模块接口、影响用户数据/体验）时：第一个非空 commit 后立即推分支并开 **Draft PR**（标题 `<type>: <摘要>`，正文 `refs #N`，label `status:in-progress`），作为并行 worktree/agent 会话的工作锚点。
   - 单会话小改动不强制提前开 PR，实现完成后再开即可。
   - 计划重大变更时同步更新 PR 描述，或关闭重开，避免过期描述误导。
3. **实现完成 → PR 转 Ready**
   - 自测/视觉验证通过后转 Ready；PR **正文**必须含关闭关键词并指向 issue：`closes #N` / `fixes #N` / `resolves #N`（写在正文而非仅 commit message，确保 Linked issues 与**合并自动关闭**），Test plan 填实际验证结果。
   - 评审/验收结论（通过或需修改）写 PR 评论 + label（如 `status:verified`），不改 issue 状态。
4. **合并 → issue 自动关闭（唯一关闭点）**
   - 能由 PR 合并自动关闭的 issue，**禁止手动提前关闭**（会断时间线并使 `closes` 失效）；只有确实无对应 PR 的纯文档/运维改动才手动关闭，并在评论说明原因。
   - 建 PR 后核对关联生效：`gh pr view <N> --repo andyhsu6/AIFictionForge --json closingIssuesReferences` 应含目标 issue，issue 时间线出现 `cross-referenced`。
   - 合并后核对自动关闭：`gh issue view <N> --repo andyhsu6/AIFictionForge --json state,stateReason` 应为 `CLOSED` / `COMPLETED`；未生效立即补关联/关闭，并在 issue 评论留下 PR 链接与验收结果。

- 阶段用 label 表达：`status:planned` → `status:in-progress` → `status:verified`；PR 状态只用 GitHub 原生 Draft/Ready/Closed/Merged 表达完成度。
- 批量/历史同步：按功能主题合并建 issue，颗粒度到功能模块而非单个提交。
- 使用仓库内的 issue 模板：`.github/ISSUE_TEMPLATE/bug_report.md`、`feature_request.md`。

## 文档目录约定
- **交付给人和 agent 的文档放 `docs/handoffs/`**（计划、需求、交接、方案）。该目录已在 `.gitignore` 里显式放行，所以能被 OpenCode 的 `@` 文件检索到；**放进 `.omo/` 的文档检索不到**（索引遵循 .gitignore），`.omo/` 只放原始证据与草稿（含书稿片段，必须保持忽略）。
- `docs/handoffs/` 的文档**默认不入库**：`.githooks/pre-commit` 会拒绝提交该目录（脚本已入库，本地通过 `.git/hooks/pre-commit` 符号链接生效）。确需入库时 `ALLOW_HANDOFF_COMMIT=1 git commit`，且必须先脱敏（不得出现书名、角色名、原文片段）。
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
- **端口**：后端 8008（`.env` 的 `APP_PORT`；8000 为历史遗留默认值且被本机无关服务占用，**禁止使用**）、前端 dev 5173（Vite，代理指向 8008）；单端口构建入口 = 8008（`npm run build` 产物输出到 `backend/static/`，由后端直接伺服）
- **前端改动工作流（硬约束）**：开发/验收期间用 5173 实时预览（HMR）；确认改动无误后，在创建/更新 PR、更新 issue 或合并前，必须 `cd frontend && npm run build` 构建到 `backend/static/`，让 8008 单端口入口同步反映改动。
- **禁止新增 8000 端口引用（硬约束）**：不得再添加任何指向端口 8000 的默认值/回退/文档引用；所有新端口默认值一律为 8008（含前端 API base URL 回退）。
- **数据库**：SQLite `backend/data/mumuai_novel.db`（本地开发不用 PostgreSQL）
- **后端启动**：必须从项目根目录以 `PYTHONPATH=backend` 启动（否则 pydantic 读不到根目录 `.env`）；venv 在 `backend/.venv`（Python 3.12）
- **前端**：`frontend/` 下 `npm run dev`；`vite.config.ts` 代理 `/api` 与 `/generated-assets` 到 8008
- **测试**：`cd backend && .venv/bin/python -m pytest tests/ -v`（pytest 仅装在 venv，不进 requirements.txt）
- **改动生效规则**：前端改动由 vite HMR 自动生效（浏览器需刷新一次拿新模块）；后端 `.py` 改动由 uvicorn `--reload` 自动重载，无需手动 restart；改 `.env`、依赖或启动参数才需要完整 `restart`。测试/验收前仍必须先 `verify`。
- **Embedding 模型**：已缓存于 `backend/embedding/onnx/`，启动自动加载
- **浏览器约定（硬约束）**：本机统一使用 Brave（Chromium 内核，`/Applications/Brave Browser.app`）；**禁止**额外安装 Chrome/Chromium/Edge 等任何浏览器或 Playwright 自带浏览器二进制；浏览器自动化用 playwright(-core) 以 `executablePath` 指向本机 Brave（headless 截图/验收均如此）

## 视觉验证约定
- **UI 改动/验收指引必须先做视觉渲染验证**（playwright-core + 本机 Brave headless 截图或 DOM 文本断言），禁止仅凭 grep 代码推断 UI 位置与文案；截图存 `/tmp/` 或 `.omo/evidence/`。
- **数据与文案边界**：数据库种子目录（写作风格预设、提示词分类/模板标题、Skill 条目）属内容数据；界面 chrome 文案必须 i18n 化，目录内容本地化需单独决策。

## 服务与测试身份约定（硬约束）
- **宣称"可以测试"前必须先跑 `./aistoryforge.sh verify`**：只有两个端点都确认运行【本 checkout】代码（进程 cwd + /health 分支双核对）才允许开始测试/验收；verify 不通过禁止宣称就绪。
- **服务脚本必须用绝对路径调用**（如 `/Users/andyhsu/codehouse/aff-i18n-internationalization/aistoryforge.sh`）：shell 工作目录可能被后台任务事件重置到别的 checkout，相对路径 `./aistoryforge.sh` 会静默操作错误目录的服务。
- `start` 遇到端口被其他 checkout 占用时默认拒绝并给出指引（接管需显式 `--force`）；禁止静默顶掉其他 checkout 的服务。
- **git 操作一律 `git -C <checkout绝对路径>`**：cwd 漂移会让相对路径的 add/commit 落到别的 checkout（本仓库挂多个 worktree）；提交后用 `git -C ... log --oneline -1` 复核落点。
- **i18n 语言语义**：未登录启动顺序 = localStorage 手动选择 → navigator（浏览器语言，默认镜像操作系统语言，即"系统优先其次浏览器"的落地）；登录页切换器仅本地预览；**登录成功后服务器偏好覆盖本地**（无服务器值时才把本地选择种子化到服务器）；`syncLanguageWithServer()` 在跳转前必须 await（登录/回调所有路径），落地第一帧即服务器偏好语言。

## 工作偏好
- **不要反复询问**：服务管理（启动/停止/重启/状态）、本地提交、常规运维操作直接执行，不要每次征求同意。
- 涉及删除数据、修改 `.env`、改动数据库 schema 等破坏性操作时仍需确认。
- **测试/验收数据直接删、不备份**：明确为测试用途的数据（验收库、临时导入项目、备份文件等）验收/确认完成后直接删除，不做备份，避免浪费 token 与磁盘空间。

## 超长内容处理约定（拆分优先原则）
- **默认拆分多次处理**：任何输入/输出过大过长（书/章节/提示词/上下文/JSON 输出等）时，默认优先考虑**拆分多次传输/处理**（分批、分段、多轮喂入），而不是截断、丢弃或整体失败。
- **截断/丢弃需论证**：只有拆分多次处理会带来明确负面影响（如破坏语义完整性、引入一致性偏差、成本不可接受、延迟不可容忍）时，才允许降级为截断/摘要/丢弃，且必须说明理由。
- **拆分方案优先保全信息**：拆分的目的是保留全部信息；截断/摘要意味着信息损失，属于最后手段。
- **超大内容（书/长文档）**：按"模型窗口 + 内容总量"综合判断注入/处理策略（全量 → 尾部加权 → 拆分多次传输），单章过长同样适用拆分，不做单章硬截断。
- 实现前先评估拆分的负面影响（轮次增加带来的成本/延迟/上下文一致性），评估结果记录在对应 issue。

## i18n 约定
- **新增 UI 文案必须进 locale 文件**：`frontend/src/locales/{zh,en}/<ns>.json`（zh 是源语言，en 必须同步；空值会挂在 CI parity 门）。
- **后端用户可见文案用错误码**：`raise ApiError(code=...)`，注册表在 `backend/app/core/errors.py`；未知码前端回退本地化通用文案，原文只进调试通道（`raw`）。
- **AI 生成语言**由 `content_language` 设置链控制：per-generation 覆盖 > 用户设置 > UI 语言 > zh（解析器 `backend/app/services/language_resolver.py`）。
- **CI 门**：`.github/workflows/i18n-check.yml` 跑 extract / types / parity / `npm test` 四道检查，locale 与类型产物必须保持零 diff。
- 贡献者指南见 `CONTRIBUTING.md`，i18n 架构详情见 `docs/i18n.md`。
