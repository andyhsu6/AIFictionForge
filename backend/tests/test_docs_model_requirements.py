"""需求 #55 步骤 6：对外文档与配置口径的守卫测试。

文档一旦「外推」就比代码更危险，所以这里把 README 的措辞钉在**已实现的行为**上：

1. **下限数字与代码同源**：README 顶栏公告写的 1M 必须等于
   `MIN_CONTEXT_WINDOW_TOKENS`（改常量而忘改文档 ⇒ 红）。
2. **破坏性公告必须在 README 顶部**：出现在特性章节**之前**，且带版本号说明。
3. **两个盲区必须写出来**：③ needle 档未接线（静默截断型网关可通过 ①②）、
   日频复测之间换模型会按旧结论放行。少写一条就是过度承诺 ⇒ 红。
4. **不写具体推荐模型名**：文档里不得出现兜底示例模型名或「推荐某某模型」条目，
   设置页「模型名称」等字段的提示文案（`form.*Tooltip`）同样不得点名具体模型。
5. **`DEFAULT_MODEL` 不再被读取**：`docker-compose.yml` / `.env.example` / README
   都不得再出现**未注释的** `DEFAULT_MODEL=` 赋值（那是宣传一个静默失效的开关），
   且必须写明它已废弃。
6. **两份 README 结构等价**：章节/表格行/引用块/列表条目数量一致，防止中英漂移。
7. **旧的分级降级口径不得复活**：`128K 可运行但降级` 一类表述不得再出现。

测试值与文案一律中性占位，不含任何导入原文、书名或角色人名（AGENTS.md 脱敏硬约束）。
"""
import os
import re

from app.services.model_capability_probe import MIN_CONTEXT_WINDOW_TOKENS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STALE_PATTERNS = (
    "128K – 1M",
    "128K - 1M",
    "可运行但降级",
    "自动降级为最近章节摘要",
    "128K 模型也能用",
    "Runs with degradation",
)

RECOMMENDED_MODEL_NAMES = (
    "gpt-4o-mini",
    "DeepSeek V4",
    "Gemini 2.0 Pro",
)

# 步骤 3 之后**保证被拒**的模型名。文档口径（0a63df7）是不推荐具体模型名，
# 而这两台曾经写在设置页「模型名称」字段的提示里——写在活字段上等于给用户预填一个必拒值。
GUARANTEED_REJECTED_MODEL_NAMES = (
    "gpt-4",
    "gpt-3.5-turbo",
)


def _read(*parts: str) -> str:
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


README_EN = _read("README.md")
README_ZH = _read("README.zh-CN.md")
ENV_EXAMPLE = _read("backend", ".env.example")
COMPOSE = _read("docker-compose.yml")


def _section(text: str, heading: str) -> str:
    """取 `## <heading>` 到下一个 `## ` 之间的正文（含标题行）。"""
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return text[start:] if end == -1 else text[start : start + len(heading) + end]


def _counts(block: str) -> dict:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    return {
        "subsections": sum(1 for ln in lines if ln.startswith("### ")),
        "table_rows": sum(1 for ln in lines if ln.startswith("|")),
        "quotes": sum(1 for ln in lines if ln.startswith(">")),
        "ordered": sum(1 for ln in lines if re.match(r"^\d+\.", ln)),
    }


def _uncommented_assignments(text: str) -> list:
    # 同时覆盖 shell 形态（`DEFAULT_MODEL=`）与 compose 的 YAML 列表形态
    # （`- DEFAULT_MODEL=${DEFAULT_MODEL:-...}`）——变异自证时发现只匹配前者会漏掉 compose。
    return re.findall(r"^\s*-?\s*DEFAULT_MODEL\s*=", text, flags=re.MULTILINE)


def test_readme_states_the_same_floor_as_the_code_constant():
    spelled = f"{MIN_CONTEXT_WINDOW_TOKENS:,}"
    assert MIN_CONTEXT_WINDOW_TOKENS == 1_000_000
    for text in (README_EN, README_ZH):
        assert spelled in text, f"README 未写出与常量同源的下限数字 {spelled}"


def test_breaking_change_notice_is_at_the_top_of_both_readmes():
    en_head = README_EN.index("## ✨ Features")
    zh_head = README_ZH.index("## ✨ 特性")
    assert "Breaking change" in README_EN[:en_head], "破坏性公告不在 README 顶部"
    assert "破坏性变更" in README_ZH[:zh_head], "破坏性公告不在 README 顶部"
    # 版本号说明：必须点名最近的已发布版本，而不是含糊说「新版」
    assert "v1.5.4" in README_EN[:en_head]
    assert "v1.5.4" in README_ZH[:zh_head]


