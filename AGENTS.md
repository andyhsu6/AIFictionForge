# AIFictionForge 项目约定

> 根目录 `GEMINI.md` 软链接指向本文件，所有 Agent 客户端共用同一份规范。

## 0. 开工与收尾硬门禁（必读）

**Start Gate（动手改代码前）**
1. 检查分支与工作区：`git -C <checkout> status` / `branch`。若当前分支属于其他主题或已有无关未提交改动，**先隔离**（切新分支/worktree 或提醒用户），严禁把多任务改动混在同一个脏分支上。
2. 判断是否走 Issue + PR（满足任一条即触发，**无需询问**）：
   - 改动 ≥ 3 个代码文件，或涉及跨模块/前后端联动；
   - 改动数据库 schema、Prompt 模板、核心导入/生成/鉴权链路；
   - 影响用户数据或 UI 交互流程。
3. 接手 `docs/handoffs/` 计划文档执行任务时，第一步先核对是否已在 `andyhsu6/AIFictionForge` 建好对应 Issue 与 `<type>/<简述>` 分支；未建则先补齐再编码。

**Done Gate（宣称完成或开 PR 前）**
1. 跑目标后端单测（`ONNX_EMBEDDING_OFFLINE=1`）与前端测试；若改了前端，必须执行 `cd frontend && npm run build` 同步 `backend/static/`。
2. 至少跑通一条真实端到端路径（API 或 UI）；UI 改动必须先 `/Users/andyhsu/codehouse/AIFictionForge/aistoryforge.sh verify` 再用本机 Brave 实际操作控件并截图留证（存 `.omo/evidence/`）。
3. 原文数据脱敏自查：确认 commit、Issue、PR、文档中不含任何书名、角色名、世界观专名或正文摘录。
4. 推送远程（`andy`）开 PR，正文写 `closes #N` 与实测证据，核对 Linked issues 生效。

---

## 1. Git、Issues 与 PR 同步规范（四同步点）

- **分支命名**：`<type>/<简述>`（`fix`/`feat`/`docs`/`refactor`/`chore`，如 `feat/book-import-career-on-demand`），禁止 `dev`/`test`/`work`/`local-works` 等泛名。
- **PR 命名与语言**：标题 `<type>: <中文摘要>`；Issue 与 PR 标题、正文、评论**直接使用中文**；前端界面文案仍保持中英双语 i18n。
- **安全与隔离**：重大改动禁止直接提交主干；所有 git 操作一律 `git -C <checkout绝对路径>`（防止多 worktree 下 cwd 漂移），提交后 `git -C ... log --oneline -1` 复核落点。
- **四同步点**（其余不额外同步；commit 只写 `refs #N`；阶段用 label `status:planned` → `status:in-progress` → `status:verified`，不靠关闭/重开 issue 表达阶段）：
  1. **计划定稿 → 建 Issue**：有可执行拆分与验收标准后，在 `andyhsu6/AIFictionForge` 建中文 Issue（`[Feature]` + `enhancement` + `status:planned`，或 `[Bug]` + `bug`，使用 `.github/ISSUE_TEMPLATE/`），计划要点同步为 Issue 评论。
  2. **开工 → Draft PR（条件触发）**：多会话/多天任务或重大改动，首个非空 commit 后即推分支开 Draft PR（正文 `refs #N`，label `status:in-progress`）；单会话小改动可实现完直接开 Ready PR。
  3. **实现完成 → PR 转 Ready**：单测与端到端验证通过后转 Ready，PR **正文**必须含 `closes #N` / `fixes #N` / `resolves #N` 并附验证证据（命令、输出、截图路径）；评审结论写 PR 评论 + label（如 `status:verified`）。
  4. **合并 → Issue 自动关闭（唯一关闭点）**：禁止手动提前关闭有对应 PR 的 Issue（纯文档/运维无 PR 除外）。建 PR 后用 `gh pr view <N> --repo andyhsu6/AIFictionForge --json closingIssuesReferences` 核对关联；合并后用 `gh issue view <N> --repo andyhsu6/AIFictionForge --json state,stateReason` 核对自动关闭。

---

## 2. 原文数据脱敏与用户数据安全（硬约束）

- **公开仓库零原文**：严禁将用户导入的书稿原文、章节摘录、书名、角色名、世界观专名推送到 GitHub（含 Issue/PR/commit/文档）。验收描述一律用中性措辞（如“真实书籍重导”“主角名 redacted”），含原文的证据一律放本地 `.omo/`（gitignore）。规范来源见 issue #23。
- **不得用用户真实数据做功能验证**：验证书稿逻辑一律新建临时项目或临时库，验完即删；确需操作用户真实数据须先获明确同意并说明影响与回滚方式。
- **本地真实库清洗规范**：对 `backend/data/mumuai_novel.db` 执行数据清洗前，必须先做 `.bak*` 备份；清洗后核验受影响行数与其他数据完整性；验收完成后由用户决定保留或清理备份，严禁 `.db` / `.bak*` 入库。
- **测试/验收数据直接删、不备份**：明确为测试用途的临时库、临时导入项目、临时备份验收完直接删除。

---

## 3. 文档目录约定

