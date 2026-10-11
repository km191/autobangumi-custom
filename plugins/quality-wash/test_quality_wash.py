"""quality-wash 插件的测试。

既可以用 pytest 跑，也可以直接 ``python test_quality_wash.py``（容器内）：

    docker exec -e PYTHONPATH=/app auto_bangumi \
        python /app/config/plugins/local/quality-wash/test_quality_wash.py

覆盖：组档位匹配（含合并发布、ANi 词边界、未知组垫底）、语言判定（ANi 强制繁中）、
择优打分、准入过滤、冲突替换、巡检计划（共用文件 / 未下完 / 卡住 三种护栏）。
"""

from __future__ import annotations

import asyncio
import http.server
import json
import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/app")

from ab_sdk.ingest import BangumiInfo, TorrentInfo  # noqa: E402
from ab_sdk.rename import Revision, RevisionTask  # noqa: E402
from ab_sdk.testing import create_plugin  # noqa: E402

from quality_wash import (  # noqa: E402
    QualityWash,
    preview_event,
    container_rank,
    detect_revision,
    WashEvent,
    anime_key,
    anime_key_from_bangumi,
    detect_language,
    group_priority,
    is_ani_group,
    outranks,
    outranks_with_rank,
    parse_episode,
    rule_tags,
    score_release,
    show_name,
)

FAILURES: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


def make_plugin(**options):
    plugin, ctx = create_plugin(QualityWash, options, plugin_id="quality-wash")
    return plugin, ctx


class StubClient:
    """替身下载器：只为巡检提供文件列表。"""

    def __init__(self, files: dict[str, list[str]]) -> None:
        self._files = files

    async def get_torrent_files(self, torrent_hash: str):
        return [{"name": n} for n in self._files.get(torrent_hash, [])]


def info(hash_: str, name: str, progress: float = 1.0, **extra) -> dict:
    return {
        "hash": hash_,
        "name": name,
        "progress": progress,
        "save_path": r"E:\好女孩\测试番剧\Season 1",
        "num_complete": 10,
        **extra,
    }


def task(hash_: str, name: str, group: str, revision: int = 1) -> RevisionTask:
    return RevisionTask(
        hash=hash_,
        name=name,
        file_count=1,
        revision=Revision(
            bangumi_id=1,
            media_type="episode",
            season=1,
            episode=1,
            group=group,
            resolution="1080p",
            revision=revision,
        ),
    )


# --------------------------------------------------------------------------- #
def test_group_tiers() -> None:
    print("\n1) 字幕组档位（优质组清单 + 垫底组清单）")
    top = ["喵萌奶茶屋", "绿茶字幕组"]
    bottom = ["LoliHouse", "ANi", "Nix-Raws"]

    def pri(text: str) -> tuple[int, str]:
        return group_priority(text, top, bottom)

    miao = pri("[Nekomoe kissaten] X")
    green = pri("[Studio GreenTea] X")
    loli = pri("[LoliHouse] X")
    ani = pri("[ANi] X")
    nix = pri("[Nix-Raws] X")
    other = pri("[SomeGroup] X")

    check("喵萌 命中优质组第一档", miao[1], "喵萌奶茶屋")
    check("绿茶 命中优质组第二档", green[1], "绿茶字幕组")
    check("优质组 高于 未列出组", miao[0] > other[0] and green[0] > other[0], True)
    check("未列出组 高于 垫底组", other[0] > loli[0], True)
    check("垫底：LoliHouse > ANi", loli[0] > ani[0], True)
    check("垫底：ANi > Nix-Raws（按你的要求）", ani[0] > nix[0], True)
    check("垫底三档都在未列出组之下", all(x[0] < other[0] for x in (loli, ani, nix)), True)
    check("合并发布取最高档", pri("Nekomoe kissaten&LoliHouse")[1], "喵萌奶茶屋")
    check("'Anime' 不会误命中 ANi", is_ani_group("[SomeGroup] Anime Title"), False)
    check("ANi 能被认出来", is_ani_group("[ANi] X - 01 [CHT].mp4"), True)
    check("把 ANi 写进优质组也能生效", group_priority("[ANi] X", ["ANi"], bottom)[1], "ANi")


