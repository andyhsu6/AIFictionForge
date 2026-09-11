"""ApiError 基建：统一错误码 registry + {detail, code, params[, raw]} envelope + 全局异常 handler。

契约（i18n plan todo 4 + task 14a）：
- HTTP 错误响应 body 为 {"detail": str, "code": str, "params": dict}，可选 "raw"。
  detail 保留原中文文案以兼容旧客户端；code/params 为新增结构化字段，供前端翻译。
- raw 是「原始诊断文案」双通道字段（task 14a）：code 未注册时前端只显示本地化
  通用文案，原文移入 raw 仅在调试界面展示。raw 为纯增量字段——未设置时响应
  不含该键；生产环境 500 兜底不外泄异常原文（只进日志）。
- dynamic_detail 是「动态 detail」站点的标记（todo 14）：这些站点的原文由运行时拼接产生、
  无法静态注册，因此前端不再展示 detail，而是按 code 命中 errors.json 的 `dynamic_detail`
  本地化通用文案；原文经可选 raw 通道保留（detail 仍填原中文以兼容旧客户端），raw 仅在
  调试界面展示。
- 完整 registry 在 todo 11 建立；本模块先提供机制 + 高频种子码。
"""
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logger import get_logger

logger = get_logger(__name__)

# dynamic_detail 标记：动态文案站点统一用该 code（todo 14 转换时逐站标注）
DYNAMIC_DETAIL_CODE = "dynamic_detail"

# HTTPException 未注册映射时的 fallback code
HTTP_ERROR_FALLBACK_CODE = "http_error"

