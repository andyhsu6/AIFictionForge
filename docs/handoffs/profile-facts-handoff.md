# 交接：档案结构化事实（issue #190 后续遗留）

> 写于 2026-10-30。上一轮已合并 PR #196 / #197 / #200，issue #190 已关闭；本文件只覆盖**未完成项**。
> 阅读顺序：先看「现状」和「环境约定」，再挑一个遗留项开工。

## 一、已经交付的东西（不要重做）

| 层 | 落点 |
|---|---|
| 事实校验 | `backend/app/services/profile_facts.py`：引文逐字存在校验、跨章纠正章号、逐字段限额、渲染、合并 |
| 字段规则单一来源 | `backend/app/services/profile_prompt_rules.py`：`TRAIT_FORM_RULE` / `ORG_TRAIT_RULE` / `ORG_APPEARANCE_RULE` / `FACTS_FIELD_BOUNDARIES` / `PROSE_FIELD_BOUNDARIES` / `PROSE_TRAIT_HINT`，被 7 处提示词拼接 |
| 提示词 | `prompt_service.py` 的 `CHARACTERS_BATCH_GENERATION_FACTS`、`PROFILE_BACKFILL_FACTS` + `PROFILE_BACKFILL_SYSTEM` |
| 写入 | 拆书导入 `book_import_service.py`、章节同步 `character_profile_sync_service.py`（facts 分支 + 回退旧格式） |
| 存储 | `characters.profile_facts` / `profile_facts_updated_at`，迁移 `alembic/{sqlite,postgres}/versions/20261029_1200_mu_profile_facts*.py` |
| 写入策略 | 新值为空绝不覆盖；`ai_profile_snapshot` 不符判为用户手改并保留；跨窗口用 `merge_profile_facts` 合并 |
| API | `CharacterResponse.profile_facts`（含 `profile_facts_updated_at`）；`OrganizationDetailResponse` 同名字段 |
| UI | `frontend/src/components/ProfileSourceModal.tsx`（共享）；`CharacterCard.tsx` 操作行入口；`Organizations.tsx` 抽屉三字段 + 入口 |
| 工具 | `backend/scripts/{dry_run_profile_facts,sweep_profiles_readonly,backfill_profiles,eval_golden_profile_facts}.py` |
| 测试 | `tests/test_profile_facts.py`、`test_profile_prompt_rules.py`、`test_book_import_profile_quality.py`、`test_character_profile_sync.py` 里的 facts 分支 |

**当前本机数据**：`characters` 共 118 行（98 人 + 20 组织），**113 行有 `profile_facts`**；角色性格文本平均 19.6 字、最长 67 字；三字段全空的实体为 0。

**规则意图（改规则前必须理解）**：claim 必须是能形容人的短语（自检「他是……」读得通），一条一个特质、不带行为尾巴；不写未发生的事；组织的性格只写行事风格/价值取向，外在表现只写看得见的建筑/标识/制服。

## 二、环境与约定（踩过的坑，照着做）

- **仓库**：`/Users/andyhsu/codehouse/AIFictionForge`，分支 `main`，remote `andy` = `https://github.com/andyhsu6/AIFictionForge.git`（`origin` 是上游，别推）。
- **改动一律走 PR**：分支名 `<type>/<简述>`，PR 标题 `<type>: <摘要>`，正文用 `refs #N`（能自动关闭才用 `closes`）。issue/PR 标题英文，正文可双语。
- **原文脱敏硬约束**：书名、角色名、章节正文**一律不准**出现在 issue/PR/commit 里；用「一本书」「某角色」这类描述代替。证据留在 `.omo/`（gitignore）。
- **服务**：`/Users/andyhsu/codehouse/AIFictionForge/aistoryforge.sh {status,start,stop,restart,verify}`，**必须用绝对路径**调用。后端 8008，前端 5173；单端口构建入口是 8008（`cd frontend && npm run build` 产物到 `backend/static/`）。
- **测试**：
  - 后端 `PYTHONPATH=backend backend/.venv/bin/python -m pytest tests/<file> -q`（不要跑全量，超过 15 分钟）
  - 前端 `cd frontend && npm test -- --run`、`npx eslint src --max-warnings 0`
