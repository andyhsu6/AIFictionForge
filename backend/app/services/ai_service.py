"""AI服务封装 - 统一的AI接口

重构后支持自动MCP工具加载：
- 所有AI方法在请求前自动检查用户MCP配置
- 如果有启用的MCP插件且有可用工具，自动发送tools
- 通过 auto_mcp 参数控制是否启用自动工具加载
"""
from typing import Optional, AsyncGenerator, List, Dict, Any, Union, Callable, Tuple

from app.config import settings as app_settings
from app.core.errors import ApiError
from app.logger import get_logger
from app.services.ai_config import AIClientConfig, default_config
from app.services.ai_metrics import AICallMetrics, TokenUsage, ToolCallMetrics
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.ai_clients.anthropic_client import AnthropicClient
from app.services.ai_clients.gemini_client import GeminiClient
from app.services.ai_clients.base_client import UPSTREAM_BODY_MARKER, cleanup_all_clients
from app.services.ai_providers.openai_provider import OpenAIProvider
from app.services.ai_providers.anthropic_provider import AnthropicProvider
from app.services.ai_providers.gemini_provider import GeminiProvider
from app.services.ai_providers.base_provider import BaseAIProvider
from app.services.json_helper import clean_json_response, parse_json
from app.services.model_capability_probe import (
    TRIGGER_DAILY,
    ensure_model_allowed,
    get_effective_context_window,
)

# 导出清理函数
cleanup_http_clients = cleanup_all_clients

logger = get_logger(__name__)


def effective_base_url(api_base_url: Optional[str]) -> Optional[str]:
    """实发请求真正会用的 base_url（AIService 与结论缓存键的唯一口径）。

    缓存键是 (provider, base_url, model) 三元组，所以「存结论时算的 base_url」与
    「派发时算的 base_url」必须是同一个函数算出来的，否则缓存永远命不中，
    等价于每次派发都重新探测。
    """
    return api_base_url or app_settings.openai_base_url


def normalize_provider(provider: Optional[str]) -> Optional[str]:
    """标准化 provider 名称，兼容 OpenAI 格式渠道别名。

    内置适配器（例如 Xiaomi MiMo）应在 API 层解析为底层兼容 provider，
    AIService 只接收可直接初始化的 provider。
    """
    if provider is None:
        return None

    normalized = provider.lower().strip()
    if normalized in ("mumu", "commandcode"):
        return "openai"
    return normalized


# 思考型模型默认 token 预算（修复 #13：deepseek 等模型长 JSON 输出时
# 推理过程会耗尽默认 32000 预算导致正文为空 / 网关 524）
THINKING_MODEL_DEFAULT_MAX_TOKENS = 64000


def is_thinking_model(model: Optional[str], base_url: Optional[str] = None) -> bool:
    """判断模型是否为思考型模型（推理与正文共享 max_tokens 预算）。

    - 模型名含 deepseek / r1 / reasoning / think 视为思考型；
    - 网关域名含 commandcode.ai 视为思考型（Command Code 通道）。
    """
    model_name = (model or "").lower()
    url = (base_url or "").lower()
    if any(k in model_name for k in ("deepseek", "r1", "reasoning", "think")):
        return True
    if "commandcode.ai" in url:
        return True
    return False


def resolve_effective_max_tokens(
    requested: Optional[int],
    default: int,
    model: Optional[str],
    base_url: Optional[str] = None,
) -> int:
    """解析实际使用的 max_tokens。

    规则：
    - 调用方显式传入 requested → 原样使用（尊重用户/任务意图）；
    - 未显式传入且模型为思考型、默认预算过低（< THINKING_MODEL_DEFAULT_MAX_TOKENS）
      → 提升到 THINKING_MODEL_DEFAULT_MAX_TOKENS，避免推理耗尽预算；
    - 其余情况原样使用 default（用户已配置高预算的不降级）。
    """
    if requested is not None:
        return requested
    if (
        is_thinking_model(model, base_url)
        and default < THINKING_MODEL_DEFAULT_MAX_TOKENS
    ):
        return THINKING_MODEL_DEFAULT_MAX_TOKENS
    return default


def ensure_thinking_model_min_tokens(
    max_tokens: int,
    model: Optional[str],
    base_url: Optional[str] = None,
    floor: int = THINKING_MODEL_DEFAULT_MAX_TOKENS,
) -> int:
    """为思考型模型显式传入的 max_tokens 兜底抬升到下限（修复 #45）。

    与 resolve_effective_max_tokens 不同：后者对显式传入的 requested 原样返回
    （该契约被 test_ai_token_budget.py 钉死，不能改）。部分调用点会显式传入一个
    很小的预算（例如局部重写按选区字数算出的下限 500），思考型模型的推理过程会
    把预算耗尽，导致正文为空、finish_reason=length。本函数只用于这类调用点，
    在不改变全局契约的前提下把预算抬到安全下限。

    max_tokens 是上限而非目标，模型遇到 EOS 会自然停止，抬高下限不会强制变长。
    """
    if is_thinking_model(model, base_url):
        return max(max_tokens, floor)
    return max_tokens


# 已知模型上下文窗口。**需求 #55 步骤 3 起降级为「提示」**：只用来决定探测
# 从哪个刻度开始探（见 model_capability_probe.hint_window_tokens），
# **不再参与任何接受/拒绝判定**——判定一律以实测/显式声明的结论为准。
# 静态登记表维护成本高且必然过期，所以它退出判据；步骤 4 起未知模型也不再
# 伪装成某个窗口值（未登记的保守回退值已删除，见 detect_context_window）。
_KNOWN_CONTEXT_WINDOWS = {
    "deepseek-v4": 1000000,
    "deepseek-v3": 1000000,
    "deepseek-r1": 163840,
    "gpt-5": 400000,
    "gpt-4o": 128000,
    "gpt-4.1": 1047576,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-3.5": 16384,
    "claude-3": 200000,
    "claude-2": 100000,
    "gemini-1.5": 2000000,
    "gemini-2": 1000000,
    "qwen": 131072,
    "glm": 131072,
    "minimax": 200000,
}


