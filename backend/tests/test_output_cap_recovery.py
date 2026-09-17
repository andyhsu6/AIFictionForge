"""上游**输出**上限的自适应钳制测试（issue #147）。

修复背景：`OpenAIClient._build_payload` 曾按「网关」写死 `max_tokens <= 384000`
（对齐 DeepSeek V4 Flash 的 limit.output），但输出上限是 **(网关, 模型)** 的属性：
同一网关上的 `meituan/LongCat-2.0:free` 报的是 131072，于是每次请求都被上游
400 拒掉（`参数校验失败: /max_tokens: 384000 is not less or equal to 131072`），
表现为「切换到该模型后报错不给用」。

判据（与 #65 同源）：**只认网关自己报出的数字**，且只在它低于我们发出去的值时才钳。
报不出数、或归属含糊（同一分句里既讲输出又讲上下文）时一律不猜——钳太狠会静默截断
正文（#13/#45 的失效形态），钳不动只是再吃一次 400，两者代价不对称。
"""
import datetime
import json

import httpx
import pytest

from app.services.ai_clients.base_client import _http_client_pool
from app.services.ai_clients.openai_client import OpenAIClient
from app.services.ai_clients.output_caps import (
    clear_output_ceiling_memo,
    learn_output_ceiling,
    parse_reported_output_ceiling,
    resolve_output_ceiling,
)
from app.services.ai_config import AIClientConfig, RateLimitConfig, RetryConfig
from app.services.model_capability_probe import (
    VERDICT_INCONCLUSIVE,
    probe_max_tokens_bound_tier,
)

COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"
LONGCAT = "meituan/LongCat-2.0:free"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"

# 真机抓取：网关把上游报错又套了一层 error.message，数字在**内层**文本里
COMMANDCODE_400_BODY = json.dumps(
    {
        "error": {
            "message": json.dumps(
                {
                    "error": {
                        "message": "参数校验失败: \n/max_tokens: 384000 is not less or equal to 131072\n",
                        "type": "invalid_request_error",
                        "code": "invalid_parameter",
                    }
                },
                ensure_ascii=False,
            ),
            "type": "invalid_request_error",
        }
    }
)


@pytest.fixture(autouse=True)
def _clean_memo():
    clear_output_ceiling_memo()
    yield
    clear_output_ceiling_memo()


# ========== 解析：只认网关自己报出的输出上限 ==========
def test_parse_reads_the_number_the_gateway_reported():
    assert parse_reported_output_ceiling(COMMANDCODE_400_BODY, 384000) == 131072


def test_parse_ignores_a_ceiling_that_is_not_below_what_we_sent():
    # 报的数 >= 我们发的数 ⇒ 没什么可钳的，也不该记
    assert parse_reported_output_ceiling(COMMANDCODE_400_BODY, 131072) is None
    assert parse_reported_output_ceiling(COMMANDCODE_400_BODY, 65536) is None


def test_parse_refuses_to_read_a_context_bound_as_an_output_cap():
    # 只报上下文窗口的经典 400：它不说明一次能生成多少，钳它等于拿窗口当输出上限
    body = (
        "This model's maximum context length is 131072 tokens, however you "
        "requested 200000 tokens, please reduce the length of the messages"
    )
    assert parse_reported_output_ceiling(body, 200000) is None


def test_parse_refuses_when_a_clause_blames_both_output_and_context():
    # 归属都不确定 ⇒ 不猜（宁可再吃一次 400，也不要静默截断）
    body = "max_tokens 384000 exceeds context window 131072"
    assert parse_reported_output_ceiling(body, 384000) is None


def _cap_rejection_body(echoed: int, ceiling: int) -> str:
    """网关的报错会**回显它收到的值**，所以夹具也要照这个形状造。"""
    return json.dumps(
        {
            "error": {
                "message": json.dumps(
                    {
                        "error": {
                            "message": f"参数校验失败: \n/max_tokens: {echoed} is not less or equal to {ceiling}\n",
                            "type": "invalid_request_error",
                        }
                    },
                    ensure_ascii=False,
                ),
                "type": "invalid_request_error",
            }
        }
    )


def test_parse_reads_the_ceiling_the_error_compares_against_not_the_echoed_value():
    # ② 档按 900000 问，网关回显 900000 并报出真上限 131072。
    # 「取最大的候选」会把回显值当成上限 —— 学到的是一个仍然会被拒的数字。
    body = _cap_rejection_body(900000, 131072)
    assert parse_reported_output_ceiling(body, 900000) == 131072


