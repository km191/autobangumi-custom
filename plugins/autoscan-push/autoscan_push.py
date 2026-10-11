"""本地插件：把整理完成的番剧目录推给 autoscan。

订阅宿主的 ``torrent.organized`` 事件（种子整理完成），把涉及的目录换算成
autoscan 容器里的路径，然后：

    POST {url}/triggers/manual?dir=<该部番的目录>

autoscan 会做优先级排队与合并，最后按路径通知 Emby（``POST /Library/Media/Updated``）
—— 只影响这一部番，不是整库刷新。

三个路径视角（默认值对应本机部署，可在设置页改）：

* AutoBangumi / 下载器：``E:\\好女孩``                  ← ``path_from``
* autoscan 容器：      ``/mnt/goodgirl``              ← ``path_to``
* Emby（Windows）：    再由 autoscan 的 target rewrite 换回去

实现要点：

* 路径按 ``path_from`` → ``path_to`` 做前缀替换（大小写不敏感、兼容 ``\\`` 与 ``/``）；
* 一次事件里的多个文件只推**去重后**的目录，避免同一部番被推很多次；
* 推送失败只记日志、不抛异常 —— 插件连续失败会被宿主熔断停用，
  而 autoscan 没开的时候我们只希望它安静地跳过。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx
from pydantic import BaseModel, Field

from ab_sdk import Plugin, PluginContext, subscribe
from ab_sdk.events import TorrentOrganized

logger = logging.getLogger(__name__)

PLUGIN_ID = "autoscan-push"
KV_PUSHED = "pushed"


class Options(BaseModel):
    url: str = Field(
        "http://host.docker.internal:3030",
        title="autoscan 地址",
        description=(
            "从 AutoBangumi 容器里能访问到的 autoscan 地址。"
            "与 AB 同机时用 host.docker.internal；同网络时可用容器名 autoscan:3030。"
        ),
    )
    path_from: str = Field(
        r"E:\Anime",
        title="媒体路径前缀（AB / 下载器视角）",
        description="整理结果里出现的路径前缀，例如 E:\\好女孩",
    )
    path_to: str = Field(
        "/mnt/anime",
        title="媒体路径前缀（autoscan 容器视角）",
        description="同一位置在 autoscan 容器里的挂载路径，例如 /mnt/goodgirl",
    )
    push_anime_root: bool = Field(
        True,
        title="推送整部番的目录",
        description=(
            "开启时推送「Season N」的上一级（整部番），让 autoscan 一次覆盖该番所有季；"
            "关闭时只推送文件所在目录。"
        ),
    )
    username: str = Field("", title="Basic 认证用户名（可选）")
    password: str = Field("", title="Basic 认证密码（可选）")
    cooldown_minutes: float = Field(
        10.0,
        ge=0,
        title="同一目录的合并窗口（分钟）",
        description=(
            "一次整理里每个文件都会单独发一条事件（22 个文件就是 22 条）。这个窗口把它们"
            "合并成一次推送：窗口内同一个目录只推一次。设 0 = 不合并（每个文件都推）。"
        ),
    )
    dedup_hours: float = Field(
        12.0,
        ge=0,
        title="同一目录最多多久重推一次（小时）",
        description=(
            "AutoBangumi 每分钟左右会把已整理过的种子重新发一遍事件，不去重就会反复通知 "
            "autoscan 重扫同一个目录。插件记住每个目录上次推的内容：没变化就不推；"
            "一直没变化时也最多这么久重推一次（兜底）。设 0 = 只在有变化时才推。"
        ),
    )
    timeout: float = Field(10.0, ge=1, title="请求超时（秒）")


# 这些状态的种子已经没有可用文件了，没必要为它等待/推送
_DEAD_STATES = {"missingfiles", "error", "unknown", "allocating"}


def is_dead_state(state: str | None) -> bool:
    return str(state or "").strip().lower() in _DEAD_STATES


def normalize(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/")


def to_autoscan_path(path: str, options: Options) -> str:
    """把 AB 视角的路径换成 autoscan 容器视角的路径。"""
    src = normalize(options.path_from)
    dst = normalize(options.path_to)
    current = normalize(path)
    if not src:
        return current
    if current.lower() == src.lower():
        return dst
    if current.lower().startswith(src.lower() + "/"):
        return dst + current[len(src) :]
    return current


def organize_folder(path: str, options: Options) -> str:
    """从文件路径推出要推送的目录。

    默认推「Season N」的上一级（整部番）；结构不符时退回文件所在目录。
    """
    folder = normalize(path)
    parent = str(PurePosixPath(folder).parent)
    if options.push_anime_root:
        name = PurePosixPath(parent).name
        if name.lower().startswith("season"):
            return str(PurePosixPath(parent).parent)
    return parent


def under_media_root(path: str, options: Options) -> bool:
    """路径是否落在配置的媒体根下（只有这些才值得推给 autoscan）。

    旧种子记录里可能混着别的盘/别的根（例如 E:/动漫/...），那些在 autoscan 里
    没有对应的库，推过去只会白跑一趟。
    """
    src = normalize(options.path_from)
    if not src:
        return True
    current = normalize(path).lower()
    return current == src.lower() or current.startswith(src.lower() + "/")


def folders_from_event(event: TorrentOrganized, options: Options) -> list[str]:
    """事件里所有正片涉及的目录（换算到 autoscan 视角、过滤、去重、保序）。"""
    seen: list[str] = []
    for file in event.files:
        if getattr(file, "kind", "media") != "media":
            continue
        if not under_media_root(file.path, options):
            continue
        mapped = to_autoscan_path(organize_folder(file.path, options), options)
        if mapped and mapped not in seen:
            seen.append(mapped)
    return seen


def grouped_files(event: TorrentOrganized, options: Options) -> list[tuple[str, list[str]]]:
    """(目录, 这条事件涉及的文件路径)。

    注意 AB 是**每个文件发一条事件**：一次整理 22 个文件就会有 22 条事件。
    所以判断"有没有变化"要看**累计**见过的文件集合，而不是单条事件的内容。
    """
    grouped: dict[str, list[str]] = {}
    for file in event.files:
        if getattr(file, "kind", "media") != "media":
            continue
        if not under_media_root(file.path, options):
            continue
        folder = to_autoscan_path(organize_folder(file.path, options), options)
        grouped.setdefault(folder, []).append(normalize(file.path))
    return [(folder, sorted(set(files))) for folder, files in grouped.items()]


@dataclass(slots=True)
class _Client:
    """薄封装，方便测试时替换。"""

    config: Options

    async def push(self, folder: str) -> bool:
        url = self.config.url.rstrip("/") + "/triggers/manual"
        auth = None
        if self.config.username:
            auth = (self.config.username, self.config.password)
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(url, params={"dir": folder}, auth=auth)
        if response.status_code >= 400:
            logger.warning(
                "[%s] autoscan 返回 HTTP %s：%s", PLUGIN_ID, response.status_code, folder
            )
            return False
        return True


class AutoscanPush(Plugin[Options]):
    config_model = Options

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self.client = _Client(self.config)
        self.pushed = 0
        self.failed = 0
        # 目录 → {"sig": 上次推送时的内容签名, "at": 时间戳}
        self._pushed: dict[str, dict] = {}
        self._skip_logged = False
        # hash → state 的短缓存，避免每条事件都去查一遍下载器
        self._states_cache: tuple[float, dict[str, str]] | None = None

    async def setup(self) -> None:
        try:
            self._pushed = dict(await self.ctx.kv.get(KV_PUSHED, {}) or {})
        except Exception as e:
            logger.debug("[%s] 读取已推记录失败：%s", PLUGIN_ID, e)
            self._pushed = {}
        logger.info(
            "[%s] 已加载：推送到 %s（%s → %s）",
            PLUGIN_ID,
            self.config.url,
            self.config.path_from,
            self.config.path_to,
        )

    @subscribe(TorrentOrganized.kind, timeout=30)
    async def on_organized(self, event: TorrentOrganized) -> None:
        states = await self._load_downloader_states()
        torrent_hash = str(getattr(event, "torrent_hash", "") or "")
        if states and torrent_hash:
            state = states.get(torrent_hash)
            if state is not None and is_dead_state(state):
                logger.debug(
                    "[%s] 跳过 %s：种子已不在下载器或文件已丢失（%s）",
                    PLUGIN_ID,
                    torrent_hash[:12] or "?",
                    state or "已移除",
                )
                return
        entries = grouped_files(event, self.config)
        if not entries:
            return
        now = time.time()
        cooldown = max(0.0, self.config.cooldown_minutes) * 60
        window = max(0.0, self.config.dedup_hours) * 3600
        pushed = 0
        skipped = 0
        dirty = False
        for folder, files in entries:
            record = self._pushed.get(folder) or {}
            known = set(record.get("files") or [])
            last = float(record.get("at") or 0)
            # 1) 同一次整理的文件级事件：合并窗口内只推一次。
            #    注意这里也要把文件合并进累计集合 —— 否则这些文件下次会被当成"新文件"再推一遍。
            if last and cooldown > 0 and now - last < cooldown:
                merged = sorted(known | set(files))
                if merged != sorted(known):
                    self._pushed[folder] = {"files": merged, "at": last}
                    dirty = True
                skipped += 1
                continue
            # 2) 没有新增文件：窗口内不重复惊动 autoscan（AB 每分钟都会重发一遍）
            if last and not [f for f in files if f not in known] and (
                window <= 0 or now - last < window
            ):
                skipped += 1
                continue
            try:
                if await self.client.push(folder):
                    self.pushed += 1
                    pushed += 1
                    self._pushed[folder] = {
                        "files": sorted(known | set(files)),
                        "at": now,
                    }
                    dirty = True
                    logger.info("[%s] 已推送 %s，等待 autoscan 通知媒体库扫描", PLUGIN_ID, folder)
                else:
                    self.failed += 1
            except Exception as e:  # autoscan 没开时安静跳过，不触发熔断
                self.failed += 1
                logger.warning("[%s] 推送 %s 失败（autoscan 没开？）：%s", PLUGIN_ID, folder, e)
        if pushed or dirty:
            await self._save_pushed()
        if skipped and not self._skip_logged:
            self._skip_logged = True
            logger.info(
                "[%s] 已按内容去重：%d 个目录没有变化，本次不重复通知 autoscan "
                "（合并窗口 %.0f 分钟；同一目录最多 %.1f 小时兜底重推一次）",
                PLUGIN_ID,
                skipped,
                self.config.cooldown_minutes,
                self.config.dedup_hours,
            )

    async def _load_downloader_states(self) -> dict[str, str]:
        """hash → 状态；查不到就返回空字典（照旧推送）。"""
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
            logger.debug("[%s] 读取下载器状态失败（照旧推送）：%s", PLUGIN_ID, e)
            return {}

    async def _save_pushed(self) -> None:
        try:
            recent = sorted(
                self._pushed.items(),
                key=lambda item: float(item[1].get("at") or 0),
                reverse=True,
            )[:200]
            self._pushed = dict(recent)
            await self.ctx.kv.set(KV_PUSHED, self._pushed)
        except Exception as e:
            logger.debug("[%s] 保存已推记录失败：%s", PLUGIN_ID, e)

    async def push_many(self, folders: list[str]) -> int:
        """给测试/手工补扫用：直接推一批目录，返回成功数。"""
        ok = 0
        for folder in folders:
            try:
                if await self.client.push(folder):
                    ok += 1
            except Exception as e:
                logger.warning("[%s] 推送 %s 失败：%s", PLUGIN_ID, folder, e)
        return ok
