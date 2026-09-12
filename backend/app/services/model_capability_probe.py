"""模型上下文窗口能力探测 + 结论缓存 + 「实发模型」硬拦门禁（需求 #55 步骤 3）。

为什么存在
----------
产品前提：底座模型的上下文窗口必须 >= 1M tokens。低于 1M 的失败是**静默**的——
助手会丢掉它最早读到的指令，一次批次摘要覆盖了半本书却仍然「读起来很完整」。
所以「<1M 半支持」比「不支持」更糟，门禁必须是硬拦，且必须绑在**本次真正发出去
的那个模型**上（只在保存时判定可被逐次传 model 完全绕过）。

三档探测（本期只接 ①②）
------------------------
| 档 | 手段                                        | 成本            |
|----|---------------------------------------------|----------------|
| ①  | GET /models/<id> 读窗口字段                   | 一次 GET，0 token |
| ②  | 极小 prompt + max_tokens=探测刻度 + stream，首块即断 | ≈0 token，靠服务端上界校验 |
| ③  | 尾部 needle 回读（填充至 ≈1.05×1M）             | ≈1M 输入 token   |

③ **本期只定义接口形状、不接线**：`probe_needle_tier` 一次请求都不发，调用即返回
`inconclusive`。它绝不返回 `qualified`——未接线的档若报合格，等于给小模型开合格证。

已知盲区（对外必须诚实，勿暗示系统万无一失）
--------------------------------------------
1. **静默截断型网关可以通过 ①②**：许多兼容网关不报错而直接把输入截断，
   「接受 max_tokens=1M」不等于「真读进去 1M」。③ 的 needle 回读是唯一解，
   本期未接线，故该误判可能如实发生，只能靠每日复测与表单三段数缓解。
2. **两次探测之间网关换模型/降配**：日频复测的粒度是一个自然日，当天首个请求
   按旧结论放行。README 只承诺「要求 ≥1M、保存时实测、低于此不受支持」，
   **不得**外推成「任何时刻都不会被绕过」。

绝对不允许「查不到就放行」：未知即不合格，必须由用户在表单里显式声明
`context_window_tokens >= MIN_CONTEXT_WINDOW_TOKENS`（source=user_declared）才开绿灯。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_write_lock import get_db_write_lock
from app.core.errors import ApiError
from app.logger import get_logger
from app.models.settings import Settings

logger = get_logger(__name__)

# ========== 产品下限 ==========
# ⚠️ 刻意不复用 ai_service._1M_THRESHOLD：那是「全书注入启用线」，语义不同
# （实测它还是死代码，见 issue #57）。这里是「产品最低要求」。
MIN_CONTEXT_WINDOW_TOKENS = 1_000_000

# ========== 探测档与触发点 ==========
TIER_METADATA = "metadata"
TIER_MAX_TOKENS_BOUND = "max_tokens_bound"
TIER_NEEDLE = "needle"

TRIGGER_SAVE = "save"
TRIGGER_MANUAL = "manual"
TRIGGER_DAILY = "daily"

# 触发点 => 允许挂的档。日频**只能**跑 ①②：把 needle 挂到日频等于每天烧 ≈1M token。
TRIGGER_ALLOWED_TIERS: Dict[str, Tuple[str, ...]] = {
    TRIGGER_SAVE: (TIER_METADATA, TIER_MAX_TOKENS_BOUND, TIER_NEEDLE),
    TRIGGER_MANUAL: (TIER_METADATA, TIER_MAX_TOKENS_BOUND, TIER_NEEDLE),
    TRIGGER_DAILY: (TIER_METADATA, TIER_MAX_TOKENS_BOUND),
}


class ProbeTierNotAllowed(AssertionError):
    """误接线守卫：把某档挂到它不被允许的触发点（典型＝日频挂 needle）。

    继承 `AssertionError` 而不用裸 `assert`：`python -O` 会剥掉裸 assert，
    而这条守卫必须在生产模式下同样生效。
    """


def assert_tier_allowed(trigger: str, tier: str) -> None:
    """架构层断言：触发点与档的组合必须在 TRIGGER_ALLOWED_TIERS 白名单内。"""
    if trigger not in TRIGGER_ALLOWED_TIERS:
        raise ProbeTierNotAllowed(
            f"未知探测触发点 {trigger!r}：未登记即视为不允许任何档（禁止新增触发点绕开白名单）"
        )
    allowed = TRIGGER_ALLOWED_TIERS[trigger]
    if tier not in allowed:
        raise ProbeTierNotAllowed(
            f"触发点 {trigger!r} 不允许 {tier!r} 档（允许 {allowed}）。"
            "日频挂 needle = 每个用户每天 ≈1M token 计费，架构层禁止。"
        )


# ========== 三态结论 ==========
VERDICT_QUALIFIED = "qualified"
VERDICT_UNQUALIFIED = "unqualified"
VERDICT_INCONCLUSIVE = "inconclusive"

SOURCE_PROBE = "probe"
SOURCE_USER_DECLARED = "user_declared"


@dataclass(frozen=True)
class ProbeOutcome:
    """一次判定结果（三态）。

    `context_window_tokens` 在 verdict=qualified 时恒为正整数：① 档是网关报的窗口，
    ② 档是「已被接受的探测刻度」（保守下界，不是猜的），user_declared 是用户声明值。
    """

    verdict: str
    context_window_tokens: Optional[int] = None
    source: str = SOURCE_PROBE
    tier: Optional[str] = None
    detail: Optional[str] = None
    # 本次实际跑过哪几档（诊断/表单展示用；不落进缓存）
    tiers_run: Tuple[str, ...] = ()
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_qualified(self) -> bool:
        return self.verdict == VERDICT_QUALIFIED


# ========== 探测专用客户端 ==========
# 探测必须**一次即止**：_request_with_retry 默认 max_retries=3，且
# non_retryable_status_codes 只排除 401/403/404 ⇒ ② 档预期拿到的 400 会被原样
# 重发 3 次（3 倍计费、3 倍延迟）。全局 semaphore 还会把探测排在日常生成队列后面。
# 所以这里直连 httpx，既不走 _request_with_retry 也不取信号量。
PROBE_MAX_ATTEMPTS = 1
PROBE_TRANSPORT_RETRIES = 0
PROBE_CONNECT_TIMEOUT_SECONDS = 10.0
PROBE_READ_TIMEOUT_SECONDS = 25.0
# ② 档默认刻度：恰为产品下限。接受 ⇒ 窗口 >= 1M（成立证据）；被上界校验拒绝 ⇒ < 1M。
# 登记表提示可以把刻度抬到 > 1M（更强的证据），此时被拒会再退到本刻度探一次。
MAX_TOKENS_PROBE_VALUE = MIN_CONTEXT_WINDOW_TOKENS
# ③ 档形状常量（未接线，仅为将来实现留契约）
NEEDLE_FILL_RATIO = 1.05
NEEDLE_MAX_OUTPUT_TOKENS = 64

_METADATA_WINDOW_KEYS = (
    "context_length",
    "max_context_length",
    "max_context_tokens",
    "context_window",
    "context_size",
    "total_context",
    "n_ctx",
)
_BOUND_REJECTION_HINTS = (
    "maximum context",
    "context length",
    "context window",
    "exceeds",
    "too long",
    "reduce",
    "max_tokens",
    "max tokens",
    "input tokens",
    "not enough tokens",
)


def create_probe_client(transport: Optional[httpx.AsyncBaseTransport] = None) -> httpx.AsyncClient:
    """构造探测专用 HTTP 客户端（独立于 BaseAIClient 的连接池与重试逻辑）。

    Args:
        transport: 测试注入口（httpx.MockTransport）；None 时构造真实传输，
            且显式 `retries=PROBE_TRANSPORT_RETRIES`（0）。
    """
    return httpx.AsyncClient(
        transport=(
            httpx.AsyncHTTPTransport(retries=PROBE_TRANSPORT_RETRIES)
            if transport is None
            else transport
        ),
        timeout=httpx.Timeout(
            connect=PROBE_CONNECT_TIMEOUT_SECONDS,
            read=PROBE_READ_TIMEOUT_SECONDS,
            write=PROBE_CONNECT_TIMEOUT_SECONDS,
            pool=PROBE_CONNECT_TIMEOUT_SECONDS,
        ),
        follow_redirects=False,
    )


def _auth_headers(provider: str, api_key: Optional[str]) -> Dict[str, str]:
    if (provider or "").lower() == "anthropic":
        return {
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {"Authorization": f"Bearer {api_key or ''}", "content-type": "application/json"}


def _metadata_url(provider: str, base_url: str, model: str) -> str:
    base = (base_url or "").rstrip("/")
    if (provider or "").lower() == "anthropic":
        return f"{base}/v1/models/{model}"
    return f"{base}/models/{model}"


def _bound_probe_request(
    provider: str, base_url: str, model: str, probe_value: int
) -> Tuple[str, Dict[str, Any]]:
    """② 档请求形态：极小 prompt + 远大于日常输出的 max_tokens + `stream=true`。"""
    base = (base_url or "").rstrip("/")
    if (provider or "").lower() == "anthropic":
        return (
            f"{base}/v1/messages",
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": probe_value,
                "stream": True,
            },
        )
    return (
        f"{base}/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": probe_value,
            "temperature": 0,
            "stream": True,
        },
    )


def _inconclusive(detail: str, tier: Optional[str] = None) -> ProbeOutcome:
    return ProbeOutcome(
        verdict=VERDICT_INCONCLUSIVE,
        context_window_tokens=None,
        source=SOURCE_PROBE,
        tier=tier,
        detail=detail[:500],
    )


def _extract_window_tokens(payload: Any) -> Optional[int]:
    """在 /models/<id> 响应里按白名单键找窗口 token 数（广度优先，只认正整数）。

    只认窗口类字段：`max_tokens`/`output` 之类是**输出**上限，把它当窗口会开出假合格证。
    """
    queue: List[Any] = [payload]
    seen = 0
    while queue and seen < 200:
        node = queue.pop(0)
        seen += 1
        if isinstance(node, dict):
            for key in _METADATA_WINDOW_KEYS:
                value = node.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)) and int(value) > 0:
                    return int(value)
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


async def probe_metadata_tier(
    *,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    client: httpx.AsyncClient,
) -> ProbeOutcome:
    """① 档：GET /models/<id> 读 `context_length`。一次 GET、0 token，多数网关不返回。"""
    try:
        response = await client.get(
            _metadata_url(provider, base_url, model), headers=_auth_headers(provider, api_key)
        )
    except Exception as exc:  # 网络/超时/非法 URL：判不出，不是「不合格」
        return _inconclusive(f"metadata tier request failed: {type(exc).__name__}: {exc}", TIER_METADATA)

    if response.status_code >= 400:
        return _inconclusive(f"metadata tier HTTP {response.status_code}", TIER_METADATA)

    try:
        payload = response.json()
    except ValueError:
        return _inconclusive("metadata tier response is not JSON", TIER_METADATA)

    tokens = _extract_window_tokens(payload)
    if tokens is None:
        return _inconclusive("metadata tier found no context-length field", TIER_METADATA)
    if tokens >= MIN_CONTEXT_WINDOW_TOKENS:
        return ProbeOutcome(
            verdict=VERDICT_QUALIFIED,
            context_window_tokens=tokens,
            tier=TIER_METADATA,
            detail=f"metadata reported {tokens} tokens",
        )
    return ProbeOutcome(
        verdict=VERDICT_UNQUALIFIED,
        context_window_tokens=tokens,
        tier=TIER_METADATA,
        detail=f"metadata reported {tokens} tokens, below {MIN_CONTEXT_WINDOW_TOKENS}",
    )


def _looks_like_bound_rejection(body: str) -> bool:
    lowered = (body or "").lower()
    return any(hint in lowered for hint in _BOUND_REJECTION_HINTS)


async def probe_max_tokens_bound_tier(
    *,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    client: httpx.AsyncClient,
    probe_value: int = MAX_TOKENS_PROBE_VALUE,
) -> ProbeOutcome:
    """② 档：让服务端自己报上界——多数实现在生成前校验 max_tokens 并回 400。

    必须 `stream=true` 且**收到首个 delta 立即断开**：少数实现不校验上界而直接开始
    生成，不断开就会真的烧掉一整个输出预算。

    判定：接受（拿到流式首块）⇒ 窗口 >= 本次刻度 ⇒ 合格；
    被上界类 400/422 拒绝 ⇒ 窗口 < 刻度。刻度恰为 1M 时这就是「< 1M」的实证；
    刻度 > 1M（登记表提示抬上去的）时被拒只给出一个上界，故退回 1M 刻度再探一次。
    """
    ladder = [probe_value]
    if probe_value > MAX_TOKENS_PROBE_VALUE:
        ladder.append(MAX_TOKENS_PROBE_VALUE)

    last = _inconclusive("max_tokens tier produced no verdict", TIER_MAX_TOKENS_BOUND)
    for value in ladder:
        url, payload = _bound_probe_request(provider, base_url, model, value)
        try:
            async with client.stream(
                "POST", url, headers=_auth_headers(provider, api_key), json=payload
            ) as response:
                if response.status_code >= 400:
                    body = ""
                    try:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:500]
                    except Exception:  # 读不到 body 也别抛：判不出而已
                        pass
                    if response.status_code in (400, 422) and _looks_like_bound_rejection(body):
                        if value > MAX_TOKENS_PROBE_VALUE:
                            last = _inconclusive(
                                f"max_tokens={value} rejected (upper bound only), retrying at {MAX_TOKENS_PROBE_VALUE}",
                                TIER_MAX_TOKENS_BOUND,
                            )
                            continue
                        return ProbeOutcome(
                            verdict=VERDICT_UNQUALIFIED,
                            context_window_tokens=None,
                            tier=TIER_MAX_TOKENS_BOUND,
                            detail=f"server rejected max_tokens={value}: {body}",
                        )
                    return _inconclusive(
                        f"max_tokens tier HTTP {response.status_code}: {body}", TIER_MAX_TOKENS_BOUND
                    )

                # 拿到任意一行非空 SSE 即证明服务端接受了这个输出预算，立刻断开
                async for line in response.aiter_lines():
                    if line and line.strip():
                        return ProbeOutcome(
                            verdict=VERDICT_QUALIFIED,
                            context_window_tokens=value,
                            tier=TIER_MAX_TOKENS_BOUND,
                            detail=(
                                f"server accepted max_tokens={value} (stream opened); "
                                "注意：静默截断型网关同样会接受，见模块 docstring 盲区 1"
                            ),
                        )
                last = _inconclusive(
                    f"max_tokens={value} accepted but stream produced no delta", TIER_MAX_TOKENS_BOUND
                )
        except Exception as exc:
            return _inconclusive(
                f"max_tokens tier request failed: {type(exc).__name__}: {exc}", TIER_MAX_TOKENS_BOUND
            )
    return last


async def probe_needle_tier(**_kwargs: Any) -> ProbeOutcome:
    """③ 档：尾部 needle 回读（填充至 ≈NEEDLE_FILL_RATIO×1M，max_tokens=NEEDLE_MAX_OUTPUT_TOKENS）。

    **本期不接线**（最小够用定案）。调用即返回 `inconclusive`，一次请求都不发。
    接线后它是唯一能区分「网关接受请求」与「模型真读进去了 1M」的手段，
    也是消除盲区 1（静默截断假阳性）的唯一途径。
    """
    return _inconclusive(
        "needle tier is interface-only in this iteration; not wired, no request sent",
        TIER_NEEDLE,
    )


_TIER_IMPLEMENTATIONS = {
    TIER_METADATA: probe_metadata_tier,
    TIER_MAX_TOKENS_BOUND: probe_max_tokens_bound_tier,
    TIER_NEEDLE: probe_needle_tier,
}


async def probe_model_context_window(
    *,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    trigger: str = TRIGGER_MANUAL,
    tiers: Optional[Tuple[str, ...]] = None,
    hint_window_tokens: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ProbeOutcome:
    """按 ①→②（→③ 若接线）顺序探一次，返回三态结论。**永不抛网络异常**。

    Args:
        trigger: 触发点，决定允许哪些档（daily 挂 needle 直接抛断言错误）。
        tiers: 要跑的档；默认 ①②（最小够用）。
        hint_window_tokens: 来自 `_KNOWN_CONTEXT_WINDOWS` 的**提示**，只用来决定
            ② 档从哪个刻度开始探，**不参与接受/拒绝判定**。
        client: 注入用的探测客户端（测试用 MockTransport）；None 时自建并在退出时关闭。
    """
    selected = tuple(tiers) if tiers else (TIER_METADATA, TIER_MAX_TOKENS_BOUND)
    for tier in selected:
        assert_tier_allowed(trigger, tier)

    owned_client = client is None
    probe_client = client or create_probe_client()
    ran: List[str] = []
    try:
        if TIER_METADATA in selected:
            ran.append(TIER_METADATA)
            outcome = await probe_metadata_tier(
                provider=provider, base_url=base_url, api_key=api_key, model=model, client=probe_client
            )
            if outcome.verdict != VERDICT_INCONCLUSIVE:
                return replace(outcome, tiers_run=tuple(ran))

        if TIER_MAX_TOKENS_BOUND in selected:
            ran.append(TIER_MAX_TOKENS_BOUND)
            hint = hint_window_tokens if isinstance(hint_window_tokens, int) and hint_window_tokens > 0 else 0
            probe_value = max(MAX_TOKENS_PROBE_VALUE, hint)
            outcome = await probe_max_tokens_bound_tier(
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                client=probe_client,
                probe_value=probe_value,
            )
            return replace(outcome, tiers_run=tuple(ran))

        if TIER_NEEDLE in selected:
            ran.append(TIER_NEEDLE)
            # 接口形状保留：接线时这里就是唯一有判据力的档
            outcome = await probe_needle_tier(
                provider=provider, base_url=base_url, api_key=api_key, model=model, client=probe_client
            )
            return replace(outcome, tiers_run=tuple(ran))

        return _inconclusive("no probe tier selected")
    finally:
        if owned_client:
            await probe_client.aclose()


# ========== 结论缓存：写 Settings.preferences（Text 存 JSON，非 JSON 列） ==========
PREFERENCES_KEY = "model_context_windows"
# 临界区取锁的兜底超时。锁**不可重入**，若将来有人在某个 preferences 临界区里
# 间接调了探测，这里会超时跳过缓存写入，而不是把生产挂死。
CACHE_LOCK_ACQUIRE_TIMEOUT_SECONDS = 5.0
# 进程内 memo：热路径（每次实发）避免多一次 SELECT。TTL 很短，
# 保证「用户在别处声明/复测完」最多 30 秒就对本进程可见。
MEMO_TTL_SECONDS = 30.0

_memo: Dict[Tuple[str, str], Tuple[float, ProbeOutcome]] = {}


def triple_key(provider: Optional[str], base_url: Optional[str], model: Optional[str]) -> str:
    """缓存主键 = (api_provider, api_base_url, llm_model) 三元组。任一要素变更即 miss。"""
    return "|".join(
        (
            (provider or "").strip().lower(),
            (base_url or "").strip().rstrip("/"),
            (model or "").strip(),
        )
    )


def _memo_get(user_id: str, key: str) -> Optional[ProbeOutcome]:
    cached = _memo.get((user_id, key))
    if not cached:
        return None
    expires_at, outcome = cached
    if expires_at < time.monotonic():
        _memo.pop((user_id, key), None)
        return None
    return outcome


def _memo_put(user_id: str, key: str, outcome: ProbeOutcome) -> None:
    _memo[(user_id, key)] = (time.monotonic() + MEMO_TTL_SECONDS, outcome)


def memo_clear() -> None:
    """测试/运维用：清空进程内 memo（缓存语义上是 per-user 的，但 memo 跨请求）。"""
    _memo.clear()


def _load_blob(raw: Optional[str]) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _outcome_to_dict(outcome: ProbeOutcome) -> Dict[str, Any]:
    return {
        "result": outcome.verdict,
        "source": outcome.source,
        "context_window_tokens": outcome.context_window_tokens,
        "tier": outcome.tier,
        "detail": outcome.detail,
        "checked_at": outcome.checked_at,
    }


def _dict_to_outcome(entry: Any) -> Optional[ProbeOutcome]:
    if not isinstance(entry, dict):
        return None
    verdict = entry.get("result")
    if verdict not in (VERDICT_QUALIFIED, VERDICT_UNQUALIFIED, VERDICT_INCONCLUSIVE):
        return None
    tokens = entry.get("context_window_tokens")
    return ProbeOutcome(
        verdict=verdict,
        context_window_tokens=int(tokens) if isinstance(tokens, int) and tokens > 0 else None,
        source=entry.get("source") or SOURCE_PROBE,
        tier=entry.get("tier"),
        detail=entry.get("detail"),
        checked_at=str(entry.get("checked_at") or ""),
    )


def _parsed_checked_at(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_due_for_daily_recheck(outcome: ProbeOutcome) -> bool:
    """结论是否已过「本自然日」（日频复测的计数口径：按自然日，不按 24h 滑动）。"""
    parsed = _parsed_checked_at(outcome.checked_at)
    if parsed is None:
        return True
    return parsed.astimezone(timezone.utc).date() < datetime.now(timezone.utc).date()


async def read_verdict(
    db: AsyncSession,
    user_id: str,
    *,
    provider: Optional[str],
    base_url: Optional[str],
    model: str,
) -> Optional[ProbeOutcome]:
    """读某三元组的既有结论（只读，不取写锁）。"""
    key = triple_key(provider, base_url, model)
    memoized = _memo_get(user_id, key)
    if memoized is not None:
        return memoized

    row = (
        await db.execute(select(Settings).where(Settings.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    entries = _load_blob(row.preferences).get(PREFERENCES_KEY)
    if not isinstance(entries, dict):
        return None
    outcome = _dict_to_outcome(entries.get(key))
    if outcome is not None:
        _memo_put(user_id, key, outcome)
    return outcome


async def write_verdict(
    db: AsyncSession,
    user_id: str,
    *,
    provider: Optional[str],
    base_url: Optional[str],
    model: str,
    outcome: ProbeOutcome,
) -> bool:
    """把结论写进 preferences blob（per-user 写锁内重新读取-合并-提交）。

    preferences 是整读整写的 JSON 字符串，探测写入与用户保存设置会互相抹键，
    所以必须走 `db_write_lock`（#56 的前置修复）。探测**绝不**在既有临界区内被调用；
    这里仍给取锁加超时兜底，万一将来有人误挂，代价是跳过缓存而不是死锁。
    """
    key = triple_key(provider, base_url, model)
    lock = await get_db_write_lock(user_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=CACHE_LOCK_ACQUIRE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            "上下文窗口结论缓存写入取锁超时，跳过本次缓存（探测疑似被挂在别的写锁临界区内）: user=%s key=%s",
            user_id,
            key,
        )
        # 结论本身有效，只是没能落库：写进 memo，避免误接线时每次派发都再等 5 秒
        _memo_put(user_id, key, outcome)
        return False

    try:
        # 临界区内必须绕过 ORM 身份映射重读，否则合并基线是进临界区之前的陈旧快照
        row = (
            await db.execute(
                select(Settings).where(Settings.user_id == user_id).execution_options(
                    populate_existing=True
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False

        blob = _load_blob(row.preferences)
        entries = blob.get(PREFERENCES_KEY)
        if not isinstance(entries, dict):
            entries = {}

        existing = _dict_to_outcome(entries.get(key))
        # 双向守卫「声明不是勾选放行通道」：
        # - inconclusive 不得抹掉用户显式声明（探测判不出 ≠ 用户没声明）
        # - 反之，有判据的探测结论（qualified/unqualified）也不得被声明覆盖掉——
        #   只有探测判不出时声明才有效（策略在 ensure_model_allowed，这里兜底）
        if existing is not None:
            if existing.source == SOURCE_USER_DECLARED and outcome.verdict == VERDICT_INCONCLUSIVE:
                return False
            if (
                outcome.source == SOURCE_USER_DECLARED
                and existing.source == SOURCE_PROBE
                and existing.verdict != VERDICT_INCONCLUSIVE
            ):
                logger.info(
                    "忽略对 %s 的窗口声明：已有带判据的探测结论 %s（声明仅适用于探测判不出的模型）",
                    key,
                    existing.verdict,
                )
                return False

        entries[key] = _outcome_to_dict(outcome)
        blob[PREFERENCES_KEY] = entries
        row.preferences = json.dumps(blob, ensure_ascii=False)
        await db.commit()
        _memo_put(user_id, key, outcome)
        return True
    except Exception as exc:
        logger.warning("写入上下文窗口结论失败（不影响本次判定）: %s", exc)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - 回滚本身再失败只记日志
            pass
        return False
    finally:
        lock.release()


def _all_verdicts(blob: Dict[str, Any]) -> List[Tuple[str, ProbeOutcome]]:
    entries = blob.get(PREFERENCES_KEY)
    if not isinstance(entries, dict):
        return []
    found: List[Tuple[str, ProbeOutcome]] = []
    for key, raw in entries.items():
        outcome = _dict_to_outcome(raw)
        if outcome is not None:
            found.append((str(key), outcome))
    return found


# ========== 失败契约 ==========
BELOW_MINIMUM_CODE = "validation.ai_model_below_minimum"


def gate_state_payload(model: str, outcome: ProbeOutcome) -> Dict[str, Any]:
    """门禁状态的**唯一**对外形态。

    两个消费方共用它，否则「保存被拒的信封」与「设置页读到的缓存结论」会各自漂移：
    - `_below_minimum_error`：拒绝时的 `params`
    - `describe_cached_gate_state`：步骤 5 的存量收口（只读缓存，供表单渲染三段数）

    `requires_explicit_declaration`：只有「探测判不出」才需要用户显式声明窗口；
    实测 `<1M` 时声明不是放行通道（计划 §2 表第 2 行）。
    注意本函数只会在非合格结论上被调用（`ensure_model_allowed` 对 qualified 直接放行），
    所以 `verdict != unqualified` 与 `verdict == inconclusive` 在此等价，取后者更直白。
    """
    return {
        "model": model,
        "min_window": MIN_CONTEXT_WINDOW_TOKENS,
        "verdict": outcome.verdict,
        "source": outcome.source,
        "measured_context_window_tokens": outcome.context_window_tokens,
        "requires_explicit_declaration": outcome.verdict == VERDICT_INCONCLUSIVE,
        "detail": outcome.detail,
        "checked_at": outcome.checked_at,
        "due_for_recheck": is_due_for_daily_recheck(outcome),
    }


def _below_minimum_error(model: str, outcome: ProbeOutcome) -> ApiError:
    """不合格 / 判不出都拒绝，且没有「勾选放行」通道。"""
    params = gate_state_payload(model, outcome)
    if outcome.verdict == VERDICT_UNQUALIFIED:
        detail = (
            f"模型 {model} 实测上下文窗口不足 {MIN_CONTEXT_WINDOW_TOKENS} tokens，"
            "已拒绝，请改用满足要求的模型"
        )
    else:
        detail = (
            f"无法判定模型 {model} 的上下文窗口，按「未知即不合格」处理；"
            f"请在表单里显式填写并确认 context_window_tokens >= {MIN_CONTEXT_WINDOW_TOKENS} 后重试"
        )
    return ApiError(code=BELOW_MINIMUM_CODE, detail=detail, params=params)


# ========== 日频复测（fire-and-forget） ==========
# 照 api/chapters.py 的后台任务范式：create_task + 强引用集合，否则 task 会被 GC。
_BACKGROUND_RECHECKS: set[asyncio.Task] = set()


async def _open_user_session(user_id: str):
    """后台复测必须自带会话：请求会话在响应返回后就关闭了。"""
    from app.database import get_engine  # 局部导入避免导入环
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engine = await get_engine(user_id)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


async def _run_daily_recheck(
    *,
    user_id: str,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    hint_window_tokens: Optional[int],
) -> None:
    """后台跑一次 ①② 并落缓存；任何异常只记日志，绝不影响已放行的本次请求。"""
    session = None
    try:
        outcome = await probe_model_context_window(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            trigger=TRIGGER_DAILY,
            hint_window_tokens=hint_window_tokens,
        )
        session = await _open_user_session(user_id)
        await write_verdict(
            session, user_id, provider=provider, base_url=base_url, model=model, outcome=outcome
        )
    except Exception as exc:
        logger.warning("日频上下文窗口复测失败（沿用既有结论）: user=%s model=%s err=%s", user_id, model, exc)
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:  # pragma: no cover
                pass


def _schedule_daily_recheck(
    *,
    user_id: str,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    hint_window_tokens: Optional[int],
) -> Optional[asyncio.Task]:
    """已有结论、只是今天没复测 ⇒ 本次按现有结论执行，复测扔后台（绝不 await）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 无事件循环时不复测
        return None
    task = loop.create_task(
        _run_daily_recheck(
            user_id=user_id,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            hint_window_tokens=hint_window_tokens,
        )
    )
    _BACKGROUND_RECHECKS.add(task)
    task.add_done_callback(_BACKGROUND_RECHECKS.discard)
    return task


