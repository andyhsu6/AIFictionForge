#!/usr/bin/env python3
"""书籍导入脏数据清理脚本（事务性、幂等）。

清理 book-import 产生的三类脏数据（仅限数据行，不触碰表结构）：

1. 重复关系合并：同一对角色（无视方向）存在多条关系时，保留最早一条，
   其余并入它 —— 并集类型链接、追加描述、删除多余行。
2. 性格字段污染清理：旧版本 bug 把关系描述写进了角色的 personality 字段，
   按启发式清空明显是关系描述的内容。
3. "我"角色合并：把名为 我/咱/俺/叙述者 的 imported 角色并入项目主角
   （重指向其关系后删除该行）。

幂等性：重复运行不会产生任何新改动（找不到可合并的重复对、无污染性格、
无"我"角色时，零行被修改）。整个清理在一个事务内执行，任一步骤失败即整体回滚。

用法（从 backend/ 目录运行）：
    python scripts/cleanup_book_import_data.py --project-id <id> [--dry-run]
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

# 与 migrate.py 一致：把 backend/ 加入 sys.path，使 app 包可导入
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

# 与 tests/conftest.py 相同的导入顺序：先完整初始化 app.database -> app.models 链，
# 避免 "cannot import name 'Project' from partially initialized module" 循环导入
import app.database  # noqa: E402,F401
from app.models.character import Character  # noqa: E402
from app.models.relationship import CharacterRelationship, RelationshipTypeLink  # noqa: E402
from app.services.relationship_service import sync_relationship_links  # noqa: E402

# "我"角色别名：旧版导入曾把第一人称代词当作角色名创建
WO_ALIAS_TOKENS = ("我", "咱", "俺", "叙述者")

# 名称中的括号说明（如"我（男主角）"）——判定别名时整体剥掉，只比对核心名。
# 与 book_import_service._first_person_core 同口径（脚本独立运行，不跨模块导入）。
_ALIAS_PAREN_RE = re.compile(r"[（(].*?[)）]")


def _first_person_core(name: str) -> str:
    """剥掉括号说明后取核心名（'我（男主角）'→'我'）；用于别名判定。"""
    return _ALIAS_PAREN_RE.sub("", str(name or "")).strip()


def _is_first_person_alias_name(name: str) -> bool:
    """名称（含括号变体）是否命中第一人称别名 token（'我'/'我（男主角）'→True）。"""
    return _first_person_core(name) in WO_ALIAS_TOKENS

# 关系来源白名单（与 book_import_service 合并判定一致）
# ai/analysis/manual/import 之外来源（如 system）不参与合并
SOURCE_WHITELIST = ("ai", "analysis", "manual", "import")

# 性格污染启发式：
# 旧版 bug 会把关系描述（"X与Y是Z关系" 之类的句子）写进 personality，
# 而正常性格描述不会以这些称谓结尾。为避免过度清理：
#   - 关系称谓词（夫妻/师徒/母子/父子/姐妹/兄弟/血亲）出现即清理（不限长度，
#     这些词是关系名称而非性格特质）；
#   - 仅含"关系"一词的：仅当文本 < 200 字（典型短关系描述句）才清理——
#     "关系"较通用，长性格描述中可能合理出现，需保留（避免过度清理）。
RELATIONSHIP_KINSHIP_TERMS = ("夫妻", "师徒", "母子", "父子", "姐妹", "兄弟", "血亲")
PERSONALITY_SHORT_MAX_LEN = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清理书籍导入产生的脏数据（重复关系、污染性格、'我'角色）"
    )
    parser.add_argument("--project-id", required=True, help="要清理的项目 ID（必填，防止误删数据）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅报告将发生的改动，不写入数据库（额外提供的安全检查）",
    )
    return parser


def _is_polluted_personality(personality: str) -> bool:
    """按启发式判断 personality 是否是被关系描述污染的内容。"""
    if not personality:
        return False
    text = personality.strip()
    if any(term in text for term in RELATIONSHIP_KINSHIP_TERMS):
        return True
    # 短文本且含"关系"：典型的"X与Y是Z关系"式描述（短句），非性格特质
    return len(text) < PERSONALITY_SHORT_MAX_LEN and "关系" in text


def _pair_type_ids(rel: CharacterRelationship, type_ids_by_rel: dict) -> frozenset:
    """关系对的分组键：无视方向 + 该关系关联的类型 ID 集合。"""
    return frozenset(
        {
            frozenset({rel.character_from_id, rel.character_to_id}),
            frozenset(type_ids_by_rel.get(rel.id, ())),
        }
    )


def _dedupe_desc(current: str, note: str) -> str:
    """追加描述：与 book_import_service 的 (desc + "\\n" + note).strip() 一致。"""
    if not note:
        return (current or "").strip()
    return ((current or "").strip() + "\n" + note).strip()


async def _load_type_ids_by_rel(db: AsyncSession, project_id: str) -> dict:
    rows = (
        await db.execute(
            select(RelationshipTypeLink).join(
                CharacterRelationship,
                RelationshipTypeLink.relationship_id == CharacterRelationship.id,
            ).where(CharacterRelationship.project_id == project_id)
        )
    ).scalars().all()
    result: dict = {}
    for link in rows:
        result.setdefault(link.relationship_id, []).append(link.relationship_type_id)
    return result


async def cleanup_project(db: AsyncSession, project_id: str) -> dict:
    """在给定 session 上执行全部清理，返回各类改动计数。

    注意：本函数不自行提交/回滚，由调用方控制事务边界
    （脚本 main() 用整段事务包裹；测试用 session 内断言后回滚）。
    """
    counts = {"duplicates_merged": 0, "personalities_cleared": 0, "wo_characters_merged": 0}

    # ---- 1. 重复关系合并 ----
    rels = (
        await db.execute(
            select(CharacterRelationship).where(CharacterRelationship.project_id == project_id)
        )
    ).scalars().all()
    deleted_rel_ids: set = set()
    if rels:
        type_ids_by_rel = await _load_type_ids_by_rel(db, project_id)
        # 按来源排序：manual 永远排在前面，保证 manual 行作为幸存者
        rels_sorted = sorted(rels, key=lambda r: (r.source != "manual", r.created_at or r.id, r.id))
        groups: dict = {}
        for rel in rels_sorted:
            if rel.source not in SOURCE_WHITELIST:
                continue
            groups.setdefault(_pair_type_ids(rel, type_ids_by_rel), []).append(rel)

        for group in groups.values():
            if len(group) < 2:
                continue
            keeper = group[0]
            for dup in group[1:]:
                # 并集类型链接（manual 幸存者同样只并类型，不覆盖其方向/描述）；
                # 过滤 None（旧行 relationship_type_id 缓存列可能为空）
                merged_type_ids = {keeper.relationship_type_id, *type_ids_by_rel.get(dup.id, [])}
                merged_type_ids.discard(None)
                await sync_relationship_links(db, keeper, sorted(merged_type_ids))
                # 描述合并：仅追加，不覆盖；已包含的相同描述跳过
                note = dup.description or dup.relationship_name or ""
                if note and note not in (keeper.description or ""):
                    keeper.description = _dedupe_desc(keeper.description, note)
                await db.delete(dup)
                deleted_rel_ids.add(dup.id)
                counts["duplicates_merged"] += 1

    # ---- 2. 性格污染清理 ----
    chars = (
        await db.execute(select(Character).where(Character.project_id == project_id))
    ).scalars().all()
    for char in chars:
        if _is_polluted_personality(char.personality or ""):
            char.personality = None
            counts["personalities_cleared"] += 1

    # ---- 3. "我"角色合并 ----
    protagonist = next(
        (c for c in chars if c.role_type == "protagonist" and not c.is_organization), None
    )
    if protagonist is not None:
        wo_chars = [
            c
            for c in chars
            if _is_first_person_alias_name(c.name)
            and c.source == "imported"
            and not c.is_organization
            and c.id != protagonist.id
        ]
        for wo_char in wo_chars:
            # 重指向：把"我"角色的所有关系改挂到主角名下
            # （排除已在重复合并中被删除的关系行）
            rels_of_wo = [
                r
                for r in rels
                if r.id not in deleted_rel_ids
                and (r.character_from_id == wo_char.id or r.character_to_id == wo_char.id)
            ]
            for rel in rels_of_wo:
                # 自环防护：若关系两端都是"我"角色，重指向后会产生
                # from_id == to_id == protagonist.id 的自环（导入路径有
                # char_a==char_b 守卫，脚本无）；此时跳过该行（其两端
                # 都已并入主角，行本身无信息增量）。
                if rel.character_from_id == wo_char.id and rel.character_to_id == wo_char.id:
                    await db.delete(rel)
                    counts["duplicates_merged"] += 1
                    continue
                if rel.character_from_id == wo_char.id:
                    rel.character_from_id = protagonist.id
                if rel.character_to_id == wo_char.id:
                    rel.character_to_id = protagonist.id
            await db.delete(wo_char)
            counts["wo_characters_merged"] += 1

    return counts


def _build_session(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()


async def _run(project_id: str, dry_run: bool) -> dict:
    import app.config as app_config

    db_url = app_config.settings.database_url
    engine_args = {"future": True}
    if "sqlite" in db_url.lower():
        engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    engine = create_async_engine(db_url, **engine_args)

    try:
        session = _build_session(engine)
        try:
            # 校验项目存在；不存在时抛错（即使 --dry-run 也要校验，避免误用）
            from app.models.project import Project

            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise SystemExit(f"错误：项目 {project_id} 不存在，已终止（未写入任何数据）")

            counts = await cleanup_project(session, project_id)
            if dry_run:
                print(f"[dry-run] 项目 {project_id} 将发生以下改动（未写入）:")
            else:
                await session.commit()
                print(f"[done] 项目 {project_id} 清理完成:")
            for key, value in counts.items():
                print(f"  - {key}: {value}")
            return counts
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_run(args.project_id, args.dry_run))
        return 0
    except SystemExit as e:
        if e.code:
            print(str(e.code), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
