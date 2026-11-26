"""一次性补跑：对已存在的项目补生成角色/组织，并按章节原文抽取角色关系。

背景
----
拆书导入时"角色与组织生成"和"原文关系抽取"两个步骤会喂入原文正文。若上游
内容审核拦截，这两步在旧版本里会整体失败，导致项目只有零星角色与关系。现在
执行器已有降级阶梯（1800→900→章节二分）并把被拦章节记入 skipped_chapters，
但书已经导入完成、内存任务早已丢失（`BookImportService._tasks` 不落库），
无法再走 `POST /api/book-import/tasks/{id}/retry-stream`。

本脚本直接调用导入服务的两个内部方法，对已存在项目做**只增不改**的补跑：
- 角色生成按 name 精确去重（同名一律跳过），不会覆盖已有角色的任何字段；
- 关系抽取按无序角色对合并，会为已有关系追加 `[第N章] …` 证据行。

用法
----
    cd backend
    PYTHONPATH=. .venv/bin/python scripts/backfill_import_extraction.py \
        --project-id <uuid> --stage characters --commit

    --stage characters|relationships|all（默认 all）
    --count  角色生成目标数（执行器硬上限 10）
    --commit 真正落库；不带该参数则整段 rollback（仍会消耗 AI 调用）

输出一份 JSON 报告到 --report（默认 /tmp），含新建角色、关系统计与被拦章节。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database  # noqa: F401 —— 必须先于 models 导入，避免 app.models 循环导入
from app.models.chapter import Chapter
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import CharacterRelationship
from app.services.book_import_service import BookImportService


def _build_engine():
    import app.config as app_config

    db_url = app_config.settings.database_url
    engine_args: dict[str, Any] = {"future": True}
    if "sqlite" in db_url.lower():
        engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    return create_async_engine(db_url, **engine_args)


async def _snapshot(db: AsyncSession, project_id: str) -> dict[str, Any]:
    chars = (
        await db.execute(select(Character.id).where(Character.project_id == project_id))
    ).scalars().all()
    rels = (
        await db.execute(
            select(CharacterRelationship.id).where(CharacterRelationship.project_id == project_id)
        )
    ).scalars().all()
    return {"characters": set(chars), "relationships": set(rels)}


async def _run(project_id: str, stage: str, count: int, commit: bool,
               chapters_from: int | None = None, chapters_to: int | None = None) -> dict[str, Any]:
    engine = _build_engine()
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    report: dict[str, Any] = {
        "project_id": project_id,
        "stage": stage,
        "committed": commit,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        async with Session() as db:
            project = (
                await db.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            if project is None:
                raise SystemExit(f"错误：项目 {project_id} 不存在，已终止（未写入任何数据）")

            before = await _snapshot(db, project_id)
            service = BookImportService()
            ai = await service._build_user_ai_service(db=db, user_id=project.user_id)

            if stage in ("characters", "all"):
                skipped: list[dict[str, Any]] = []
                created = await service._generate_characters_and_organizations_from_project(
                    db=db,
                    user_id=project.user_id,
                    project=project,
                    count=count,
                    ai_service=ai,
                    skipped_chapters=skipped,
                )
                report["characters_created_reported"] = created
                report["characters_skipped_chapters"] = skipped
                print(f"[characters] 执行器报告新建 {created} 条；被拦章节 {len(skipped)} 条", flush=True)

            if stage in ("relationships", "all"):
                chapter_query = select(Chapter).where(Chapter.project_id == project_id)
                if chapters_from is not None:
                    chapter_query = chapter_query.where(Chapter.chapter_number >= chapters_from)
                if chapters_to is not None:
                    chapter_query = chapter_query.where(Chapter.chapter_number <= chapters_to)
                chapters = (
                    await db.execute(chapter_query.order_by(Chapter.chapter_number))
                ).scalars().all()
                print(
                    f"[relationships] 章节范围 {chapters[0].chapter_number if chapters else '-'}"
                    f"..{chapters[-1].chapter_number if chapters else '-'}"
                    f"（{len(chapters)} 章）",
                    flush=True,
                )
                rel_result = await service._extract_relationships_from_chapters(
                    db=db,
                    user_id=project.user_id,
                    project=project,
                    chapters=list(chapters),
                    ai_service=ai,
                )
                report["relationship_result"] = rel_result
                # created_types / created_characters 是计数（int），不是列表；
                # 且这里的任何格式化异常都不能发生在 commit 之前，否则整轮 AI 结果白跑。
                print(
                    "[relationships] 抽取关系 "
                    f"{rel_result.get('extracted_relationships')} 条，"
                    f"新建类型 {rel_result.get('created_types')}，"
                    f"补建角色 {rel_result.get('created_characters')}，"
                    f"被拦章节 {len(rel_result.get('skipped_chapters') or [])}",
                    flush=True,
                )

            # 先提交再汇报：报告阶段任何格式化/查询异常都不得把已完成的 AI 结果回滚掉
            # （上一轮就是这样白跑了一整轮关系抽取）。
            if commit:
                await db.commit()
                print("[commit] 已提交", flush=True)

            after = await _snapshot(db, project_id)
            new_char_ids = sorted(after["characters"] - before["characters"])
            new_rel_ids = sorted(after["relationships"] - before["relationships"])
            report["new_character_ids"] = new_char_ids
            report["new_relationship_ids"] = new_rel_ids

            if new_char_ids:
                rows = (
                    await db.execute(select(Character).where(Character.id.in_(new_char_ids)))
                ).scalars().all()
                report["new_characters"] = [
                    {"id": r.id, "name": r.name, "is_organization": r.is_organization,
                     "role_type": r.role_type, "source": getattr(r, "source", None)}
                    for r in rows
                ]
                print(f"[characters] 本次新增 {len(rows)} 条", flush=True)
                for r in rows:
                    print(f"  + {r.name}（{'组织' if r.is_organization else r.role_type}）", flush=True)

            if not commit:
                await db.rollback()
                print("[dry-run] 已回滚，未写入任何数据", flush=True)
    finally:
        await engine.dispose()
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="补跑角色/组织生成与关系抽取")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--stage", choices=["characters", "relationships", "all"], default="all")
    parser.add_argument("--count", type=int, default=10, help="角色生成目标数（执行器上限 10）")
    parser.add_argument("--commit", action="store_true", help="真正落库；缺省则回滚")
    parser.add_argument("--chapters-from", type=int, default=None, help="关系抽取起始章号（含）")
    parser.add_argument("--chapters-to", type=int, default=None, help="关系抽取结束章号（含）")
    parser.add_argument("--report", default="/tmp/backfill_import_extraction.json")
    args = parser.parse_args()

    report = asyncio.run(_run(args.project_id, args.stage, args.count, args.commit,
                          args.chapters_from, args.chapters_to))
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[report] {args.report}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