def pending_recheck_tasks() -> List[asyncio.Task]:
    """暴露给测试：当前在飞的后台复测任务。"""
    return list(_BACKGROUND_RECHECKS)


async def resolve_verdict(
    *,
    user_id: str,
    db: AsyncSession,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    trigger: str = TRIGGER_DAILY,
    hint_window_tokens: Optional[int] = None,
) -> ProbeOutcome:
    """拿到「本次判定要用」的结论，区分两种缺结论。

    - **从未有过结论**（新三元组 / 老用户升级后首次遇到）⇒ **同步 await ①②** 再定论。
      成本是一次 GET + 一次极小请求，同步做完全可接受；反过来「先拒绝、探测还在后台跑」
      会让用户看到「AI 坏了，重试就好」这种非确定性故障。
    - **已有结论、只是今天没复测** ⇒ 立刻按现有结论执行 + fire-and-forget 后台复测。
      也就是说「日频非阻塞」只适用于复测，不适用于首次定论。
    """
    existing = await read_verdict(db, user_id, provider=provider, base_url=base_url, model=model)
    if existing is None:
        outcome = await probe_model_context_window(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            trigger=trigger,
            hint_window_tokens=hint_window_tokens,
        )
        await write_verdict(
            db, user_id, provider=provider, base_url=base_url, model=model, outcome=outcome
        )
        logger.info(
            "上下文窗口首次定论: user=%s model=%s verdict=%s tier=%s tokens=%s",
            user_id,
            model,
            outcome.verdict,
            outcome.tier,
            outcome.context_window_tokens,
        )
        return outcome

    if is_due_for_daily_recheck(existing):
        _schedule_daily_recheck(
            user_id=user_id,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            hint_window_tokens=hint_window_tokens,
        )
        logger.info(
            "上下文窗口结论已过本自然日，按现有结论放行并排队后台复测: user=%s model=%s verdict=%s",
            user_id,
            model,
            existing.verdict,
        )
    return existing