def test_language() -> None:
    print("\n2) 语言档位（含 ANi 永远繁中）")
    check("CHS → 简", detect_language(None, "[X] Y [CHS_JPN].mkv"), "简")
    check("CHT → 繁", detect_language(None, "[X] Y [CHT_JPN].mkv"), "繁")
    check("CHI → 简繁", detect_language(None, "[X] Y [CHI_JPN].mkv"), "简繁")
    check("ASSx2 → 简繁", detect_language(None, "[X] Y [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"), "简繁")
    check("没有语言标记 → 未知", detect_language(None, "[X] Y [WebRip 1080p HEVC-10bit AAC].mkv"), "未知")
    check("ANi 强制繁中（就算没标记）", detect_language(None, "[ANi] Y - 01 [1080P][Baha].mp4", force_traditional=True), "繁")


def test_scoring() -> None:
    print("\n3) 择优打分")
    options = QualityWash.config_model.model_validate({})
    miao = score_release(
        "[Nekomoe kissaten] X - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv", None, options
    )
    ani = score_release(
        "[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", None, options
    )
    loli = score_release("[LoliHouse] X - 01 [WebRip 1080p HEVC-10bit AAC].mkv", None, options)
    nix = score_release("[Nix-Raws] X - 01 [CR 1080p AVC AAC].mkv", None, options)
    check("喵萌/简繁/10bit 的档位", (miao.group_name, miao.language, miao.bit_depth), ("喵萌奶茶屋", "简繁", 10))
    check("ANi 语言被算成繁中", ani.language, "繁")
    check("喵萌 优于 ANi", outranks(ani, miao), True)
    check("LoliHouse 优于 ANi", outranks(ani, loli), True)
    check("ANi 优于 Nix-Raws", outranks(nix, ani), True)
    check("更好的已在前 → 不算升级", outranks(miao, ani), False)
    check("720p 不允许顶掉 1080p", outranks(
        score_release("[X] Y - 01 [1080p AVC-8bit].mkv", None, options),
        score_release("[Y] Y - 01 [720p AVC-8bit].mkv", None, options),
    ), False)


def test_episode_and_show() -> None:
    print("\n4) 集数与番剧名解析")
    check("S01E05", parse_episode("番剧 S01E05.mkv"), (1, 5.0))
    check("[05] 形式", parse_episode("[ANi] X - 05 [1080P].mp4"), (1, 5.0))
    check("第05集", parse_episode("某番 第05集.mkv"), (1, 5.0))
    check("解析不出来 → None", parse_episode("某番 合集.mkv"), None)
    check("从保存路径取番剧名", show_name(r"E:\好女孩\再见，拉拉\Season 1"), "再见，拉拉")
    check("没有 Season 段时取末段", show_name(r"E:/好女孩/某番"), "某番")


def test_admission_filter() -> None:
    print("\n5) 准入过滤：同一集只放行最优")
    plugin, _ctx = make_plugin()
    bangumi = BangumiInfo(id=1, official_title="测试番剧", title_raw="测试番剧", season=1)
    ani = TorrentInfo(name="[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", url="u1")
    miao = TorrentInfo(name="[Nekomoe kissaten] X - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv", url="u2")
    ani2 = TorrentInfo(name="[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", url="u3")

    class FakeRelease:
        group = None
        subtitle = None
        resolution = None
        codecs = ()
        season = 1
        episode = 1
        version = None

    check("第一个（ANi）放行", plugin.prefer_best(ani, FakeRelease(), bangumi).accept, True)
    # 同一部番的**另一条规则**（不同 id、同一保存目录）必须落在同一个桶里，
    # 否则两条规则各算各的，"每集只留最优"和下载顺序都不会生效（踩过）
    other_rule = BangumiInfo(id=2, official_title="测试番剧", title_raw="测试番剧", season=1,
                             save_path=bangumi.save_path)
    check("另一条规则的桶与第一条相同",
          f"{anime_key_from_bangumi(other_rule)}:1" == f"{anime_key_from_bangumi(bangumi)}:1", True)
    check("更好的（喵萌）放行", plugin.prefer_best(miao, FakeRelease(), bangumi).accept, True)
    verdict = plugin.prefer_best(ani2, FakeRelease(), bangumi)
    check("再来一个更差的被拒", verdict.accept, False)
    check("拒绝原因可读", bool(verdict.reason), True)