def test_parse_keeps_the_binding_limit_when_several_are_named():
    body = "max_tokens must be <= 131072; for this tier max_tokens is at most 65536"
    assert parse_reported_output_ceiling(body, 384000) == 65536


def test_parse_ignores_a_stray_small_number_sharing_the_clause():
    # 候选取最小 ⇒ 同一分句里无关的小数字一旦被判成上限，就会把预算钳到不可用。
    # 只有**紧贴**在对比短语后面的数字才算上限。
    body = "/max_tokens 900000 is not less or equal to 131072 in region 2"
    assert parse_reported_output_ceiling(body, 900000) == 131072


def test_parse_handles_thousands_separators():
    assert parse_reported_output_ceiling("max_tokens 500000 exceeds limit of 131,072", 500000) == 131072


def test_parse_returns_none_for_bodies_that_name_no_number():
    assert parse_reported_output_ceiling("max_tokens is not supported by this model", 384000) is None


def test_parse_refuses_a_ceiling_phrase_that_is_not_about_output():
    # 分句里出现了「up to」这类对比短语，但整句讲的是别的东西 ⇒ 那个数字不是输出上限。
    # 认了它，预算就会被钳成 5。
    assert (
        parse_reported_output_ceiling("retry with up to 5 messages, the gateway rate-limits you", 1_000_000)
        is None
    )


def test_parse_ignores_digits_inside_unicode_escapes():
    # 同一条报错经 `ensure_ascii` 转义后的形态：`\uXXXX` 里贴着字母的数字串不是上限
    body = json.dumps(
        {"error": {"message": "参数校验失败: /max_tokens: 384000 is not less or equal to 131072"}}
    )
    assert "\\u" in body, "夹具本身就是转义形态才有意义"
    # 131072 已经是我们发出去的值 ⇒ 没有任何东西可钳，也不能从转义噪声里编一个数
    assert parse_reported_output_ceiling(body, 131072) is None
    assert parse_reported_output_ceiling(body, 384000) == 131072


# ========== 记忆与钳制：按 (base_url, model) 归键 ==========
def _payload_max_tokens(base_url, model, requested):
    client = OpenAIClient(api_key="k", base_url=base_url)
    payload = client._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model=model,
        temperature=0.7,
        max_tokens=requested,
    )
    return payload["max_tokens"]


def test_commandcode_is_no_longer_capped_by_a_hardcoded_number():
    # 旧行为：任何 commandcode 请求都被压到 384000，与模型无关
    assert _payload_max_tokens(COMMANDCODE_BASE, LONGCAT, 500000) == 500000


def test_payload_clamps_to_the_ceiling_the_gateway_reported():
    learn_output_ceiling(
        base_url=COMMANDCODE_BASE,
        model=LONGCAT,
        body=COMMANDCODE_400_BODY,
        requested_max_tokens=384000,
    )
    assert resolve_output_ceiling(COMMANDCODE_BASE, LONGCAT) == 131072
    assert _payload_max_tokens(COMMANDCODE_BASE, LONGCAT, 1_000_000) == 131072


def test_clamp_never_raises_a_smaller_user_budget():
    learn_output_ceiling(
        base_url=COMMANDCODE_BASE, model=LONGCAT, body=COMMANDCODE_400_BODY, requested_max_tokens=384000
    )
    assert _payload_max_tokens(COMMANDCODE_BASE, LONGCAT, 8192) == 8192


def test_ceiling_is_scoped_to_the_model_that_reported_it():
    # 同网关另一个模型没报过上限 ⇒ 不得继承别人的数字（那正是 #147 的成因）
    learn_output_ceiling(
        base_url=COMMANDCODE_BASE, model=LONGCAT, body=COMMANDCODE_400_BODY, requested_max_tokens=384000
    )
    assert resolve_output_ceiling(COMMANDCODE_BASE, DEEPSEEK) is None
    assert _payload_max_tokens(COMMANDCODE_BASE, DEEPSEEK, 500000) == 500000


def test_ceiling_is_scoped_to_the_base_url_too():
    learn_output_ceiling(
        base_url=COMMANDCODE_BASE, model=LONGCAT, body=COMMANDCODE_400_BODY, requested_max_tokens=384000
    )
    assert resolve_output_ceiling("https://other.test/v1", LONGCAT) is None


