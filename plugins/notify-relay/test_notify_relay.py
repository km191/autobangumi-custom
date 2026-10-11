"""通知中继的测试。

    docker exec -e PYTHONPATH=/app auto_bangumi \
        python /app/config/plugins/local/notify-relay/test_notify_relay.py

用本地 HTTP 服务扮演 Telegram，验证：单集通知被丢弃、系统事件被转发（含转义）、
关掉 drop_episode 后单集也会转发、以及未配置凭据时不报错。
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "/app")

from ab_sdk.notify import NotificationMessage, NotifierSettings  # noqa: E402
from ab_sdk.testing import create_plugin  # noqa: E402

from notify_relay import NotifyRelay, TelegramRelay, compose  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        print(f"         期望: {expected!r}")
        FAILURES.append(label)


class Catcher(http.server.BaseHTTPRequestHandler):
    posts: list[str] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        Catcher.posts.append(raw)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def make_plugin(**options):
    return create_plugin(NotifyRelay, options, plugin_id="notify-relay")[0]


def main() -> int:
    server = http.server.HTTPServer(("127.0.0.1", 0), Catcher)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        opts = {"telegram_token": "tk", "telegram_chat_id": "42", "telegram_api": base}
        relay = TelegramRelay(NotifierSettings(), NotifyRelay.config_model.model_validate(opts))

        print("\n1) 单集通知（重命名完成）被丢弃")
        Catcher.posts.clear()
        ok = asyncio.run(relay.send(NotificationMessage(kind="episode", title="某番剧", body="S01E01")))
        check("返回成功（宿主不报错）", ok, True)
        check("没有发出任何请求", len(Catcher.posts), 0)

        print("\n2) 系统事件被转发（含标题与转义）")
        Catcher.posts.clear()
        ok = asyncio.run(
            relay.send(NotificationMessage(kind="event", title="洗版完成", body="🎬 A & B\n📺 S01E05"))
        )
        check("返回成功", ok, True)
        check("发出了 1 条", len(Catcher.posts), 1)
        if Catcher.posts:
            data = parse_qs(Catcher.posts[0])
            text = unquote_plus(data.get("text", [""])[0])
            check("带上了事件标题", "洗版完成" in text, True)
            check("正文字段都在（& 被转义是正确行为）", ("🎬 A &amp; B" in text) and ("📺 S01E05" in text), True)
            check("chat_id 正确", data.get("chat_id", [""])[0], "42")
            check("parse_mode 是 HTML", data.get("parse_mode", [""])[0], "HTML")

        print("\n3) 关掉 drop_episode 后单集也会转发")
        Catcher.posts.clear()
        relay2 = TelegramRelay(
            NotifierSettings(),
            NotifyRelay.config_model.model_validate({**opts, "drop_episode": False}),
        )
        asyncio.run(relay2.send(NotificationMessage(kind="episode", title="某番剧", body="S01E02")))
        check("发出了 1 条", len(Catcher.posts), 1)

        print("\n4) 未配置凭据时不报错")
        bare = TelegramRelay(NotifierSettings(), NotifyRelay.config_model.model_validate({}))
        ok = asyncio.run(bare.send(NotificationMessage(kind="event", title="x", body="y")))
        check("安静地返回成功", ok, True)
        ok2, msg = asyncio.run(bare.test())
        check("test() 明确说没配置", ok2, False)

        print("\n5) compose() 的排版")
        text = compose("洗版完成", "🎬 1 处重复", NotifyRelay.config_model.model_validate(opts))
        check("标题加粗", text.split("\n")[0], "<b>洗版完成</b>")
        check("正文第二行", text.split("\n")[1], "🎬 1 处重复")
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
