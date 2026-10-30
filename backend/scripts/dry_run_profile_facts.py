#!/usr/bin/env python3
"""P3 dry-run：用真实章节对比新提示词（profile_facts）与旧提示词（legacy）的表现。

只读脚本（issue #190 P3）：
- 只执行 SELECT，绝不 commit；脚本结束前统一 rollback，不落库。
- 不新增依赖，不打印任何 API key / token（只从 Settings 里读，不复述）。
- 不修改任何既有文件。

度量：
- facts 模式 → quote 逐字命中率（facts_accepted / facts_in）、丢弃原因分布、
  各字段事实条数与渲染字符数、渲染结果被旧门禁 looks_like_profile_narration
  命中的条数，以及成本代理（prompt 字符数 / 输出 JSON 字符数 / 耗时）。
- legacy 模式 → 各字段非空条数、平均字符数、门禁命中字段与实体名。

输出：``<out-dir>/report.md`` + ``<out-dir>/raw.json``，stdout 末尾打印 SUMMARY 行。

用法示例::

    DATABASE_URL="sqlite+aiosqlite:////abs/path/backend/data/mumuai_novel.db" \
    PYTHONPATH=backend \\
    backend/.venv/bin/python backend/scripts/dry_run_profile_facts.py \
        --project-id <uuid> --user-id <uid> --chapters 15 --entity-count 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 与 backend/scripts/migrate.py 一致：允许直接 `python backend/scripts/<name>.py`
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.database import get_engine  # noqa: E402
from app.models import Chapter, Project, Settings  # noqa: E402
from app.services.book_import_service import BookImportService  # noqa: E402
from app.services.import_validators import (  # noqa: E402
    looks_like_profile_narration,
    validate_characters_batch,
)
from app.services.profile_facts import (  # noqa: E402
    FACT_FIELDS,
    parse_profile_payload,
    render_profile,
    validate_profile_facts,
)
from app.services.prompt_service import PromptService  # noqa: E402

# 与 book_import_service._build_requirements 里那段原话逐字一致（不带 careers_context）
EXCERPT_HEADER = (
    "\n\n【章节原文摘录】以下为已导入章节的正文摘录，"
    "请基于其中真实出现的人物、组织与剧情生成角色，"
    "避免编造与正文不符的内容。\n"
)

# 丢弃原因（可直接读作"为什么这条事实没进档案"）
DISCARD_REASON_KEYS = (
    "payload_invalid",
    "field_invalid",
    "claim_invalid",
    "quote_invalid",
    "quote_not_found",
    "chapter_invalid",
    "inferred_not_allowed",
    "org_personality_dropped",
)
# 非丢弃类的统计键
COUNTER_KEYS = ("facts_in", "facts_accepted", "chapter_corrected")


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    default_out = os.environ.get("DRY_RUN_OUT_DIR") or os.path.join(
        tempfile.gettempdir(), f"p3-profile-facts-{ts}"
    )

    parser = argparse.ArgumentParser(
        description="P3 dry-run：facts vs legacy 提示词真实章节对比（只读）",
    )
    parser.add_argument("--project-id", required=True, help="项目 UUID")
    parser.add_argument("--user-id", required=True, help="用户 ID（读取 settings 里的 provider/model）")
    parser.add_argument("--chapters", type=int, default=15, help="取前 N 章（0 = 全部），默认 15")
    parser.add_argument("--per-chapter-chars", type=int, default=3000, help="每章摘录字符数，默认 3000")
    parser.add_argument("--entity-count", type=int, default=8, help="本批次要求生成的实体数，默认 8")
    parser.add_argument("--modes", default="facts,legacy", help="逗号分隔，可选 facts/legacy")
    parser.add_argument("--max-retries", type=int, default=2, help="call_with_json_retry 重试次数，默认 2")
    parser.add_argument("--out-dir", default=default_out, help=f"输出目录，默认 {default_out}")
    args = parser.parse_args(argv)

    modes = [m.strip() for m in (args.modes or "").split(",") if m.strip()]
    unknown = [m for m in modes if m not in ("facts", "legacy")]
    if unknown:
        parser.error(f"--modes 含非法值: {unknown}（仅支持 facts / legacy）")
    if not modes:
        parser.error("--modes 不能为空")
    args.modes = modes

    if args.chapters < 0:
        parser.error("--chapters 不能为负（0 = 全部）")
    if args.per_chapter_chars <= 0:
        parser.error("--per-chapter-chars 必须为正数")
    if args.entity_count <= 0:
        parser.error("--entity-count 必须为正数")
    if args.max_retries < 0:
        parser.error("--max-retries 不能为负")
    return args


# --------------------------------------------------------------------------- #
# 数据加载（只读）
# --------------------------------------------------------------------------- #
async def _load_project_and_chapters(
    db: AsyncSession, project_id: str, limit: int
) -> tuple[Project, list[Chapter]]:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise RuntimeError(f"项目不存在: {project_id}")

    rows = (
        await db.execute(
            select(Chapter)
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.chapter_number)
        )
    ).scalars().all()

    chapters = [c for c in rows if (c.content or "").strip()]
    if limit > 0:
        chapters = chapters[:limit]
    if not chapters:
        raise RuntimeError(f"项目 {project_id} 没有正文非空的章节")
    return project, chapters


async def _user_settings_exists(db: AsyncSession, user_id: str) -> bool:
    """只做 SELECT：确认 settings 存在，避免 _build_user_ai_service 走"缺配置即 insert"分支。

    `_build_user_ai_service` 在用户没有 Settings 行时会 `db.add(...) + flush`
    （见 book_import_service.py）。本脚本承诺"绝不写库"，所以先查再决定是否调用；
    缺配置直接报错退出，而不是让它在事务里插入一行。
    """
    row = (
        await db.execute(select(Settings.id).where(Settings.user_id == user_id))
    ).scalar_one_or_none()
    return row is not None


def _build_requirements_text(chapters: list[Any], per_chapter_chars: int) -> str:
    """与 book_import_service._build_requirements 口径一致（不含 careers_context）。"""
    excerpt = BookImportService._build_chapter_excerpt(chapters, per_chapter_chars)
    return EXCERPT_HEADER + excerpt


def _build_prompt(mode: str, project: Project, requirements_text: str, entity_count: int) -> str:
    template = (
        PromptService.CHARACTERS_BATCH_GENERATION_FACTS
        if mode == "facts"
        else PromptService.CHARACTERS_BATCH_GENERATION
    )
    return PromptService.format_prompt(
        template,
        count=entity_count,
        time_period=project.world_time_period or "未设定",
        location=project.world_location or "未设定",
        atmosphere=project.world_atmosphere or "未设定",
        rules=project.world_rules or "未设定",
        theme=project.theme or "未设定",
        genre=project.genre or "未设定",
        requirements=requirements_text,
    )


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #
def _measure_facts(entities: list[Any], chapters_by_number: dict[int, str]) -> dict[str, Any]:
    totals: Counter = Counter()
    entity_rows: list[dict[str, Any]] = []
    field_facts: Counter = Counter()
    field_rendered_chars: defaultdict[str, list[int]] = defaultdict(list)
    narrated_hits: list[dict[str, str]] = []
    modes: Counter = Counter()

    for entity in entities:
        parsed = parse_profile_payload(entity)
        modes[parsed.mode] += 1
        kind = "organization" if entity.get("is_organization") is True else "character"
        result = validate_profile_facts(
            {"name": parsed.entity, "facts": parsed.facts}, chapters_by_number, kind=kind
        )
        totals.update(result.stats)

        for fact in result.facts:
            field_facts[fact["field"]] += 1

        rendered = render_profile(result.facts, kind=kind)
        rendered_chars = {field: len(rendered.get(field, "")) for field in FACT_FIELDS}
        for field, text in rendered.items():
            field_rendered_chars[field].append(len(text))

        narrated_fields = [
            field for field, text in rendered.items() if looks_like_profile_narration(text)
        ]
        for field in narrated_fields:
            narrated_hits.append({"name": parsed.entity, "field": field})

        entity_rows.append(
            {
                "name": parsed.entity,
                "parsed_mode": parsed.mode,
                "facts_in": result.stats.get("facts_in", 0),
                "facts_accepted": result.stats.get("facts_accepted", 0),
                "facts_by_field": dict(Counter(f["field"] for f in result.facts)),
                "discarded": {
                    key: result.stats.get(key, 0)
                    for key in DISCARD_REASON_KEYS
                    if result.stats.get(key, 0)
                },
                "chapter_corrected": result.stats.get("chapter_corrected", 0),
                "rendered_chars": rendered_chars,
                "narrated_fields": narrated_fields,
            }
        )

    facts_in = totals.get("facts_in", 0)
    accepted = totals.get("facts_accepted", 0)
    hit_rate = (accepted / facts_in) if facts_in else None

    return {
        "totals": dict(totals),
        "hit_rate": hit_rate,
        "payload_modes": dict(modes),
        "field_facts": dict(field_facts),
        "field_rendered_chars": {
            field: {
                "count": len(values),
                "total": sum(values),
                "avg": round(sum(values) / len(values), 1) if values else 0.0,
            }
            for field, values in field_rendered_chars.items()
        },
        "narrated_hits": narrated_hits,
        "narrated_entities": len({hit["name"] for hit in narrated_hits}),
        "entities": entity_rows,
    }


def _measure_legacy(entities: list[Any]) -> dict[str, Any]:
    entity_rows: list[dict[str, Any]] = []
    field_nonempty: Counter = Counter()
    field_chars: defaultdict[str, list[int]] = defaultdict(list)
    narrated_hits: list[dict[str, str]] = []
    unexpected_mode = 0

    for entity in entities:
        parsed = parse_profile_payload(entity)
        if parsed.mode != "legacy":
            unexpected_mode += 1

        legacy = parsed.legacy
        for field, text in legacy.items():
            field_nonempty[field] += 1
            field_chars[field].append(len(text))

        narrated_fields = [
            field for field, text in legacy.items() if looks_like_profile_narration(text)
        ]
        for field in narrated_fields:
            narrated_hits.append({"name": parsed.entity, "field": field})

        entity_rows.append(
            {
                "name": parsed.entity,
                "parsed_mode": parsed.mode,
                "fields_nonempty": sorted(legacy.keys()),
                "field_chars": {field: len(text) for field, text in legacy.items()},
                "narrated_fields": narrated_fields,
            }
        )

    return {
        "unexpected_mode": unexpected_mode,
        "field_nonempty_counts": dict(field_nonempty),
        "field_chars": {
            field: {
                "count": len(values),
                "total": sum(values),
                "avg": round(sum(values) / len(values), 1) if values else 0.0,
            }
            for field, values in field_chars.items()
        },
        "narrated_hits": narrated_hits,
        "narrated_entities": len({hit["name"] for hit in narrated_hits}),
        "entities": entity_rows,
    }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.3f}"


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def _render_facts_section(mode_data: dict[str, Any]) -> str:
    metrics = mode_data.get("metrics") or {}
    out = ["### 指标", ""]

    if "error" in mode_data:
        out.append(f"**运行失败**：`{mode_data['error']}`")
        out.append("")
        if mode_data.get("traceback"):
            out.append("```")
            out.append(mode_data["traceback"].strip())
            out.append("```")
            out.append("")
        return "\n".join(out)

    if mode_data.get("unexpected_payload"):
        out.append("**模型返回不是 JSON 数组（unexpected_payload），本模式度量已跳过。**")
        out.append("")
        return "\n".join(out)

    totals = metrics["totals"]
    entities = mode_data.get("entities_count", 0)
    facts_in = totals.get("facts_in", 0)
    accepted = totals.get("facts_accepted", 0)

    out.append(
        _md_table(
            ["指标", "值"],
            [
                ["实体数", entities],
                ["facts_in", facts_in],
                ["facts_accepted", accepted],
                ["quote 逐字命中率 (accepted/in)", _fmt_rate(metrics["hit_rate"])],
                ["chapter_corrected（章号被纠正，未丢弃）", totals.get("chapter_corrected", 0)],
                ["payload 判定分布", metrics["payload_modes"] or "{}"],
                ["prompt 字符数", mode_data.get("prompt_chars", 0)],
                ["输出 JSON 字符数", mode_data.get("out_chars", 0)],
                ["耗时（秒）", mode_data.get("seconds", 0.0)],
            ],
        )
    )
    out.append("")

    out.append("#### 丢弃原因分布")
    out.append("")
    reason_rows = [[key, totals.get(key, 0)] for key in DISCARD_REASON_KEYS]
    reason_rows.append(["（合计丢弃）", sum(totals.get(key, 0) for key in DISCARD_REASON_KEYS)])
    out.append(_md_table(["原因键", "条数"], reason_rows))
    out.append("")

    out.append("#### 字段分布（通过校验的事实条数 / 渲染结果）")
    out.append("")
    field_rows = []
    for field in FACT_FIELDS:
        chars = metrics["field_rendered_chars"].get(field, {})
        field_rows.append(
            [
                field,
                metrics["field_facts"].get(field, 0),
                chars.get("count", 0),
                chars.get("total", 0),
                chars.get("avg", 0.0),
            ]
        )
    out.append(_md_table(["字段", "事实条数", "有渲染的实体数", "渲染总字符", "平均字符"], field_rows))
    out.append("")

    out.append("#### 旧门禁 looks_like_profile_narration 命中")
    out.append("")
    out.append(
        f"- 命中字段条数：**{len(metrics['narrated_hits'])}**，涉及实体 {metrics['narrated_entities']} 个"
    )
    for hit in metrics["narrated_hits"]:
        out.append(f"  - `{hit['name']}` / `{hit['field']}`")
    out.append("")

    out.append("#### 实体明细")
    out.append("")
    rows = []
    for row in mode_data["entities"]:
        rows.append(
            [
                row["name"] or "(无名)",
                row["parsed_mode"],
                row["facts_in"],
                row["facts_accepted"],
                json.dumps(row["discarded"], ensure_ascii=False) if row["discarded"] else "-",
                "/".join(
                    f"{field}:{row['rendered_chars'].get(field, 0)}" for field in FACT_FIELDS
                ),
                ",".join(row["narrated_fields"]) or "-",
            ]
        )
    out.append(
        _md_table(
            ["实体", "返回格式", "facts_in", "accepted", "丢弃原因", "渲染字符", "门禁命中字段"],
            rows,
        )
    )
    out.append("")
    return "\n".join(out)


def _render_legacy_section(mode_data: dict[str, Any]) -> str:
    metrics = mode_data.get("metrics") or {}
    out = ["### 指标", ""]

    if "error" in mode_data:
        out.append(f"**运行失败**：`{mode_data['error']}`")
        out.append("")
        if mode_data.get("traceback"):
            out.append("```")
            out.append(mode_data["traceback"].strip())
            out.append("```")
            out.append("")
        return "\n".join(out)

    if mode_data.get("unexpected_payload"):
        out.append("**模型返回不是 JSON 数组（unexpected_payload），本模式度量已跳过。**")
        out.append("")
        return "\n".join(out)

    out.append(
        _md_table(
            ["指标", "值"],
            [
                ["实体数", mode_data.get("entities_count", 0)],
                ["unexpected_mode（未按旧格式返回）", metrics["unexpected_mode"]],
                ["prompt 字符数", mode_data.get("prompt_chars", 0)],
                ["输出 JSON 字符数", mode_data.get("out_chars", 0)],
                ["耗时（秒）", mode_data.get("seconds", 0.0)],
            ],
        )
    )
    out.append("")

    out.append("#### 各字段非空条数 / 字符统计")
    out.append("")
    rows = []
    for field in FACT_FIELDS:
        chars = metrics["field_chars"].get(field, {})
        rows.append(
            [
                field,
                metrics["field_nonempty_counts"].get(field, 0),
                chars.get("total", 0),
                chars.get("avg", 0.0),
            ]
        )
    out.append(_md_table(["字段", "非空条数", "总字符", "平均字符"], rows))
    out.append("")

    out.append("#### 旧门禁 looks_like_profile_narration 命中")
    out.append("")
    out.append(
        f"- 命中字段条数：**{len(metrics['narrated_hits'])}**，涉及实体 {metrics['narrated_entities']} 个"
    )
    for hit in metrics["narrated_hits"]:
        out.append(f"  - `{hit['name']}` / `{hit['field']}`")
    out.append("")

    out.append("#### 实体明细")
    out.append("")
    rows = []
    for row in mode_data["entities"]:
        rows.append(
            [
                row["name"] or "(无名)",
                row["parsed_mode"],
                ",".join(row["fields_nonempty"]) or "-",
                "/".join(f"{field}:{chars}" for field, chars in row["field_chars"].items()) or "-",
                ",".join(row["narrated_fields"]) or "-",
            ]
        )
    out.append(_md_table(["实体", "返回格式", "非空字段", "字段字符", "门禁命中字段"], rows))
    out.append("")
    return "\n".join(out)


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    run = payload["run"]
    data = payload["data"]
    lines: list[str] = []
    lines.append("# P3 dry-run：profile_facts vs legacy")
    lines.append("")
    lines.append(f"生成时间：{run['started_at']}")
    lines.append("")
    lines.append("## 运行参数")
    lines.append("")
    lines.append(
        _md_table(
            ["参数", "值"],
            [
                ["project_id", run["args"]["project_id"]],
                ["user_id", run["args"]["user_id"]],
                ["chapters", run["args"]["chapters"]],
                ["per_chapter_chars", run["args"]["per_chapter_chars"]],
                ["entity_count", run["args"]["entity_count"]],
                ["modes", ",".join(run["args"]["modes"])],
                ["max_retries", run["args"]["max_retries"]],
                ["out_dir", run["args"]["out_dir"]],
            ],
        )
    )
    lines.append("")
    lines.append("## 数据概况")
    lines.append("")
    lines.append(
        _md_table(
            ["项", "值"],
            [
                ["实际章节数", data["chapter_count"]],
                ["章节号范围", data["chapter_range"]],
                ["摘录字符数", data["excerpt_chars"]],
                ["全文总字符数", data["full_chars"]],
                ["requirements 字符数", data["requirements_chars"]],
            ],
        )
    )
    lines.append("")
    lines.append("每章字数：" + ", ".join(f"第{n}章={c}" for n, c in data["chapter_chars"].items()))
    lines.append("")

    for mode in run["args"]["modes"]:
        mode_data = payload["modes"][mode]
        lines.append(f"## 模式：`{mode}`")
        lines.append("")
        lines.append(
            "模板：" + ("`CHARACTERS_BATCH_GENERATION_FACTS`" if mode == "facts" else "`CHARACTERS_BATCH_GENERATION`")
        )
        lines.append("")
        section = (
            _render_facts_section(mode_data) if mode == "facts" else _render_legacy_section(mode_data)
        )
        lines.append(section)
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 单模式执行
# --------------------------------------------------------------------------- #
async def _run_mode(
    mode: str,
    *,
    db: AsyncSession,
    project: Project,
    chapters: list[Any],
    chapters_by_number: dict[int, str],
    args: argparse.Namespace,
    ai_service: Any,
) -> dict[str, Any]:
    requirements_text = _build_requirements_text(chapters, args.per_chapter_chars)
    prompt = _build_prompt(mode, project, requirements_text, args.entity_count)

    started = time.perf_counter()
    data = await ai_service.call_with_json_retry(
        prompt=prompt,
        max_retries=args.max_retries,
        expected_type="array",
        validator=validate_characters_batch,
        auto_mcp=False,
    )
    seconds = round(time.perf_counter() - started, 1)
    out_chars = len(json.dumps(data, ensure_ascii=False, default=str))

    result: dict[str, Any] = {
        "prompt_chars": len(prompt),
        "out_chars": out_chars,
        "seconds": seconds,
        "raw": data,
        "entities_count": 0,
        "entities": [],
        "unexpected_payload": False,
    }

    if not isinstance(data, list):
        result["unexpected_payload"] = True
        return result

    result["entities_count"] = len(data)
    if mode == "facts":
        metrics = _measure_facts(data, chapters_by_number)
    else:
        metrics = _measure_legacy(data)

    result["metrics"] = metrics
    result["entities"] = metrics.pop("entities")
    return result


def _summary_line(mode: str, mode_data: dict[str, Any]) -> str:
    if "error" in mode_data or mode_data.get("unexpected_payload"):
        status = "error" if "error" in mode_data else "unexpected_payload"
        return (
            f"SUMMARY mode={mode} entities=0 status={status} "
            f"prompt_chars={mode_data.get('prompt_chars', 0)} "
            f"out_chars={mode_data.get('out_chars', 0)} seconds={mode_data.get('seconds', 0.0)}"
        )

    metrics = mode_data.get("metrics") or {}
    head = (
        f"SUMMARY mode={mode} entities={mode_data.get('entities_count', 0)}"
    )
    if mode == "facts":
        totals = metrics.get("totals", {})
        head += (
            f" facts_in={totals.get('facts_in', 0)}"
            f" accepted={totals.get('facts_accepted', 0)}"
            f" hit_rate={_fmt_rate(metrics.get('hit_rate'))}"
        )
    else:
        head += (
            f" unexpected_mode={metrics.get('unexpected_mode', 0)}"
            f" narrated={len(metrics.get('narrated_hits', []))}"
        )
    return (
        head
        + f" prompt_chars={mode_data.get('prompt_chars', 0)}"
        + f" out_chars={mode_data.get('out_chars', 0)}"
        + f" seconds={mode_data.get('seconds', 0.0)}"
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "run": {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "args": {
                "project_id": args.project_id,
                "user_id": args.user_id,
                "chapters": args.chapters,
                "per_chapter_chars": args.per_chapter_chars,
                "entity_count": args.entity_count,
                "modes": args.modes,
                "max_retries": args.max_retries,
                "out_dir": str(out_dir),
            },
        },
        "data": {},
        "modes": {},
    }
    summary_lines: list[str] = []
    success_count = 0

    # 复用 app.database 的引擎工厂（不自造 engine），会话写法与 get_db 一致
    engine = await get_engine(args.user_id)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        try:
            project, chapters = await _load_project_and_chapters(
                db, args.project_id, args.chapters
            )
            # 校验引文必须用全文，不能用截断后的 excerpt
            chapters_by_number = {c.chapter_number: (c.content or "") for c in chapters}
            numbers = sorted(chapters_by_number)
            excerpt = BookImportService._build_chapter_excerpt(chapters, args.per_chapter_chars)
            requirements_text = _build_requirements_text(chapters, args.per_chapter_chars)

            payload["data"] = {
                "chapter_count": len(chapters),
                "chapter_range": f"{numbers[0]}-{numbers[-1]}",
                "chapter_chars": {n: len(chapters_by_number[n]) for n in numbers},
                "excerpt_chars": len(excerpt),
                "full_chars": sum(len(v) for v in chapters_by_number.values()),
                "requirements_chars": len(requirements_text),
            }

            ai_service = None
            if await _user_settings_exists(db, args.user_id):
                ai_service = await BookImportService()._build_user_ai_service(
                    db=db, user_id=args.user_id
                )

            for mode in args.modes:
                if ai_service is None:
                    mode_data = {
                        "error": (
                            f"用户 {args.user_id} 没有 Settings 记录。为保证不写库，"
                            "脚本不会让 _build_user_ai_service 走 insert 分支；"
                            "请先在应用里配置一次 AI 设置后再跑。"
                        ),
                        "prompt_chars": 0,
                        "out_chars": 0,
                        "seconds": 0.0,
                    }
                else:
                    try:
                        mode_data = await _run_mode(
                            mode,
                            db=db,
                            project=project,
                            chapters=chapters,
                            chapters_by_number=chapters_by_number,
                            args=args,
                            ai_service=ai_service,
                        )
                        success_count += 1
                    except Exception as exc:  # 单模式失败不影响另一模式
                        mode_data = {
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(),
                            "prompt_chars": 0,
                            "out_chars": 0,
                            "seconds": 0.0,
                        }
                payload["modes"][mode] = mode_data
                summary_lines.append(_summary_line(mode, mode_data))
        finally:
            # 只读承诺：无论成功失败都不提交，统一回滚
            try:
                await db.rollback()
            except Exception:
                pass

    (out_dir / "raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _write_report(out_dir / "report.md", payload)

    print(f"REPORT {out_dir / 'report.md'}")
    print(f"RAW {out_dir / 'raw.json'}")
    for line in summary_lines:
        print(line)

    return 0 if success_count > 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