def detect_context_window(model: Optional[str]) -> Optional[int]:
    """登记表给出的窗口**提示**（token 数）；未登记返回 `None`。

    按键长度降序匹配已知表（更具体的键优先，如 gpt-4o 先于 gpt-4）。

    需求 #55 步骤 4：未知模型不再有保守回退值（原先未命中即返回一个固定的
    保守窗口）。那个回退是一个**伪装成实测结论的猜测**：它把「不知道」渲染成
    「32K」，下游据此把窗口预算压到 3K 字符、拆书路径整体退回章节摘录，
    功能还在跑但质量已经塌了 —— 正是本需求要根除的静默降级。
    返回 None 才是诚实的「无提示」。
    """
    name = (model or "").lower()
    for key, window in sorted(
        _KNOWN_CONTEXT_WINDOWS.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        if key in name:
            return window
    return None


# 保守默认输出上限（章内续写 B0：未登记模型按此值推导输出预算）
_DEFAULT_MAX_OUTPUT_TOKENS = 8192

# 已知模型最大输出 token（模型输出能力注册表；未列出的按保守值处理。
# 思考/推理模型条目不得低于 THINKING_MODEL_DEFAULT_MAX_TOKENS：推理与正文
# 共享输出预算，登记过低会让续写段正文为空，与修复 #13 的语义一致）
_KNOWN_OUTPUT_LIMITS: Dict[str, int] = {
    "deepseek-v4": 64000,
    "deepseek-v3": 64000,
    "deepseek-r1": 64000,
    "gpt-5": 128000,
    "gpt-4o": 16384,
    "gpt-4.1": 32768,
    "gpt-4-turbo": 16384,
    "gpt-4": 8192,
    "gpt-3.5": 4096,
    "claude-3": 8192,
    "claude-2": 4096,
    "gemini-1.5": 8192,
    "gemini-2": 65536,
    "qwen": 8192,
    "glm": 8192,
    "minimax": 16384,
}


def detect_max_output_tokens(model: Optional[str], base_url: Optional[str] = None) -> int:
    """检测模型最大输出 token 数（输出能力注册表，镜像 detect_context_window）。

    按键长度降序匹配已知表（更具体的键优先，如 gpt-4.1 先于 gpt-4），
    未命中或登记值非正数时返回保守值 _DEFAULT_MAX_OUTPUT_TOKENS，
    保证返回值恒 > 0。base_url 与 is_thinking_model 保持同签名形态，
    预留给后续按网关覆盖，当前解析仅依据模型名；对 None/空 model 健壮。
    """
    name = (model or "").lower()
    for key, limit in sorted(
        _KNOWN_OUTPUT_LIMITS.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        if key in name and limit > 0:
            return limit
    return _DEFAULT_MAX_OUTPUT_TOKENS


# 中文约 1 字符 ≈ 1 token（1M 字符 ≈ 1M token），预算按字符计算
# 全书注入保留 40% 余量给输出/系统提示词/思考模型推理（0.6 为保守值：
# 800K 字符 prompt 的 prefill TTFB 未实测，且 1M 窗口需容纳基础上下文栈
# + 输出预算；或acle 评审 F1 指出单位错配风险，保守化先行）
_FULL_BOOK_BUDGET_RATIO = 0.6
_1M_THRESHOLD = 800000  # 达到此上下文窗口才启用全书全量注入（#57 范围外，本步不动）

# 需求 #55 步骤 4：原先的 `resolve_context_budget_chars(model)` 已删除。它按模型名
# 查静态登记表再分三档（1M→0.6 / 128K–1M→0.3 / 小窗口→0.1），后两档是「小模型半支持」
# 的降级，第一档也建立在猜测窗口之上。预算现在是**单一来源**：本次实发模型
# 实测/显式声明的窗口 × `_FULL_BOOK_BUDGET_RATIO`，见
# `AIService.resolve_full_book_budget_chars`。


class AIService:
    """
    AI服务统一接口
    
    MCP工具支持：
    - 在创建服务时传入 user_id 和 db_session
    - 根据用户MCP插件的enabled状态自动决定是否启用MCP
    - 如果有任意一个MCP插件启用，则加载并使用工具
    - 如果所有插件都关闭，则不使用任何MCP工具
    - 通过 auto_mcp=False 可临时禁用自动工具加载
    - 通过 mcp_max_rounds 控制工具调用轮数
    - 通过 clear_mcp_cache() 可清理MCP工具缓存
    
    MCP启用逻辑（backend/app/api/settings.py 中的 get_user_ai_service）：
    - 查询用户的所有MCP插件
    - 如果有启用的插件 (enabled=True)，则 enable_mcp=True
    - 如果所有插件都关闭或没有插件，则 enable_mcp=False
    
    使用示例：
        # 创建支持MCP的AI服务（根据插件状态自动决定是否启用）
        ai_service = create_user_ai_service_with_mcp(
            api_provider="openai",
            api_key="...",
            user_id="user123",
            db_session=db
        )
        
        # 自动加载MCP工具（如果有启用的插件）
        result = await ai_service.generate_text(prompt="...")
        
        # 临时禁用MCP工具
        result = await ai_service.generate_text(prompt="...", auto_mcp=False)
        
        # 自定义轮数
        result = await ai_service.generate_text(prompt="...", mcp_max_rounds=3)
    """

    def __init__(
        self,
        api_provider: Optional[str] = None,
        api_key: Optional[str] = None,
        api_base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        default_temperature: Optional[float] = None,
        default_max_tokens: Optional[int] = None,
        default_system_prompt: Optional[str] = None,
        config: Optional[AIClientConfig] = None,
        # MCP支持参数
        user_id: Optional[str] = None,
        db_session: Optional[Any] = None,
        enable_mcp: bool = True,
        disable_thinking: bool = False,
    ):
        self.raw_api_provider = (api_provider or app_settings.default_ai_provider or "openai").lower().strip()
        self.api_provider = normalize_provider(self.raw_api_provider)
        # 需求 #55 步骤 2：这里只装**用户自己配置的**默认模型。
        # 系统不再回填默认模型常量替用户猜一个；未配置即为 None，
        # 由 _require_model() 在使用点抛 validation.ai_model_not_configured。
        self.default_model: Optional[str] = (default_model or "").strip() or None
        self.default_temperature = default_temperature or app_settings.default_temperature
        self.default_max_tokens = default_max_tokens or app_settings.default_max_tokens
        # 探测需要按 (provider, base_url, key) 直连模型，所以 key/base_url 要留在实例上；
        # base_url 统一走 effective_base_url，保证与结论缓存键同一口径。
        self.api_key = api_key
        self.base_url = effective_base_url(api_base_url)
        self.default_system_prompt = default_system_prompt
        self.config = config or default_config
        
        # MCP配置
        self.user_id = user_id
        self.db_session = db_session
        self._enable_mcp = enable_mcp
        # 关闭思考（vLLM/Qwen 等思考模型）：通过 chat_template_kwargs 注入
        self.disable_thinking = disable_thinking
        self._cached_tools: Optional[List[Dict]] = None
        self._tools_loaded = False
        
        self._openai_provider: Optional[OpenAIProvider] = None
        self._anthropic_provider: Optional[AnthropicProvider] = None
        self._gemini_provider: Optional[GeminiProvider] = None
        
        # 初始化 OpenAI 兼容接口
        openai_key = None
        openai_base_url = None
        if self.api_provider == "openai":
            openai_key = api_key or app_settings.openai_api_key
            openai_base_url = api_base_url or app_settings.openai_base_url
        else:
            openai_key = app_settings.openai_api_key
            openai_base_url = app_settings.openai_base_url

        if openai_key:
            # 关闭思考: vLLM 标准写法, 不支持该字段的 OpenAI 兼容服务端会忽略
            openai_extra_body = (
                {"chat_template_kwargs": {"enable_thinking": False}}
                if disable_thinking else None
            )
            client = OpenAIClient(openai_key, openai_base_url or "https://api.openai.com/v1", self.config, extra_body=openai_extra_body)
            self._openai_provider = OpenAIProvider(client)
        
        # 初始化 Anthropic
        anthropic_key = api_key if self.api_provider == "anthropic" else app_settings.anthropic_api_key
        anthropic_base_url = api_base_url if self.api_provider == "anthropic" else app_settings.anthropic_base_url
        if anthropic_key:
            client = AnthropicClient(anthropic_key, anthropic_base_url, self.config)
            self._anthropic_provider = AnthropicProvider(client)
        
        # 初始化 Gemini
        gemini_key = api_key if self.api_provider == "gemini" else None
        gemini_base_url = api_base_url if self.api_provider == "gemini" else None
        if self.api_provider == "gemini" and api_key:
            client = GeminiClient(gemini_key, gemini_base_url, self.config)
            self._gemini_provider = GeminiProvider(client)

        # 派发口径登记表（需求 #55 审核项）：每个槽位**真正会打的** host/key 就是喂给上面
        # 三个 client 构造函数的那两个局部变量。客户端可能因为没拿到 key 而不建（槽位为
        # None），但登记必须照建 ⇒ 门禁与 `_get_provider` 读同一份登记，两边不再各留一套
        # 算法（各算各的正是 b09a047 被审核出的 wrong-host admit 的成因）。登记不等于
        # 无法绕过：用户仍能把某个 host 配成自己的默认网关，那部分的诚实说明见
        # model_capability_probe 模块头的盲区清单。
        # base_url 一律过 `effective_base_url`，与保存路径（settings.py）同一口径，
        # 否则结论缓存永远命不中（等价于每次派发都重探一遍）。
        self._provider_endpoints: Dict[str, Tuple[str, Optional[str]]] = {
            "openai": (effective_base_url(openai_base_url) or "", openai_key),
            "anthropic": (effective_base_url(anthropic_base_url) or "", anthropic_key),
            "gemini": (effective_base_url(gemini_base_url) or "", gemini_key),
        }

    @property
    def enable_mcp(self) -> bool:
        """是否启用MCP工具"""
        return self._enable_mcp
    
    @enable_mcp.setter
    def enable_mcp(self, value: bool):
        """设置MCP启用状态，如果禁用则清理缓存"""
        if value is False and self._enable_mcp is True:
            # 从启用变为禁用，清理缓存
            self.clear_mcp_cache()
        self._enable_mcp = value
    
    def clear_mcp_cache(self):
        """
        清理MCP工具缓存
        
        当禁用MCP时调用此方法，确保后续AI调用不会使用缓存的工具。
        同时更新 _tools_loaded 状态，使下次调用时重新检查。
        """
        if self._cached_tools is not None:
            logger.info(f"🔧 清理MCP工具缓存，移除 {len(self._cached_tools)} 个工具")
            self._cached_tools = None
        else:
            logger.debug(f"🔧 MCP工具缓存已经是空，无需清理")
        
        # 更新加载状态，确保下次调用会重新检查
        self._tools_loaded = False
        logger.debug(f"🔧 MCP工具状态已重置: enable_mcp={self._enable_mcp}, _tools_loaded=False")
    
    def _get_provider(self, provider: Optional[str] = None) -> BaseAIProvider:
        """获取对应的 Provider"""
        p = normalize_provider(provider or self.api_provider)
        if p == "openai" and self._openai_provider:
            return self._openai_provider
        if p == "anthropic" and self._anthropic_provider:
            return self._anthropic_provider
        if p == "gemini" and self._gemini_provider:
            return self._gemini_provider
        raise ValueError(f"Provider {p} 未初始化")

    def _dispatch_endpoint(self, provider: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
        """本次调用**实际派发到**的 (provider, base_url, api_key) 三元组。

        provider 的解析表达式与 `_get_provider` 逐字相同（`normalize_provider(provider
        or self.api_provider)`），host/key 直接读构造期登记的 `_provider_endpoints`
        ⇒ 门禁与派发共用同一个来源，不存在「各算各的」这种漂移。

        为什么必须按 per-call 参数算：`provider` 和 `model` 一样取自请求体
        （`api/outlines.py` 的 `data.get("provider")`、`api/wizard_stream.py`、
        `api/polish.py` 的 `request.provider`），而窗口是 **(provider, base_url, model)
        三元组的属性**，不是模型名的属性。用实例自己的网关去判定、却把请求发给别家
        ＝ 门禁给一个它没量过的 host 开合格证。
        """
        p = normalize_provider(provider or self.api_provider)
        entry = self._provider_endpoints.get(p or "")
        if entry is None:
            # 未知 provider 名（`_get_provider` 同样会抛 ValueError）：没有槽位就没有
            # host 可登记，退回实例口径。请求在派发那一刻必失败 ⇒ 不存在「按错误的
            # host 放行」，因为根本没有请求发出去。
            return p or "", self.base_url or "", self.api_key
        base_url, api_key = entry
        return p or "", base_url, api_key

    @staticmethod
    def _resolve_model_or_raise(model: Optional[str], default_model: Optional[str]) -> str:
        """解析本次请求的实发模型：显式传入优先，否则用**用户配置的**默认模型。

        两者都为空时抛 `validation.ai_model_not_configured`——系统绝不替用户猜一个
        模型（需求 #55 步骤 2），也绝不允许 None/空串穿透到 provider 变成
        400/422 或 provider 端随机报错。所有 provider 调用点都必须经此取模型。
        """
        resolved = (model or default_model or "").strip()
        if not resolved:
            raise ApiError(code="validation.ai_model_not_configured")
        return resolved

    async def _require_model(
        self, model: Optional[str] = None, provider: Optional[str] = None
    ) -> str:
        """实发模型的唯一汇合点：解析 + 上下文窗口硬拦门禁，返回后才允许发请求。

        为什么门禁必须长在这里（需求 #55 步骤 3 的审核致命项）：
        `custom_model` 取自请求体 `generate_request.model` 与后台任务的
        `task_input["model"]`，最终写进 `generate_kwargs["model"]`。用户配好合格的
        1M 模型后，仍可逐次传 `gpt-4o-mini` 进去 ⇒ 静默截断，**只在保存时判定的门禁
        会被这条路径完全绕过**。per-usage 预设本就允许多个不同模型，也不能靠
        「删掉 per-request 覆盖」了事。所以判定收敛到「model 已解析、请求还没发出」
        的这里，查的是 preferences 里的缓存结论：一次 dict/行查询，**零网络、零 token**。

        `provider`（同一份请求体里的 per-call 覆盖）必须一起进来：调用点怎么选槽位用的是
        `_get_provider(provider)`，门禁就必须用 `_dispatch_endpoint(provider)` 算出的
        同一三元组去查/写结论（需求 #55 审核项：门禁绑定的是**实发**三元组，
        漏掉 provider 等于给一个没量过的 host 开合格证）。

        异步是因为「从未有过结论」的三元组要同步补测 ①② 再定论（成本是一次 GET +
        一次极小请求）；已有结论只是过期时走 fire-and-forget 后台复测，不 await。
        触发点固定用 `daily`：它的档白名单只有 ①②，结构上就把 ≈1M token 的 needle
        档挡在派发路径之外（`TRIGGER_ALLOWED_TIERS` + `assert_tier_allowed`）。
        """
        resolved = self._resolve_model_or_raise(model, self.default_model)
        gate_provider, gate_base_url, gate_api_key = self._dispatch_endpoint(provider)
        await ensure_model_allowed(
            user_id=self.user_id,
            db=self.db_session,
            provider=gate_provider,
            base_url=gate_base_url,
            api_key=gate_api_key,
            model=resolved,
            trigger=TRIGGER_DAILY,
            hint_window_tokens=detect_context_window(resolved),
        )
        return resolved

    async def resolve_full_book_budget_chars(
        self, model: Optional[str] = None, provider: Optional[str] = None
    ) -> int:
        """全书注入字符预算的**唯一来源**：本次实发模型实测/声明的上下文窗口。

        需求 #55 步骤 4（取代按模型名分三档的 `resolve_context_budget_chars`）：
        窗口来自 `get_effective_context_window`（计划 §4a 定死的对外访问器），
        不再查静态登记表、不再有 128K/小窗口降级档。

        先过 `_require_model` 而不是裸读缓存，是为了保持与派发**同一套**缺结论语义：
        「从未有过结论」必须同步补测 ①② 再定论（计划 §5），绝不能因为预算换算
        抢在门禁之前而把一个合格模型直接拒掉。缓存命中时这两次读取都是内存字典
        （`read_verdict` 的 memo），热路径不额外花网络。

        失败契约：未配置 → `validation.ai_model_not_configured`；窗口不合格/无合格
        结论 → `validation.ai_model_below_minimum`。返回值恒 > 0 ⇒「没有预算」不再是
        一个可表示的状态（计划 §4b：`0 = 禁用` 就是静默失效）。

        `provider` 与派发路径同源（需求 #55 审核项）：调用方如果允许请求体里的
        per-call `provider` 覆盖走到派发，就必须把同一个值传进来，否则预算会按
        **另一个 host** 的结论换算。默认 None ⇒ 用实例自己的网关。
        """
        resolved = await self._require_model(model, provider)
        if not self.user_id or self.db_session is None:
            # 未绑定用户的诊断实例在门禁里允许直通（它不发产品 AI 请求），但结论缓存
            # 按 (user, provider, base_url, model) 存 ⇒ 这里必然拿不到窗口。
            # 「拿不到预算」必须是错误，不能退回 0 或某个猜测值（计划 §4b）。
            raise ApiError(
                code="validation.ai_model_below_minimum",
                detail="该 AI 服务未绑定用户与会话，无法取得上下文窗口结论",
                params={"model": resolved},
                raw="resolve_full_book_budget_chars called on an unbound AIService",
            )
        # 窗口必须按**实发三元组**读：`_require_model` 刚刚判定的就是这把键，
        # 这里换成实例自己的网关会读到另一条结论（甚至读不到）。
        gate_provider, gate_base_url, _ = self._dispatch_endpoint(provider)
        window = await get_effective_context_window(
            self.user_id,
            resolved,
            self.db_session,
            provider=gate_provider,
            base_url=gate_base_url,
        )
        budget = int(window * _FULL_BOOK_BUDGET_RATIO)
        if budget <= 0:  # pragma: no cover - 门禁已保证 window >= 1M
            raise ApiError(code="validation.ai_model_below_minimum", params={"model": resolved})
        return budget

    def _build_call_metrics(
        self,
        *,
        request_mode: str,
        provider: Optional[str],
        model: Optional[str],
        prompt: str,
        auto_mcp: bool,
        tools_count: int,
        stream: bool,
    ) -> AICallMetrics:
        return AICallMetrics(
            request_mode=request_mode,
            provider=normalize_provider(provider or self.api_provider) or "unknown",
            model=model or self.default_model,
            user_id=self.user_id,
            stream=stream,
            auto_mcp=auto_mcp,
            tools_count=tools_count,
            prompt_length=len(prompt or ""),
        )

    def _log_call_metrics(self, metrics: AICallMetrics, title: Optional[str] = None):
        log_title = title or ("AI调用完成" if metrics.success else "AI调用失败")
        message = metrics.to_log_message(log_title)
        if metrics.success:
            logger.info(message)
        else:
            logger.error(message)

    async def _prepare_mcp_tools(self, auto_mcp: bool = True, force_refresh: bool = False) -> Optional[List[Dict]]:
        """
        预处理MCP工具
        
        检查用户MCP配置并加载可用工具。
        结果会被缓存，避免重复加载。
        
        Args:
            auto_mcp: 是否自动加载MCP工具（来自调用方参数）
            force_refresh: 是否强制刷新缓存
            
        Returns:
            - None: 无可用工具（未配置/未启用/加载失败）
            - List[Dict]: OpenAI格式的工具列表
        """
        # 前置条件检查
        if not self._enable_mcp:
            logger.debug(f"🔧 MCP工具未启用 (_enable_mcp=False)")
            # 即使有缓存也清理掉，确保不使用
            self._cached_tools = None
            self._tools_loaded = False
            return None
        
        if not auto_mcp:
            logger.debug(f"🔧 auto_mcp=False，跳过MCP工具加载")
            # 即使有缓存也清理掉，确保不使用
            self._cached_tools = None
            self._tools_loaded = False
            return None
        
        if not self.user_id:
            logger.debug(f"🔧 MCP工具加载跳过: user_id未设置")
            return None
        
        if not self.db_session:
            logger.debug(f"🔧 MCP工具加载跳过: db_session未设置")
            return None
        
        # 使用缓存（只有 enable_mcp=True 时才使用缓存）
        if self._tools_loaded and not force_refresh:
            if self._cached_tools:
                logger.debug(f"🔧 使用缓存的MCP工具 ({len(self._cached_tools)}个)")
            return self._cached_tools
        
        try:
            from app.services.mcp_tools_loader import mcp_tools_loader
            
            self._cached_tools = await mcp_tools_loader.get_user_tools(
                user_id=self.user_id,
                db_session=self.db_session,
                use_cache=True,
                force_refresh=force_refresh
            )
            self._tools_loaded = True
            
            if self._cached_tools:
                logger.info(f"🔧 已加载 {len(self._cached_tools)} 个MCP工具")
            else:
                logger.debug(f"📭 用户 {self.user_id} 没有可用的MCP工具")
            
            return self._cached_tools
            
        except Exception as e:
            logger.warning(f"⚠️ 加载MCP工具失败: {e}")
            self._tools_loaded = True
            self._cached_tools = None
            return None

    async def _handle_tool_calls(
        self,
        original_prompt: str,
        response: Dict[str, Any],
        max_rounds: int = 2,
        **kwargs
    ) -> Dict[str, Any]:
        """
        处理AI返回的工具调用
        
        Args:
            original_prompt: 原始提示词
            response: AI响应（包含tool_calls）
            max_rounds: 最大工具调用轮数
            **kwargs: 传递给generate_text的其他参数
            
        Returns:
            最终的AI响应
        """
        from app.mcp import mcp_client
        
        tool_calls = response.get("tool_calls", [])
        if not tool_calls or not self.user_id:
            return response

        tool_metrics = ToolCallMetrics()
        tool_metrics.usage.add(TokenUsage.from_response(response))
        
        result = {
            "content": response.get("content", ""),
            "tool_calls_made": 0,
            "tools_used": [],
            "finish_reason": response.get("finish_reason", ""),
            "mcp_enhanced": True,
            "usage": response.get("usage"),
        }
        
        prompt = original_prompt
        
        for round_num in range(max_rounds):
            logger.info(f"🔧 工具调用 - 第{round_num+1}/{max_rounds}轮，{len(tool_calls)}个工具")
            tool_metrics.mcp_rounds += 1
            
            try:
                # 批量执行工具调用
                tool_results = await mcp_client.batch_call_tools(
                    user_id=self.user_id,
                    tool_calls=tool_calls
                )
                
                # 记录使用的工具
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    tool_metrics.add_tool_name(name)
                    if name not in result["tools_used"]:
                        result["tools_used"].append(name)
                result["tool_calls_made"] += len(tool_calls)
                tool_metrics.tool_calls_count += len(tool_calls)
                
                # 构建工具上下文
                tool_context = mcp_client.build_tool_context(tool_results, format="markdown")
                
                # 更新提示词
                if round_num == max_rounds - 1:
                    # 最后一轮，强制要求回答
                    prompt = f"{original_prompt}\n\n{tool_context}\n\n⚠️ 重要：请基于以上工具查询结果，给出完整详细的最终答案。不要再调用工具。"
                    tool_choice = "none"
                else:
                    prompt = f"{original_prompt}\n\n{tool_context}\n\n请基于以上工具查询结果，继续完成任务。"
                    tool_choice = kwargs.get("tool_choice", "auto")
                
                # 继续调用AI
                next_provider = kwargs.get("provider")
                prov = self._get_provider(next_provider)
                # 实发模型统一经 _require_model（含上下文窗口门禁）取一次并复用；
                # provider 必须与上面 `_get_provider` 选槽位用的是同一个值，否则门禁
                # certify 的是另一家网关（需求 #55 审核项）。
                next_model = await self._require_model(kwargs.get("model"), next_provider)
                next_response = await prov.generate(
                    prompt=prompt,
                    model=next_model,
                    temperature=kwargs.get("temperature") or self.default_temperature,
                    max_tokens=resolve_effective_max_tokens(
                        kwargs.get("max_tokens"), self.default_max_tokens,
                        next_model, self.base_url,
                    ),
                    system_prompt=kwargs.get("system_prompt") or self.default_system_prompt,
                    tools=None if tool_choice == "none" else self._cached_tools,
                    tool_choice=tool_choice,
                )
                tool_metrics.usage.add(TokenUsage.from_response(next_response))
                
                tool_calls = next_response.get("tool_calls", [])
                
                if not tool_calls:
                    # 没有更多工具调用，返回结果
                    result["content"] = next_response.get("content", "")
                    result["finish_reason"] = next_response.get("finish_reason", "stop")
                    result["usage"] = {
                        "prompt_tokens": tool_metrics.usage.prompt_tokens,
                        "completion_tokens": tool_metrics.usage.completion_tokens,
                        "total_tokens": tool_metrics.usage.total_tokens,
                    }
                    break
                    
            except Exception as e:
                logger.error(f"❌ 工具调用失败: {e}")
                tool_metrics.tool_error_count += 1
                result["content"] = response.get("content", "")
                result["finish_reason"] = "tool_error"
                result["usage"] = {
                    "prompt_tokens": tool_metrics.usage.prompt_tokens,
                    "completion_tokens": tool_metrics.usage.completion_tokens,
                    "total_tokens": tool_metrics.usage.total_tokens,
                }
                break

        result["__tool_metrics"] = tool_metrics
        
        return result

    async def generate_text(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        auto_mcp: bool = True,
        handle_tool_calls: bool = True,
        mcp_max_rounds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        生成文本（自动支持MCP工具）
        
        Args:
            prompt: 用户提示词
            provider: AI提供商
            model: 模型名称
            temperature: 温度
            max_tokens: 最大令牌数
            system_prompt: 系统提示词
            tools: 手动指定的工具列表（优先级高于自动加载）
            tool_choice: 工具选择策略
            auto_mcp: 是否自动加载MCP工具（默认True）
            handle_tool_calls: 是否自动处理工具调用（默认True）
            mcp_max_rounds: 最大工具调用轮数（None使用默认值3）
            
        Returns:
            包含生成内容的字典
        """
        # 未配置模型即明确报错，且在加载 MCP 工具/发请求之前就失败（需求 #55 步骤 2）
        # provider 一起传入：门禁判定的三元组必须与下面 `_get_provider(provider)` 一致
        model = await self._require_model(model, provider)
        # 使用全局配置的MCP轮数（如果未指定）
        if mcp_max_rounds is None:
            mcp_max_rounds = app_settings.mcp_max_rounds
        
        # 自动加载MCP工具
        if auto_mcp and tools is None:
            tools = await self._prepare_mcp_tools(auto_mcp=auto_mcp)

        metrics = self._build_call_metrics(
            request_mode="文本",
            provider=provider,
            model=model,
            prompt=prompt,
            auto_mcp=auto_mcp,
            tools_count=len(tools) if tools else 0,
            stream=False,
        )
        
        try:
            prov = self._get_provider(provider)
            response = await prov.generate(
                prompt=prompt,
                model=model,
                temperature=temperature or self.default_temperature,
                max_tokens=resolve_effective_max_tokens(
                    max_tokens, self.default_max_tokens, model, self.base_url
                ),
                system_prompt=system_prompt or self.default_system_prompt,
                tools=tools,
                tool_choice=tool_choice,
            )
            usage = TokenUsage.from_response(response)
            
            # 处理工具调用
            if handle_tool_calls and response.get("tool_calls"):
                response = await self._handle_tool_calls(
                    original_prompt=prompt,
                    response=response,
                    provider=provider,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    tool_choice=tool_choice,
                    max_rounds=mcp_max_rounds,
                )
                usage = TokenUsage.from_response(response)
                tool_metrics = response.get("__tool_metrics")
                if tool_metrics:
                    metrics.merge_tool_metrics(tool_metrics)

            metrics.finish(
                success=True,
                response_length=len(response.get("content", "") or ""),
                finish_reason=response.get("finish_reason"),
                usage=usage,
            )
            self._log_call_metrics(metrics)
            return response
        except Exception as e:
            metrics.finish(success=False, error=e)
            self._log_call_metrics(metrics)
            raise

    async def generate_text_stream(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        tool_choice: Optional[str] = None,
        auto_mcp: bool = True,
        mcp_max_rounds: Optional[int] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> AsyncGenerator[str, None]:
        """
        流式生成文本（自动支持MCP工具）
        
        工具调用在 Provider 层通过流式方式处理，支持真正的流式工具调用。
        
        Args:
            prompt: 用户提示词
            provider: AI提供商
            model: 模型名称
            temperature: 温度
            max_tokens: 最大令牌数
            system_prompt: 系统提示词
            tool_choice: 工具选择策略（"auto"/"none"/"required"）
            auto_mcp: 是否自动加载MCP工具
            mcp_max_rounds: 最大工具调用轮数（None使用默认值3）
            response_format: OpenAI 兼容的响应格式约束（如 {"type": "json_object"}）
            
        Yields:
            生成的文本块
        """
        logger.debug(f"🔧 generate_text_stream: auto_mcp={auto_mcp}, tool_choice={tool_choice}")
        # 未配置模型即明确报错，且在加载 MCP 工具/发请求之前就失败（需求 #55 步骤 2）
        # provider 一起传入：门禁判定的三元组必须与下面 `_get_provider(provider)` 一致
        model = await self._require_model(model, provider)
        
        tools_to_use = None
        
        # 加载MCP工具
        if auto_mcp:
            tools_to_use = await self._prepare_mcp_tools(auto_mcp=auto_mcp)
            if tools_to_use:
                logger.info(f"🔧 已获取 {len(tools_to_use)} 个MCP工具")

        # 冲突处理：response_format 与 MCP tools / 非 OpenAI 提供商不兼容，注入前丢弃
        if response_format and tools_to_use:
            logger.warning("response_format 与 MCP tools 冲突，丢弃 response_format")
            response_format = None
        if response_format and normalize_provider(provider or self.api_provider) != "openai":
            logger.warning("非 OpenAI 提供商不支持 response_format，跳过")
            response_format = None

        metrics = self._build_call_metrics(
            request_mode="流式文本",
            provider=provider,
            model=model,
            prompt=prompt,
            auto_mcp=auto_mcp,
            tools_count=len(tools_to_use) if tools_to_use else 0,
            stream=True,
        )
        response_parts: List[str] = []
        latest_usage = TokenUsage()
        finish_reason = "stop"
        
        try:
            # 流式生成（Provider 层处理工具调用）
            prov = self._get_provider(provider)
            logger.debug(f"🔧 开始流式生成，provider={provider or self.api_provider}, tools_count={len(tools_to_use) if tools_to_use else 0}")
            async for chunk in prov.generate_stream(
                prompt=prompt,
                model=model,
                temperature=temperature or self.default_temperature,
                max_tokens=resolve_effective_max_tokens(
                    max_tokens, self.default_max_tokens, model, self.base_url
                ),
                system_prompt=system_prompt or self.default_system_prompt,
                tools=tools_to_use,
                tool_choice=tool_choice,
                user_id=self.user_id,
                response_format=response_format,
            ):
                if isinstance(chunk, dict):
                    if chunk.get("usage"):
                        latest_usage = TokenUsage.from_response({"usage": chunk.get("usage")})
                    if chunk.get("finish_reason"):
                        finish_reason = chunk.get("finish_reason") or finish_reason
                    continue

                if chunk:
                    metrics.mark_first_chunk()
                    metrics.chunk_count += 1
                    response_parts.append(chunk)
                yield chunk

            metrics.finish(
                success=True,
                response_length=len("".join(response_parts)),
                finish_reason=finish_reason,
                usage=latest_usage,
            )
            self._log_call_metrics(metrics)
        except Exception as e:
            metrics.finish(
                success=False,
                response_length=len("".join(response_parts)),
                finish_reason=finish_reason,
                usage=latest_usage,
                error=e,
            )
            self._log_call_metrics(metrics)
            raise

    async def generate_text_stream_full(
        self,
        prompt: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        auto_mcp: bool = True,
    ) -> Dict[str, Any]:
        """流式累积生成，返回与 generate_text 相同的完整响应结构。

        用于长回复场景（如创作助手最终回答）：流式输出规避网关单次
        响应时长上限（Cloudflare 524），同时调用方仍拿到 content、
        finish_reason、usage 字段。
        """
        tools_to_use = None
        # 未配置模型即明确报错，且在加载 MCP 工具/发请求之前就失败（需求 #55 步骤 2）
        # provider 一起传入：门禁判定的三元组必须与下面 `_get_provider(provider)` 一致
        model = await self._require_model(model, provider)
        if auto_mcp:
            tools_to_use = await self._prepare_mcp_tools(auto_mcp=auto_mcp)

        prov = self._get_provider(provider)
        accumulated: List[str] = []
        latest_usage = TokenUsage()
        finish_reason = "stop"
        async for chunk in prov.generate_stream(
            prompt=prompt,
            model=model,
            temperature=temperature or self.default_temperature,
            max_tokens=resolve_effective_max_tokens(
                max_tokens, self.default_max_tokens, model, self.base_url
            ),
            system_prompt=system_prompt or self.default_system_prompt,
            tools=tools_to_use,
            tool_choice="none" if tools_to_use else None,
            user_id=self.user_id,
        ):
            if isinstance(chunk, dict):
                if chunk.get("usage"):
                    latest_usage = TokenUsage.from_response({"usage": chunk["usage"]})
                if chunk.get("finish_reason"):
                    finish_reason = chunk.get("finish_reason") or finish_reason
                if chunk.get("content"):
                    accumulated.append(chunk["content"])
                continue
            if chunk:
                accumulated.append(chunk)
        return {
            "content": "".join(accumulated),
            "tool_calls": None,
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": latest_usage.prompt_tokens,
                "completion_tokens": latest_usage.completion_tokens,
                "total_tokens": latest_usage.total_tokens,
            },
        }

    async def call_with_json_retry(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_retries: int = 3,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        expected_type: Optional[str] = None,
        auto_mcp: bool = True,
        validator: Optional[Callable[[Any], None]] = None,
        validator_max_retries: int = 2,
        response_format: Optional[Dict[str, str]] = None,
    ) -> Union[Dict, List]:
        """
        带重试的 JSON 调用（自动支持MCP工具）
        
        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词
            max_retries: 最大重试次数
            temperature: 温度
            max_tokens: 最大令牌数
            provider: AI提供商
            model: 模型名称
            expected_type: 期望的返回类型（"object"或"array"）
            auto_mcp: 是否自动加载MCP工具
            validator: 可选 schema 校验器，解析出的数据在返回前调用；
                抛 ValueError 视为可重试失败，错误信息注入重试提示
            validator_max_retries: validator 独立重试上限（不消耗 max_retries），
                超过后抛 ValueError("校验失败: ...")
            response_format: OpenAI 兼容的响应格式约束（如 {"type": "json_object"}）。
                默认 None 时自动注入 json_object（除非会加载 MCP tools 或非 OpenAI 提供商）
            
        Returns:
            解析后的JSON数据
        """
        # 未配置模型即明确报错：重试循环一次都不启动，也不发请求（需求 #55 步骤 2）
        # provider 一起传入：门禁判定的三元组必须与循环里转发给 `generate_text_stream`
        # 的那个 provider 一致，否则判的是别家网关。
        model = await self._require_model(model, provider)
        last_response = ""
        aggregate_usage = TokenUsage()
        metrics = self._build_call_metrics(
            request_mode="JSON重试",
            provider=provider,
            model=model,
            prompt=prompt,
            auto_mcp=auto_mcp,
            tools_count=0,
            stream=True,
        )
        
        # OpenAI 禁止 response_format 与 tools 同用，非 OpenAI 提供商不支持该参数
        if response_format is None:
            tools_to_use = await self._prepare_mcp_tools(auto_mcp=auto_mcp)
            if tools_to_use:
                logger.warning("检测到 MCP tools，跳过 response_format 注入以避免 API 冲突")
            elif normalize_provider(provider or self.api_provider) != "openai":
                logger.warning("非 OpenAI 提供商不支持 response_format，跳过 JSON 格式约束")
            else:
                response_format = {"type": "json_object"}
        
        try:
            validator_retries = 0
            validator_error = None
            json_error = None
            for attempt in range(1, max_retries + 1):
                current_prompt = prompt if attempt == 1 else self._add_json_hint(
                    prompt, attempt, extra_error=validator_error, json_error=json_error
                )
                
                # 流式累积：思考型模型长 JSON 输出时，推理增量随块送达，
                # 避免非流式单次响应超过网关时长上限（Cloudflare 524，#13）
                accumulated: List[str] = []
                finish_reason = None
                # while 保底：response_format 降级即置空，后续不会再次命中，不会死循环
                while True:
                    try:
                        async for chunk in self.generate_text_stream(
                            prompt=current_prompt,
                            provider=provider,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            system_prompt=system_prompt,
                            auto_mcp=auto_mcp,
                            response_format=response_format,
                        ):
                            if isinstance(chunk, dict):
                                if chunk.get("finish_reason"):
                                    finish_reason = chunk.get("finish_reason")
                                if chunk.get("usage"):
                                    aggregate_usage.add(TokenUsage.from_response({"usage": chunk["usage"]}))
                                continue
                            if chunk:
                                accumulated.append(chunk)
                        break
                    except Exception as e:
                        # 只在「上游响应: 」标记之前匹配原始异常文本：上游 400 body 增强后
                        # 恰含 response_format 字样时不得误判为本端注入的参数不被支持而降级
                        head = str(e).split(UPSTREAM_BODY_MARKER, 1)[0]
                        if response_format and "response_format" in head:
                            logger.warning("API 不支持 response_format，降级为文本模式: %s", str(e)[:200])
                            response_format = None
                            accumulated = []
                            finish_reason = None
                            continue
                        raise
                result = {
                    "content": "".join(accumulated),
                    "finish_reason": finish_reason,
                }
                metrics.retry_count = attempt
                metrics.tools_count = max(metrics.tools_count, len(self._cached_tools) if self._cached_tools else 0)
                
                last_response = result.get("content", "")
                
                # 思考型模型可能把 token 预算耗在推理上致正文为空，跳过解析直接重试
                if not last_response.strip():
                    metrics.json_parse_success = False
                    if attempt < max_retries:
                        logger.warning(
                            "模型输出为空，立即重试 %d/%d: model=%s",
                            attempt, max_retries, model or self.default_model,
                        )
                        continue
                    raise ValueError("模型输出为空，无法解析 JSON")
                
                try:
                    data = parse_json(last_response)
                    if expected_type == "object" and not isinstance(data, dict):
                        raise ValueError("期望对象")
                    if expected_type == "array" and not isinstance(data, list):
                        raise ValueError("期望数组")
                except Exception as e:
                    metrics.json_parse_success = False
                    json_error = str(e)
                    if attempt == max_retries:
                        raise ValueError(f"JSON 解析失败: {e}")
                    continue

                if validator is not None:
                    try:
                        validator(data)
                    except ValueError as ve:
                        validator_error = str(ve)
                        if validator_retries < validator_max_retries:
                            validator_retries += 1
                            continue
                        raise ValueError(f"校验失败: {validator_error}")

                metrics.json_parse_success = True
                metrics.finish(
                    success=True,
                    response_length=len(last_response),
                    finish_reason=result.get("finish_reason"),
                    usage=aggregate_usage,
                )
                self._log_call_metrics(metrics, title="AI调用汇总")
                return data

            if validator_error:
                # validator 在最后一次尝试失败且未达独立上限（如 max_retries=2、
                # validator_max_retries=2 时循环耗尽）——优先透出具体校验错误，
                # 避免用户只看到泛化的"JSON 调用失败"而丢失可纠正的信息。
                raise ValueError(f"校验失败: {validator_error}")
            raise ValueError("JSON 调用失败")
        except Exception as e:
            metrics.finish(
                success=False,
                response_length=len(last_response),
                usage=aggregate_usage,
                error=e,
            )
            self._log_call_metrics(metrics, title="AI调用汇总")
            raise

    @staticmethod
    def _add_json_hint(prompt: str, attempt: int, extra_error: Optional[str] = None, json_error: Optional[str] = None) -> str:
        hint = f"{prompt}\n\n⚠️ 第{attempt}次重试，请返回纯JSON，不要markdown包裹。"
        if json_error:
            hint += f"\n\nJSON 错误详情: {json_error[:300]}"
        if extra_error:
            hint += f"\n\n校验提示: {extra_error}"
        return hint

    @staticmethod
    def _clean_json_response(text: str) -> str:
        """清洗 JSON 响应"""
        return clean_json_response(text)


def create_user_ai_service(
    api_provider: str,
    api_key: str,
    api_base_url: str,
    model_name: Optional[str],
    temperature: float,
    max_tokens: int,
    system_prompt: Optional[str] = None,
    user_id: Optional[str] = None,
    db_session=None,
) -> AIService:
    """创建用户 AI 服务（不带MCP支持）

    model_name 为**用户配置的**默认模型；None/空表示未配置，调用 AI 时会抛
    validation.ai_model_not_configured（需求 #55 步骤 2）。

    user_id / db_session：需求 #55 步骤 3 起，**产品路径必须传**。上下文窗口门禁
    按 (user, provider, base_url, model) 读结论缓存，未绑定用户的服务拿不到结论
    ⇒ 等于给该路径开了一个绕过门禁的口子。仅「诊断类临时实例」（设置页测试按钮）
    允许不绑定。
    """
    return AIService(
        api_provider=api_provider,
        api_key=api_key,
        api_base_url=api_base_url,
        default_model=model_name,
        default_temperature=temperature,
        default_max_tokens=max_tokens,
        default_system_prompt=system_prompt,
        user_id=user_id,
        db_session=db_session,
    )


def create_user_ai_service_with_mcp(
    api_provider: str,
    api_key: str,
    api_base_url: str,
    model_name: Optional[str],
    temperature: float,
    max_tokens: int,
    user_id: str,
    db_session,
    system_prompt: Optional[str] = None,
    enable_mcp: bool = True,
    disable_thinking: bool = False,
) -> AIService:
    """
    创建支持MCP的用户AI服务
    
    Args:
        api_provider: AI提供商
        api_key: API密钥
        api_base_url: API基础URL
        model_name: 模型名称
        temperature: 温度
        max_tokens: 最大令牌数
        user_id: 用户ID（用于加载MCP工具）
        db_session: 数据库会话
        system_prompt: 系统提示词
        enable_mcp: 是否启用MCP工具
        
    Returns:
        配置好的AIService实例
    """
    return AIService(
        api_provider=api_provider,
        api_key=api_key,
        api_base_url=api_base_url,
        default_model=model_name,
        default_temperature=temperature,
        default_max_tokens=max_tokens,
        default_system_prompt=system_prompt,
        user_id=user_id,
        db_session=db_session,
        enable_mcp=enable_mcp,
        disable_thinking=disable_thinking,
    )