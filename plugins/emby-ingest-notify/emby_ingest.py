"""入库通知：等 Emby 真正收录该集，再发一条带海报的通知。

流程（全部在插件内完成，不改动 AutoBangumi 本体）：

1. ``torrent.organized``：拿到整理后的正片路径；
2. 轮询 Emby ``GET /emby/Items?…&Path=<文件>`` 直到查得到这一集（说明已入库）；
3. 取该集的标题/简介/所属剧集与海报，剧名以 **AutoBangumi 的官方名**为准
   （Emby 认成别的番时只提示，不改标题）；
4. Telegram 发图（``sendPhoto``，失败降级纯文本）＋站内通知中心一条事件。

配套细节：

* **字幕组**：整理后的文件名里没有字幕组（AB 会重命名成「剧名 SxxEyy」），
  所以订阅 ``torrent.filter`` / ``title.parsed`` 钩子，把
  ``bangumi_id → {官方剧名, 字幕组}`` 记在插件私有 KV 里，入库时取用；
* **幂等**：``torrent.organized`` 是「至少一次」投递（进程重启后已整理的种子会再发
  一次），所以按「番剧 + 季 + 集」去重，避免重复轰炸；
* **失败不熔断**：任何异常都只记日志（插件连续失败会被宿主停用）。
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from ab_sdk import Plugin, PluginContext, Verdict, hook, points, subscribe
from ab_sdk.events import SystemEvent, TorrentOrganized
from ab_sdk.ingest import BangumiInfo, Release, TorrentInfo

logger = logging.getLogger(__name__)

# httpx 默认把请求 URL 记进 INFO 日志，而 Telegram 的 bot token 就在 URL 里 ——
# 压到 WARNING，避免 token 出现在容器日志里。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

PLUGIN_ID = "emby-ingest-notify"
KV_EPISODES = "episodes"          # 已通知过的「番剧|季|集」
KV_BANGUMI = "bangumi_cache"      # bangumi_id → {title, group, season}


class Options(BaseModel):
    emby_url: str = Field("http://emby.local:8096", title="Emby 地址")
    emby_token: str = Field("", title="Emby API Key")
    telegram_token: str = Field("", title="Telegram Bot Token")
    telegram_chat_id: str = Field("", title="Telegram Chat ID")
    telegram_api: str = Field("https://api.telegram.org", title="Telegram API 基址（测试用）")
    wait_seconds: int = Field(180, ge=0, title="等待入库的最长时间（秒）")
    poll_interval: float = Field(10.0, ge=1, title="轮询间隔（秒）")
    include_overview: bool = Field(True, title="包含剧情简介")
    include_tags: bool = Field(True, title="包含类别（优先 bgm.tv 标签，失败用 Emby 分类）")
    overview_limit: int = Field(180, ge=40, title="简介截断长度")
    tag_limit: int = Field(5, ge=1, title="类别最多几个")
    path_from: str = Field("", title="路径前缀（AutoBangumi 视角，可选）")
    path_to: str = Field("", title="路径前缀（Emby 视角，可选）")
    min_episode: float = Field(1, title="只通知不小于这一集的（0 = 全部）")
    publish_inbox: bool = Field(
        False,
        title="同时写入站内通知中心",
        description=(
            "开启后通知会进 AB 的消息中心，但宿主也会把这条事件按纯文字推到外部渠道，"
            "等于 Telegram 里多一条（海报那条之外）。想要干净的“一张图 + 一段文案”就保持关闭。"
        ),
    )


# --------------------------------------------------------------------------- #
# 纯函数部分（好测）
# --------------------------------------------------------------------------- #

# 这些状态的种子已经没有可用文件了，没必要为它等待/推送
_DEAD_STATES = {"missingfiles", "error", "unknown", "allocating"}


def is_dead_state(state: str | None) -> bool:
    return str(state or "").strip().lower() in _DEAD_STATES


def normalize(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/")


_EPISODE_RE = re.compile(r"S(\d{1,2})E(\d{1,3}(?:\.\d+)?)", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"Season\s+(\d{1,2})", re.IGNORECASE)
_BARE_EP_RE = re.compile(r"(?:^|[\s\[\]_-])(\d{1,3}(?:\.5)?)(?:[\s\]\[_.v]|$)")


def parse_episode(path: str) -> tuple[int, float] | None:
    """从整理后的文件名解析（季, 集）。AB 的 pn/advance 命名是 ``… SxxEyy.ext``。

    取不到 ``SxxEyy`` 时退回到「Season N 目录 + 文件名里的裸集数」。
    """
    normalized = normalize(path)
    name = PurePosixPath(normalized).name
    match = _EPISODE_RE.search(name)
    if match:
        return int(match.group(1)), float(match.group(2))

    season = 1
    for part in normalized.split("/"):
        found = _SEASON_DIR_RE.search(part)
        if found:
            season = int(found.group(1))
            break
    bare = _BARE_EP_RE.search(PurePosixPath(name).stem)
    if bare:
        return season, float(bare.group(1))
    return None


def map_path(path: str, options: Options) -> str:
    """把 AB 视角的路径换成 Emby 视角（两边相同时原样返回）。"""
    src = normalize(options.path_from)
    if not src:
        return path
    dst = normalize(options.path_to)
    current = normalize(path)
    if current.lower() == src.lower():
        return dst
    if current.lower().startswith(src.lower() + "/"):
        return dst + current[len(src):]
    return path


def anime_folder(path: str) -> str:
    """从文件路径推出番剧目录（「Season N」的上一级；没有 Season 就用父目录）。"""
    parts = normalize(path).split("/")
    if len(parts) < 2:
        return normalize(path)
    parent = parts[-2]
    if parent.lower().startswith("season") and len(parts) >= 3:
        return "/".join(parts[:-2])
    return "/".join(parts[:-1])


def show_name_from_path(path: str) -> str:
    """番剧名兜底：取番剧目录名（AutoBangumi 用官方名建目录）。"""
    return normalize(anime_folder(path)).split("/")[-1]


def escape(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def compose_message(
    *,
    title: str,
    season: int | None,
    episode: float | None,
    episode_title: str | None = None,
    group: str | None = None,
    tags: list[str] | None = None,
    overview: str | None = None,
    emby_series: str | None = None,
    options: Options,
    for_telegram: bool = True,
) -> str:
    """拼通知正文。for_telegram=True 时转义 HTML（配 parse_mode=HTML）。"""
    def esc(value: str) -> str:
        return escape(value) if for_telegram else str(value)

    lines: list[str] = []
    lines.append(f"🎬 <b>{esc(title)}</b>" if for_telegram else f"🎬 {title}")

    ep = ""
    if season is not None:
        ep = f"S{int(season):02d}"
    if episode is not None:
        label = f"E{episode:g}" if float(episode) != int(episode) else f"E{int(episode):02d}"
        ep = f"{ep}{label}"
    if ep:
        suffix = f" · 「{esc(episode_title)}」" if episode_title else ""
        lines.append(f"📺 {ep}{suffix}")

    if group:
        lines.append(f"🏷 字幕组：{esc(group)}")
    if options.include_tags and tags:
        lines.append(f"✨ 类别：{esc(' / '.join(tags))}")
    if options.include_overview and overview:
        text = overview.strip().replace("\n", " ")
        if len(text) > options.overview_limit:
            text = text[: options.overview_limit].rstrip() + "…"
        lines.append(f"📝 剧情：{esc(text)}")

    # Emby 认成别的番时：标题仍用 AutoBangumi 的，另外给一行提示
    if emby_series and normalize(emby_series) != normalize(title):
        lines.append(f"⚠️ Emby 里识别为「{esc(emby_series)}」")
    return "\n".join(lines)


def dedup_key(title: str, season: int | None, episode: float | None, path: str) -> str:
    if episode is None:
        return normalize(path)
    return f"{normalize(title)}|{season}|{episode:g}"


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class EmbyClient:
    base: str
    token: str
    timeout: float = 20.0

    def _headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.token, "Accept": "application/json"}

    async def find_episode(self, path: str) -> dict[str, Any] | None:
        """按文件路径查这一集；Emby 已收录时返回该集对象，否则 None。

        注意：事件里的路径通常是正斜杠（``E:/好女孩/…``），而 Emby（Windows）的
        ``Path`` 过滤只认反斜杠 —— 所以两种形式各试一次，避免永远查不到。
        """
        candidates: list[str] = [path]
        windows_form = path.replace("/", "\\")
        if windows_form != path:
            candidates.append(windows_form)

        for candidate in candidates:
            url = self.base.rstrip("/") + "/emby/Items"
            params = {
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "Path,IndexNumber,Name,Overview,SeriesName,SeriesId,Genres,ParentIndexNumber",
                "Path": candidate,
                "Limit": "5",
            }
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, params=params, headers=self._headers())
                response.raise_for_status()
                data = response.json()
            for item in data.get("Items", []):
                if normalize(item.get("Path", "")) == normalize(path):
                    return item
        return None

    async def series_name(self, series_id: str) -> str | None:
        url = f"{self.base.rstrip('/')}/emby/Items/{series_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=self._headers())
            if response.status_code >= 400:
                return None
            return response.json().get("Name")

    async def download_image(self, item_id: str, max_bytes: int = 5_000_000) -> bytes | None:
        url = f"{self.base.rstrip('/')}/emby/Items/{item_id}/Images/Primary"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers={"X-Emby-Token": self.token})
            if response.status_code >= 400:
                return None
            data = response.content
            if not data or len(data) > max_bytes:
                return None
            return data


@dataclass(slots=True)
class TelegramClient:
    token: str
    chat_id: str
    api_base: str = "https://api.telegram.org"
    timeout: float = 30.0

    def _url(self, method: str) -> str:
        return f"{self.api_base.rstrip('/')}/bot{self.token}/{method}"

    async def send_photo(self, photo: bytes, caption: str) -> bool:
        url = self._url("sendPhoto")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("poster.jpg", photo, "image/jpeg")},
            )
        return response.status_code == 200 and response.json().get("ok", False)

    async def send_text(self, text: str) -> bool:
        url = self._url("sendMessage")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
            )
        return response.status_code == 200 and response.json().get("ok", False)


BGM_API_BASE = "https://api.bgm.tv"

# bgm 标签里混着放送季度之类的噪声（例如「2026年7月」），它们不算"类别"
_NOISE_TAG_RE = re.compile(r"^\d{4}年\d{1,2}月$")


def is_noise_tag(name: str) -> bool:
    return bool(_NOISE_TAG_RE.match(name.strip()))


async def fetch_bgm_tags(title: str, limit: int = 5, timeout: float = 10.0) -> list[str]:
    """bgm.tv 的风格标签（类别）。取不到就返回空列表，由调用方退回 Emby 分类。"""
    headers = {"User-Agent": f"{PLUGIN_ID}/0.1 (AutoBangumi plugin)"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            search = await client.post(
                f"{BGM_API_BASE}/v0/search/subjects",
                json={"keyword": title, "filter": {"type": [2]}},
            )
            if search.status_code >= 400:
                return []
            items = search.json().get("data", [])
            if not items:
                return []
            subject_id = items[0].get("id")
            if not subject_id:
                return []
            detail = await client.get(f"{BGM_API_BASE}/v0/subjects/{subject_id}")
            if detail.status_code >= 400:
                return []
            tags = detail.json().get("tags", [])
        names = [t["name"] for t in tags if t.get("name") and not is_noise_tag(t["name"])]
        return names[:limit]
    except Exception as e:  # 网络问题不该影响通知
        logger.debug("[%s] 取 bgm 标签失败：%s", PLUGIN_ID, e)
        return []


# --------------------------------------------------------------------------- #
# 站内事件
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class IngestEvent(SystemEvent):
    """入库通知（站内通知中心 + 外部渠道的系统事件）。"""

    kind: ClassVar[str] = f"{PLUGIN_ID}.ingested"
    title: str = ""
    season: int | None = None
    episode: float | None = None
    episode_title: str | None = None
    group: str | None = None
    tags: tuple[str, ...] = ()
    overview: str | None = None
    emby_series: str | None = None

    def describe(self) -> tuple[str, str]:
        head = f"{self.title}"
        if self.season is not None and self.episode is not None:
            head += f" S{int(self.season):02d}E{self.episode:g}"
        lines = [f"已入库：{head}"]
        if self.episode_title:
            lines.append(f"标题：{self.episode_title}")
        if self.group:
            lines.append(f"字幕组：{self.group}")
        if self.tags:
            lines.append(f"类别：{' / '.join(self.tags)}")
        if self.overview:
            lines.append(f"剧情：{self.overview}")
        if self.emby_series and normalize(self.emby_series) != normalize(self.title):
            lines.append(f"注意：Emby 里识别为「{self.emby_series}」")
        return ("入库通知", "\n".join(lines))


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #

@dataclass
class _State:
    episodes: set[str] = field(default_factory=set)
    bangumi: dict[str, dict[str, Any]] = field(default_factory=dict)
    bangumi_dirty: bool = False
    sent: int = 0
    failed: int = 0
    skipped: int = 0


class EmbyIngestNotify(Plugin[Options]):
    config_model = Options

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self.state = _State()
        self.emby = EmbyClient(self.config.emby_url, self.config.emby_token)
        self.telegram = TelegramClient(
            self.config.telegram_token,
            self.config.telegram_chat_id,
            api_base=self.config.telegram_api,
        )
        self._pending: set[asyncio.Task] = set()
        self._inflight: set[str] = set()
        self._sem: asyncio.Semaphore | None = None

    # ---------------- 生命周期 ----------------
    async def setup(self) -> None:
        self._sem = asyncio.Semaphore(8)
        self.state.episodes = set(await self.ctx.kv.get(KV_EPISODES, []) or [])
        self.state.bangumi = dict(await self.ctx.kv.get(KV_BANGUMI, {}) or {})
        logger.info(
            "[%s] 已加载：Emby %s ｜ Telegram %s ｜ 已通知过 %d 集",
            PLUGIN_ID,
            self.config.emby_url,
            "已配置" if self.config.telegram_token else "未配置（只发站内）",
            len(self.state.episodes),
        )
        # hash → state 的短缓存，避免每条事件都去查一遍下载器
        self._states_cache: tuple[float, dict[str, str]] | None = None
        self._dead_skipped = False

    def _cache_bangumi(self, bangumi: BangumiInfo | None) -> None:
        """在（同步）钩子里把规则信息记到内存，稍后异步落盘。"""
        if bangumi is None or getattr(bangumi, "id", None) is None:
            return
        entry = {
            "title": getattr(bangumi, "official_title", None),
            "group": getattr(bangumi, "group_name", None),
            "season": getattr(bangumi, "season", None),
        }
        if self.state.bangumi.get(str(bangumi.id)) != entry:
            self.state.bangumi[str(bangumi.id)] = entry
            self.state.bangumi_dirty = True

    async def _flush_bangumi(self) -> None:
        if self.state.bangumi_dirty:
            await self.ctx.kv.set(KV_BANGUMI, self.state.bangumi)
            self.state.bangumi_dirty = False

    # ---------------- 钩子：记录规则信息（不参与决策） ----------------
    @hook(points.TORRENT_FILTER, priority=80)
    def remember_on_filter(
        self, torrent: TorrentInfo, release: Release | None, bangumi: BangumiInfo
    ):
        # 这是决策型钩子，必须返回 Verdict；我们只顺手记下规则信息，一律放行
        self._cache_bangumi(bangumi)
        return Verdict.ok()

    @hook(points.TITLE_PARSED, priority=90)
    def remember_on_parsed(self, release: Release):
        self._cache_bangumi(getattr(release, "bangumi", None))
        return None

    # ---------------- 订阅：整理完成 → 等入库 → 通知 ----------------
    @subscribe(TorrentOrganized.kind, timeout=600)
    async def on_organized(self, event: TorrentOrganized) -> None:
        """订阅者队列是**按顺序**执行的，所以这里立刻返回，等待放到后台任务。

        否则：遇到一个永远等不到入库的旧种子，就会把队列堵住 wait_seconds，
        后面真正入库的事件全部排队（实测踩过：对照组收到 22 条，这边只处理了 1 条）。
        """
        logger.debug(
            "[%s] 收到整理事件：%d 个文件，首个=%s（bangumi_id=%s）",
            PLUGIN_ID,
            len(getattr(event, "files", ()) or ()),
            (getattr(event, "files", ()) or (None,))[0].path
            if getattr(event, "files", ())
            else None,
            getattr(event, "bangumi_id", None),
        )
        task = asyncio.create_task(self._handle_safe(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _handle_safe(self, event: TorrentOrganized) -> None:
        try:
            await self._handle(event)
        except Exception as e:  # 任何问题都不该影响宿主整理流程
            self.state.failed += 1
            logger.warning("[%s] 处理入库通知失败：%s", PLUGIN_ID, e)

    async def _load_downloader_states(self) -> dict[str, str]:
        """hash → 状态。查不到就返回空字典（那就照旧处理，不误跳）。"""
        now = time.time()
        cache = getattr(self, "_states_cache", None)   # 惰性：不依赖构造函数
        if cache and now - cache[0] < 60:
            return cache[1]
        try:
            from module.downloader import DownloadClient

            async with DownloadClient() as client:
                infos = await client.get_torrent_info(category=None, status_filter=None)
            states = {
                str(info.get("hash") or ""): str(info.get("state") or "")
                for info in infos or []
            }
            self._states_cache = (now, states)
            return states
        except Exception as e:
            logger.debug("[%s] 读取下载器状态失败（照旧处理）：%s", PLUGIN_ID, e)
            return {}

    async def _handle(self, event: TorrentOrganized) -> None:
        await self._flush_bangumi()
        # E 盘清空之类的情况：种子已经没了/文件已丢失 → 直接跳过，不空等 180 秒
        states = await self._load_downloader_states()
        torrent_hash = str(getattr(event, "torrent_hash", "") or "")
        if states and torrent_hash:
            state = states.get(torrent_hash)
            if state is not None and is_dead_state(state):
                if not getattr(self, "_dead_skipped", False):
                    self._dead_skipped = True
                    logger.info(
                        "[%s] 开始跳过已失效的种子（文件已丢失/种子已移除），"
                        "不再为它们空等或发通知；例如 %s（%s）",
                        PLUGIN_ID,
                        torrent_hash[:12] or "?",
                        state or "已移除",
                    )
                else:
                    logger.debug(
                        "[%s] 跳过 %s：种子已不在下载器或文件已丢失（%s）",
                        PLUGIN_ID,
                        torrent_hash[:12] or "?",
                        state or "已移除",
                    )
                return
        info = self.state.bangumi.get(str(event.bangumi_id)) if event.bangumi_id else None
        media = [f for f in event.files if getattr(f, "kind", "media") == "media"]
        if not media:
            return

        # 整批共享一个截止时间：否则 13 集的批次会按「每集等 180 秒」串行下去，
        # 最坏几十分钟，还会被订阅处理超时截断（剩下一半文件永远没机会处理）。
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, self.config.wait_seconds)
        await asyncio.gather(
            *[self._handle_file(f.path, info, deadline) for f in media],
            return_exceptions=True,
        )

    async def _handle_file(
        self, path: str, info: dict[str, Any] | None, deadline: float | None = None
    ) -> None:
        parsed = parse_episode(path)
        if parsed is None:
            return
        season, episode = parsed
        if episode < self.config.min_episode:
            return

        title = (info or {}).get("title") or show_name_from_path(path)
        group = (info or {}).get("group")
        key = dedup_key(title, season, episode, path)
        if key in self.state.episodes or key in self._inflight:
            self.state.skipped += 1
            logger.debug("[%s] %s 已经通知过（或正在处理），跳过", PLUGIN_ID, key)
            return

        # 先占位，避免并发的同一集重复发送
        self._inflight.add(key)
        try:
            if self._sem is not None:
                async with self._sem:
                    await self._process(path, title, season, episode, group, key, deadline)
            else:
                await self._process(path, title, season, episode, group, key, deadline)
        finally:
            self._inflight.discard(key)

    async def _process(
        self,
        path: str,
        title: str,
        season: int,
        episode: float,
        group: str | None,
        key: str,
        deadline: float | None,
    ) -> None:
        emby_path = map_path(path, self.config)
        item = await self._wait_for_emby(emby_path, deadline)
        if item is None:
            logger.info(
                "[%s] %s 等待期间未在 Emby 里查到，先不通知（下次整理会再试）",
                PLUGIN_ID,
                emby_path,
            )
            return

        await self._notify(title, season, episode, item, group)

        self.state.episodes.add(key)
        await self.ctx.kv.set(KV_EPISODES, sorted(self.state.episodes)[-500:])

    async def _wait_for_emby(
        self, emby_path: str, deadline: float | None = None
    ) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + max(0, self.config.wait_seconds)
        attempt = 0
        while True:
            attempt += 1
            try:
                item = await self.emby.find_episode(emby_path)
                if item:
                    logger.debug("[%s] 第 %d 次查询命中：%s", PLUGIN_ID, attempt, emby_path)
                    return item
            except Exception as e:
                logger.debug("[%s] 查询 Emby 失败（第 %d 次）：%s", PLUGIN_ID, attempt, e)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self.config.poll_interval, remaining))

    async def _notify(
        self,
        title: str,
        season: int,
        episode: float,
        item: dict[str, Any],
        group: str | None,
    ) -> None:
        episode_title = item.get("Name")
        overview = item.get("Overview")
        series_id = item.get("SeriesId")
        emby_series = item.get("SeriesName")
        if series_id and not emby_series:
            try:
                emby_series = await self.emby.series_name(series_id)
            except Exception:
                emby_series = None

        tags: list[str] = []
        if self.config.include_tags:
            tags = await fetch_bgm_tags(title, self.config.tag_limit)
            if not tags:
                tags = list(item.get("Genres") or [])[: self.config.tag_limit]

        # 站内通知中心（纯文本）。默认关闭：宿主会把它当系统事件再推一次外部渠道，
        # 那样 Telegram 里会多出一条纯文字（与海报那条重复）。
        if self.config.publish_inbox:
            self.ctx.bus.publish(
                IngestEvent(
                    title=title,
                    season=season,
                    episode=episode,
                    episode_title=episode_title,
                    group=group,
                    tags=tuple(tags),
                    overview=overview,
                    emby_series=emby_series,
                )
            )

        if not (self.config.telegram_token and self.config.telegram_chat_id):
            self.state.sent += 1
            logger.info("[%s] 已记入库通知（未配置 Telegram）：%s", PLUGIN_ID, title)
            return

        caption = compose_message(
            title=title,
            season=season,
            episode=episode,
            episode_title=episode_title,
            group=group,
            tags=tags,
            overview=overview,
            emby_series=emby_series,
            options=self.config,
        )
        photo = None
        if series_id:
            try:
                photo = await self.emby.download_image(series_id)
            except Exception as e:
                logger.debug("[%s] 取海报失败：%s", PLUGIN_ID, e)

        ok = False
        if photo:
            ok = await self.telegram.send_photo(photo, caption[:1024])
        if not ok:
            plain = compose_message(
                title=title,
                season=season,
                episode=episode,
                episode_title=episode_title,
                group=group,
                tags=tags,
                overview=overview,
                emby_series=emby_series,
                options=self.config,
                for_telegram=False,
            )
            ok = await self.telegram.send_text(f"{plain}\n\n（海报发送失败，已降级为文字）")

        if ok:
            self.state.sent += 1
            logger.info("[%s] 入库通知已发送：%s S%02dE%g", PLUGIN_ID, title, season, episode)
        else:
            self.state.failed += 1
            logger.warning("[%s] 入库通知发送失败：%s S%02dE%g", PLUGIN_ID, title, season, episode)