async def ensure_model_allowed(
    *,
    user_id: Optional[str],
    db: Optional[AsyncSession],
    provider: str,
    base_url: str,
    api_key: Optional[str],
    model: str,
    trigger: str = TRIGGER_DAILY,
    hint_window_tokens: Optional[int] = None,
    declared_tokens: Optional[int] = None,
) -> ProbeOutcome:
    """门禁：本次**实发**模型必须持有合格结论，否则抛 `validation.ai_model_below_minimum`。

    这是唯一汇合点（`AIService._require_model` 内），查的是缓存结论 ⇒ 热路径零网络、
    零 token。绝不允许「查不到就放行」。

    `declared_tokens`（仅保存路径会传）：**不能抢在实测之前**。先定论（必要时同步补测
    ①②），只有结论是 inconclusive/未登记时才接受显式声明；实测 <1M 的模型即使
    用户声明 >=1M 也照样拒——声明是给「探测判不出」留的出口，不是勾选放行通道。
    """
    if not user_id or db is None:
        # 未绑定用户的诊断实例（/settings/test、/check-function-calling 的临时 AIService、
        # 以及本模块自己的探测客户端）允许直通：它们不是产品 AI 派发路径，
        # 且「先探再决定」的探测端点必须能碰不合格模型，否则无法实测。
        # 产品路径全部经 get_user_ai_service*，user_id/db_session 必带 ⇒ 门禁必生效。
        return ProbeOutcome(verdict=VERDICT_QUALIFIED, source=SOURCE_PROBE, detail="unbound diagnostic service")

    outcome = await resolve_verdict(
        user_id=user_id,
        db=db,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        trigger=trigger,
        hint_window_tokens=hint_window_tokens,
    )
    if outcome.is_qualified:
        return outcome

    if outcome.verdict == VERDICT_INCONCLUSIVE and declared_tokens is not None:
        declared = user_declared_outcome(declared_tokens)  # 声明 <1M 在这里直接抛
        await write_verdict(
            db, user_id, provider=provider, base_url=base_url, model=model, outcome=declared
        )
        logger.info(
            "探测判不出，按用户显式声明放行: user=%s model=%s declared=%s",
            user_id,
            model,
            declared_tokens,
        )
        return declared

    raise _below_minimum_error(model, outcome)


