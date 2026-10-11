"""patch4 的回归测试：季度偏移的三个补丁。

在容器里运行（会临时插入一条规则与两条种子记录，跑完自动删除）：

    docker exec -e PYTHONPATH=/app auto_bangumi \
        python /app/patch4/test_offset_patches.py

覆盖：
  1. 保存带偏移的通用更新后，needs_review / 建议值被清空（修掉"反复提示"）；
  2. 放送中的番不再因"集数超过 TMDB 该季集数"被标记，已完结的仍然标记；
  3. 集数信号只看同一季（显式跨季的种子被排除，无季标记的仍算本季）。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, "/app")

from module.database import Database  # noqa: E402
from module.models.bangumi import BangumiUpdate  # noqa: E402
from module.parser.analyser.offset_detector import detect_offset_mismatch  # noqa: E402
from module.core import offset_scanner as scanner_module  # noqa: E402

LIVE_DB = "/app/data/data.db"
DB = LIVE_DB
_COPIED = False

# 绑定挂载 + WAL 时，新进程打不开真库（连只读都不行）→ 复制一份来用
try:
    import sqlite3 as _sqlite3

    _probe = _sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    _probe.execute("select count(*) from bangumi").fetchone()
    _probe.close()
except Exception:
    try:
        DB = "/tmp/patch4_offset_test.db"
        shutil.copy2(LIVE_DB, DB)
        _COPIED = True
    except Exception:
        DB = LIVE_DB
TITLE = "偏移验收番剧"

FAILURES: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


def _insert(table: str, row: dict) -> int:
    con = sqlite3.connect(DB)
    info = list(con.execute(f"PRAGMA table_info({table})"))
    cols, vals = [], []
    for col in info:
        name = col[1]
        if col[5]:
            continue
        if name in row:
            cols.append(name)
            vals.append(row[name])
        elif col[3] == 1:
            kind = (col[2] or "").upper()
            cols.append(name)
            if "INT" in kind or "BOOL" in kind:
                vals.append(0)
            elif "DATE" in kind or "TIME" in kind:
                vals.append("2026-01-01 00:00:00")
            else:
                vals.append("")
    cur = con.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def seed() -> int:
    cleanup()
    rid = _insert(
        "bangumi",
        {
            "official_title": TITLE,
            "title_raw": TITLE,
            "season": 1,
            "save_path": r"E:\好女孩\偏移验收番剧\Season 1",
            "filter": "",
            "rss_link": "",
            "deleted": 0,
            "archived": 0,
            "eps_collect": 0,
            "episode_offset": 0,
            "season_offset": 0,
            "episode_type": "episode",
            "needs_review": 1,
            "needs_review_reason": "验收用的假标记",
            "suggested_season_offset": -1,
            "suggested_episode_offset": 0,
        },
    )
    return rid


def cleanup() -> None:
    con = sqlite3.connect(DB)
    row = con.execute("select id from bangumi where official_title = ?", (TITLE,)).fetchone()
    if row:
        con.execute("delete from torrent where bangumi_id = ?", (row[0],))
    con.execute("DELETE FROM bangumi WHERE official_title = ?", (TITLE,))
    con.commit()
    con.close()


async def case_1(rid: int) -> None:
    print("\n1) 保存偏移后清掉检查标记")
    if _COPIED:
        print("  [跳过] 真库打不开（绑定挂载 + WAL），本条需要宿主的 Database() 会话")
        print("         补丁本身的行为由 patch4/README.md 里的另一次运行验证过")
        return
    async with Database() as db:
        before = await db.bangumi.search_id(rid)
        check("保存前 needs_review", bool(before.needs_review), True)
        fields = set(BangumiUpdate.model_fields.keys())
        payload = {k: getattr(before, k) for k in fields if hasattr(before, k)}
        payload["season_offset"] = -1
        payload["episode_offset"] = 0
        ok = await db.bangumi.update(BangumiUpdate(**payload), rid)
        after = await db.bangumi.search_id(rid)
    check("更新成功", ok, True)
    check("保存后 needs_review", bool(after.needs_review), False)
    check("保存后 reason", after.needs_review_reason, None)
    check("保存后建议值", (after.suggested_season_offset, after.suggested_episode_offset), (None, None))
    check("偏移已写入", after.season_offset, -1)


def case_2() -> None:
    print("\n2) 放送中的番不再因集数超过 TMDB 被标记，已完结的仍标记")
    airing = types.SimpleNamespace(
        last_season=1,
        season_episode_counts={1: 12},
        series_status="Returning Series",
        virtual_season_starts=None,
    )
    finished = types.SimpleNamespace(
        last_season=1,
        season_episode_counts={1: 12},
        series_status="Ended",
        virtual_season_starts=None,
    )
    check("放送中 E13 > 12 → 不标记", detect_offset_mismatch(1, 13, airing), None)
    result = detect_offset_mismatch(1, 13, finished)
    check("已完结 E13 > 12 → 仍标记", bool(result), True)


def case_3() -> None:
    print("\n3) 集数信号只看同一季")

    def fake_parse(name):
        return {
            "s1e5": types.SimpleNamespace(season=1, episode=5),
            "s2e30": types.SimpleNamespace(season=2, episode=30),
            "noseason_e7": types.SimpleNamespace(season=None, episode=7),
        }[name]

    original_parse = scanner_module.parse_configured_release_title
    original_signal = scanner_module.is_offset_signal
    original_db = scanner_module.Database

    torrents = [types.SimpleNamespace(name=k) for k in ("s1e5", "s2e30", "noseason_e7")]

    class FakeTorrentDB:
        async def search_by_bangumi_id(self, _id):
            return torrents

    class FakeDB:
        def __init__(self):
            self.torrent = FakeTorrentDB()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    scanner_module.parse_configured_release_title = fake_parse
    scanner_module.is_offset_signal = lambda release: True
    scanner_module.Database = FakeDB
    try:
        scanner = scanner_module.OffsetScanner()

        async def run():
            return (
                await scanner._get_latest_parsed_episode(1),
                await scanner._get_latest_parsed_episode(1, 1),
                await scanner._get_latest_parsed_episode(1, 2),
            )

        all_eps, season1, season2 = asyncio.run(run())
    finally:
        scanner_module.parse_configured_release_title = original_parse
        scanner_module.is_offset_signal = original_signal
        scanner_module.Database = original_db

    check("不限季 → 30（显式 S2 也参与）", all_eps, 30)
    check("规则第 1 季 → 7（S2E30 被排除，S1E05 与无季 E07 保留）", season1, 7)
    check("规则第 2 季 → 30", season2, 30)


def main() -> int:
    rid = seed()
    try:
        asyncio.run(case_1(rid))
        case_2()
        case_3()
    finally:
        cleanup()
    print()
    if _COPIED:
        print(f"（本次跑在数据库副本上：{DB}）")
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