def test_conflict_policy() -> None:
    print("\n6) 冲突替换策略")
    plugin, _ctx = make_plugin()
    policy = plugin.conflict_policy()
    ani = task("a" * 40, "[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", "ANi")
    miao = task("b" * 40, "[Nekomoe kissaten] X - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv", "喵萌奶茶屋")

    from ab_sdk.rename import ConflictRequest

    check("更好的进来 → replace", policy.decide(
        ConflictRequest("p", miao, (ani,), strict_upgrade=False)
    ).action, "replace")
    hold = policy.decide(ConflictRequest("p", ani, (miao,), strict_upgrade=False))
    check("更差的进来 → hold", hold.action, "hold")
    check("hold 有原因", bool(hold.reason), True)
    same_group_higher = task("c" * 40, "[ANi] X - 01 v2 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", "ANi", revision=2)
    check("同组更高版本号 → replace", policy.decide(
        ConflictRequest("p", same_group_higher, (ani,), strict_upgrade=True)
    ).action, "replace")

    off, _ = make_plugin(replace_on_conflict=False)
    check("关掉开关后 → hold", off.conflict_policy().decide(
        ConflictRequest("p", miao, (ani,), strict_upgrade=False)
    ).action, "hold")


def test_sweep_plan() -> None:
    print("\n7) 巡检计划（护栏）")
    plugin, _ctx = make_plugin()

    ani_name = "[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
    miao_name = "[Nekomoe kissaten] X - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"

    async def plan(infos, files, titles=None):
        return await plugin._plan_removals(
            StubClient(files), infos, titles={} if titles is None else titles
        )

    # 更好的一份已下完 → 删 ANi
    infos = [info("a" * 40, ani_name), info("b" * 40, miao_name)]
    files = {"a" * 40: [ani_name], "b" * 40: [miao_name]}
    result = asyncio.run(plan(infos, files))
    check("更好的一份已下完 → 计划删 ANi", [r["loser_group"] for r in result], ["ANi"])

    # 更好的一份还没下完 → 一个都不删
    infos = [info("a" * 40, ani_name), info("b" * 40, miao_name, progress=0.3)]
    result = asyncio.run(plan(infos, files))
    check("更好的一份没下完 → 不删", result, [])

    # 种子里没有组名（AB 把 qB 种子名改写成了「剧名 SxxEyy」）→ 那是同一次下载，
    # 不算"可洗的另一份"，因此不发洗版通知、也不删
    renamed = "X S01E01.mp4"
    infos = [info("a" * 40, ani_name), info("c" * 40, renamed)]
    files = {"a" * 40: [ani_name], "c" * 40: [renamed]}
    result = asyncio.run(plan(infos, files))
    check("未知组不参与洗版 → 计划为空", result, [])

    # 两个种子指向同一个文件 → 不删（曾经把共用文件删掉过）
    shared = "同名文件 S01E01.mp4"
    infos = [info("a" * 40, ani_name), info("b" * 40, miao_name)]
    files = {"a" * 40: [shared], "b" * 40: [shared]}
    result = asyncio.run(plan(infos, files))
    check("共用同一个文件 → 不删", result, [])

    # 首选卡住（0% + 无做种者 + 很久没动）→ 计划清掉卡住的那份
    stuck = info("b" * 40, miao_name, progress=0.0, num_complete=0, last_activity=0)
    infos = [info("a" * 40, ani_name), stuck]
    result = asyncio.run(plan(infos, files))
    check("首选卡住 → 清掉它（放次优进来）", [r["stuck"] for r in result], [True])

    # 不同番剧、同一个根目录、同一集 → 绝不能混在一起清理（踩过的坑）
    ani_root = dict(info("a" * 40, ani_name))
    ani_root["save_path"] = r"E:\夸克动漫"
    other = dict(info("c" * 40, "[LoliHouse] 别的番剧 - 01 [WebRip 1080p HEVC-10bit AAC].mkv"))
    other["save_path"] = r"E:\夸克动漫"
    result = asyncio.run(
        plan([ani_root, other], {"a" * 40: [ani_name], "c" * 40: [other["name"]]})
    )
    check("不同番剧同根目录同集 → 不混桶", result, [])

    # 同一部番的两条规则（ab: 不同、官方名相同）→ 身份一致，能洗
    ani_tagged = dict(info("a" * 40, ani_name, tags="ab:1"))
    miao_tagged = dict(info("b" * 40, miao_name, tags="ab:2"))
    result = asyncio.run(
        plan(
            [ani_tagged, miao_tagged],
            {"a" * 40: [ani_name], "b" * 40: [miao_name]},
            titles={1: "测试番剧", 2: "测试番剧"},
        )
    )
    check("同一部番两条规则 → 能洗", [r["loser_group"] for r in result], ["ANi"])

    # 两条规则的官方名不同（其实是不同番）→ 不洗
    result = asyncio.run(
        plan(
            [ani_tagged, miao_tagged],
            {"a" * 40: [ani_name], "b" * 40: [miao_name]},
            titles={1: "测试番剧", 2: "另一部番"},
        )
    )
    check("官方名不同 → 不洗", result, [])

    # 更优的还在下，但更差的那份已经卡死（0% + 无做种 + 很久没动）→ 顺手清掉
    stuck_loser = dict(info("a" * 40, ani_name, progress=0.0, num_complete=0, last_activity=0, tags="ab:2"))
    healthy = dict(info("b" * 40, miao_name, progress=0.5, tags="ab:1"))
    result = asyncio.run(
        plan([stuck_loser, healthy],
             {"a" * 40: [ani_name], "b" * 40: [miao_name]},
             titles={1: "测试番剧", 2: "测试番剧"})
    )
    check("更优未下完但次优已卡死 → 清掉卡死的", [r["stuck"] for r in result], [True])

    # 没有重复 → 空计划
    result = asyncio.run(plan([info("a" * 40, ani_name)], {"a" * 40: [ani_name]}))
    check("没有重复 → 空", result, [])