def user_declared_outcome(declared_tokens: Optional[int]) -> ProbeOutcome:
    """把「用户显式声明的窗口」变成一条结论。声明 <1M 仍然拒绝（无勾选放行通道）。"""
    if not isinstance(declared_tokens, int) or isinstance(declared_tokens, bool):
        raise ApiError(
            code=BELOW_MINIMUM_CODE,
            detail=(
                f"未登记或探测不出的模型必须显式填写 context_window_tokens"
                f"（>= {MIN_CONTEXT_WINDOW_TOKENS}）才能保存"
            ),
            params={
                "min_window": MIN_CONTEXT_WINDOW_TOKENS,
                "requires_explicit_declaration": True,
            },
        )
    if declared_tokens < MIN_CONTEXT_WINDOW_TOKENS:
        raise ApiError(
            code=BELOW_MINIMUM_CODE,
            detail=(
                f"声明的上下文窗口 {declared_tokens} tokens 低于下限 "
                f"{MIN_CONTEXT_WINDOW_TOKENS} tokens，拒绝"
            ),
            params={
                "min_window": MIN_CONTEXT_WINDOW_TOKENS,
                "declared_context_window_tokens": declared_tokens,
                "requires_explicit_declaration": True,
            },
        )
    return ProbeOutcome(
        verdict=VERDICT_QUALIFIED,
        context_window_tokens=declared_tokens,
        source=SOURCE_USER_DECLARED,
        tier=None,
        detail=f"user declared {declared_tokens} tokens",
    )


