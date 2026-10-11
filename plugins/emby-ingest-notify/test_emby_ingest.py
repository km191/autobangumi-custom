"""入库通知插件的测试。

容器内直接跑（配置目录已挂载，无需拷贝）：

    docker exec -e PYTHONPATH=/app auto_bangumi \
        python /app/config/plugins/local/emby-ingest-notify/test_emby_ingest.py

用一个本地 HTTP 服务同时扮演 Emby 与 Telegram，所以整条流程都是**真实请求**：
查 Emby 收录 → 取海报 → 发图（带降级）→ 站内事件，另含去重与超时路径。
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

from ab_sdk.testing import create_plugin  # noqa: E402

from emby_ingest import (  # noqa: E402
    EmbyIngestNotify,
    anime_folder,
    compose_message,
    dedup_key,
    map_path,
    parse_episode,
    show_name_from_path,
)

FAILURES: list[str] = []
POSTER_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


def check_in(label: str, needle: str, haystack: str) -> None:
    ok = needle in haystack
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         没找到: {needle!r}\n         正文: {haystack!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------- #
# 假 Emby + 假 Telegram
# --------------------------------------------------------------------------- #

class Fake:
    episode: dict | None = None
    series_name: str | None = None
    have_poster: bool = True
    bgm_search: list | None = None
    bgm_detail: dict | None = None
    require_backslash: bool = True
    calls: list[dict] = []


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, kind, payload):
        Fake.calls.append({"kind": kind, "payload": payload})

    # ---- Emby ----
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/emby/Items":
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            asked = (query.get("Path") or [""])[0]
            Fake.calls.append({"kind": "emby-query", "payload": asked})
            # 真 Emby（Windows）只认反斜杠：这里模拟它，逼插件走退回逻辑
            if Fake.require_backslash and "/" in asked:
                return self._json({"Items": [], "TotalRecordCount": 0})
            item = Fake.episode
            self._record("emby-items", self.path)
            if item:
                # 真 Emby 返回的条目路径就是被查询的那个文件路径，这里照做
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                item = {**item, "Path": (query.get("Path") or [item.get("Path")])[0]}
            return self._json({"Items": [item] if item else [], "TotalRecordCount": 1 if item else 0})
        if path.startswith("/emby/Items/") and path.endswith("/Images/Primary"):
            if not Fake.have_poster:
                return self._json({"error": "no image"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(POSTER_BYTES)))
            self.end_headers()
            self.wfile.write(POSTER_BYTES)
            return
        if path.startswith("/emby/Items/"):
            return self._json({"Name": Fake.series_name})
        self._json({}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        if path.endswith("/sendPhoto") or path.endswith("/sendMessage"):
            kind = "sendPhoto" if path.endswith("sendPhoto") else "sendMessage"
            text = raw.decode("utf-8", "replace")
            # multipart 里也能读到 caption / text
            self._record(kind, text)
            return self._json({"ok": True})
        if path.startswith("/v0/search/subjects"):
            self._record("bgm-search", raw.decode("utf-8", "replace"))
            data = Fake.bgm_search
            return self._json({"data": data or []})
        self._json({}, 404)

    def log_message(self, *args):
        pass


def run_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def test_parsing() -> None:
    print("\n1) 文件名解析与路径推导")
    check("S01E11", parse_episode(r"E:\好女孩\恶女\Season 1\恶女 S01E11.mp4"), (1, 11.0))
    check("S02E05.5（半集）", parse_episode(r"E:\好女孩\X\Season 2\X S02E05.5.mkv"), (2, 5.5))
    check("退回 Season 目录 + 裸集数", parse_episode(r"E:\好女孩\X\Season 3\X - 07.mkv"), (3, 7.0))
    check("解析不出来 → None", parse_episode(r"E:\好女孩\X\movie.mkv"), None)
    check("番剧目录", anime_folder(r"E:\好女孩\恶女\Season 1\a.mkv"), "E:/好女孩/恶女")
    check("没有 Season 时用父目录", anime_folder(r"E:\好女孩\某电影 (2026)\a.mkv"), "E:/好女孩/某电影 (2026)")
    check("番剧名兜底", show_name_from_path(r"E:\好女孩\恶女不才\Season 1\a.mkv"), "恶女不才")


def test_map_path() -> None:
    print("\n2) 路径映射（可选）")
    plugin, _ = make_plugin()
    check("未配置时原样返回", map_path(r"E:\好女孩\x.mkv", plugin.config), r"E:\好女孩\x.mkv")
    mapped, _ = make_plugin(path_from=r"E:\好女孩", path_to="/media/anime")
    check("配置后替换前缀", map_path(r"E:\好女孩\恶女\x.mkv", mapped.config), "/media/anime/恶女/x.mkv")


def test_compose() -> None:
    print("\n3) 正文拼装")
    options = make_plugin()[0].config
    text = compose_message(
        title="恶女不才，请多关照 ～雏宫蝶鼠换身传～",
        season=1,
        episode=11,
        episode_title="第 11 话",
        group="喵萌奶茶屋",
        tags=["奇幻", "恋爱"],
        overview="这是一段足够长的剧情简介。" * 20,
        emby_series="我是不才惡女",
        options=options,
    )
    check_in("有剧名", "恶女不才，请多关照", text)
    check_in("有季集与集标题", "📺 S01E11 · 「第 11 话」", text)
    check_in("有字幕组", "🏷 字幕组：喵萌奶茶屋", text)
    check_in("有类别", "✨ 类别：奇幻 / 恋爱", text)
    check_in("简介被截断", "…", text)
    check_in("名字不一致时给出提示", "⚠️ Emby 里识别为「我是不才惡女」", text)

    same = compose_message(
        title="我是不才惡女", season=1, episode=11, emby_series="我是不才惡女",
        options=make_plugin()[0].config,
    )
    check("名字一致时没有提示行", "⚠️" in same, False)

    plain = compose_message(
        title="A & B", season=1, episode=1, episode_title="<x>",
        options=make_plugin()[0].config, for_telegram=False,
    )
    check("纯文本模式不转义", "<x>" in plain, True)

    html_text = compose_message(
        title="A & B", season=1, episode=1, episode_title="<x>",
        options=make_plugin()[0].config, for_telegram=True,
    )
    check("HTML 模式转义 &", "A &amp; B" in html_text, True)
    check("HTML 模式转义 <", "&lt;x&gt;" in html_text, True)

    check("去重键", dedup_key("恶女", 1, 11.0, "x"), "恶女|1|11")


# --------------------------------------------------------------------------- #
# 端到端（假 Emby + 假 Telegram）
# --------------------------------------------------------------------------- #

def make_plugin(**options):
    plugin, ctx = create_plugin(EmbyIngestNotify, options, plugin_id="emby-ingest-notify")
    return plugin, ctx


def run_handler(plugin, event):
    """触发订阅处理器并等待它派生出的后台任务结束（处理器本身立刻返回）。"""

    async def go():
        await plugin.on_organized(event)
        pending = list(plugin._pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(go())


def fake_event(path: str, bangumi_id: int | None = 7):
    return types.SimpleNamespace(
        bangumi_id=bangumi_id,
        files=[types.SimpleNamespace(kind="media", path=path)],
    )


PATH = r"E:\好女孩\恶女不才，请多关照 ～雏宫蝶鼠换身传～\Season 1\恶女不才，请多关照 ～雏宫蝶鼠换身传～ S01E11.mp4"


def test_end_to_end(base: str) -> None:
    print("\n4) 端到端：等入库 → 发图 → 去重")
    Fake.calls.clear()
    Fake.have_poster = True
    Fake.episode = {
        "Id": "abc123",
        "Path": PATH,
        "Name": "第 11 话 转生与打架",
        "Overview": "简介内容。",
        "IndexNumber": 11,
        "SeriesId": "series1",
        "SeriesName": "恶女不才，请多关照 ～雏宫蝶鼠换身传～",
        "Genres": ["动画"],
    }
    Fake.series_name = "恶女不才，请多关照 ～雏宫蝶鼠换身传～"
    Fake.bgm_search = [{"id": 123}]

    plugin, ctx = make_plugin(
        emby_url=base, emby_token="tok", telegram_token="tk", telegram_chat_id="42",
        telegram_api=base, wait_seconds=5, poll_interval=1,
    )
    # 模拟钩子记下的规则信息（剧名 + 字幕组）
    plugin.state.bangumi["7"] = {"title": "恶女不才，请多关照 ～雏宫蝶鼠换身传～", "group": "喵萌奶茶屋", "season": 1}

    run_handler(plugin, fake_event(PATH.replace("\\", "/")))

    photos = [c for c in Fake.calls if c["kind"] == "sendPhoto"]
    texts = [c for c in Fake.calls if c["kind"] == "sendMessage"]
    check("发出了 sendPhoto", len(photos), 1)
    check("没有降级成纯文本", len(texts), 0)
    if photos:
        body = photos[0]["payload"]
        check_in("照片里有海报字节标记", "fake-jpeg-bytes", body)
        check_in("caption 有字幕组", "字幕组", body)
        check_in("caption 有季集", "S01E11", body)
    check("计数 +1", plugin.state.sent, 1)
    asked = [c["payload"] for c in Fake.calls if c["kind"] == "emby-query"]
    check("先按路径原样查一次", any("/" in a for a in asked), True)
    check("失败后退回 Windows 反斜杠形式", any("\\" in a for a in asked), True)

    # 再来一次同样的整理事件 → 应被去重
    before = len(Fake.calls)
    run_handler(plugin, fake_event(PATH.replace("\\", "/")))
    check("重复事件不再发送", len(Fake.calls), before)
    check("跳过计数 +1", plugin.state.skipped, 1)


def test_mismatch_and_tags(base: str) -> None:
    print("\n5) Emby 认成别的番 + bgm 类别")
    Fake.calls.clear()
    Fake.episode = {
        "Id": "abc999",
        "Path": PATH,
        "Name": "第 12 话",
        "Overview": "简介。",
        "IndexNumber": 12,
        "SeriesId": "series2",
        "SeriesName": "我是不才惡女",
        "Genres": ["动画", "日本动画"],
    }
    Fake.series_name = "我是不才惡女"
    Fake.bgm_search = [{"id": 555}]
    Fake.bgm_detail = {"tags": [{"name": "奇幻"}, {"name": "恋爱"}, {"name": "喜剧"}]}

    plugin, _ = make_plugin(
        emby_url=base, emby_token="tok", telegram_token="tk", telegram_chat_id="42",
        telegram_api=base, wait_seconds=5, poll_interval=1,
    )
    plugin.state.bangumi["8"] = {"title": "恶女不才，请多关照 ～雏宫蝶鼠换身传～", "group": "ANi", "season": 1}
    event = fake_event(PATH.replace("E11", "E12"), bangumi_id=8)
    run_handler(plugin, event)

    photos = [c for c in Fake.calls if c["kind"] == "sendPhoto"]
    check("发出了通知", len(photos), 1)
    if photos:
        body = photos[0]["payload"]
        check_in("标题用 AB 的官方名", "恶女不才", body)
        check_in("给出 Emby 的识别差异提示", "我是不才惡女", body)
        check_in("类别来自 bgm", "奇幻", body)


def test_not_ingested(base: str) -> None:
    print("\n6) Emby 还没收录 → 不通知、不抛异常")
    Fake.calls.clear()
    Fake.episode = None   # Emby 查不到
    plugin, _ = make_plugin(
        emby_url=base, emby_token="tok", telegram_token="tk", telegram_chat_id="42",
        telegram_api=base, wait_seconds=0, poll_interval=1,
    )
    raised = False
    try:
        run_handler(plugin, fake_event(PATH.replace("E11", "E13")))
    except Exception as e:
        raised = True
        print("      异常:", e)
    check("没有抛异常", raised, False)
    check("没有发出任何消息", [c["kind"] for c in Fake.calls if c["kind"].startswith("send")], [])
    check("没有计入已通知", plugin.state.sent, 0)


def test_photo_fallback(base: str) -> None:
    print("\n7) 海报取不到 → 降级纯文本")
    Fake.calls.clear()
    Fake.have_poster = False
    Fake.episode = {
        "Id": "abc777",
        "Path": PATH,
        "Name": "第 14 话",
        "Overview": "简介。",
        "IndexNumber": 14,
        "SeriesId": "series3",
        "SeriesName": "恶女不才，请多关照 ～雏宫蝶鼠换身传～",
        "Genres": ["动画"],
    }
    Fake.bgm_search = []
    plugin, _ = make_plugin(
        emby_url=base, emby_token="tok", telegram_token="tk", telegram_chat_id="42",
        telegram_api=base, wait_seconds=5, poll_interval=1,
    )
    run_handler(plugin, fake_event(PATH.replace("E11", "E14")))
    texts = [c for c in Fake.calls if c["kind"] == "sendMessage"]
    check("降级发出了 sendMessage", len(texts), 1)
    if texts:
        from urllib.parse import unquote_plus

        decoded = unquote_plus(texts[0]["payload"])
        check_in("降级文案里有说明", "海报发送失败", decoded)
    Fake.have_poster = True


def test_batch_deadline(base: str) -> None:
    print("\n8) 处理器立刻返回，一批多集共享等待时间")
    import time

    Fake.calls.clear()
    Fake.episode = None  # 都查不到，纯看等待耗时
    plugin, _ = make_plugin(
        emby_url=base, emby_token="tok", wait_seconds=2, poll_interval=1,
    )
    files = [
        types.SimpleNamespace(kind="media", path=PATH.replace("E11", f"E{n:02d}"))
        for n in range(1, 6)
    ]
    event = types.SimpleNamespace(bangumi_id=None, files=files)

    async def go():
        started = time.monotonic()
        await plugin.on_organized(event)
        immediate = time.monotonic() - started
        pending = list(plugin._pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return immediate, time.monotonic() - started

    immediate, total = asyncio.run(go())
    print(f"      5 集、等待上限 2 秒 → 处理器返回用了 {immediate:.2f} 秒，后台全部结束共 {total:.1f} 秒")
    check("处理器立刻返回（不再堵订阅队列）", immediate < 0.5, True)
    check("整批耗时接近等待上限而不是 5 倍", total < 4.0, True)
    check("没有发出通知", [c["kind"] for c in Fake.calls if c["kind"].startswith("send")], [])


class _StubDownloader:
    """桩：指定 hash 的种子处于 missingFiles。"""

    def __init__(self, states=None):
        self._states = states if states is not None else [{"hash": "deadbeef", "state": "missingFiles"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_torrent_info(self, category=None, status_filter=None):
        return self._states


def test_dead_torrent(base: str) -> None:
    """文件已丢失的种子（missingFiles）：不空等、不查 Emby、不发通知。"""
    print("\n9) 失效种子直接跳过")
    Fake.calls.clear()
    Fake.episode = None
    plugin, _ = make_plugin(
        emby_url=base, emby_token="tok", telegram_token="tk", telegram_chat_id="42",
        telegram_api=base, wait_seconds=5, poll_interval=1,
    )

    import module.downloader as downloader

    original = downloader.DownloadClient
    downloader.DownloadClient = _StubDownloader
    try:
        event = types.SimpleNamespace(
            bangumi_id=None,
            torrent_hash="deadbeef",
            files=[types.SimpleNamespace(kind="media", path=PATH)],
        )
        run_handler(plugin, event)
    finally:
        downloader.DownloadClient = original

    check("没有发出通知", [c["kind"] for c in Fake.calls if c["kind"].startswith("send")], [])
    check("也没有去查 Emby", [c for c in Fake.calls if c["kind"] == "emby-items"], [])



def main() -> int:
    test_parsing()
    test_map_path()
    test_compose()
    server, base = run_server()
    try:
        test_end_to_end(base)
        test_mismatch_and_tags(base)
        test_not_ingested(base)
        test_photo_fallback(base)
        test_batch_deadline(base)
        test_dead_torrent(base)
    finally:
        server.shutdown()
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
