"""测量脚本的两个新入口：`backfill_profiles.py --kind/--dump-facts`、
`sample_golden_profile_facts.py --facts-tsv`（issue #198 最后一轮）。

全部用例不连 DB、不调模型：实体用 ``SimpleNamespace``，脚本模块用 importlib 按文件
加载（``backend/scripts`` 没有 ``__init__.py``）。TSV 内容一律占位文本。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    # backfill_profiles 顶层 `import sweep_profiles_readonly as sweep` 依赖脚本目录
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _entity(name: str, is_organization: bool) -> SimpleNamespace:
    return SimpleNamespace(name=name, is_organization=is_organization)


# --------------------------------------------------------------------------- #
# 任务 A：--kind 过滤
# --------------------------------------------------------------------------- #
def test_keep_kind_filters_entities_by_is_organization():
    backfill = _load("backfill_profiles")
    rows = [
        _entity("e-person-a", False),
        _entity("e-person-b", False),
        _entity("e-org-a", True),
        _entity("e-org-b", True),
    ]

    def kept(kind: str) -> list[str]:
        return [row.name for row in rows if backfill._keep_kind(row, kind)]

    assert kept("all") == ["e-person-a", "e-person-b", "e-org-a", "e-org-b"]
    assert kept("person") == ["e-person-a", "e-person-b"]
    assert kept("organization") == ["e-org-a", "e-org-b"]


# --------------------------------------------------------------------------- #
# 任务 A：--dump-facts 输出
# --------------------------------------------------------------------------- #
def test_write_facts_dump_roundtrips_quotes_tabs_and_newlines(tmp_path: Path):
    """写侧 csv.writer / 读侧 csv.DictReader 同一方言：引号、制表符、换行都不丢原文。"""
    backfill = _load("backfill_profiles")
    sample = _load("sample_golden_profile_facts")

    # 四种最容易踩坑的形态：以引号开头、成对引号、含制表符、含换行
    claims = ['"代号甲"是其前身', '外号"甲"的旧人', "行内\t指标", "第一行\n第二行"]
    quotes = ['"原话甲"开头', '他自称"甲"', "含\t制表符的原文", "跨\n行原文"]

    projects = [
        {
            "project_id": "proj-x",
            "fact_rows": [
                {
                    "project_id": "proj-x",
                    "kind": "organization",
                    "entity": "占位实体",
                    "field": "personality",
                    "claim": claim,
                    "quote": quote,
                    "chapter_number": idx,
                }
                for idx, (claim, quote) in enumerate(zip(claims, quotes), start=1)
            ],
        }
    ]
    out = tmp_path / "facts.tsv"
    written = backfill._write_facts_dump(out, projects)

    text = out.read_text(encoding="utf-8")
    assert written == 4
    # 表头逐字固定（表头本身不含需引用的字符，csv 不会给它加引号）
    assert text.split("\n", 1)[0] == "project_id\tkind\tentity\tfield\tclaim\tquote\tchapter_number"
    assert text.endswith("\n")

    items, has_kind = sample._read_facts_tsv(out)
    assert has_kind is True
    assert [item["text"] for item in items] == claims
    assert [item["quote"] for item in items] == quotes
    assert [item["entity"] for item in items] == ["占位实体"] * 4
    assert [item["is_organization"] for item in items] == [1] * 4


def test_write_facts_dump_is_csv_quoted_not_flattened(tmp_path: Path):
    """摊平会丢原文；这里确认内嵌制表符/换行是被引用而不是被替换成空格。"""
    backfill = _load("backfill_profiles")
    out = tmp_path / "facts.tsv"
    backfill._write_facts_dump(
        out,
        [
            {
                "project_id": "proj-x",
                "fact_rows": [
                    {
                        "project_id": "proj-x",
                        "kind": "person",
                        "entity": "占位人物",
                        "field": "personality",
                        "claim": 'a\tb"c\nd',
                        "quote": '"q"',
                        "chapter_number": 2,
                    }
                ],
            }
        ],
    )
    text = out.read_text(encoding="utf-8")
    assert "\ta\tb" not in text  # 没有裸制表符混进 claim
    assert '"a\tb""c\nd"' in text  # csv 引号 + 双写引号


# --------------------------------------------------------------------------- #
# 任务 B：--facts-tsv 读候选
# --------------------------------------------------------------------------- #
FACT_HEADER = "project_id\tkind\tentity\tfield\tclaim\tquote\tchapter_number"


def _write_tsv(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_read_facts_tsv_uses_kind_column(tmp_path: Path):
    sample = _load("sample_golden_profile_facts")
    path = _write_tsv(
        tmp_path / "facts.tsv",
        FACT_HEADER,
        [
            "proj-x\torganization\t占位组织\tpersonality\t占位性情\t占位引文\t3",
            "proj-x\tperson\t占位人物\tbackground\t占位背景\t占位引文\t5",
        ],
    )
    items, has_kind = sample._read_facts_tsv(path)

    assert has_kind is True
    assert [(i["entity"], i["is_organization"], i["field"]) for i in items] == [
        ("占位组织", 1, "personality"),
        ("占位人物", 0, "background"),
    ]
    assert items[0]["text"] == "占位性情"
    assert items[0]["quote"] == "占位引文"


def test_read_facts_tsv_without_kind_column_defaults_to_person(tmp_path: Path):
    sample = _load("sample_golden_profile_facts")
    header = "project_id\tentity\tfield\tclaim\tquote\tchapter_number"
    path = _write_tsv(
        tmp_path / "old.tsv", header, ["proj-x\t占位实体\tappearance\t占位外形\t占位引文\t2"]
    )
    items, has_kind = sample._read_facts_tsv(path)

    assert has_kind is False
    assert len(items) == 1
    assert items[0]["is_organization"] == 0


def test_facts_tsv_mode_does_not_touch_database(tmp_path: Path, monkeypatch):
    sample = _load("sample_golden_profile_facts")
    path = _write_tsv(
        tmp_path / "facts.tsv",
        FACT_HEADER,
        [
            "proj-x\torganization\t占位组织\tpersonality\t占位性情\t占位引文\t3",
            "proj-x\tperson\t占位人物\tappearance\t占位外形\t占位引文\t4",
        ],
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("--facts-tsv 模式不应访问数据库")

    monkeypatch.setattr(sample, "_read_facts", forbidden)
    monkeypatch.setattr(sample, "_db_path", forbidden)
    monkeypatch.setattr(sample.sqlite3, "connect", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sample_golden_profile_facts.py",
            "--facts-tsv",
            str(path),
            "--count",
            "2",
            "--out-dir",
            str(tmp_path / "set"),
        ],
    )

    assert sample.main() == 0
    out = (tmp_path / "set" / "golden-set-198.tsv").read_text(encoding="utf-8")
    assert len(out.splitlines()) == 3  # 表头 + 2 条


# --------------------------------------------------------------------------- #
# 任务 B：跨项目同名实体不合并
# --------------------------------------------------------------------------- #
def test_facts_tsv_dedupes_per_project_not_globally(tmp_path: Path):
    sample = _load("sample_golden_profile_facts")
    path = _write_tsv(
        tmp_path / "facts.tsv",
        FACT_HEADER,
        [
            "proj-a\tperson\t占位实体\tpersonality\t同名同文\t占位引文\t3",
            "proj-b\tperson\t占位实体\tpersonality\t同名同文\t占位引文\t3",
        ],
    )
    items, _ = sample._read_facts_tsv(path)
    assert items[0]["entity_id"] != items[1]["entity_id"]

    rows, stats = sample._build_rows(items, count=2, seed=1)
    assert stats["candidates"] == 2  # 没有被当成重复合并
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# MAJOR 2：--include-existing
# --------------------------------------------------------------------------- #
def _row_entity(name: str, *, is_organization: bool = False, profile_facts: str = ""):
    return SimpleNamespace(
        id=name,
        name=name,
        aliases=None,
        is_organization=is_organization,
        profile_facts=profile_facts,
        ai_profile_snapshot=None,
        personality=None,
        background=None,
        appearance=None,
    )


def _blank_record() -> dict:
    return {
        "skipped_kind": 0,
        "skipped_existing": 0,
        "included_existing": 0,
        "skipped_not_empty": 0,
        "skipped_limit": 0,
    }


def test_select_targets_skips_existing_by_default():
    backfill = _load("backfill_profiles")
    record = _blank_record()
    rows = [
        _row_entity("占位实体甲", profile_facts='[{"field": "personality", "claim": "旧事实甲"}]'),
        _row_entity("占位实体乙"),
    ]

    targets = backfill._select_targets(
        rows,
        kind="all",
        include_existing=False,
        only_empty=False,
        budget={"remaining": None},
        record=record,
    )

    assert [t.name for t in targets] == ["占位实体乙"]
    assert record["skipped_existing"] == 1
    assert record["included_existing"] == 0


def test_select_targets_include_existing_keeps_both_and_counts():
    backfill = _load("backfill_profiles")
    record = _blank_record()
    rows = [
        _row_entity("占位实体甲", profile_facts='[{"field": "personality", "claim": "旧事实甲"}]'),
        _row_entity("占位实体乙"),
    ]

    targets = backfill._select_targets(
        rows,
        kind="all",
        include_existing=True,
        only_empty=False,
        budget={"remaining": None},
        record=record,
    )

    assert [t.name for t in targets] == ["占位实体甲", "占位实体乙"]
    assert record["skipped_existing"] == 0
    assert record["included_existing"] == 1
    assert record["skipped_not_empty"] == 0  # 已有 facts 不被当作"非空"跳过


def _patch_fake_project(backfill, monkeypatch, rows: list) -> None:
    """把 `_run_project` 的取数/模型/校验全部换成假实现（不连库、不调模型）。"""
    chapter = SimpleNamespace(chapter_number=1, content="占位正文")

    async def fake_load_rows(db, project_id):
        return rows

    async def fake_load_chapters(db, project_id):
        return [chapter]

    async def fake_call_batch(*, ai_service, entities, chapters, build_prompt, system_prompt):
        return {
            "error": "",
            "entries": [
                {
                    "name": entity.name,
                    "facts": [
                        {
                            "field": "personality",
                            "claim": f"本次新事实：{entity.name}",
                            "quote": "占位引文",
                            "chapter_number": 1,
                            "inferred": True,
                        }
                    ],
                }
                for entity in entities
            ],
        }

    monkeypatch.setattr(backfill, "_load_entity_rows", fake_load_rows)
    monkeypatch.setattr(backfill.sweep, "_load_chapters", fake_load_chapters)
    monkeypatch.setattr(backfill.sweep, "_split_windows", lambda chapters, n: [chapters])
    monkeypatch.setattr(backfill.sweep, "_call_batch", fake_call_batch)
    monkeypatch.setattr(
        backfill.sweep,
        "_entry_map",
        lambda entries, batch: ({entry["name"]: entry for entry in entries}, []),
    )
    monkeypatch.setattr(backfill, "unique_tokens_by_entity", lambda rows: {})
    monkeypatch.setattr(backfill, "mention_tokens", lambda entity, unique_map: [])
    monkeypatch.setattr(backfill, "count_mentions", lambda text, tokens: 1)
    monkeypatch.setattr(
        backfill,
        "parse_profile_payload",
        lambda entry: SimpleNamespace(
            mode="facts", entity=entry["name"], facts=list(entry["facts"])
        ),
    )
    # 校验层换成直通：本用例测的是"新旧事实边界"，不是 validate 的谓词
    monkeypatch.setattr(
        backfill,
        "validate_profile_facts",
        lambda payload, chapters_by_number, kind="character": SimpleNamespace(
            entity=payload["name"], facts=list(payload["facts"]), stats={}
        ),
    )


def _fake_args(include_existing: bool) -> SimpleNamespace:
    return SimpleNamespace(
        kind="all",
        include_existing=include_existing,
        only_empty=False,
        chapters_per_window=15,
        entities_per_call=8,
        apply=False,
    )


def _run_fake_project(backfill, monkeypatch, *, include_existing: bool) -> dict:
    rows = [
        _row_entity("占位实体甲", profile_facts='[{"field": "personality", "claim": "旧事实甲"}]'),
        _row_entity("占位实体乙"),
    ]
    _patch_fake_project(backfill, monkeypatch, rows)
    project = SimpleNamespace(id="proj-x", title="占位项目")

    return asyncio.run(
        backfill._run_project(
            db=None,
            project=project,
            ai_service=None,
            args=_fake_args(include_existing),
            budget={"remaining": None},
        )
    )


def test_run_project_default_still_skips_existing(monkeypatch):
    backfill = _load("backfill_profiles")
    record = _run_fake_project(backfill, monkeypatch, include_existing=False)

    assert record["processed"] == 1
    assert record["skipped_existing"] == 1
    assert record["included_existing"] == 0
    assert [row["claim"] for row in record["fact_rows"]] == ["本次新事实：占位实体乙"]


def test_run_project_include_existing_dumps_only_new_claims(monkeypatch, tmp_path: Path):
    backfill = _load("backfill_profiles")
    sample = _load("sample_golden_profile_facts")
    record = _run_fake_project(backfill, monkeypatch, include_existing=True)

    assert record["processed"] == 2
    assert record["skipped_existing"] == 0
    assert record["included_existing"] == 1

    claims = [row["claim"] for row in record["fact_rows"]]
    assert claims == ["本次新事实：占位实体甲", "本次新事实：占位实体乙"]
    assert all("旧事实甲" not in claim for claim in claims)

    out = tmp_path / "facts.tsv"
    backfill._write_facts_dump(out, [record])
    items, _ = sample._read_facts_tsv(out)
    assert [item["text"] for item in items] == claims
    assert all("旧事实甲" != item["text"] for item in items)


def test_write_entity_replace_facts_drops_old_facts():
    """`--include-existing` 下写库的合并基准是空：`profile_facts` 只留本次新生成。"""
    backfill = _load("backfill_profiles")

    class FakeDb:
        def __init__(self, orm):
            self.orm = orm

        async def get(self, model, entity_id):
            return self.orm

    old = [{"field": "personality", "claim": "旧事实甲", "quote": "占位", "chapter_number": 1}]
    new = [{"field": "personality", "claim": "本次新事实", "quote": "占位", "chapter_number": 2}]
    plan = {
        "entity_id": "占位实体甲",
        "fields": [],
        "facts": new,
        "snapshot": {},
        "changed": False,
    }

    orm = SimpleNamespace(profile_facts=json.dumps(old, ensure_ascii=False))
    asyncio.run(backfill._write_entity(FakeDb(orm), plan, replace_facts=True))
    assert "旧事实甲" not in orm.profile_facts
    assert "本次新事实" in orm.profile_facts

    orm_keep = SimpleNamespace(profile_facts=json.dumps(old, ensure_ascii=False))
    asyncio.run(backfill._write_entity(FakeDb(orm_keep), plan))
    assert "旧事实甲" in orm_keep.profile_facts  # 默认行为不变