async def get_effective_context_window(
    user_id: str,
    model: str,
    db: AsyncSession,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> int:
    """对外具名访问器：本次实发模型「当前生效」的上下文窗口 token 数。

    消费方两个：本模块的门禁校验、prompt 预算换算（计划 §4a 的拆分接缝）。

    失败契约（硬要求）：**没有合格结论就抛错**，禁止返回 0 / None / 保守猜测值。
    本仓库里 `0` 已有真实语义（=禁用全书注入，计划 §4b），返回 0 等于把功能静默关掉，
    正是这个需求要根除的失效形态。

    `provider`/`base_url` 建议显式传入（三元组精确命中）；省略时按模型名在缓存里找，
    命中 0 个或多于 1 个都抛错——那是「不确定」，不是「可以猜」。
    """
    if provider is not None or base_url is not None:
        outcome = await read_verdict(db, user_id, provider=provider, base_url=base_url, model=model)
    else:
        row = (
            await db.execute(select(Settings).where(Settings.user_id == user_id))
        ).scalar_one_or_none()
        blob = _load_blob(row.preferences) if row is not None else {}
        wanted = (model or "").strip()
        matches = [
            outcome
            for key, outcome in _all_verdicts(blob)
            if key.split("|")[-1].strip() == wanted
        ]
        outcome = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            raise ApiError(
                code=BELOW_MINIMUM_CODE,
                detail=(
                    f"模型 {model} 在多个 (provider, base_url) 上有不同结论，"
                    "请显式传入 provider/base_url"
                ),
                params={"model": model, "min_window": MIN_CONTEXT_WINDOW_TOKENS},
            )

    if outcome is None or not outcome.is_qualified:
        raise _below_minimum_error(model, outcome or _inconclusive("no verdict on file"))
    return int(outcome.context_window_tokens or MIN_CONTEXT_WINDOW_TOKENS)


# ========== 步骤 5：存量用户收口（只读缓存，供表单渲染） ==========
#
# 计划 §5 的触发点是「应用启动 / 首个 AI 请求」。本仓库选**首个 AI 请求**，理由是
# 启动路径做不了这件事，而不是它不重要：
#   1. `lifespan` 里没有任何用户上下文 ⇒ 拒绝了也无法「弹出表单」——表单只存在于某个
#      已登录用户的这一次会话里。
#   2. 启动要枚举用户就得扫全表、并用**每个用户自己的** API Key 向各家网关发探测请求：
#      无人对着屏幕判断结果，失败只能写进日志；一旦某家网关把 ② 档当真开始生成，
#      这就是无人监督的计费。
#   3. 拒绝的执行点本来就在 `AIService._require_model`（= 实发模型的唯一汇合点），
#      它天然就是「首个 AI 请求」。
#
# 本函数补的是另一半：拒绝**已经在派发路径发生**了，但缓存结论此前只躺在 preferences
# blob 里，界面上没有任何地方能读出它 —— 用户点「AI 功能」吃一个错误码、顺着引导回到
# 设置页，如果他的网关这一刻不可达，表单只会显示「未检测」。存量收口要的是「当场看到
# 需要重新选择模型」，所以缓存结论必须可寻址。
#
# ⚠️ 这里**绝不发探测请求**：页面渲染不是 AI 功能，§3 的「缺结论 ⇒ 同步补测 ①②」
# 只约束真要跑 AI 功能的那条路。若设置页一打开就打网关，等于给每个用户每次开设置
# 都加一次对外请求与一次潜在计费。
async def describe_cached_gate_state(
    db: AsyncSession,
    user_id: str,
    *,
    provider: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
) -> Optional[Dict[str, Any]]:
    """返回某三元组**已缓存**的门禁结论（表单可直接渲染），没有结论就返回 `None`。

    返回 `None` 的含义是「从未定论」，**不是**「不合格」：那种用户的首次补测发生在
    派发路径（`ensure_model_allowed`），由 §3 同步 await ①② 后再定论。据 `None` 直接
    拒绝就是计划 §5 明确禁止的写法。
    """
    wanted = (model or "").strip()
    if not wanted:
        # 未配置模型没有可判定的三元组；使用点报 validation.ai_model_not_configured
        return None
    outcome = await read_verdict(db, user_id, provider=provider, base_url=base_url, model=wanted)
    if outcome is None:
        return None
    return gate_state_payload(wanted, outcome)