# 错误码 registry: code -> (默认中文 detail, 默认 HTTP status)
# 全量码表（todo 11 CSV registry 落库，207 码 + 4 个 handler 专用码，共 211）。
# 默认 detail 即该 code 的 original_detail（静态站逐字、参数化站含 i18next 占位符
# {{param}}，参数化站点 raise 时必须显式传原文案 detail 以保证旧客户端 byte-identity）。
ERROR_REGISTRY: Dict[str, Tuple[str, int]] = {
    "auth.admin_required": ("需要管理员权限", 403),
    "auth.email_already_registered": ("该邮箱已注册", 400),
    "auth.email_not_registered": ("该邮箱尚未注册", 404),
    "auth.email_auth_disabled": ("邮箱认证未启用", 403),
    "auth.email_register_disabled": ("邮箱注册未启用", 403),
    "auth.identity_missing": ("未登录或用户ID缺失", 401),
    "auth.local_login_disabled": ("本地账户登录未启用", 403),
    "auth.login_failed": ("用户名或密码错误", 401),
    "auth.oauth_params_missing": ("缺少 code 或 state 参数", 400),
    "auth.oauth_request_invalid": ("无效的 state 参数", 400),
    "auth.oauth_upstream_failed": ("第三方登录失败", 400),
    "auth.password_already_initialized": ("密码已经初始化，请使用密码修改功能", 400),
    "auth.smtp_not_configured": ("系统 SMTP 未配置完整，暂无法发送验证码", 400),
    "auth.unauthorized": ("未登录", 401),
    "auth.verification_code_expired": ("验证码已过期，请重新发送", 400),
    "auth.verification_code_required": ("请先发送验证码", 400),
    "auth.verification_code_wrong": ("验证码错误", 400),
    "conflict.agent_modification_state": ("该修改已处理或正在执行", 409),
    "conflict.agent_preview_stale": ("数据已发生变化，差异预览已刷新，请重新确认", 409),
    "conflict.agent_tool_unavailable": ("工具已不再可用", 409),
    "conflict.career_in_use": ("该职业被{{usage_count}}个角色使用，无法删除。请先移除角色的职业关联。", 400),
    "conflict.chapter_order_exists": ("第{{order_index}}章已存在，不能重复创建", 400),
    "conflict.cover_exists": ("当前项目已存在封面，如需覆盖请传入 overwrite=true", 400),
    "conflict.cover_generating": ("封面正在生成中，请勿重复提交", 409),
    "conflict.plugin_name_exists": ("插件名已存在: {{plugin_name}}", 400),
    "conflict.relationship_type_duplicate": ("同项目已存在同名关系类型", 409),
    "conflict.relationship_type_in_use": ("该类型仍被关系使用，无法删除", 409),
    "conflict.username_exists": ("用户名已存在", 409),
    "email.verification_code_label": ("你的验证码是：{{code}}", 200),
    "email.verification_desc_default": ("你正在进行邮箱身份验证。", 200),
    "email.verification_desc_login": ("你正在使用邮箱验证码登录 AIFictionForge。", 200),
    "email.verification_desc_register": ("欢迎注册 AIFictionForge。", 200),
    "email.verification_desc_reset_password": ("你正在重置 AIFictionForge 账号密码。", 200),
    "email.verification_ignore_notice": ("如果这不是你的操作，请忽略本邮件。", 200),
    "email.verification_subject_default": ("邮箱验证码", 200),
    "email.verification_subject_login": ("邮箱登录验证码", 200),
    "email.verification_subject_register": ("邮箱注册验证码", 200),
    "email.verification_subject_reset_password": ("重置密码验证码", 200),
    "email.verification_ttl_minutes": ("有效期：{{ttl_minutes}} 分钟", 200),
    "forbidden.last_admin_delete": ("不能删除最后一个管理员账号", 400),
    "forbidden.last_admin_revoke": ("不能取消最后一个管理员的权限", 400),
    "forbidden.other_user_cache": ("无权清理其他用户的缓存", 403),
    "forbidden.other_user_style": ("无权修改其他用户的风格", 403),
    "forbidden.preset_style_readonly": ("不能修改全局预设风格，只能修改自定义风格", 403),
    "forbidden.self_delete": ("不能删除自己的账号", 400),
    "forbidden.self_disable": ("不能禁用自己的账号", 400),
    "forbidden.system_relationship_type_readonly": ("系统预置类型不可修改", 403),
    "forbidden.task_access": ("无权访问该任务", 403),
    # ---- 拆书导入（book import）i18n 双通道码 ----
    # import.progress.*：SSE 进度通道（_notify → progress_callback → message_code）
    # import.task.*：任务状态轮询通道（_set_task_state → task.status_code）
    # import.warning.*：告警对象通道（BookImportWarning.code/params）
    # 约定：message 原文案字节不变，中文模板即原文案（f-string 值 → {{param}} 占位符），
    # status 200 为语义占位（跟随既有 progress.* 约定）。
    "import.progress.careersAiGenerating": ("💼 AI正在生成职业体系...", 200),
    "import.progress.careersDone": ("💼 职业体系生成完成（{{careers}}个）", 200),
    "import.progress.careersFailed": ("⚠️ 职业体系生成失败：{{error}}，将继续后续步骤", 200),
    "import.progress.careersInitAi": ("💼 正在初始化AI服务...", 200),
    "import.progress.careersParsing": ("💼 正在解析职业数据...", 200),
    "import.progress.careersPreparingPrompt": ("💼 正在准备职业体系提示词...", 200),
    "import.progress.charactersBatch": ("👥 AI正在生成角色与组织（第 {{batch}}/{{total}} 批）...", 200),
    "import.progress.charactersDone": ("👥 角色/组织生成完成（{{entities}}个）", 200),
    "import.progress.charactersFailed": ("⚠️ 角色/组织生成失败：{{error}}", 200),
    "import.progress.charactersInitAi": ("👥 正在初始化AI服务...", 200),
    "import.progress.charactersParsing": ("👥 正在解析角色数据...", 200),
    "import.progress.charactersPreparingPrompt": ("👥 正在准备角色生成提示词...", 200),
    "import.progress.chaptersImported": ("已导入 {{chapters}} 个章节（{{words}}字）", 200),
    "import.progress.creatingProject": ("正在创建项目...", 200),
    "import.progress.doneWithFailures": ("⚠️ 导入完成，但有 {{failed_count}} 个生成步骤失败，可点击重试", 200),
    "import.progress.extractingRelationships": ("🔗 正在从原文抽取人物关系...", 200),
    "import.progress.generatingCareers": ("💼 正在生成职业体系...", 200),
    "import.progress.generatingCharacters": ("👥 正在生成角色与组织...", 200),
    "import.progress.generatingWorld": ("🌍 正在生成世界观...", 200),
    "import.progress.importingChapters": ("正在导入 {{chapters}} 个章节...", 200),
    "import.progress.importingOutlines": ("正在导入大纲...", 200),
    "import.progress.outlinesImported": ("已导入 {{outlines}} 个大纲", 200),
    "import.progress.projectCreated": ("项目创建完成", 200),
    "import.progress.relationshipsDone": ("🔗 原文关系抽取完成（{{relationships}}条关系）", 200),
    "import.progress.relationshipsFailed": ("⚠️ 原文关系抽取失败：{{error}}，将继续后续步骤", 200),
    "import.progress.retryCareers": ("🔄 正在重试职业体系生成...", 200),
    "import.progress.retryCareersDone": ("✅ 职业体系重试成功（{{careers}}个）", 200),
    "import.progress.retryCareersFailed": ("⚠️ 职业体系重试失败：{{error}}", 200),
    "import.progress.retryCharacters": ("🔄 正在重试角色与组织生成...", 200),
    "import.progress.retryCharactersDone": ("✅ 角色/组织重试成功（{{entities}}个）", 200),
    "import.progress.retryCharactersFailed": ("⚠️ 角色/组织重试失败：{{error}}", 200),
    "import.progress.retryRelationships": ("🔄 正在重试原文关系抽取...", 200),
    "import.progress.retryRelationshipsDone": ("✅ 原文关系抽取重试成功", 200),
    "import.progress.retryRelationshipsFailed": ("⚠️ 原文关系抽取重试失败：{{error}}", 200),
    "import.progress.retryWorld": ("🔄 正在重试世界观生成...", 200),
    "import.progress.retryWorldDone": ("✅ 世界观重试成功", 200),
    "import.progress.retryWorldFailed": ("⚠️ 世界观重试失败：{{error}}", 200),
    "import.progress.savingDb": ("正在保存到数据库...", 200),
    "import.progress.savedDb": ("数据保存完成", 200),
    "import.progress.worldAiGenerating": ("🌍 AI正在生成世界观...", 200),
    "import.progress.worldDone": ("🌍 世界观生成完成", 200),
    "import.progress.worldFailed": ("⚠️ 世界观生成失败：{{error}}，将继续后续步骤", 200),
    "import.progress.worldInitAi": ("🌍 正在初始化AI服务...", 200),
    "import.progress.worldParsing": ("🌍 正在解析世界观数据...", 200),
    "import.progress.worldPreparingPrompt": ("🌍 正在准备世界观提示词...", 200),
    "import.progress.worldWritten": ("🌍 世界观写入完成", 200),
    "import.task.aiAnalyzing": ("AI正在分析文本内容...", 200),
    "import.task.aiDetectingTheme": ("AI正在识别故事主题与类型...", 200),
    "import.task.aiGeneratingDescription": ("AI正在生成项目简介...", 200),
    "import.task.aiInferringPerspective": ("AI正在推断叙事角度...", 200),
    "import.task.aiOutlinesFailedFallback": ("AI大纲生成失败，使用规则大纲", 200),
    "import.task.aiProjectDone": ("AI生成完成，正在整理项目信息...", 200),
    "import.task.aiProjectFailedFallback": ("AI生成失败，使用规则推断项目信息", 200),
    "import.task.aiSummarizing": ("AI正在整理生成结果...", 200),
    # 按解析口径拆分：Full=整本（无标签参数）/ Tail=末N章（chapters 为数值参数）。
    # 旧合并码 import.task.chapterStructures 的 selection_label 参数携带中文运行时
    # 值（整本/末N章），会泄漏进 en 模板，故拆码；各变体 zh 模板与拆分前对应
    # 模式的原文案字节一致。
    "import.task.chapterStructuresFull": ("已处理整本 {{index}}/{{total}} 个章节结构...", 200),
    "import.task.chapterStructuresTail": ("已处理末{{chapters}}章 {{index}}/{{total}} 个章节结构...", 200),
    "import.task.chaptersDetected": ("已识别 {{chapters}} 个章节，正在构建预览结构...", 200),
    "import.task.detectEncoding": ("正在识别编码并读取文本...", 200),
    "import.task.filteringChapters": ("正在按解析配置筛选章节并构建预览...", 200),
    "import.task.initAiService": ("正在初始化AI服务...", 200),
    "import.task.outlineBatch": ("正在生成大纲批次 {{batch}}/{{total}}（第{{start}}-{{end}}章）...", 200),
    "import.task.outlinesReady": ("大纲反向生成完成，正在整理预览...", 200),
    "import.task.parseCompleted": ("解析完成，可预览并确认导入", 200),
    "import.task.parseFailed": ("解析失败", 200),
    "import.task.preparingPrompt": ("正在准备AI提示词...", 200),
    "import.task.projectSuggestionReady": ("项目信息生成完毕，准备预览...", 200),
    "import.task.reverseOutlinesStart": ("正在反向生成章节大纲（分批5章）...", 200),
    "import.task.reverseProjectSuggestion": ("正在调用AI反向生成项目信息（标题/简介/主题/类型）...", 200),
    "import.task.sampleInsufficientFallback": ("文本样本不足，使用规则推断项目信息", 200),
    "import.task.textCleaned": ("文本清洗完成（编码：{{encoding}}）", 200),
    "import.warning.applyTrimmed": ("导入阶段已按解析配置仅保留 {{kept}} 章", 200),
    "import.warning.chapterTooLong": ("章节「{{title}}」内容较长，建议确认是否应继续拆分", 200),
    "import.warning.chapterTooShort": ("章节「{{title}}」内容较短，建议检查切分结果", 200),
    "import.warning.duplicateTitles": ("检测到重复章节标题「{{title}}」共 {{occurrences}} 次", 200),
    # 按解析口径拆分（同 import.task.chapterStructures*）：Tail 为实际触发变体
    # （trim 前提即 tail 模式且选中数 <= 50），Full 仅为对称注册；kept 数值参数
    # 同时承担模板中的末章数与保留章数，zh 与拆分前原文案字节一致。
    "import.warning.filteredChaptersFull": ("已按解析配置仅保留整本 {{kept}} 章用于导入（原始识别 {{detected}} 章）", 200),
    "import.warning.filteredChaptersTail": ("已按解析配置仅保留末{{kept}}章 {{kept}} 章用于导入（原始识别 {{detected}} 章）", 200),
    "internal.agent_execution_failed": ("灵创创作助手执行失败：{{error}}", 200),
    "internal.ai_chapter_plan_failed": ("AI分析失败，未能生成章节规划", 200),
    "internal.ai_empty_response": ("AI服务返回空响应", 200),
    "internal.ai_json_unparsable": ("AI返回的内容无法解析为JSON：{{error}}", 200),
    "internal.ai_service_failed": ("AI 服务配置错误: {{error}}", 200),
    "internal.career_parse_exhausted": ("职业体系解析失败（已达最大重试次数）", 200),
    # 共享码：仅剩 wizard_stream.py 一处使用，其余已拆分（todo15）
    "internal.career_retry_exhausted": ("职业体系生成失败（AI多次返回为空）", 200),
    "internal.career_save_exhausted": ("职业体系保存失败（已达最大重试次数）", 200),
    "internal.continue_capability_insufficient": ("当前模型输出上限过低，无法支持续写", 200),
    "internal.generation_failed": ("生成失败: {{error}}", 200),
    "internal.import_preview_missing": ("预览数据不存在", 500),
    "internal.outline_continue_failed": ("续写失败: {{error}}", 200),
    "internal.outline_expand_failed": ("展开失败: {{error}}", 200),
    "internal.outline_generation_failed": ("大纲生成失败，请重试", 200),
    "internal.outline_import_failed": ("大纲导入失败，请稍后重试", 500),
    "internal.plot_analysis_failed": ("剧情分析失败", 500),
    "internal.plugin_create_failed": ("插件注册失败: {{plugin_name}}", 500),
    "internal.relationship_type_create_failed": ("关系类型创建失败", 500),
    "internal.user_id_missing_for_project": ("用户ID缺失，无法创建项目", 200),
    "not_found.agent_conversation": ("对话不存在", 404),
    "not_found.agent_tool_call": ("工具调用不存在", 404),
    "not_found.api_route": ("API路径不存在", 404),
    "not_found.batch_task": ("批量生成任务不存在", 404),
    "not_found.career": ("职业不存在", 404),
    "not_found.chapter": ("章节不存在", 404),
    "not_found.chapter_analysis": ("该章节暂无分析结果", 404),
    "not_found.character": ("角色不存在", 404),
    "not_found.character_career": ("角色职业关联不存在", 404),
    "not_found.cover": ("当前项目尚未生成可下载的封面", 404),
    "not_found.cover_file": ("封面文件路径无效，请重新生成", 404),
    "not_found.foreshadow": ("伏笔不存在", 404),
    "not_found.frontend_route": ("页面不存在", 404),
    "not_found.model_list": ("未能从 API 获取到可用的模型列表", 404),
    "not_found.organization": ("组织不存在", 404),
    "not_found.organization_member": ("成员记录不存在", 404),
    "not_found.outline": ("大纲不存在", 404),
    "not_found.outline_to_expand": ("没有找到要展开的大纲", 200),
    "not_found.plugin": ("插件不存在", 404),
    "not_found.preset": ("预设不存在", 404),
    "not_found.preset_style": ("预设风格 '{{preset_id}}' 不存在", 400),
    "not_found.project": ("项目不存在", 404),
    "not_found.project_chapters": ("没有找到章节", 404),
    "not_found.project_or_forbidden": ("项目不存在或无权访问", 404),
    "not_found.prompt_template": ("模板 {{template_key}} 不存在", 404),
    "not_found.relationship": ("关系不存在", 404),
    "not_found.relationship_character": ("角色A（ID: {{character_id}}）不存在", 404),
    "not_found.relationship_type": ("关系类型不存在", 404),
    "not_found.setting": ("设置不存在，请先创建设置", 404),
    "not_found.skill": ("未找到 Skill: {{skill_key}}", 404),
    "not_found.task": ("任务不存在", 404),
    "not_found.user": ("用户不存在", 404),
    "not_found.writing_style": ("写作风格不存在", 404),
    "progress.career_done": ("新职业生成完成！（主职业{{total_main}}个，副职业{{total_sub}}个）", 200),
    "progress.creation_done_words": ("创作和分析完成！共 {{word_count}} 字", 200),
    "progress.done": ("生成完成", 200),
    "progress.import_done": ("导入完成！", 200),
    "progress.import_retry_all_done": ("所有步骤重试成功！", 200),
    "progress.import_retry_partial": ("重试完成，仍有 {{still_failed_count}} 个步骤失败", 200),
    "progress.import_retry_start": ("开始重试失败的生成步骤...", 200),
    "progress.import_start": ("开始导入拆书数据...", 200),
    "progress.outline_batch_parse_failed": ("第{{batch_num}}批解析失败", 200),
    "progress.outline_continue_done": ("成功续写{{outline_count}}章大纲", 200),
    "progress.outline_done": ("成功生成{{outline_count}}章大纲", 200),
    "progress.outline_expand_done": ("《{{outline_title}}》展开完成（{{chapter_count}} 章）", 200),
    "progress.outline_expand_item_failed": ("❌ {{outline_title}} 展开失败: {{error}}", 200),
    "progress.outline_expand_skipped": ("《{{outline_title}}》已展开过，已跳过", 200),
    "progress.retry": ("⚠️ {{reason}}... ({{retry_count}}/{{max_retries}})", 200),
    "progress.retry_ai_failed": ("AI返回为空", 200),
    "progress.retry_json_parse": ("JSON解析失败", 200),
    "progress.retry_save_failed": ("保存失败", 200),
    "progress.skill_in_use": ("正在使用 {{template_name}}...", 200),
    "progress.start": ("开始生成...", 200),
    "progress.warning": ("⚠️ {{message}}", 200),
    "rate_limit.verification_code_attempts": ("验证码错误次数过多，请重新发送", 429),
    "rate_limit.verification_code_send": ("验证码发送过于频繁，请 {{remain_seconds}} 秒后重试", 429),
    "security.url_blocked_with_hint": ("{{reason}}。如需连接本地或 Docker 内网 LLM，请设置 ALLOW_PRIVATE_AI_ENDPOINTS=true，或把主机名加入 ALLOWED_AI_HOSTS（例如 host.docker.internal,127.0.0.1）。", 400),
    "security.url_credentials": ("URL不允许包含认证信息", 400),
    "security.url_empty": ("URL不能为空", 400),
    "security.url_host_missing": ("URL缺少主机名", 400),
    "security.url_host_unresolvable": ("URL主机名无法解析", 400),
    "security.url_loopback": ("URL不允许指向本机地址", 400),
    "security.url_private": ("URL不允许指向内网或保留地址", 400),
    "security.url_reserved": ("URL不允许指向链路本地、组播或未指定地址", 400),
    "security.url_scheme": ("仅支持 HTTP/HTTPS URL", 400),
    "task.analysis_status_line": ("第{{chapter_number}}章《{{chapter_title}}》{{status_text}}", 200),
    "task.batch_generate_failed": ("批量章节生成失败", 200),
    "task.batch_status_completed": ("已完成 {{completed}} 章", 200),
    "task.batch_status_generating": ("正在生成第 {{chapter_number}} 章 ({{current}}/{{total}})", 200),
    "task.batch_status_waiting": ("等待中，共 {{total}} 章", 200),
    "task.cancel_invalid": ("无法取消任务（不存在或已完成）", 400),
    "task.cancel_invalid_status": ("任务已处于 {{status}} 状态，无法取消", 400),
    "task.cancelled": ("任务已取消", 200),
    "task.failed": ("任务失败", 500),
    "task.not_completed": ("任务尚未完成，无法获取预览", 400),
    "task.running_mutation_blocked": ("无法删除进行中的任务，请先取消", 400),
    "validation.ai_config_missing": ("请先在「设置 → 文本模型配置」中配置模型", 400),
    "validation.book_import_extract_mode": ("extract_mode 仅支持 tail 或 full", 400),
    "validation.book_import_mode": ("import_mode 仅支持 append 或 overwrite", 400),
    "validation.book_import_new_project": ("当前仅支持新建项目导入，不支持指定 project_id", 400),
    "validation.career_stage_out_of_range": ("阶段超出范围，该职业最大阶段为{{max_stage}}", 400),
    "validation.career_sub_type_mismatch": ("该职业不是副职业类型，无法添加为副职业", 400),
    # 共享码：仅剩 careers.py 一处使用，其余已拆分（todo15）
    "validation.career_type_mismatch": ("该职业不是主职业类型，无法设置为主职业", 400),
    "validation.chapter_content_empty": ("章节内容为空", 400),
    "validation.chapter_content_empty_for_analysis": ("章节内容为空，无法分析", 400),
    "validation.chapter_content_empty_for_regenerate": ("章节内容为空，无法重新生成", 400),
    "validation.character_career_duplicate": ("该角色已拥有此副职业", 400),
    "validation.character_in_organization": ("该角色已在组织中", 400),
    "validation.characters_selected_min_one": ("请至少选择一个角色/组织", 400),
    "validation.config": ("配置数据格式错误", 500),
    "validation.content_hash_mismatch": ("章节内容已变化，请重新生成后再应用", 409),
    "validation.continue_segment_index_invalid": ("续写分段序号超出范围", 400),
    "validation.continue_segment_limit_exceeded": ("续写分段数超出上限，请降低目标字数", 400),
    "validation.continue_target_too_large": ("续写目标字数超出上限", 400),
    "validation.cover_config_incomplete": ("封面图片配置不完整，请填写 provider、api_key 和 model", 400),
    "validation.cover_provider_unsupported": ("当前版本仅支持 Gemini 或 Grok", 400),
    "validation.email_format": ("请输入有效的邮箱地址", 400),
    "validation.export_type_unsupported": ("不支持的导出类型", 404),
    "validation.file_too_large": ("文件大小超过 {{max_mb}}MB 限制", 413),
    "validation.import_json_invalid": ("JSON格式错误: {{error}}", 400),
    "validation.import_retry_steps_invalid": ("以下步骤不在失败列表中，无法重试: {{steps}}", 400),
    "validation.import_target_project_missing": ("缺少目标项目ID", 400),
    "validation.json_only": ("只支持 JSON 格式文件", 400),
    # 共享码：仅剩 users.py 一处使用，其余已拆分（todo15）
    "validation.last_admin_required": ("无法撤销管理员权限，至少需要保留一个管理员", 400),
    "validation.main_career_delete_blocked": ("无法删除主职业，只能更换", 400),
    "validation.main_career_invalid": ("主职业不存在或类型错误", 400),
    "validation.mcp_config_invalid": ("配置JSON必须包含mcpServers字段", 400),
    "validation.model_list_endpoint_unsupported": ("该 API 提供商不支持模型列表查询接口 (/models 返回 404)，请手动输入模型名称。当前请求地址: {{api_base_url}}/models", 400),
    "validation.model_list_http_error": ("无法从 API 获取模型列表 (HTTP {{status}})", 400),
    "validation.model_provider_unsupported": ("不支持的提供商: {{provider}}", 400),
    "validation.organization_detail_exists": ("该角色已有组织详情记录", 400),
    "validation.organization_type_required": ("关联的角色不是组织类型", 400),
    "validation.outline_continue_requires_existing": ("续写模式需要已有大纲", 400),
    "validation.outline_import_file_invalid": ("导入文件验证失败：{{error}}", 400),
    "validation.outline_import_mode": ("导入模式必须是 append 或 merge", 422),
    "validation.outline_mode_single_create_blocked": ("当前项目为{{outline_mode}}模式，不支持一对一创建。请使用展开功能。", 400),
    "validation.outline_mode_unsupported": ("不支持的模式: {{mode}}", 400),
    "validation.outline_plan_list_empty": ("章节规划列表不能为空", 400),
    "validation.password_too_short": ("密码长度至少为6个字符", 400),
    "validation.plugin_config_json_invalid": ("配置JSON格式错误: {{error}}", 400),
    "validation.plugin_disabled": ("插件未启用", 400),
    "validation.plugin_server_type_unsupported": ("不支持的服务器类型: {{server_type}}", 400),
    "validation.plugin_server_url_required": ("{{plugin_type}}类型插件必须提供server_url", 400),
    "validation.plugin_transport_fields_required": ("Stdio类型插件必须提供command字段", 400),
    "validation.polish_new_content_empty": ("新内容不能为空", 400),
    "validation.polish_position_invalid": ("位置参数无效", 400),
    "validation.polish_range_out_of_bounds": ("位置超出内容范围", 400),
    "validation.polish_selection_mismatch": ("选中的文本与章节内容不匹配，请刷新页面后重试", 400),
    "validation.polish_start_before_end": ("起始位置必须小于结束位置", 400),
    "validation.preset_active_delete_blocked": ("无法删除激活中的预设，请先激活其他预设", 400),
    "validation.qq_smtp_host": ("QQ 邮箱 SMTP 主机必须为 smtp.qq.com", 400),
    "validation.relationship_type_name_empty": ("关系类型名称不能为空", 422),
    "validation.required_fields": ("name 和 prompt_content 是必填字段", 400),
    "validation.self_admin_revoke": ("不能撤销自己的管理员权限", 400),
    "validation.self_password_reset": ("不能重置自己的密码，请使用修改密码功能", 400),
    "validation.smtp_fields_missing": ("请先完善 SMTP 主机、用户名和授权码", 400),
    "validation.smtp_ssl_tls_conflict": ("SSL 和 TLS 不能同时启用", 400),
    "validation.style_default_delete_blocked": ("不能删除默认风格，请先设置其他风格为默认", 400),
    "validation.sub_career_limit": ("副职业数量已达上限（最多5个）", 400),
    "validation.tail_chapter_count": ("tail_chapter_count 不能小于 5", 400),
    "validation.txt_only": ("仅支持 .txt 文件", 400),
    "validation.user_delete_blocked": ("无法删除该用户（用户不存在或为管理员）", 400),
    "validation.verification_code_format": ("请输入6位数字验证码", 400),
    "validation.verification_code_scene": ("不支持的验证码场景", 400),
    "validation.wizard_fields_missing": ("{{fields}} 是必需的参数", 400),
    # ---- handler 专用码（非 CSV registry：全局兜底与验证错误）----
    "internal.error": ("服务器内部错误", 500),
    "validation.error": ("请求参数验证失败", 422),
    "conflict.error": ("资源冲突", 409),
    DYNAMIC_DETAIL_CODE: ("", 500),
}