- **i18n 门禁（踩过一次）**：改 UI 文案后必须 `npm run i18n:extract && npm run i18n:types`，否则 CI 报 "no key differences — only formatting or ordering changes"。
- **浏览器验证**：playwright-core 在 `.omo/pw-tools/node_modules`，`executablePath` 指向 `/Applications/Brave Browser.app/...`，**禁止安装浏览器**。登录态造法：
  ```bash
  cd backend && PYTHONPATH=$PWD .venv/bin/python -c "from app.security import create_session_token; print(create_session_token('local_21232f297a57a5a7', 900))"
  ```
  注入 cookie `session_token`（domain `localhost`）。角色页路由 `/project/02401f00-ff2b-4e81-872c-0724c3d30bcf/characters`，组织页 `.../organizations`。
- **代理**：`127.0.0.1:10808` 时好时坏。跑模型脚本时用
  `env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY -u https_proxy -u http_proxy -u all_proxy NO_PROXY='*'`。
- **模型脚本调用模板**：
  ```bash
  env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY -u https_proxy -u http_proxy -u all_proxy NO_PROXY='*' \
  DATABASE_URL="sqlite+aiosqlite:////Users/andyhsu/codehouse/AIFictionForge/backend/data/mumuai_novel.db" \
  PYTHONPATH=/Users/andyhsu/codehouse/AIFictionForge/backend \
  ONNX_EMBEDDING_MODEL_DIR=/Users/andyhsu/codehouse/AIFictionForge/backend/embedding/onnx \
  backend/.venv/bin/python backend/scripts/backfill_profiles.py --user-id local_21232f297a57a5a7 --out-dir /tmp/x
  ```
  不加 `--apply` 即 dry-run（只读）。
- **迁移**：`python -m alembic -c alembic-sqlite.ini upgrade head`（`.venv/bin/alembic` 的 shebang 坏了，用 `python -m`）。DATABASE_URL 需 `sqlite+aiosqlite:////绝对路径`。

## 三、遗留项（按建议顺序）

### 1. 实体别名映射（影响 5/118）

**现象**：档案只能填给「名字在自己项目正文里逐字出现」的实体。库里 5 个实体拿不到事实：3 个是正式名与正文短称不一致（机构全称 vs 简称这类），1 个正文只用亲属称谓指代，1 个疑似旧导入凭空生成的实体（正文完全不存在）。

**证据**：`scripts/backfill_profiles.py` 的 `--only-empty` dry-run 报告里这些实体状态是 `not_in_text`。

**建议做法**：
1. 先做只读探测脚本：对每个 `not_in_text` 实体，统计正文里名字的子串/去尾字/称谓匹配情况，输出候选别名与命中章数（人工确认，不自动落地）。
2. 落地一张 `character_aliases(character_id, alias)` 表（或复用现有 relationship/别名机制，先查 `app/models/` 有没有现成字段）。
3. 改 `sweep_profiles_readonly.py` / `backfill_profiles.py` 的窗口匹配函数，把别名一并参与匹配；**提示词里传入的 `profiles` 也要带上 known aliases**，否则模型仍会写成主名。
4. 验收：dry-run 的 `not_in_text` 数量从 5 降到 ≤2，且没有把不同角色错配（抽 5 条人工核）。

**风险**：别名误配会把 A 的事实写进 B —— 这是最严重的错误类型。规则建议：**只在项目内唯一命中时接受别名**（`character_profile_sync_service._match_character` 已有同款保守逻辑，可复用）。

### 2. Paraphrase 去重（影响 1 例已知）

**现象**：`merge_profile_facts` 按 `normalize_quote(claim)` 精确去重，同一特征换种说法会并存。已知 1 个实体的 appearance 里同一件外套被描述两次。

**建议做法**：在 `render_profile` 之前加一层相似度合并，用本机已缓存的 ONNX embedding（`backend/embedding/onnx/`）。阈值要先量：拿现有 113 个实体的 facts 跑一遍，看 0.9 / 0.92 / 0.95 三档各合并多少条、误合并多少条（人工抽检 20 条）。

