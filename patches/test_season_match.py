"""patch4 的跨季匹配测试。

直接对容器里**已挂载补丁**的 ``module.database.bangumi`` 断言：

    docker cp patch4/test_season_match.py auto_bangumi:/tmp/test_season_match.py
    docker exec -e PYTHONPATH=/app auto_bangumi python /tmp/test_season_match.py

覆盖：同季/无季标记照旧命中；**相邻下一季**放行（半年番第二季自动接上）；
隔季、上一季、混合合集、电影/特别篇类型都不受影响；以及用环境变量
``AB_STRICT_SEASON_MATCH=1`` 一键回到官方严格行为。
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, "/app")

from module.database.bangumi import (  # noqa: E402
    _release_matches_bangumi,
    match_bangumi_in_list,
)
from module.models.bangumi import Bangumi  # noqa: E402
from module.parser.analyser.tokenizer import MediaType  # noqa: E402

FAILURES: list[str] = []

EPISODE = getattr(MediaType, "EPISODE", None)
if EPISODE is None:
    EPISODE = next(
        m
        for m in MediaType
        if m.name not in ("PV", "OPENING", "ENDING", "MOVIE", "OVA", "OAD", "SPECIAL")
    )


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


def rule(season: int = 1, episode_type: str = "episode"):
    return types.SimpleNamespace(
        season=season, episode_type=episode_type, title_raw="X", official_title="X", deleted=False
    )


def release(
    season: int | None = 1,
    media_type=EPISODE,
    mixed: bool = False,
):
    return types.SimpleNamespace(
        season=season,
        media_type=media_type,
        is_mixed_collection=mixed,
        title_en="X",
        title_zh=None,
        title_jp=None,
        group=None,
        episode=1,
    )



def test_real_entry() -> None:
    """走 RSS 真正用的入口 ``match_bangumi_in_list``（纯内存，不写库）。"""
    print("\n5) 真实入口：第二季的发布名能不能命中 S1 的规则")
    rule_row = Bangumi(
        official_title="花織即使是轉生也想打架",
        title_raw="花織即使是轉生也想打架",
        season=1,
        episode_type="episode",
        save_path=r"E:\好女孩\花織即使是轉生也想打架\Season 1",
        filter="",
    )
    name = (
        "[Nekomoe kissaten] 花織即使是轉生也想打架 S02E01 "
        "[WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
    )
    hit = match_bangumi_in_list(name, [rule_row])
    check("S2 的种子命中了 S1 的规则", hit is not None, True)

    os.environ["AB_STRICT_SEASON_MATCH"] = "1"
    try:
        strict = match_bangumi_in_list(name, [rule_row])
    finally:
        os.environ.pop("AB_STRICT_SEASON_MATCH", None)
    check("严格模式下不命中（可一键回退）", strict, None)

    same = match_bangumi_in_list(
        "[Nekomoe kissaten] 花織即使是轉生也想打架 S01E05 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
        [rule_row],
    )
    check("同季照旧命中", same is not None, True)

def main() -> int:
    print("1) 原有行为不变")
    check("同季命中", _release_matches_bangumi(release(1), rule(1)), True)
    check("没有季标记照样命中", _release_matches_bangumi(release(None), rule(1)), True)
    check("别的季（隔一季）不命中", _release_matches_bangumi(release(3), rule(1)), False)
    check("上一季不命中", _release_matches_bangumi(release(1), rule(2)), False)

    print("\n2) 半年番：相邻下一季放行（本次补丁）")
    check("S2 进 S1 的规则", _release_matches_bangumi(release(2), rule(1)), True)
    check("S3 进 S2 的规则", _release_matches_bangumi(release(3), rule(2)), True)
    check("但再下一季不会被吞", _release_matches_bangumi(release(4), rule(2)), False)

    print("\n3) 类型护栏不受影响")
    check("混合合集仍然拒绝", _release_matches_bangumi(release(2, mixed=True), rule(1)), False)
    check("电影不进食剧规则", _release_matches_bangumi(release(2, MediaType.MOVIE), rule(1)), False)
    check("特别篇不进食剧规则", _release_matches_bangumi(release(2, MediaType.SPECIAL), rule(1)), False)
    check(
        "特别篇规则只吃第 0 季",
        _release_matches_bangumi(release(0, MediaType.SPECIAL), rule(0, "special")),
        True,
    )
    # 注意：特别篇走的是原代码的分支（expected_type == "special" 时直接返回），
    # 官方行为就是"特别篇规则不看季号"——本次补丁没有触碰它
    check(
        "特别篇规则不看季号（原有行为）",
        _release_matches_bangumi(release(1, MediaType.SPECIAL), rule(0, "special")),
        True,
    )
    check("PV 仍然拒绝", _release_matches_bangumi(release(2, MediaType.PV), rule(1)), False)

    print("\n4) 一键回退：AB_STRICT_SEASON_MATCH=1")
    os.environ["AB_STRICT_SEASON_MATCH"] = "1"
    try:
        check("严格模式下 S2 不进 S1", _release_matches_bangumi(release(2), rule(1)), False)
        check("严格模式下同季照常", _release_matches_bangumi(release(1), rule(1)), True)
    finally:
        os.environ.pop("AB_STRICT_SEASON_MATCH", None)
    check("去掉环境变量后恢复放行", _release_matches_bangumi(release(2), rule(1)), True)

    test_real_entry()
    print()
    print("=" * 70)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