# (status_code, 默认detail) -> code 反查表：把存量 HTTPException 归类到已知码
_STATUS_DETAIL_TO_CODE: Dict[Tuple[int, str], str] = {
    (meta[1], meta[0]): code
    for code, meta in ERROR_REGISTRY.items()
    if code != DYNAMIC_DETAIL_CODE
}


class ApiError(Exception):
    """带错误码的 API 异常。

    Args:
        code: 错误码（registry key）。未注册的 code 允许，detail 必须显式提供。
        detail: 错误文案；省略时取 registry 默认中文 detail（向后兼容旧客户端）。
        status: HTTP 状态码；省略时取 registry 默认值，再省略则 500。
        params: 结构化参数，供前端模板化翻译（如 {"chapter_id": "..."}）。
        raw: 原始诊断文案（task 14a 双通道）。仅调试界面展示；未设置时响应
            不含 raw 键（纯增量，旧客户端与精确断言不受影响）。
    """

    def __init__(
        self,
        code: str,
        detail: Optional[str] = None,
        status: Optional[int] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: Optional[str] = None,
    ):
        self.code = code
        default_detail, default_status = ERROR_REGISTRY.get(code, ("", 500))
        self.detail = detail if detail is not None else default_detail
        self.status = status if status is not None else default_status
        self.params: Dict[str, Any] = params or {}
        self.raw = raw
        super().__init__(f"[{code}] {self.detail}")

    def to_envelope(self) -> Dict[str, Any]:
        """HTTP 响应 envelope：detail 保留旧中文文案，code/params 为结构化新字段。"""
        content: Dict[str, Any] = {"detail": self.detail, "code": self.code, "params": self.params}
        if self.raw:
            content["raw"] = self.raw
        return content


