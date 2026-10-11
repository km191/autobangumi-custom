"""autoscan-push 插件的测试。

既可用 pytest，也可直接跑（容器内）：

    docker exec -e PYTHONPATH=/app auto_bangumi \
        python /app/config/plugins/local/autoscan-push/test_autoscan_push.py

覆盖：路径前缀换算（大小写/斜杠）、要推送的目录推导（整部番 vs 文件目录）、
一次事件里去重、字幕文件跳过、以及**真实发出的 HTTP 请求形状**
（用本地临时监听抓请求行），另外验证 autoscan 没开时不会抛异常。
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/app")

from ab_sdk.testing import create_plugin  # noqa: E402

from autoscan_push import (  # noqa: E402
    AutoscanPush,
    folders_from_event,
    normalize,
    organize_folder,
    to_autoscan_path,
)

FAILURES: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


def make_plugin(**options):
    plugin, ctx = create_plugin(AutoscanPush, options, plugin_id="autoscan-push")
    return plugin, ctx


def fake_event(files):
    """files: [(kind, path), ...]"""
    return types.SimpleNamespace(
        files=[types.SimpleNamespace(kind=kind, path=path) for kind, path in files]
    )


def test_path_mapping() -> None:
    print("\n1) 路径前缀换算")
    plugin, _ = make_plugin()
    opts = plugin.config
    check("normalize 反斜杠", normalize(r"E:\好女孩\花织\Season 1"), "E:/好女孩/花织/Season 1")
    check(
        "Windows → autoscan 容器路径",
        to_autoscan_path(r"E:\好女孩\花织\Season 1\a.mkv", opts),
        "/mnt/goodgirl/花织/Season 1/a.mkv",
    )
    check(
        "大小写与正斜杠都兼容",
        to_autoscan_path("e:/好女孩/花织/Season 1/a.mkv", opts),
        "/mnt/goodgirl/花织/Season 1/a.mkv",
    )
    check("恰好等于前缀", to_autoscan_path(r"E:\好女孩", opts), "/mnt/goodgirl")
    check(
        "不属于该前缀时原样返回",
        to_autoscan_path(r"F:\别的盘\x.mkv", opts),
        "F:/别的盘/x.mkv",
    )
    check(
        "前缀相似但不能误判（E:\\好女孩2 ≠ E:\\好女孩）",
        to_autoscan_path(r"E:\好女孩2\x.mkv", opts),
        "E:/好女孩2/x.mkv",
    )


def test_folder_derivation() -> None:
    print("\n2) 要推送哪个目录")
    plugin, _ = make_plugin()
    opts = plugin.config
    check(
        "默认推整部番（Season N 的上一级）",
        organize_folder(r"E:\好女孩\花织\Season 1\a.mkv", opts),
        "E:/好女孩/花织",
    )
    check(
        "结构里没有 Season 时退回文件目录",
        organize_folder(r"E:\好女孩\某电影 (2026)\a.mkv", opts),
        "E:/好女孩/某电影 (2026)",
    )
    off, _ = make_plugin(push_anime_root=False)
    check(
        "关掉开关后只推文件所在目录",
        organize_folder(r"E:\好女孩\花织\Season 1\a.mkv", off.config),
        "E:/好女孩/花织/Season 1",
    )


def test_event_folders() -> None:
    print("\n3) 一次事件里推哪些目录（去重、跳过字幕）")
    plugin, _ = make_plugin()
    event = fake_event(
        [
            ("media", r"E:\好女孩\花织\Season 1\a.mkv"),
            ("media", r"E:\好女孩\花织\Season 1\b.mkv"),   # 同一目录 → 只推一次
            ("subtitle", r"E:\好女孩\花织\Season 1\a.ass"),  # 字幕 → 跳过
            ("media", r"E:\好女孩\花织\Season 2\c.mkv"),    # 同番不同季 → 同目录
            ("media", r"E:\好女孩\再见，拉拉\Season 1\d.mkv"),
        ]
    )
    check(
        "去重后的目录列表",
        folders_from_event(event, plugin.config),
        ["/mnt/goodgirl/花织", "/mnt/goodgirl/再见，拉拉"],
    )
    # 不在媒体根下的旧路径不该推（autoscan 里没有对应库）
    foreign = fake_event(
        [
            ("media", r"E:\动漫\二十世纪电气目录 (2026)\a.mkv"),
            ("media", r"F:\别的盘\x.mkv"),
            ("media", r"E:\好女孩\花织\Season 1\a.mkv"),
        ]
    )
    check(
        "媒体根之外的路径被跳过",
        folders_from_event(foreign, plugin.config),
        ["/mnt/goodgirl/花织"],
    )


class _Catcher(http.server.BaseHTTPRequestHandler):
    requests: list[tuple[str, str, str]] = []

    def do_POST(self):  # noqa: N802
        _Catcher.requests.append((self.command, self.path, dict(self.headers).get("Authorization", "")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # 静音
        pass


def test_real_http_request() -> None:
    print("\n4) 真实发出的 HTTP 请求形状")
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        plugin, _ = make_plugin(
            url=f"http://127.0.0.1:{port}", username="u", password="p"
        )
        event = fake_event([("media", r"E:\好女孩\花织\Season 1\a.mkv")])
        asyncio.run(plugin.on_organized(event))

        method, path, auth = _Catcher.requests[0]
        check("用的是 POST", method, "POST")
        check("路径是 /triggers/manual", path.split("?")[0], "/triggers/manual")
        check(
            "dir 参数是 autoscan 视角的目录",
            path.split("dir=")[1],
            "%2Fmnt%2Fgoodgirl%2F%E8%8A%B1%E7%BB%87",
        )
        check("带上了 Basic 认证", auth.startswith("Basic "), True)
        check("计数 +1", plugin.pushed, 1)
    finally:
        server.shutdown()


def test_autoscan_down() -> None:
    print("\n5) autoscan 没开时不抛异常（避免插件被熔断）")
    plugin, _ = make_plugin(url="http://127.0.0.1:1")  # 必然失败
    event = fake_event([("media", r"E:\好女孩\花织\Season 1\a.mkv")])
    try:
        asyncio.run(plugin.on_organized(event))
        raised = False
    except Exception:
        raised = True
    check("没有向外抛异常", raised, False)
    check("失败被计数", plugin.failed, 1)



def test_dedup() -> None:
    print("\n6) 去重：一次整理每个文件各一条事件，只应推一次")
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def one_file(name: str):
        return fake_event([("media", rf"E:\好女孩\花织\Season 1\{name}")])

    try:
        # —— 真实场景：一次整理 3 个文件 → AB 发 3 条事件（每条只有 1 个文件）
        _Catcher.requests.clear()
        plugin, _ = make_plugin(url=base, cooldown_minutes=10, dedup_hours=12)
        for name in ("a.mkv", "b.mkv", "c.mkv"):
            asyncio.run(plugin.on_organized(one_file(name)))
        check("同一次整理的 3 条事件 → 只推 1 次", len(_Catcher.requests), 1)

        # —— 之后 AB 每分钟重发同样的事件：合并窗口过了也不该再推
        plugin._pushed[list(plugin._pushed)[0]]["at"] = time.time() - 3600
        for _ in range(3):
            for name in ("a.mkv", "b.mkv", "c.mkv"):
                asyncio.run(plugin.on_organized(one_file(name)))
        check("内容没有新增 → 不重复推", len(_Catcher.requests), 1)

        # —— 真的多了一集（窗口也过了）→ 立刻推
        plugin._pushed[list(plugin._pushed)[0]]["at"] = time.time() - 3600
        asyncio.run(plugin.on_organized(one_file("d.mkv")))
        check("多了新文件 → 立刻推", len(_Catcher.requests), 2)

        # —— cooldown_minutes=0：不合并，但内容没变仍然不推
        plain, _ = make_plugin(url=base, cooldown_minutes=0, dedup_hours=0)
        asyncio.run(plain.on_organized(one_file("x.mkv")))
        first = len(_Catcher.requests)
        asyncio.run(plain.on_organized(one_file("x.mkv")))
        check("cooldown=0 / dedup=0：内容没变不推", len(_Catcher.requests), first)
        asyncio.run(plain.on_organized(one_file("y.mkv")))
        check("cooldown=0 / dedup=0：有新文件就推", len(_Catcher.requests), first + 1)

        # —— 兜底窗口：很久没推过、内容没变，也重推一次
        windowed, _ = make_plugin(url=base, cooldown_minutes=0, dedup_hours=12)
        asyncio.run(windowed.on_organized(one_file("z.mkv")))
        before = len(_Catcher.requests)
        windowed._pushed[list(windowed._pushed)[0]]["at"] = time.time() - 13 * 3600
        asyncio.run(windowed.on_organized(one_file("z.mkv")))
        check("超过兜底窗口 → 重推一次", len(_Catcher.requests), before + 1)
    finally:
        server.shutdown()


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


def test_dead_torrent() -> None:
    print("\n7) 文件已丢失的种子不再推送")
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _Catcher.requests.clear()
        plugin, _ = make_plugin(url=base)
        import module.downloader as downloader

        original = downloader.DownloadClient
        downloader.DownloadClient = _StubDownloader
        try:
            event = types.SimpleNamespace(
                bangumi_id=None,
                torrent_hash="deadbeef",
                files=[
                    types.SimpleNamespace(
                        kind="media", path=r"E:\好女孩\花织\Season 1\a.mkv"
                    )
                ],
            )
            asyncio.run(plugin.on_organized(event))
        finally:
            downloader.DownloadClient = original
        check("失效种子不推送", len(_Catcher.requests), 0)

        alive = _StubDownloader([{"hash": "deadbeef", "state": "uploading"}])
        plugin._states_cache = None      # 清掉 60 秒缓存，否则还是上一例的失效状态
        downloader.DownloadClient = alive
        try:
            asyncio.run(plugin.on_organized(event))
        finally:
            downloader.DownloadClient = original
        check("正常的种子照常推送", len(_Catcher.requests), 1)
    finally:
        server.shutdown()


def main() -> int:
    test_path_mapping()
    test_folder_derivation()
    test_event_folders()
    test_real_http_request()
    test_autoscan_down()
    test_dedup()
    test_dead_torrent()
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
