"""回归测试：v2 尾部风格块、跨章语感锚点、完整句截断与世界观上下文注入。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.chapters import _append_style_tail, _append_world_context
from app.database import Base
from app.models.chapter import Chapter
from app.models.project import Project
from app.models.project_style_anchor import ProjectStyleAnchor
from app.services.chapter_context_service import _build_world_context
from app.services.style_anchors import fragment_hash, load_style_anchor_fragments
from app.services.style_profile import (
    extract_rendered_do_items,
    extract_rendered_ending_habits,
    extract_rendered_signature_words,
    render_style_tail_block,
)


SAMPLE_V2_PROFILE = """本书文笔画像（测试书）

## 整体印象
> 整体文笔通俗直白，段落短促，善用因果长句剖析博弈。

## 句式节奏与场景切换
- 全书统计参考：平均句长：29.7 字，短句占比：28%，对话占比：23%，段落长度中位：38 字

## 本书语言指纹（全书实测）
- 作者高频语感词（请保留自然使用，勿被去AI味误删）：可是、虽然、因为、仿佛
- 原文极少/几乎不出现（切勿滥用）：——、顿了顿、瞳孔一缩、识海
- 标点与章尾习惯：感叹号约 8.3 次/千字，破折号全书共 2 次；章尾对话收尾占比 56%

## 必须做到
1. 依据人物阶层区分口吻。
2. 交锋打斗时用因果逻辑长句写出博弈感。
"""


def test_v2_tail_block_preserves_short_paragraphs_and_fingerprint():
    sig = extract_rendered_signature_words(SAMPLE_V2_PROFILE)
    dos = extract_rendered_do_items(SAMPLE_V2_PROFILE)
    endings = extract_rendered_ending_habits(SAMPLE_V2_PROFILE)

    assert sig == "可是、虽然、因为、仿佛"
    assert len(dos) == 2
    assert "感叹号" in endings

    tail = _append_style_tail("正文提示词", SAMPLE_V2_PROFILE, target_chars=4000)
    # 短段原著（中位 38 字 <= 45）不应再输出自相矛盾的“不得刻意拆成满屏单句碎段”
    assert "不得刻意拆成满屏单句碎段" not in tail
    assert "单句或两三句成段" in tail
    assert "可是、虽然、因为、仿佛" in tail
    assert "依据人物阶层区分口吻" in tail
    assert "瞳孔一缩" in tail
    assert "感叹号约 8.3 次/千字" in tail


def test_v2_tail_block_keeps_anti_fragment_rule_for_long_paragraph_books():
    block = render_style_tail_block(
        {"avg_sentence_chars": 30.0, "paragraph_chars_p50": 120},
        language="zh",
        impression="厚重舒缓的长段叙事",
    )
    assert "不得刻意拆成满屏单句碎段" in block


def test_world_context_built_and_appended():
    project = SimpleNamespace(
        world_rules="本世界为中武体系，严禁出现修仙灵气。",
        world_atmosphere="仲夏水乡与命案阴影交织。",
        world_location="连云泽与湖阳县。",
        world_time_period="虚构王朝承平末段。",
    )
    ctx = _build_world_context(project)
    assert ctx is not None
    assert "中武体系" in ctx
    assert "硬边界" in ctx

    prompt = _append_world_context("基础提示词", ctx)
    assert '<world_rules priority="P0">' in prompt
    assert "中武体系" in prompt


def test_style_anchors_spread_across_chapters_and_truncate_on_sentence_boundary():
    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with session_maker() as db:
            project = Project(
                id="p1",
                user_id="u1",
                title="测试书",
                narrative_perspective="第三人称",
                style_anchor_enabled=True,
                style_anchor_chars=300,
                style_anchor_count=3,
            )
            db.add(project)
            ch1_text = "他在湖边慢慢走着，心里盘算着明天的行船路线，虽然风浪不小，可是船夫手艺极稳，并不会出什么差错。"
            ch2_text = (
                "他转身推开木门，屋内油灯昏黄，桌上放着半盏凉茶，显然刚才有人匆忙离开。"
                "如果对方真的是冲着账册来的，此刻多半已经顺着暗巷逃往码头方向去了，追上去未必来得及。"
            )
            ch3_text = "他低头看着掌心的旧伤，想起师父当年说过的话，心里顿时一阵发紧，连呼吸都慢了半拍。"
            c1 = Chapter(id="c1", project_id="p1", chapter_number=1, title="一", content=ch1_text)
            c2 = Chapter(id="c2", project_id="p1", chapter_number=2, title="二", content=ch2_text)
            c3 = Chapter(id="c3", project_id="p1", chapter_number=3, title="三", content=ch3_text)
            db.add_all([c1, c2, c3])

            # 第 3 章放 2 条锚点，第 2 章和第 1 章各 1 条；应优先跨章选 3、2、1
            a1 = ProjectStyleAnchor(
                project_id="p1",
                chapter_id="c3",
                start_offset=0,
                end_offset=len(ch3_text),
                text_hash=fragment_hash(ch3_text),
                order_index=0,
            )
            a2 = ProjectStyleAnchor(
                project_id="p1",
                chapter_id="c3",
                start_offset=0,
                end_offset=len(ch3_text) - 5,
                text_hash=fragment_hash(ch3_text[:-5]),
                order_index=1,
            )
            a3 = ProjectStyleAnchor(
                project_id="p1",
                chapter_id="c2",
                start_offset=0,
                end_offset=len(ch2_text),
                text_hash=fragment_hash(ch2_text),
                order_index=2,
            )
            a4 = ProjectStyleAnchor(
                project_id="p1",
                chapter_id="c1",
                start_offset=0,
                end_offset=len(ch1_text),
                text_hash=fragment_hash(ch1_text),
                order_index=3,
            )
            db.add_all([a1, a2, a3, a4])
            await db.commit()

            # 预算设为 195：c3(41) + c2(86) + c1(47) = 174，测试跨章选出 3 章
            project.style_anchor_chars = 195
            frags = await load_style_anchor_fragments(db, project=project, before_chapter_number=4)
            assert len(frags) == 3
            assert any("旧伤" in f for f in frags)
            assert any("油灯昏黄" in f for f in frags)
            assert any("湖边慢慢走着" in f for f in frags)

            # 预算设为 155（>= ANCHOR_MIN_CHARS=150）：c3(41) + c2(86) = 127，
            # 剩余 28 不足 c1(47) 且无完整句末落点，测试不会把完整句之外的前半段切坏
            # 再测长段落截断：给 c2 放一个 130 字的两句段，预算 155 -> c3(41) 后剩余 114，
            # 应在第 1 句末“。”处截断而非腰斩第 2 句
            long_c2 = (
                "他转身推开木门，屋内油灯昏黄，桌上放着半盏凉茶，显然刚才有人匆忙离开，连门闩都来不及扣好，地上还留着几道湿漉漉的脚印。"
                "如果对方真的是冲着账册来的，此刻多半已经顺着暗巷逃往码头方向去了，就算立刻追上去恐怕也未必来得及拦截，只能先派人去渡口盯紧来往船只，免得打草惊蛇。"
            )
            c2.content = long_c2
            a3.end_offset = len(long_c2)
            a3.text_hash = fragment_hash(long_c2)
            await db.commit()

            project.style_anchor_chars = 155
            frags_trunc = await load_style_anchor_fragments(
                db, project=project, before_chapter_number=4, override={"count": 2}
            )
            assert len(frags_trunc) == 2
            assert frags_trunc[1].endswith("。")
            assert "如果对方" not in frags_trunc[1]

        await engine.dispose()

    asyncio.run(_run())