def envelope(
    detail: str,
    code: str,
    params: Optional[Dict[str, Any]] = None,
    raw: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一错误 envelope（供 handler 与非异常路径复用）；raw 真值才输出该键。"""
    content: Dict[str, Any] = {"detail": detail, "code": code, "params": params or {}}
    if raw:
        content["raw"] = raw
    return content


def code_for_http_exception(exc: HTTPException) -> str:
    """把存量 HTTPException 归类到 registry code；未命中返回 http_error fallback。"""
    detail = exc.detail if isinstance(exc.detail, str) else ""
    return _STATUS_DETAIL_TO_CODE.get((exc.status_code, detail), HTTP_ERROR_FALLBACK_CODE)


def sse_code_for_exception(exc: BaseException) -> Tuple[Optional[str], Dict[str, Any]]:
    """SSE 通道的异常 → 结构化 (error_code, params) 映射（i18n todo13 part 2）。

    - ApiError：自带 code/params，直接透传（raise 站点已在 todo12/13 part 1 码化）。
    - HTTPException：经 (status, detail) 反查 registry；未注册（http_error 兜底）
      返回 (None, {})，调用点保持旧 (detail, status) 通道，前端按 legacy 显示。
    - 其余异常：(None, {})。
    调用点负责把 error_code=None 时走旧路径；不为站点发明新 code。
    另：detail 非字符串的 HTTPException 同样返回 (None, {})；嵌套/`__cause__`
    异常不展开（动态站点由 todo14 处理）。
    """
    if isinstance(exc, ApiError):
        return exc.code, dict(exc.params or {})
    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        resolved = code_for_http_exception(exc)
        if resolved != HTTP_ERROR_FALLBACK_CODE:
            return resolved, {}
    return None, {}


def exc_status(exc: BaseException) -> int:
    """HTTP status for either HTTPException (`.status_code`) or ApiError (`.status`)."""
    return getattr(exc, "status_code", None) or getattr(exc, "status", None) or 500


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常 handler（ApiError / HTTPException / 验证错误 / 兜底）。

    handler 逻辑集中在本模块，main.py 只调用一次，便于测试复用同一装配函数。
    """

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        """ApiError → {detail, code, params} envelope。"""
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """存量 HTTPException → ApiError 化：已知 (status, detail) 归码，未知走 http_error。

        task 14a：detail 保持 byte-identical（旧契约）；未知站点的 detail 即现场
        诊断原文，随 raw 双通道下发，前端只显示本地化通用文案。
        """
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        code = code_for_http_exception(exc)
        raw = getattr(exc, "raw", None)
        if raw is None and code == HTTP_ERROR_FALLBACK_CODE:
            raw = detail
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(detail=detail, code=code, raw=raw),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """请求验证失败 → 422，复用 Pydantic 错误 type 生成 errors.validation.<type> 码。

        body.errors 保留（旧客户端兼容），并新增 envelope 字段。
        """
        errors = exc.errors()
        first_type = ""
        if errors:
            first_type = str(errors[0].get("type", ""))
        code = f"errors.validation.{first_type}" if first_type else "validation.error"
        logger.error(f"请求验证失败: {errors}")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": "请求参数验证失败",
                "code": code,
                "params": {"index": 0} if errors else {},
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """未捕获异常 → 500，code=internal.error；真实异常仅记日志，不外泄。"""
        logger.error(f"未处理的异常: {type(exc).__name__}: {str(exc)}", exc_info=True)
        detail, default_status = ERROR_REGISTRY["internal.error"]
        content = envelope(detail=detail, code="internal.error")
        if config_debug():
            # task 14a：debug 才随 raw 下发原文；生产环境原文只进日志。
            content["message"] = str(exc)  # compat alias，保留一个版本
            content["raw"] = str(exc)
        else:
            content["message"] = "请稍后重试"
        return JSONResponse(status_code=default_status, content=content)


def config_debug() -> bool:
    """debug 开关（延迟导入避免模块循环）。debug 模式下兜底 500 附带原始异常文本。"""
    try:
        from app.config import settings
        return bool(getattr(settings, "debug", False))
    except Exception:
        return False