- **人机协作文档放 `docs/handoffs/`**（计划、需求、交接、方案），该目录在 `.gitignore` 中放行以供索引；`.omo/` 仅放原始证据与草稿（保持忽略）。
- `docs/handoffs/` 默认不入库（`.githooks/pre-commit` 拦截）；确需入库时先彻底脱敏再 `ALLOW_HANDOFF_COMMIT=1 git commit`。
- 文档文件名使用英文或双语，避免纯中文文件名。

---

## 4. 服务管理、环境与端到端验证（硬约束）

### 服务脚本与端口
- **端口**：后端固定 **8008**（`.env` 的 `APP_PORT`；**禁止新增或回退到 8000**），前端 Vite dev **5173**（代理 `/api` 与 `/generated-assets` 到 8008）；单端口构建入口为 8008（`cd frontend && npm run build` 输出到 `backend/static/`）。
- **必须用当前 checkout 绝对路径调用服务脚本**：
  ```bash
  /Users/andyhsu/codehouse/AIFictionForge/aistoryforge.sh status|verify|start|stop|restart [backend|frontend]
  /Users/andyhsu/codehouse/AIFictionForge/aistoryforge.sh logs backend|frontend
  ```
- **身份核对**：宣称可测试或开始验收前必须先跑 `aistoryforge.sh verify`，确认 8008/5173 跑的均为本 checkout 代码（健康检查 `curl http://localhost:8008/health` 返回 `{"status":"ok"}`）。遇到端口被其他 checkout 占用时不静默顶掉（接管需显式 `--force`）。
- **改动生效**：前端由 Vite HMR 自动生效；后端 `.py` 由 uvicorn `--reload` 自动重载；改 `.env`/依赖/启动参数需 `restart`。

### 验证与浏览器约定
- **端到端验证**：任何功能/修复宣称完成或开 PR 前，必须跑相关单测 + 至少一条真实端到端路径（API 或 UI）；验证暴露的问题必须修复后重验。
- **UI 视觉与交互验证**：UI 改动禁止仅靠 grep 代码推断位置与文案，必须用本机 Brave（`/Applications/Brave Browser.app`）实际操作关键控件并断言 URL/DOM 与截图（存 `/tmp/` 或 `.omo/evidence/`）。
- **浏览器工具**：统一走 `browse` 扩展（`browse.execute` / `browse execute`），使用 `--rm` 一次性 session，**禁止** `session_adopt` 用户已登录标签页，**禁止**额外安装 Chrome/Chromium/Edge 或 Playwright 自带浏览器。仅在 `browse` 不可用时回退 `.omo/pw-tools/node_modules` 的 `playwright-core` 指向本机 Brave。

---

## 5. 超长内容处理约定（拆分优先原则）

- 输入/输出过长（书稿、章节、提示词、上下文、JSON 输出）时，默认优先**拆分多次传输/处理**（分批、分段、多轮）以保全信息；仅当拆分导致明确负面影响（破坏语义、一致性偏差、成本/延迟不可接受）时才允许降级为截断或摘要，且须在对应 Issue 说明理由。
- 超大书稿按“模型窗口 + 内容总量”分级处理（全量 → 尾部加权 → 拆分多次），避免单章硬截断丢信息。

---

## 6. i18n 与协作工作偏好

- **i18n 硬门禁**：新增 UI 文案必须同步写入 `frontend/src/locales/{zh,en}/<ns>.json`（zh 为源语言，不可留空），并执行 `npm run i18n:types` 保持 `resources.d.ts` 零 diff；后端用户可见文案统一注册在 `backend/app/core/errors.py` 的 `ERROR_REGISTRY` 并使用 `ApiError(code=...)`。CI（`.github/workflows/i18n-check.yml`）校验 extract / types / parity / `npm test`。详见 `CONTRIBUTING.md` 与 `docs/i18n.md`。
- **工作偏好**：服务管理、本地提交、常规运维与单测直接执行，不反复征求同意；涉及删除用户数据、改 `.env`、改数据库 schema 等破坏性操作时先确认。

---

## 7. 环境与架构参考

- **数据库与后端环境**：本地使用 SQLite `backend/data/mumuai_novel.db`；venv 在 `backend/.venv`（Python 3.12）；后端启动须在根目录以 `PYTHONPATH=backend` 运行；Embedding 模型已缓存于 `backend/embedding/onnx/`。
- **后端测试命令**：`cd backend && ONNX_EMBEDDING_OFFLINE=1 PYTHONPATH=. .venv/bin/python -m pytest tests/<目标测试>.py -v`（全量 `tests/` 含 130+ 文件且部分加载 457MB embedding，开发期优先跑关联测试与 `.github/workflows/backend-tests.yml` 核心集）。
- **配置与文档守卫**：`backend/tests/test_docs_model_requirements.py` 校验 README、`.env.example` 与 `settings.json`（不得推荐具体模型名、不得保留未注释 `DEFAULT_MODEL=`、900,000 上下文下限需与代码一致）。
- **i18n 语言解析链**：UI 未登录按 `localStorage → navigator`，登录后服务器偏好覆盖本地（跳转前 `await syncLanguageWithServer()`）；AI 生成语言由 `backend/app/services/language_resolver.py` 控制（`per-generation > 用户设置 > UI 语言 > zh`）；种子目录（预设风格、模板标题、Skill 条目）属内容数据而非界面 chrome。