def test_both_readmes_disclose_the_two_probe_blind_spots():
    for text in (README_EN, README_ZH):
        assert ("not wired" in text or "未接线" in text), "盲区一（③ 档未接线）未写出"
        assert ("truncat" in text.lower() or "截断" in text), "盲区一（静默截断型网关）未写出"
        assert ("stale verdict" in text or "过期结论" in text), "盲区二（旧结论放行）未写出"
        assert ("UTC" in text), "盲区二的复测口径（UTC 自然日）未写出"
        # 不得外推成「任何时刻都不会被绕过」
        assert ("can never be bypassed" in text or "都不会被绕过" in text), (
            "缺少「不等于永远无法绕过」的自我限定"
        )


def test_readmes_do_not_recommend_specific_models():
    for name, text in (("README.md", README_EN), ("README.zh-CN.md", README_ZH)):
        for model in RECOMMENDED_MODEL_NAMES:
            assert model not in text, f"{name} 仍在推荐具体模型名 {model!r}"


def _settings_form_tooltips() -> dict:
    """两份 settings 命名空间里 `form.*Tooltip` 的文案：`文件名 -> {键: 文案}`。

    口径同 README：不推荐具体模型名。这里是**活字段**上的提示，写错比文档更贵——
    评审第 4 项抓到的正是「`llm_model` 字段推荐两台保证被拒的模型」。
    """
    import json

    found = {}
    for locale in ("zh", "en"):
        path = ("frontend", "src", "locales", locale, "settings.json")
        payload = json.loads(_read(*path))
        form = payload.get("form", {})
        found["/".join(path)] = {
            key: value for key, value in form.items() if key.endswith("Tooltip")
        }
    return found


def test_settings_form_copy_does_not_recommend_specific_models():
    banned = RECOMMENDED_MODEL_NAMES + GUARANTEED_REJECTED_MODEL_NAMES
    tooltips = _settings_form_tooltips()
    assert len(tooltips) == 2, "两份 settings 文案都必须可寻址"
    for name, values in tooltips.items():
        assert "llmModelTooltip" in values, f"{name} 的「模型名称」字段提示消失了"
        for key, text in values.items():
            for model in banned:
                assert model not in text, f"{name} 的 form.{key} 仍在推荐具体模型名 {model!r}"


def test_default_model_env_var_is_no_longer_advertised_as_a_setting():
    for name, text in (
        ("docker-compose.yml", COMPOSE),
        ("backend/.env.example", ENV_EXAMPLE),
        ("README.md", README_EN),
        ("README.zh-CN.md", README_ZH),
    ):
        assert not _uncommented_assignments(text), f"{name} 仍有未注释的 DEFAULT_MODEL 赋值"
    assert "DEFAULT_MODEL" in ENV_EXAMPLE and "DEFAULT_MODEL" in COMPOSE, (
        "配置文件须写明该变量已废弃，而不是悄悄删掉让读者找不到"
    )
    assert "DEFAULT_MODEL" in README_EN and "DEFAULT_MODEL" in README_ZH


def test_small_model_tier_wording_does_not_come_back():
    for name, text in (("README.md", README_EN), ("README.zh-CN.md", README_ZH)):
        for stale in STALE_PATTERNS:
            assert stale not in text, f"{name} 仍写着已废除的降级口径：{stale}"


def test_zh_and_en_model_requirement_sections_are_structurally_equivalent():
    en = _section(README_EN, "## 🧠 Model Requirements")
    zh = _section(README_ZH, "## 🧠 模型要求")
    assert _counts(en) == _counts(zh), f"中英「模型要求」章节结构漂移：{_counts(en)} != {_counts(zh)}"
    assert _counts(en)["table_rows"] >= 4 and _counts(en)["quotes"] >= 4
    # 状态表：恰好一行「支持」，其余一律「不支持」
    rows = [ln for ln in en.splitlines() if ln.startswith("|") and ln.count("|") >= 3]
    supported = [ln for ln in rows if "✅" in ln]
    rejected = [ln for ln in rows if "❌" in ln]
    assert len(supported) == 1 and len(rejected) == 2, "状态表必须只有一档支持"
    zh_rows = [ln for ln in zh.splitlines() if ln.startswith("|") and ln.count("|") >= 3]
    assert sum(1 for ln in zh_rows if "✅" in ln) == 1
    assert sum(1 for ln in zh_rows if "❌" in ln) == 2