def test_download_order() -> None:
    print("\n8) 下载顺序（同档位时先取谁）")
    plugin, _ctx = make_plugin()

    class FakeRule:
        def __init__(self, rid, group, path, season=1, dpi="1080p", subtitle="CHS", source="WebRip"):
            self.id = rid
            self.group_name = group
            self.save_path = path
            self.season = season
            self.dpi = dpi
            self.subtitle = subtitle
            self.source = source
            self.official_title = "测试番剧"

    a = FakeRule(11, "ANi", r"E:\好女孩\测试番剧\Season 1", subtitle="CHT")
    b = FakeRule(12, "喵萌奶茶屋", r"E:\好女孩\测试番剧\Season 1")
    other = FakeRule(13, "别的组", r"E:\好女孩\另一部番\Season 1")

    check("同一部番的 key 相同", anime_key(a) == anime_key(b), True)
    check("不同番的 key 不同", anime_key(a) == anime_key(other), False)
    check("规则标签拼装", rule_tags(a), "1080p · CHT · WebRip")

    # 用户把 ANi 拖到第一位（快的先取）
    plugin._orders = {anime_key(a): [11, 12]}
    plugin._rebuild_rank_index()
    check("顺序里的位置（ANi 先）", plugin._download_rank(11), 0)
    check("顺序里的位置（喵萌 后）", plugin._download_rank(12), 1)
    check("没设过的规则排最后", plugin._download_rank(999), 10_000)

    options = QualityWash.config_model.model_validate({})
    same_one = score_release("[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", None, options)
    same_two = score_release("[ANi] X - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", None, options)
    check("同分时靠前的算更优", outranks_with_rank(same_one, same_two, 1, 0), True)
    check("同分时靠后的不算更优", outranks_with_rank(same_one, same_two, 0, 1), False)

    better = score_release("[Nekomoe kissaten] X - 01 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv", None, options)
    check("质量更高时顺序不影响", outranks_with_rank(same_one, better, 0, 5), True)



def test_notification_layout() -> None:
    """通知排版与「入库通知」保持一致：标题 + emoji 分行字段，且是纯文本。"""
    preview = WashEvent(
        title_text="1 处重复",
        episode="巡检预览（dry-run）",
        detail="未知组 → ANi",
        action="未删除任何文件",
    )
    title, body = preview.describe()
    lines = body.split("\n")
    check("预览通知标题", title, "洗版完成")
    check("预览通知第一行", lines[0], "🎬 1 处重复")
    check("预览通知第二行", lines[1], "📺 巡检预览（dry-run）")
    check("预览通知替换行", lines[2], "🔁 替换：未知组 → ANi")
    check("预览通知处理行", lines[3], "🧹 处理：未删除任何文件")
    check("不使用旧标签", any(l.startswith("番剧：") or l.startswith("集数：") for l in lines), False)
    check("不含 HTML 标签", ("<b>" in body or "</b>" in body), False)

    washed = WashEvent(title_text="某番剧", episode="S01E05", detail="ANi → 喵萌奶茶屋")
    _, body2 = washed.describe()
    check("真实洗版无处理行时少一行", len(body2.split("\n")), 3)
    check("真实洗版替换行", body2.split("\n")[2], "🔁 替换：ANi → 喵萌奶茶屋")



