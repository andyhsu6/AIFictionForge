#!/usr/bin/env python3
"""为存量项目回填文笔画像（issue #254 P2，一次性脚本，默认 dry-run）。

背景
----
画像抽取是拆书导入链路里的能力（`BookImportService._extract_style_profile`），
只对"本次导入"生效。P2 之前已经存在的项目没有画像行，也没有入口补跑：内存任务
不落库，`retry-stream` 无法复用。本脚本对存量项目按同一抽取链路补画像。

行为
----
- 只处理"有章节、且没有 `preset_id=profile:<project_id>` 画像行"的项目；
  已有画像行一律跳过（用户手改过的 `profile_edited` 也保留），除非 `--force`；
- 默认不替换项目默认风格；`--set-default` 才把新画像设为默认；
- 幂等：二次运行会因为画像行已存在而全部跳过；
- 图像/正文只用于本次模型调用，日志只打项目 id、章节数、是否成功，
  不打印书名、原文或角色名；锚点不落库、不导出。

用法
----
    cd backend
    PYTHONPATH=. .venv/bin/python scripts/backfill_style_profiles.py            # dry-run
    PYTHONPATH=. .venv/bin/python scripts/backfill_style_profiles.py --apply
    PYTHONPATH=. .venv/bin/python scripts/backfill_style_profiles.py --apply --set-default
    PYTHONPATH=. .venv/bin/python scripts/backfill_style_profiles.py \
        --project-id <uuid> --apply --force

注意：dry-run 仍会执行一次抽取（调用模型）以便报告"是否成功"，只是事务末尾
整体回滚；不想消耗模型调用时加 `--limit 1` 先验证单个项目。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 允许 `python scripts/backfill_style_profiles.py` 直接运行（sys.path[0] 是 scripts/）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.database  # noqa: E402,F401 —— 必须先于 models 导入，避免 app.models 循环导入
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.writing_style import WritingStyle
from app.services.book_import_service import BookImportService
from app.services.style_profile import profile_preset_id


def _build_engine():
    import app.config as app_config

    db_url = app_config.settings.database_url
    engine_args: dict[str, Any] = {"future": True}
    if "sqlite" in db_url.lower():
        engine_args["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    return create_async_engine(db_url, **engine_args)


async def backfill_projects(
    db: AsyncSession,
    *,
    dry_run: bool = True,
    set_default: bool = False,
    project_id: Optional[str] = None,
    limit: Optional[int] = None,
    force: bool = False,
    service: Optional[BookImportService] = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """遍历候选项目补画像；返回统计报告。

    ``service`` 可注入（测试用桩替换 ``_extract_style_profile``，避免真实模型调用）。
    dry_run 时结束时统一 rollback，不落任何写入。
    """
    service = service or BookImportService()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "scanned": 0,
        "candidates": 0,
        "written": 0,
        "planned": 0,
        "skipped_existing": 0,
        "skipped_no_chapters": 0,
        "skipped_no_sample": 0,
        "failed": 0,
        "project_ids": [],
    }

    query = select(Project).order_by(Project.created_at)
    if project_id:
        query = query.where(Project.id == project_id)
    projects = (await db.execute(query)).scalars().all()

    for project in projects:
        if limit is not None and report["candidates"] >= limit:
            break
        report["scanned"] += 1

        chapters = (
            await db.execute(
                select(Chapter)
                .where(Chapter.project_id == project.id)
                .order_by(Chapter.chapter_number)
            )
        ).scalars().all()
        if not chapters:
            report["skipped_no_chapters"] += 1
            continue

        existing = (
            await db.execute(
                select(WritingStyle).where(
                    WritingStyle.user_id == project.user_id,
                    WritingStyle.preset_id == profile_preset_id(project.id),
                )
            )
        ).scalars().all()
        if existing and not force:
            report["skipped_existing"] += 1
            continue

        report["candidates"] += 1
        log(f"[{project.id}] 章节 {len(chapters)}，开始抽取画像")

        try:
            profile = await service._extract_style_profile(
                db=db,
                user_id=project.user_id,
                project=project,
                chapters=list(chapters),
                extract_mode="full",
            )
        except Exception as exc:  # noqa: BLE001 —— 单项目失败不影响其余项目
            report["failed"] += 1
            log(f"[{project.id}] 失败：抽取异常（{type(exc).__name__}）")
            continue

        if profile.get("skipped"):
            report["skipped_no_sample"] += 1
            log(f"[{project.id}] 跳过：样本不足（{profile.get('sample_chars')} 字）")
            continue

        try:
            style_id = await service._upsert_project_profile_style(
                db=db,
                project=project,
                user_id=project.user_id,
                prompt_content=profile["prompt_content"],
                description=profile["description"],
                replace_default=set_default,
                allow_overwrite_edited=force,
                profile_data=profile.get("profile_data"),
                profile_version=profile.get("profile_version"),
            )
        except Exception as exc:  # noqa: BLE001
            report["failed"] += 1
            log(f"[{project.id}] 失败：写入异常（{type(exc).__name__}）")
            continue

        if style_id is None:
            report["skipped_existing"] += 1
            log(f"[{project.id}] 跳过：画像已被用户编辑")
            continue

        report["project_ids"].append(project.id)
        if dry_run:
            report["planned"] += 1
            log(f"[{project.id}] 成功（dry-run，未写入）")
        else:
            report["written"] += 1
            log(f"[{project.id}] 成功（style_id={style_id}）")

    if dry_run:
        await db.rollback()
        log("[dry-run] 已回滚，未写入任何数据")
    else:
        await db.commit()
        log(f"[commit] 已写入 {report['written']} 个项目的画像")

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为存量项目回填文笔画像（默认 dry-run，--apply 才写入）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="只跑一遍并回滚（默认；注意仍会调用一次模型）",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="真正写入数据库",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
        help="回填后把画像设为项目默认风格（默认不动现有默认）",
    )
    parser.add_argument("--project-id", default=None, help="只处理指定项目")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个候选项目")
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有画像（含用户编辑过的）也覆盖",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        print("错误：--limit 必须为正整数", file=sys.stderr, flush=True)
        return 1

    async def _run() -> dict[str, Any]:
        engine = _build_engine()
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with Session() as db:
                return await backfill_projects(
                    db,
                    dry_run=args.dry_run,
                    set_default=args.set_default,
                    project_id=args.project_id,
                    limit=args.limit,
                    force=args.force,
                )
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"错误：执行失败（{type(exc).__name__}: {exc}）", file=sys.stderr, flush=True)
        return 1

    print(
        "扫描 {scanned}，候选 {candidates}，写入 {written}，"
        "预演 {planned}，已有画像 {skipped_existing}，无章节 {skipped_no_chapters}，"
        "样本不足 {skipped_no_sample}，失败 {failed}".format(**report),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
