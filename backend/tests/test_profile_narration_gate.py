"""档案字段内容体检（issue #188）。

背景：自动补建角色曾把关系抽取返回的 evidence 直接写进 personality，得到
「因为她和丈夫不仅是夫妻，还是师兄妹……」这类剧情复述。第一批导入的角色没这个
问题，是因为生成路径已有 `_looks_like_pasted_narration` 质量门；后续的档案补全与
档案同步两条写入路径没有。本文件钉死新增的判定，防止同一类错误换个入口复现。

夹具文本全部为中性占位/用户提供的样例句子。
"""
from __future__ import annotations

import pytest

from app.services.import_validators import looks_like_profile_narration


@pytest.mark.parametrize(
    "text",
    [
        # 关系/情节复述：主语 + 因果连词 + 亲属称谓
        "因为她和丈夫不仅是夫妻，还是师兄妹，她的骑术和刀法都不在角色乙之下，「占位双刀」的名头她可是占了一半的。",
        # 纯因果复述
        "他之所以离开山门，是因为师父当年的一句话，所以这些年一直不肯回头。",
        # 抄录对话
        "他对她说：「占位台词，不必当真。」",
        # 省略号信号
        "她望着远方，心里想着……",
    ],
)
def test_narration_like_fields_are_rejected(text):
    assert looks_like_profile_narration(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "性格沉稳果断，遇事不慌，重情重义，对陌生人保持距离。",
        "她性格冷淡，对家人却极为护短。",  # 第三人称但无因果/亲属叙述
        "外貌清秀，常年一袭青衫，左手有旧伤。",
        "占位组织：以行商为主，规矩森严。",
        "",
    ],
)
def test_profile_style_fields_pass(text):
    assert looks_like_profile_narration(text) is False