def test_quality_order() -> None:
    """画质排序：10bit > 8bit、mkv > mp4，且分辨率优先于两者。"""
    options = QualityWash.config_model.model_validate({})

    def score(name: str):
        return score_release(name, None, options)

    base8_mp4 = score("[Group] X - 01 [1080p AVC 8bit AAC].mp4")
    base8_mkv = score("[Group] X - 01 [1080p AVC 8bit AAC].mkv")
    base10_mkv = score("[Group] X - 01 [1080p AVC 10bit AAC].mkv")
    base10_mp4 = score("[Group] X - 01 [1080p AVC 10bit AAC].mp4")
    uhd_mp4 = score("[Group] X - 01 [2160p AVC 8bit AAC].mp4")
    hevc = score("[Group] X - 01 [1080p HEVC AAC].mkv")

    check("位深：10bit > 8bit（同容器）", outranks(base8_mkv, base10_mkv), True)
    check("位深：8bit 不优于 10bit", outranks(base10_mkv, base8_mkv), False)
    check("容器：mkv > mp4（同位深）", outranks(base8_mp4, base8_mkv), True)
    check("容器：mp4 不优于 mkv", outranks(base8_mkv, base8_mp4), False)
    check("位深优先于容器：10bit mp4 > 8bit mkv", outranks(base8_mkv, base10_mp4), True)
    check("分辨率优先于两者：2160p mp4 > 1080p mkv", outranks(base8_mkv, uhd_mp4), True)
    check("分辨率不降级保护仍然生效", outranks(uhd_mp4, base10_mkv), False)
    check("未写位深的 HEVC 按 10bit 处理", hevc.bit_depth, 10)
    check("容器档位：mkv > 其它 > mp4", (container_rank(".mkv"), container_rank(".avi"), container_rank(".mp4")), (2, 1, 0))
    check("未知容器与居中同档", container_rank(""), 1)



# --------------------------------------------------------------------------- #
# 库中已有保护（episode-guard）
# --------------------------------------------------------------------------- #