**验收**：同一字段内不再出现语义重复；误合并率 ≤5%（人工抽检）；`render_profile` 的确定性单测仍全绿。

**注意**：不要为了去重引入随机性或外部服务调用，`profile_facts.py` 目前是纯函数层，加 embedding 会破坏这个性质——建议放到渲染前的独立步骤或 service 层。

### 3. 分类错填率重测（issue #198，需要人工标注）

**为什么必须人工**：`verdict` 只回答「这段文字是否属于这个字段」，三个轴（字段分类 / 表达形态 / 主体归属）不可混算。

**步骤**：
1. 用 `scripts/dry_run_profile_facts.py` 生成新样本（约 60–80 条，覆盖人和组织）。
2. 做成勾选页（上一轮有个可复用的单文件 HTML 生成方式，见 `.omo/evidence/golden/label.html` 与其 README），只填 `verdict`（通过/不通过/拿不准）。
3. 跑 `scripts/eval_golden_profile_facts.py <标注文件>` 得分类错误率，目标 ≤5%。
4. 结果写进 issue #198，标注文件留在 `.omo/`（含原文，禁止入库）。

### 4. 回填的 UI 入口（产品向）

**现状**：只能跑脚本；用户无法自助重生成，也看不到改动前后的 diff。

**建议**：在角色/组织管理页加一个「重算档案」入口 → 先跑 dry-run（复用 `backfill_profiles.py` 的逻辑抽成 service）→ 展示 plan（字段级 旧值/新值/动作）→ 用户确认后 apply。策略必须与脚本一致：空值不覆盖、手改保留、写 `ai_profile_snapshot`。

**验收**：dry-run 不产生任何写操作（跑前后 `SELECT COUNT(*), MAX(updated_at) FROM characters` 一致）；apply 后 `profile_facts_updated_at` 更新且三字段不为空；UI 上能看到 diff。

### 5. Postgres 迁移未验证

`alembic/postgres/versions/20261029_1200_mu_profile_facts_pg.py` 从未在真实 Postgres 上跑过（SQLite 已验 upgrade/downgrade）。需要起一个临时 pg 容器验证 `upgrade head` 与 `downgrade -1`，确认 `batch_alter_table` 在 pg 方言下的行为（该迁移用了 batch 模式，pg 上是直通，通常没问题，但没证据）。

### 6. wizard 路径（低优先，可能无需处理）

`backend/app/api/wizard_stream.py:772` 仍用 `CHARACTERS_BATCH_GENERATION` 旧模板。**结构性原因**：创建项目时还没有章节正文，没有引文可引用，facts 模式无法工作。除非 wizard 将来能拿到源文本，否则保持现状，PR 里已有说明。

## 四、已知不可解（不要再重复尝试）

**主体归属**：引文存在 ≠ 这句话是关于该实体的。已实测两种确定性补丁都不可用：

| 补丁 | 误杀 |
|---|---|
| 引文必须含实体名 | 18–20%（大量引文用代词/称谓） |
| 引文 ±200 字窗口必须含实体名 | 16%，且对以头衔称呼的角色几乎全灭 |

现有缓解手段：提示词把该实体现有档案一起喂给模型（显著降低串人）、保留溯源让错误可见、写入策略避免清空。若将来要真正解决，只能上 LLM 判定，但必须先在标注集上证明它优于现状——不要凭直觉引入。

## 五、上手最短路径

```bash
cd /Users/andyhsu/codehouse/AIFictionForge
./aistoryforge.sh status                    # 服务在不在
git log --oneline -5                        # 确认在 main 且包含 #200
PYTHONPATH=backend backend/.venv/bin/python -m pytest backend/tests/test_profile_facts.py backend/tests/test_profile_prompt_rules.py -q
sqlite3 backend/data/mumuai_novel.db "SELECT COUNT(*), COUNT(profile_facts) FROM characters;"   # 期望 118 / 113
```

然后挑第三节的任意一项，先写**只读探测**，拿到数字再改代码。