def test_learning_is_idempotent_and_only_moves_on_new_gateway_numbers():
    assert learn_output_ceiling(
        base_url=COMMANDCODE_BASE, model=LONGCAT, body=COMMANDCODE_400_BODY, requested_max_tokens=384000
    ) == 131072
    # 网关后来报出更高的上限（实测 131072→262144 发生过）：跟着更新，不锁死
    higher = COMMANDCODE_400_BODY.replace("131072", "262144").replace("384000", "400000")
    assert learn_output_ceiling(
        base_url=COMMANDCODE_BASE, model=LONGCAT, body=higher, requested_max_tokens=400000
    ) == 262144
    assert resolve_output_ceiling(COMMANDCODE_BASE, LONGCAT) == 262144


# ========== 非流式：同一次调用内纠正并重发 ==========
def _fast_config():
    return AIClientConfig(
        retry=RetryConfig(max_retries=3, base_delay=0.0),
        rate_limit=RateLimitConfig(request_delay=0.0),
    )


def _completion_body():
    return {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
    }


def _response(status: int, **kwargs) -> httpx.Response:
    """MockTransport 造出来的响应没有 `_elapsed`，而 base_client 会记录它。"""
    response = httpx.Response(status, **kwargs)
    response.elapsed = datetime.timedelta(0)
    return response


@pytest.mark.anyio
async def test_non_streaming_request_recovers_inside_the_same_call():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content.decode()))
        if len(sent) == 1:
            return _response(400, text=COMMANDCODE_400_BODY)
        return _response(200, json=_completion_body())

    client = OpenAIClient(api_key="k", base_url=COMMANDCODE_BASE, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            model=LONGCAT,
            temperature=0.7,
            max_tokens=1_000_000,
        )
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    assert result["content"] == "ok"
    assert sent[0]["max_tokens"] == 1_000_000  # 不再被写死的 384000 污染
    assert sent[1]["max_tokens"] == 131072


@pytest.mark.anyio
async def test_non_streaming_gives_up_instead_of_guessing_when_no_number_is_named():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content.decode()))
        return _response(400, json={"error": {"message": "max_tokens is not supported here"}})

    client = OpenAIClient(api_key="k", base_url=COMMANDCODE_BASE, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model=LONGCAT,
                temperature=0.7,
                max_tokens=1_000_000,
            )
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    assert resolve_output_ceiling(COMMANDCODE_BASE, LONGCAT) is None
    assert all(call["max_tokens"] == 1_000_000 for call in sent)


# ========== 流式：本次仍失败，但下一次不再撞同一堵墙 ==========
@pytest.mark.anyio
async def test_stream_failure_learns_the_ceiling_for_the_next_call():
    def handler(request):
        return httpx.Response(400, text=COMMANDCODE_400_BODY)

    client = OpenAIClient(api_key="k", base_url=COMMANDCODE_BASE, config=_fast_config())
    pooled = client.http_client
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            async for _ in client.chat_completion_stream(
                messages=[{"role": "user", "content": "hi"}],
                model=LONGCAT,
                temperature=0.7,
                max_tokens=1_000_000,
            ):
                pass
    finally:
        await client.http_client.aclose()
        await pooled.aclose()
        _http_client_pool.pop(client._get_client_key(), None)

    assert resolve_output_ceiling(COMMANDCODE_BASE, LONGCAT) == 131072


# ========== 保存路径顺手学到的上限：探测 tier ② 已经在打这个请求 ==========
@pytest.mark.anyio
async def test_probe_tier_two_primes_the_ceiling_while_it_judges_the_window():
    asked = []

    def handler(request):
        payload = json.loads(request.content.decode())
        asked.append(payload["max_tokens"])
        # 真网关会把收到的值回显在报错里 ⇒ 学的必须是它对比的那个数字
        return httpx.Response(400, text=_cap_rejection_body(payload["max_tokens"], 131072))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as probe_client:
        outcome = await probe_max_tokens_bound_tier(
            provider="openai",
            base_url=COMMANDCODE_BASE,
            api_key="k",
            model=LONGCAT,
            client=probe_client,
        )

    # 输出上限不构成窗口判据（#65）⇒ 仍然判不出，但数字该被学到
    assert asked == [900000]
    assert outcome.verdict == VERDICT_INCONCLUSIVE
    assert resolve_output_ceiling(COMMANDCODE_BASE, LONGCAT) == 131072