class _EmbyStub(http.server.BaseHTTPRequestHandler):
    episodes: list[dict] = []
    asked: list[str] = []

    def do_GET(self):  # noqa: N802
        _EmbyStub.asked.append(self.path)
        body = json.dumps({"Items": _EmbyStub.episodes}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _episode(path: str, container: str = "mkv", bit_depth: int = 8, height: int = 1080, name: str = "x"):
    return {
        "Name": name,
        "Path": path,
        "MediaSources": [
            {
                "Container": container,
                "Height": height,
                "MediaStreams": [{"Type": "Video", "BitDepth": bit_depth, "Height": height}],
            }
        ],
    }


def test_library_guard() -> None:
    print("\n9) 库中已有保护：重建规则后不重头下，但更好的版本仍放行")
    server = http.server.HTTPServer(("127.0.0.1", 0), _EmbyStub)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _EmbyStub.episodes = [
            _episode(r"E:\好女孩\花织即使是转生也想打架\Season 1\花织 S01E05.mkv", "mkv", 8, 1080, "第 5 话"),
            _episode(r"E:\好女孩\花织即使是转生也想打架\Season 1\花织 S01E06.mp4", "mp4", 8, 1080, "第 6 话"),
            _episode(r"E:\好女孩\别的番\Season 1\别的番 S01E01.mkv", "mkv", 10, 2160, "第 1 话"),
        ]
        plugin, _ctx = create_plugin(
            QualityWash,
            {"emby_url": base, "emby_token": "tok", "known_library_guard": True},
            plugin_id="quality-wash",
        )
        asyncio.run(plugin._scan_library())
        check("抓到 3 集", len(plugin._library), 3)
        check(
            "桶键与规则侧一致（番剧文件夹|季:集）",
            "花织即使是转生也想打架|1:5" in plugin._library,
            True,
        )
        score, _name, _stamp = plugin._library["花织即使是转生也想打架|1:5"]
        check("库里那份的分辨率/位深/容器", (score.resolution, score.bit_depth, score.container), (1080, 8, ".mkv"))

        bangumi = BangumiInfo(
            id=1,
            official_title="花织即使是转生也想打架",
            title_raw="花織即使是轉生也想打架",
            season=1,
            save_path=r"E:\好女孩\花织即使是转生也想打架\Season 1",
        )

        def release(season: int = 1, episode: float = 5):
            # 宿主真实调用时总会带 release；这里给一个最小的，别让过滤器在开头就放行
            return types.SimpleNamespace(
                group=None, subtitle=None, resolution=None, codecs=(),
                season=season, episode=episode, version=None, raw="",
            )

        def play(name: str):
            return plugin.prefer_best(TorrentInfo(name=name, url="u"), release(), bangumi)

        same = play("[Unknown] 花织 - 05 [1080p AVC 8bit AAC].mkv")
        check("同规格再下一遍 → 拒绝（这就是不重头下）", same.accept, False)
        worse_res = play("[Unknown] 花织 - 05 [720p AVC 8bit AAC].mkv")
        check("更低分辨率 → 拒绝", worse_res.accept, False)
        better_bit = play("[Unknown] 花织 - 05 [1080p AVC 10bit AAC].mkv")
        check("10bit 优于库里的 8bit → 放行", better_bit.accept, True)
        better_group = play("[Nekomoe kissaten] 花织 - 05 [1080p AVC 8bit AAC].mp4")
        check("优质组即使规格略低 → 放行（组档位优先）", better_group.accept, True)
        bottom_group = play("[ANi] 花织 - 05 [2160p AVC 10bit AAC].mkv")
        check("垫底组不因为规格高就放行（组档位优先）", bottom_group.accept, False)
        check("拒绝原因里说明是库里已有", "库里已有" in (same.reason or "") or "已有不差于它" in (same.reason or ""), True)

        # 基线要能持久化（重启后仍然生效）
        asyncio.run(plugin._save_library())
        plugin._library = {}
        asyncio.run(plugin._load_library())
        check("重启后基线仍在", "花织即使是转生也想打架|1:5" in plugin._library, True)

        # 关掉保护 → 同规格也会放行
        off, _ = create_plugin(
            QualityWash,
            {"emby_url": base, "emby_token": "tok", "known_library_guard": False},
            plugin_id="quality-wash",
        )
        asyncio.run(off._scan_library())
        allowed = off.prefer_best(TorrentInfo(name="[Unknown] 花织 - 05 [1080p AVC 8bit AAC].mkv", url="u"), release(), bangumi)
        check("关掉保护后同规格放行", allowed.accept, True)
    finally:
        server.shutdown()




def test_revision_preference() -> None:
    """v2 优先：01 和 01v2 选 v2；且不误判 V2Sub / AV1。"""
    options = QualityWash.config_model.model_validate({})

    check("v2 解析", detect_revision("[ANi] X - 01v2 [1080P].mp4"), 2)
    check("v3 解析", detect_revision("X S01E05v3 [WebRip].mkv"), 3)
    check("括号里的 v2", detect_revision("X - 01 (v2) [1080p].mkv"), 2)
    check("没有版本号 → 1", detect_revision("[ANi] X - 01 [1080P].mp4"), 1)
    check("组名 V2Sub 不误判", detect_revision("[V2Sub] X - 01 [1080p AVC].mkv"), 1)
    check("编码 AV1 不误判", detect_revision("[Group] X - 01 [1080p AV1 AAC].mkv"), 1)
    check("01v2 与 01 是同一集", parse_episode("[ANi] X - 01v2 [1080P].mp4"), (1, 1.0))
    check("S01E05v3 也能解析", parse_episode("X S01E05v3 [WebRip].mkv"), (1, 5.0))

    def score(name: str):
        return score_release(name, None, options, revision=detect_revision(name))

    v1 = score("[ANi] X - 01 [1080p AVC 8bit AAC].mp4")
    v2 = score("[ANi] X - 01v2 [1080p AVC 8bit AAC].mp4")
    check("v2 优于 v1", outranks(v1, v2), True)
    check("v1 不优于 v2", outranks(v2, v1), False)
    check("版本号只在其余相同时起作用", (v2.revision, v1.revision), (2, 1))

    good_v1 = score_release("[Nekomoe kissaten] X - 01 [1080p AVC 8bit AAC].mp4", None, options, revision=1)
    bad_v2 = score_release("[ANi] X - 01v2 [1080p AVC 8bit AAC].mp4", None, options, revision=2)
    check("字幕组档位仍优先：好组的 v1 压过差组的 v2", outranks(bad_v2, good_v1), True)



def test_hold_new_subscriptions() -> None:
    """新订阅先暂停：开关打开后，未确认的规则一律拦在准入处。"""
    print("\n11) 新订阅先暂停")
    bangumi = BangumiInfo(
        id=99, official_title="某新番", title_raw="某新番", season=1,
        save_path=r"E:\好女孩\某新番\Season 1",
    )

    def release(ep: float = 1):
        return types.SimpleNamespace(
            group=None, subtitle=None, resolution=None, codecs=(),
            season=1, episode=ep, version=None, raw="",
        )

    # 开关关闭：什么都不拦（默认行为不变）
    off, _ = create_plugin(QualityWash, {"hold_new_subscriptions": False}, plugin_id="quality-wash")
    check("开关关闭 → 不暂停", off._is_held(99), False)
    verdict = off.prefer_best(
        TorrentInfo(name="[ANi] 某新番 - 01 [1080p AVC 8bit AAC].mkv", url="u"), release(), bangumi
    )
    check("开关关闭 → 正常放行", verdict.accept, True)

    # 开关打开：现有规则算已确认，之后新建的算未确认
    on, _ = create_plugin(QualityWash, {"hold_new_subscriptions": True}, plugin_id="quality-wash")
    asyncio.run(on._sync_hold_arming())
    check("开关打开后未确认 → 暂停", on._is_held(99), True)
    verdict = on.prefer_best(
        TorrentInfo(name="[ANi] 某新番 - 01 [1080p AVC 8bit AAC].mkv", url="u"), release(), bangumi
    )
    check("暂停中的规则被拒绝", verdict.accept, False)
    check("拒绝原因说明了要怎么办", "下载顺序" in (verdict.reason or ""), True)

    # 放行（相当于在界面上点「立即放行」或保存顺序）
    asyncio.run(on.release_rule(99))
    check("放行后不再暂停", on._is_held(99), False)
    verdict = on.prefer_best(
        TorrentInfo(name="[ANi] 某新番 - 01 [1080p AVC 8bit AAC].mkv", url="u"), release(), bangumi
    )
    check("放行后正常放行", verdict.accept, True)

    # 暂停只影响未确认的那条，别的规则不受影响
    asyncio.run(on.release_rule(100))
    check("已确认的规则不受影响", on._is_held(100), False)
    check("另一条未确认的仍然暂停", on._is_held(101), True)



def test_wash_notification_names_anime() -> None:
    """洗版通知必须说清是哪些动漫（第一行就是动漫名）。"""
    plan = [
        {"title": "至高之力", "episode": "第1季 第3集", "loser_group": "ANi", "winner_group": "LoliHouse"},
        {"title": "转生就是剑", "episode": "第2季 第5集", "loser_group": "ANi", "winner_group": "喵萌奶茶屋"},
    ]
    title, body = preview_event(plan).describe()
    lines = body.split("\n")
    check("预览通知标题", title, "洗版完成")
    check("第一行是动漫名（不是数字）", lines[0], "🎬 至高之力、转生就是剑")
    check("第二行说明是 dry-run 与处数", lines[1], "📺 巡检预览（dry-run）· 2 处重复")
    check(
        "第三行带上动漫名与替换关系",
        lines[2],
        "🔁 替换：至高之力 ANi → LoliHouse（第1季 第3集）；转生就是剑 ANi → 喵萌奶茶屋（第2季 第5集）",
    )
    check("第四行是处理说明", lines[3], "🧹 处理：未删除任何文件")

    # 多部动漫时只列前 4 个并给总数
    many = [
        {"title": f"番剧{i}", "episode": "", "loser_group": "ANi", "winner_group": "LoliHouse"}
        for i in range(1, 7)
    ]
    _, body2 = preview_event(many).describe()
    check("超过 4 部时给总数", body2.split("\n")[0], "🎬 番剧1、番剧2、番剧3、番剧4 等 6 部")
    check("明细也会省略", body2.split("\n")[2].endswith("……等 6 处"), True)


def main() -> int:
    test_group_tiers()
    test_language()
    test_scoring()
    test_episode_and_show()
    test_admission_filter()
    test_conflict_policy()
    test_sweep_plan()
    test_download_order()
    test_quality_order()
    test_library_guard()
    test_revision_preference()
    test_hold_new_subscriptions()
    test_wash_notification_names_anime()
    test_notification_layout()
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
