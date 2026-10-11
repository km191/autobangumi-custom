"""本地插件：质量择优与洗版（quality-wash）。

按「字幕组质量层级 → 语言档 → 分辨率 → 位深」给每个发布打分，然后：

1. ``torrent.filter``：同一集的候选只放行最优的那一个 —— 更差的直接拒绝，
   更好的到达时放行（然后用 2 / 3 把旧的清掉）；
2. ``conflict_policy``：重命名时目标路径已被更差的版本占用 → 返回 ``replace``，
   **删除由宿主执行**，宿主只在「唯一占用者 + 双方单文件 + 身份完整」时才真删；
3. ``scheduled_task``：定时巡检同一集里容器格式不同的重复（``.mp4`` ↔ ``.mkv``
   这种重命名钩子覆盖不到的情况），删掉更差的那份 —— **默认 dry-run，只报告**；
4. ``title.parsed``：把 ANi 的发布强制标成繁中。

安全护栏（都是踩过坑之后加的）：

* 只删「更好的那份**已经下完**」的情况；
* 文件名被多个种子引用的，一律不删（曾经把共用的文件删掉过）；
* 同一个「目录 + 季 + 集」桶里只留一个，且必须严格更优才删；
* 首选卡住（没下完 + 群里一个做种者都没有 + 超过 ``stuck_hours``）时，
   清掉卡住的那份，让次优能进来。
"""

from __future__ import annotations

import collections
import logging
import httpx
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ab_sdk import Plugin, PluginContext, Verdict, hook, points, provider
from ab_sdk.events import SystemEvent
from ab_sdk.ingest import BangumiInfo, Release, TorrentInfo
from ab_sdk.rename import ConflictDecision, ConflictRequest
from ab_sdk.tasks import ScheduledTask

logger = logging.getLogger(__name__)

PLUGIN_ID = "quality-wash"

# 种子里提取不到字幕组时的兜底名（与 score_release 共用同一个常量）
UNKNOWN_GROUP = "未知组"

# 优质组：默认只有这两个，用户可以自由加、自由排序
DEFAULT_GROUPS = ["喵萌奶茶屋", "绿茶字幕组"]
# 垫底组：顺序即优先级（LoliHouse > ANi > Nix-Raws）
DEFAULT_BOTTOM_GROUPS = ["LoliHouse", "ANi", "Nix-Raws"]

# 档位换算成"越大越好"的优先级时的基准值
_TOP_BASE = 1000
_UNLISTED = 500
_BOTTOM_BASE = 100

# 同一档的常见写法；合并发布（Nekomoe kissaten&LoliHouse）取命中的最高档
GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "喵萌奶茶屋": ("喵萌奶茶屋", "喵萌", "nekomoe", "奶茶屋"),
    "绿茶字幕组": ("绿茶字幕组", "绿茶", "studio greentea", "greentea"),
    "LoliHouse": ("lolihouse",),
    "ANi": ("ani",),
    "Nix-Raws": ("nix-raws", "nix raws", "nixraws"),
}

LANGUAGE_ORDER = ["简繁", "简", "繁", "日", "未知"]

_MEDIA_SUFFIXES = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".flv", ".webm", ".mov")

_BOTH_TOKENS = (
    "简繁", "繁简", "双语", "雙語", "简繁内封", "简繁日内封",
    "chi_jpn", "assx2", "ass×2", "ass x2", "字幕x2",
)
_SIMPLIFIED_TOKENS = ("chs", "gb", "sc", "简体", "简日", "简体内嵌", "简体内封")
_TRADITIONAL_TOKENS = ("cht", "big5", "tc", "繁体", "繁日", "繁体内嵌", "繁体内封")
_JAPANESE_TOKENS = ("jpn", "日语", "日語", "日内嵌", "日文")

_SXXEXX = re.compile(r"\bS(\d{1,2})E(\d{1,3}(?:\.5)?)(?:v\d{1,2})?\b", re.IGNORECASE)
_BRACKET_EP = re.compile(r"\[(\d{1,3}(?:\.5)?)(?:v\d)?\]")
_DASH_EP = re.compile(r"-\s*(\d{1,3}(?:\.5)?)(?:v\d)?\s*[\[\(]")
_CN_EP = re.compile(r"第\s*(\d{1,3}(?:\.5)?)\s*[集话話]")
_PLAIN_E = re.compile(r"\bE(?:P)?(\d{1,3}(?:\.5)?)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 配置（宿主据此自动生成设置页表单）
# --------------------------------------------------------------------------- #
class Options(BaseModel):
    enabled: bool = Field(True, title="启用择优与洗版")

    groups: list[str] = Field(
        default_factory=lambda: list(DEFAULT_GROUPS),
        title="优质组顺序（高 → 低）",
        description=(
            "你最信任、画质最好的字幕组，按质量从高到低排。默认：喵萌奶茶屋 > "
            "绿茶字幕组。可以自由加（SweetSub、Sakurato…）和调整顺序。"
        ),
    )
    bottom_groups: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BOTTOM_GROUPS),
        title="垫底组顺序（高 → 低）",
        description=(
            "固定排在最后的字幕组，顺序即优先级。默认：LoliHouse > ANi > "
            "Nix-Raws。没写进这两个清单的其它组，排在优质组之后、垫底组之前。"
        ),
    )
    language_order: list[str] = Field(
        default_factory=lambda: list(LANGUAGE_ORDER),
        title="语言档位顺序（高 → 低）",
        description="可选值：简繁 / 简 / 繁 / 日 / 未知。",
    )
    ani_always_traditional: bool = Field(
        True,
        title="ANi 永远算繁中",
        description="ANi 的发布不管标了什么，一律按繁中参与比较。",
    )
    allow_resolution_downgrade: bool = Field(
        False,
        title="允许用更低分辨率替换",
        description="默认不允许：1080p 不会被 720p 顶掉。",
    )
    replace_on_conflict: bool = Field(
        True,
        title="重命名冲突时替换更差的版本",
        description="目标文件名被更差的版本占用时，删除旧的并让更好的落位。",
    )
    hold_new_subscriptions: bool = Field(
        False,
        title="新订阅先暂停，确认后再下载",
        description=(
            "开启后，新录入的 RSS 规则不会立刻按默认字幕组顺序下载：它的种子会被拦在准入处，"
            "直到你在「编辑规则 → 下载顺序」里保存顺序，或点该标签页里的「立即放行」。"
            "开关打开时已存在的规则一律视为已确认，不受影响。"
        ),
    )
    known_library_guard: bool = Field(
        True,
        title="库里已有就不再重复下载",
        description=(
            "读一次 Emby 里已经收录的剧集，把它们记成「已知最优」。"
            "之后重新建规则、或 RSS 又把老集数推一遍时，只要新来的并不更好就拒绝，"
            "避免「删了规则就要重头下」。更好的版本（更高的字幕组档位/分辨率/位深/容器）"
            "仍然放行，交给洗版处理。"
        ),
    )
    emby_url: str = Field("", title="Emby 地址（读库中已有）", description="例如 http://emby.local:8096；留空则不启用该保护。")
    emby_token: str = Field("", title="Emby API Key")
    library_scan_hours: float = Field(
        6.0, ge=0.25, title="多久重新读一次媒体库（小时）"
    )
    skip_unknown_group: bool = Field(
        True,
        title="未知组的种子不参与洗版",
        description=(
            "AutoBangumi 会把 qB 里的种子名改写成「剧名 SxxEyy」，改完就没有字幕组了。"
            "这种「未知组」往往就是同一次下载，把它当成可洗的另一份会产生"
            "「未知组 → X」这种没有意义的洗版通知。开启后它们不参与巡检。"
        ),
    )
    sweep_enabled: bool = Field(
        True, title="定时清理重复集（容器格式不同等）"
    )
    sweep_interval: int = Field(900, ge=60, title="巡检间隔（秒）")
    sweep_dry_run: bool = Field(
        True,
        title="巡检只报告不删除（dry-run）",
        description=(
            "默认开启：只把「会删掉什么」写进日志与通知；确认无误后再关掉，"
            "巡检才会真的删。"
        ),
    )
    stuck_hours: float = Field(
        6.0,
        ge=0,
        title="首选卡住多少小时后清掉它",
        description=(
            "首选（更优的那份）没下完、群里一个做种者都没有、且超过这个时间，"
            "就把它清掉，让次优能进来。填 0 关闭。"
        ),
    )
    notify: bool = Field(True, title="洗版后发通知")


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Score:
    """打分结果。**所有字段都是"越大越好"**（档位已换算成优先级）。

    ``group_priority`` / ``language_priority`` 越大越优 —— 注意不要直接塞列表
    下标（0 是最好的一档，用下标比大小会把最差的排到最前，踩过）。
    """

    group_priority: int
    group_name: str
    language_priority: int
    language: str
    resolution: int
    bit_depth: int
    revision: int
    container: str

    def key(self) -> tuple[int, ...]:
        # 顺序即优先级：字幕组 → 语言 → 分辨率 → 位深(10bit>8bit) → 容器(mkv>mp4) → 版本
        return (
            self.group_priority,
            self.language_priority,
            self.resolution,
            self.bit_depth,
            container_rank(self.container),
            self.revision,
        )


def _alias_pattern(token: str) -> re.Pattern[str]:
    """把别名编译成带边界的正则，避免 'ani' 命中 'Anime' 这类误判。"""
    escaped = re.escape(token.lower())
    if re.search(r"[\u4e00-\u9fff]", token):
        return re.compile(escaped)
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")


def _hit(
    text: str | None, names: list[str], aliases: dict[str, tuple[str, ...]] | None = None
) -> tuple[int, str] | None:
    """在 ``names`` 里找出命中的**最高**档（下标最小者）；没命中返回 None。

    合并发布（``Nekomoe kissaten&LoliHouse``）会同时命中两档，取更高的一档。
    """
    aliases = aliases if aliases is not None else GROUP_ALIASES
    haystack = (text or "").lower()
    if not haystack.strip():
        return None
    best: tuple[int, str] | None = None
    for index, name in enumerate(names):
        tokens = [name, *aliases.get(name, ())]
        if any(_alias_pattern(token).search(haystack) for token in tokens if token):
            if best is None or index < best[0]:
                best = (index, name)
    return best


def group_priority(
    text: str | None,
    groups: list[str],
    bottom_groups: list[str],
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> tuple[int, str]:
    """返回 ``(越大越好的组优先级, 命中的档名)``。

    * 优质组清单：``1000`` 起往下（越靠前越高）；
    * 垫底组清单：``100`` 起往下（默认 LoliHouse > ANi > Nix-Raws）；
    * 两边都没命中：``500`` —— 排在优质组之后、垫底组之前。
    """
    top = _hit(text, groups, aliases)
    if top is not None:
        return _TOP_BASE - top[0], top[1]
    bottom = _hit(text, bottom_groups, aliases)
    if bottom is not None:
        return _BOTTOM_BASE - bottom[0], bottom[1]
    return _UNLISTED, ""


def is_ani_group(text: str | None) -> bool:
    """是不是 ANi 的发布（用来强制按繁中处理）。"""
    return _hit(text, ["ANi"]) is not None


def detect_language(release: Release | None, raw: str, *, force_traditional: bool = False) -> str:
    if force_traditional:
        return "繁"
    subtitle = (getattr(release, "subtitle", None) or "") if release is not None else ""
    text = f"{subtitle} {raw}".lower()
    if any(token in text for token in _BOTH_TOKENS):
        return "简繁"
    if any(token in text for token in _SIMPLIFIED_TOKENS):
        return "简"
    if any(token in text for token in _TRADITIONAL_TOKENS):
        return "繁"
    if any(token in text for token in _JAPANESE_TOKENS):
        return "日"
    return "未知"


def detect_resolution(release: Release | None, raw: str) -> int:
    hint = (getattr(release, "resolution", None) or "") if release is not None else ""
    match = re.search(r"(\d{3,4})\s*[pP]", f"{hint} {raw}")
    if match:
        return int(match.group(1))
    if re.search(r"\b4k\b|2160", f"{hint} {raw}", re.IGNORECASE):
        return 2160
    if re.search(r"\b(720|1080)\b", f"{hint} {raw}"):
        return int(re.search(r"\b(720|1080)\b", f"{hint} {raw}").group(1))
    return 0


def detect_bit_depth(release: Release | None, raw: str) -> int:
    codecs = " ".join(getattr(release, "codecs", ()) or ()) if release is not None else ""
    text = f"{codecs} {raw}"
    match = re.search(r"(\d{1,2})\s*bit", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"\bhevc\b|\bh\.?265\b|\bx265\b|\bav1\b", text, re.IGNORECASE):
        return 10  # 现代 HEVC / AV1 发布基本都是 10bit
    return 8


def detect_container(raw: str) -> str:
    lowered = raw.lower()
    for suffix in _MEDIA_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return ""


# 容器偏好：mkv 优于 mp4（明确要求）；其它已知容器居中；未知与居中同档。
# 注意 key() 里位深排在容器前面 —— 即"10bit 的 mp4 优于 8bit 的 mkv"，
# 想让容器优先就把 key() 里这两项换个位置。
_CONTAINER_RANK = {
    ".mkv": 2,
    ".mp4": 0,
    ".m4v": 0,
    ".webm": 1,
    ".avi": 1,
    ".ts": 1,
    ".mov": 1,
}


# v 前面不能是字母（避免 AV1/WebRip 之类误判），后面不能紧跟字母数字（避免 V2Sub）
_REVISION_RE = re.compile(r"(?<![A-Za-z])v(\d{1,2})(?![\w])", re.IGNORECASE)


def detect_revision(name: str) -> int:
    """从发布名里取修订号（v2 / V2 / (v2)）。取不到算 1。

    不能误伤：``V2Sub`` 这种组名不匹配（v 后面不是独立数字），
    ``HEVC``／``AVC``／``x264`` 也不会被当成版本号。
    """
    best = 0
    for match in _REVISION_RE.finditer(str(name or "")):
        try:
            best = max(best, int(match.group(1)))
        except ValueError:
            continue
    return best if best > 0 else 1


def container_rank(container: str) -> int:
    """容器换算成"越大越好"的档位。"""
    return _CONTAINER_RANK.get(str(container or "").lower(), 1)


def parse_episode(raw: str) -> tuple[int, float] | None:
    """从发布名里猜 ``(季, 集)``；猜不出来返回 None（保守：不参与择优/清理）。"""
    match = _SXXEXX.search(raw)
    if match:
        return int(match.group(1)), float(match.group(2))
    for pattern in (_BRACKET_EP, _DASH_EP, _CN_EP, _PLAIN_E):
        match = pattern.search(raw)
        if match:
            return 1, float(match.group(1))
    return None


def score_release(
    raw: str, release: Release | None, options: Options, *, revision: int = 1
) -> Score:
    group_raw = ((getattr(release, "group", None) if release is not None else None) or "").strip()
    top_groups = [g for g in options.groups if g and g.strip()]
    bottom_groups = [g for g in options.bottom_groups if g and g.strip()]
    group_value, tier = group_priority(group_raw or raw, top_groups, bottom_groups)
    is_ani = tier == "ANi" or is_ani_group(f"{group_raw} {raw}")
    language = detect_language(
        release, raw, force_traditional=is_ani and options.ani_always_traditional
    )
    language_order = [lang for lang in options.language_order if lang] or list(LANGUAGE_ORDER)
    language_index = (
        language_order.index(language) if language in language_order else len(language_order)
    )
    # 语言也换算成"越大越好"
    return Score(
        group_priority=group_value,
        group_name=tier or (group_raw or UNKNOWN_GROUP),
        language_priority=len(language_order) - language_index,
        language=language,
        resolution=detect_resolution(release, raw),
        bit_depth=detect_bit_depth(release, raw),
        revision=int(revision or 1),
        container=detect_container(raw),
    )


# --------------------------------------------------------------------------- #
# 库中已有保护（episode-guard）：让重建规则后不会重头下
# --------------------------------------------------------------------------- #

_EMBY_EPISODE_RE = re.compile(r"S(\d{1,2})E(\d{1,3}(?:\.\d+)?)", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"(?i)season\s*(\d+)")


def library_bucket(path: str) -> tuple[str, float] | None:
    """把 Emby 里的文件路径换算成与 ``prefer_best`` 相同的桶键。

    ``E:/好女孩/<番剧>/Season 1/<...> S01E05.mkv`` → ``("<番剧>|1", 5.0)``
    """
    normalized = str(path or "").replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    if len(parts) < 2:
        return None
    name = parts[-1]
    season = None
    anime = None
    for index in range(len(parts) - 1, -1, -1):
        found = _SEASON_DIR_RE.fullmatch(parts[index])
        if found and index >= 1:
            season = int(found.group(1))
            anime = parts[index - 1]
            break
    if season is None or not anime:
        return None
    episode_match = _EMBY_EPISODE_RE.search(name)
    if episode_match is None:
        return None
    if int(episode_match.group(1)) != season:
        return None
    return f"{anime}|{season}", float(episode_match.group(2))


def resolution_from_height(height: int | float | None) -> int:
    value = int(height or 0)
    if value >= 2000:
        return 2160
    if value >= 1000:
        return 1080
    if value >= 700:
        return 720
    if value >= 500:
        return 576
    return 480


def outranks(old: Score, new: Score, *, allow_resolution_downgrade: bool = False) -> bool:
    """新的这份是否**严格**优于旧的。"""
    if not allow_resolution_downgrade and new.resolution < old.resolution:
        return False
    return new.key() > old.key()


def outranks_library(old: Score, new: Score, *, allow_resolution_downgrade: bool = False) -> bool:
    """库里那份（Emby 已收录）是否被新的超过。

    库里那份是从 Emby 的媒体信息反推的：字幕组未知（按未列出档）、语言无从得知，
    所以只比 **组档位 / 分辨率 / 位深 / 容器 / 版本**，语言不参与 —— 否则一个
    没标语言的 10bit 好版本会被"基线语言最高"莫名挡掉。
    """
    if not allow_resolution_downgrade and new.resolution < old.resolution:
        return False
    old_key = (
        old.group_priority, old.resolution, old.bit_depth,
        container_rank(old.container), old.revision,
    )
    new_key = (
        new.group_priority, new.resolution, new.bit_depth,
        container_rank(new.container), new.revision,
    )
    return new_key > old_key


def outranks_with_rank(
    old: Score,
    new: Score,
    old_rank: int,
    new_rank: int,
    *,
    allow_resolution_downgrade: bool = False,
) -> bool:
    """质量优先、同档位时按用户设定的下载顺序（rank 小 = 靠前 = 先取）。"""
    if not allow_resolution_downgrade and new.resolution < old.resolution:
        return False
    if new.key() != old.key():
        return new.key() > old.key()
    return new_rank < old_rank


def anime_key(rule: Any) -> str:
    """同一部番的标识：优先用保存目录里的番剧文件夹，退回官方名；带上季度。"""
    save_path = str(getattr(rule, "save_path", "") or "")
    parts = [p for p in save_path.replace("\\", "/").split("/") if p]
    season = getattr(rule, "season", 1)
    if len(parts) >= 2 and re.fullmatch(r"(?i)season\s*\d+", parts[-1]):
        return f"{parts[-2]}|{season}"
    return f"{str(getattr(rule, 'official_title', '') or '')}|{season}"


def anime_key_from_bangumi(bangumi: Any) -> str:
    """``BangumiInfo`` 版本（SDK 视图，字段少一点）。"""
    save_path = str(getattr(bangumi, "save_path", "") or "")
    parts = [p for p in save_path.replace("\\", "/").split("/") if p]
    season = getattr(bangumi, "season", 1)
    if len(parts) >= 2 and re.fullmatch(r"(?i)season\s*\d+", parts[-1]):
        return f"{parts[-2]}|{season}"
    return f"{str(getattr(bangumi, 'official_title', '') or '')}|{season}"


def rule_tags(rule: Any) -> str:
    """规则卡片上的小标签：分辨率 · 字幕 · 来源。"""
    bits = [
        str(getattr(rule, "dpi", "") or ""),
        str(getattr(rule, "subtitle", "") or ""),
        str(getattr(rule, "source", "") or ""),
    ]
    return " · ".join(b for b in bits if b)


def show_name(save_path: str) -> str:
    """从 ``.../番剧名/Season 1`` 里取出番剧名（拿不到就返回原路径）。"""
    parts = [p for p in str(save_path or "").replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    if re.fullmatch(r"(?i)season\s*\d+", parts[-1]) and len(parts) >= 2:
        return parts[-2]
    return parts[-1]


_GROUP_TAG = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*")
_TECH_TAG = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_EPISODE_TAIL = re.compile(
    r"(?:\s*-\s*\d{1,3}(?:\.5)?(?:v\d)?\b.*$)|(?:\bS\d{1,2}E\d{1,3}\b.*$)"
    r"|(?:第\s*\d{1,3}\s*[集话話].*$)|(?:[\[\(]\d{1,3}(?:\.5)?(?:v\d)?[\]\)].*$)",
    re.IGNORECASE,
)


def title_key(raw: str) -> str:
    """从发布名里提取"作品名"的归一化键。

    去掉开头的字幕组标签、结尾的集数与后面的技术标签，再去掉标点与空白。
    用途：给**没有 ab: 标签**的种子一个粗略的作品身份，避免把不同番剧当成
    同一集的重复互相清理。跨组洗版靠的是番剧规则（``ab:<id>`` → 官方名），
    不是这个键。
    """
    text = str(raw or "")
    text = _TECH_TAG.sub(" ", text)          # 所有 [xxx] (xxx) 都去掉
    text = _GROUP_TAG.sub(" ", text)
    text = _EPISODE_TAIL.sub(" ", text)
    text = re.sub(r"[\s\.\-_/·・:：,，。!！?？~～&＆]+", "", text)
    return text.lower()


def parse_ab_tag(tags: Any) -> int | None:
    """从 ``ab:12`` 标签里取出番剧 id。"""
    for tag in str(tags or "").split(","):
        tag = tag.strip()
        if tag.startswith("ab:"):
            try:
                return int(tag[3:])
            except ValueError:
                continue
    return None


# --------------------------------------------------------------------------- #
# 通知事件（kind 必须以插件 id 开头）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WashEvent(SystemEvent):
    kind: ClassVar[str] = f"{PLUGIN_ID}.wash"
    severity: ClassVar[str] = "info"
    once: ClassVar[bool] = False

    title_text: str = ""
    episode: str = ""
    detail: str = ""
    action: str = ""

    def describe(self) -> tuple[str, str]:
        # 排版与「入库通知」保持一致：标题 + 带 emoji 的分行字段
        # （纯文本，不要 HTML 标签 —— 事件走宿主的模板渲染）
        lines = [f"🎬 {self.title_text}"]
        if self.episode:
            lines.append(f"📺 {self.episode}")
        if self.detail:
            lines.append(f"🔁 替换：{self.detail}")
        if self.action:
            lines.append(f"🧹 处理：{self.action}")
        return ("洗版完成", "\n".join(lines))


@dataclass(frozen=True, slots=True)
class SubscriptionHeld(SystemEvent):
    """新订阅被暂停、等待确认。"""

    kind: ClassVar[str] = f"{PLUGIN_ID}.subscription_held"
    official_title: str = ""
    rule_id: int = 0

    def describe(self) -> tuple[str, str]:
        return (
            "新订阅待确认",
            f"「{self.official_title}」已暂停，还没有开始下载。\n"
            "请在「编辑规则 → 下载顺序」里排好字幕组顺序（保存即放行），"
            "或在该标签页点「立即放行」。",
        )


def preview_event(plan: list[dict]) -> WashEvent:
    """把巡检计划变成通知事件，**第一行就是动漫名**（别只给个数字）。

    例：
        🎬 至高之力、转生就是剑
        📺 巡检预览（dry-run）· 2 处重复
        🔁 替换：至高之力 ANi → LoliHouse；转生就是剑 LoliHouse → 喵萌奶茶屋
        🧹 处理：未删除任何文件
    """
    titles: list[str] = []
    for item in plan:
        name = str(item.get("title") or "").strip()
        if name and name not in titles:
            titles.append(name)
    shown = "、".join(titles[:4])
    if len(titles) > 4:
        shown += f" 等 {len(titles)} 部"

    detail_parts: list[str] = []
    for item in plan[:4]:
        name = str(item.get("title") or "?")
        part = f"{name} {item['loser_group']} → {item['winner_group']}"
        episode = str(item.get("episode") or "")
        if episode:
            part += f"（{episode}）"
        detail_parts.append(part)
    detail = "；".join(detail_parts)
    if len(plan) > 4:
        detail += f"；……等 {len(plan)} 处"

    return WashEvent(
        title_text=shown or f"{len(plan)} 处重复",
        episode=f"巡检预览（dry-run）· {len(plan)} 处重复",
        detail=detail,
        action="未删除任何文件",
    )


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class OrderPayload(BaseModel):
    """``PUT /order`` 的请求体。"""

    bangumi_id: int
    rule_ids: list[int] = Field(default_factory=list)


class QualityWash(Plugin[Options]):
    config_model = Options

    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        # 同一集当前的最优候选：bucket -> (Score, 种子名, 时间戳)
        self._best: dict[str, tuple[Score, str, float, int]] = {}
        # 库中已有保护：bucket → (库里那份的打分, 说明, 记录时间)
        self._library: dict[str, tuple[Score, str, float]] = {}
        self._library_scanned_at: float = 0.0
        # 新订阅先暂停：已确认（放行）的规则 id 与开关状态
        self._confirmed: set[int] = set()
        self._hold_armed: bool = False
        self._held_notified: set[int] = set()
        # 用户在同一部番里设定的下载顺序：anime_key -> [规则 id, ...]（靠前 = 先取）
        self._orders: dict[str, list[int]] = {}
        # 规则 id -> 顺序位置（由 _orders 派生，供同步钩子快速查询）
        self._rank_by_rule: dict[int, int] = {}
        # 读番剧列表失败只提醒一次，避免刷日志
        self._titles_warned = False

    # ---------------- 生命周期 ----------------
    async def setup(self) -> None:
        await self._load_state()
        await self._load_orders()
        logger.info(
            "[%s] 已加载：优质组 %s ｜ 垫底组 %s ｜ 巡检 %s（%s）｜ 冲突替换 %s ｜ 洗版通知 %s",
            PLUGIN_ID,
            " > ".join(self.config.groups) or "（空）",
            " > ".join(self.config.bottom_groups) or "（空）",
            "开启" if self.config.sweep_enabled else "关闭",
            "dry-run" if self.config.sweep_dry_run else "实删",
            "开启" if self.config.replace_on_conflict else "关闭",
            "开启" if self.config.notify else "关闭",
        )
        await self._load_library()
        await self._load_hold_state()

    async def teardown(self) -> None:
        await self._save_state()

    async def _load_state(self) -> None:
        try:
            raw = await self.ctx.kv.get("best", {}) or {}
            for bucket, item in raw.items():
                score, name, stamp = item[0], item[1], item[2]
                rank = int(item[3]) if len(item) > 3 else 10_000
                self._best[bucket] = (Score(**score), name, float(stamp), rank)
        except Exception as e:
            logger.warning("[%s] 读取状态失败，忽略：%s", PLUGIN_ID, e)
            self._best = {}

    async def _save_state(self) -> None:
        try:
            await self.ctx.kv.set(
                "best",
                {
                    bucket: [asdict(score), name, stamp, rank]
                    for bucket, (score, name, stamp, rank) in self._best.items()
                },
            )
        except Exception as e:
            logger.debug("[%s] 保存状态失败：%s", PLUGIN_ID, e)


    async def _load_library(self) -> None:
        """读回上次从 Emby 抓到的「库里已有」基线。"""
        try:
            raw = await self.ctx.kv.get("library", {}) or {}
            for bucket, item in raw.items():
                self._library[bucket] = (Score(**item[0]), str(item[1]), float(item[2]))
            self._library_scanned_at = float(await self.ctx.kv.get("library_scanned_at", 0) or 0)
            if self._library:
                age = (time.time() - self._library_scanned_at) / 3600 if self._library_scanned_at else -1
                logger.info("[%s] 已载入库中已有基线：%d 集（%.1f 小时前抓的）", PLUGIN_ID, len(self._library), age)
        except Exception as e:
            logger.warning("[%s] 读取库中已有基线失败，忽略：%s", PLUGIN_ID, e)
            self._library = {}

    async def _save_library(self) -> None:
        try:
            await self.ctx.kv.set(
                "library",
                {b: [asdict(score), name, stamp] for b, (score, name, stamp) in self._library.items()},
            )
            await self.ctx.kv.set("library_scanned_at", self._library_scanned_at)
        except Exception as e:
            logger.debug("[%s] 保存库中已有基线失败：%s", PLUGIN_ID, e)

    def _library_score(self, container: str, bit_depth: int, height: int | float | None) -> Score:
        """库里那份的打分：字幕组未知（未列出档）、语言不参与，其余用 Emby 的真实媒体信息。"""
        top_groups = [g for g in self.config.groups if g and g.strip()]
        bottom_groups = [g for g in self.config.bottom_groups if g and g.strip()]
        unlisted = group_priority("\u0000-unlisted", top_groups, bottom_groups)[0]
        return Score(
            group_priority=unlisted,
            group_name=UNKNOWN_GROUP,
            language_priority=len(self.config.language_order or LANGUAGE_ORDER),
            language="未知",
            resolution=resolution_from_height(height),
            bit_depth=int(bit_depth or 8),
            revision=1,
            container=("." + str(container or "").lower()) if container else "",
        )

    # ---------------- 新订阅先暂停 ----------------
    async def _load_hold_state(self) -> None:
        try:
            self._confirmed = {int(x) for x in (await self.ctx.kv.get("confirmed", []) or [])}
            self._hold_armed = bool(await self.ctx.kv.get("hold_armed", False))
        except Exception as e:
            logger.warning("[%s] 读取「新订阅先暂停」状态失败：%s", PLUGIN_ID, e)
        await self._sync_hold_arming()

    async def _save_hold_state(self) -> None:
        try:
            await self.ctx.kv.set("confirmed", sorted(self._confirmed)[-2000:])
            await self.ctx.kv.set("hold_armed", self._hold_armed)
        except Exception as e:
            logger.debug("[%s] 保存暂停状态失败：%s", PLUGIN_ID, e)

    async def _all_rule_ids(self) -> list[int]:
        try:
            from module.database import Database

            async with Database() as db:
                rules = await db.bangumi.search_all()
            return [
                int(rule.id)
                for rule in rules
                if getattr(rule, "id", None) is not None and not getattr(rule, "deleted", False)
            ]
        except Exception as e:
            logger.warning("[%s] 读取规则列表失败：%s", PLUGIN_ID, e)
            return []

    async def _sync_hold_arming(self) -> None:
        """维护「哪些规则算已确认」：开关刚打开时把现有规则全部视为已确认。"""
        want = bool(self.config.hold_new_subscriptions)
        if want and not self._hold_armed:
            existing = await self._all_rule_ids()
            self._confirmed |= set(existing)
            self._hold_armed = True
            await self._save_hold_state()
            logger.info(
                "[%s] 已开启「新订阅先暂停」：现有 %d 条规则视为已确认，之后新建的会先暂停",
                PLUGIN_ID,
                len(existing),
            )
        elif not want and self._hold_armed:
            self._hold_armed = False
            await self._save_hold_state()
            logger.info("[%s] 已关闭「新订阅先暂停」，新规则会直接开始下载", PLUGIN_ID)

    def _is_held(self, rule_id: int | None) -> bool:
        if not self.config.hold_new_subscriptions or rule_id is None:
            return False
        return int(rule_id) not in self._confirmed

    async def release_rule(self, rule_id: int) -> None:
        """放行一条规则（保存下载顺序或点「立即放行」都会走这里）。"""
        self._confirmed.add(int(rule_id))
        await self._save_hold_state()

    def _notify_held_once(self, bangumi) -> None:
        rule_id = int(getattr(bangumi, "id", 0) or 0)
        if rule_id in self._held_notified:
            return
        self._held_notified.add(rule_id)
        try:
            self.ctx.bus.publish(
                SubscriptionHeld(
                    official_title=str(getattr(bangumi, "official_title", "") or ""),
                    rule_id=rule_id,
                )
            )
        except Exception as e:
            logger.debug("[%s] 发暂停通知失败：%s", PLUGIN_ID, e)

    async def _scan_library(self) -> None:
        """从 Emby 读全部剧集，建/刷新「库里已有」基线。"""
        if not (self.config.known_library_guard and self.config.emby_url and self.config.emby_token):
            return
        headers = {"X-Emby-Token": self.config.emby_token, "Accept": "application/json"}
        params = {"Recursive": "true", "IncludeItemTypes": "Episode", "Fields": "Path,MediaSources", "Limit": "5000"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    self.config.emby_url.rstrip("/") + "/emby/Items", params=params, headers=headers
                )
                response.raise_for_status()
                items = response.json().get("Items", [])
        except Exception as e:
            logger.warning("[%s] 读取 Emby 媒体库失败：%s", PLUGIN_ID, e)
            return

        fresh: dict[str, tuple[Score, str, float]] = {}
        now = time.time()
        for item in items:
            parsed = library_bucket(str(item.get("Path") or ""))
            if parsed is None:
                continue
            bucket, episode = parsed
            sources = item.get("MediaSources") or [{}]
            source = sources[0] if sources else {}
            bit_depth = 8
            height = source.get("Height") or 0
            for stream in source.get("MediaStreams") or []:
                if str(stream.get("Type") or "").lower() != "video":
                    continue
                bit_depth = max(bit_depth, int(stream.get("BitDepth") or 8))
                height = height or stream.get("Height") or 0
            score = self._library_score(str(source.get("Container") or ""), bit_depth, height)
            fresh[f"{bucket}:{episode:g}"] = (score, str(item.get("Name") or bucket), now)
        self._library = fresh
        self._library_scanned_at = now
        await self._save_library()
        logger.info("[%s] 库中已有基线已更新：%d 集", PLUGIN_ID, len(self._library))

    async def _maybe_scan_library(self) -> None:
        due = time.time() - self._library_scanned_at >= max(0.25, self.config.library_scan_hours) * 3600
        if due or not self._library:
            await self._scan_library()

    # ---------------- 1) 解析修正：ANi → 繁中 ----------------
    @hook(points.TITLE_PARSED, priority=40)
    def normalize_release(self, release: Release):
        if not (self.config.enabled and self.config.ani_always_traditional):
            return None
        try:
            group = getattr(release, "group", None) or ""
            if not is_ani_group(f"{group} {getattr(release, 'raw', '') or ''}"):
                return None
            if (getattr(release, "subtitle", None) or "") == "CHT":
                return None
            return replace(release, subtitle="CHT")
        except Exception as e:
            logger.debug("[%s] title.parsed 失败：%s", PLUGIN_ID, e)
            return None

    # ---------------- 2) 准入择优 ----------------
    @hook(points.TORRENT_FILTER, priority=60)
    def prefer_best(
        self, torrent: TorrentInfo, release: Release | None, bangumi: BangumiInfo
    ) -> Verdict:
        if not self.config.enabled:
            return Verdict.ok()
        if self._is_held(getattr(bangumi, "id", None)):
            self._notify_held_once(bangumi)
            return Verdict.reject(
                "新订阅待确认：请在「编辑规则 → 下载顺序」里保存顺序，或点「立即放行」"
            )
        try:
            parsed = parse_episode(torrent.name)
            if parsed is None or release is None:
                return Verdict.ok()
            season, episode = parsed
            # 分桶必须按**番剧**（保存目录 + 季）而不是规则 id：同一部番的 ANi 与
            # 喵萌是两条规则，按规则分桶会让它们各算各的、从不互相比较 —— 那样
            # 下载顺序和"每集只留最优"就全都不生效了（踩过）。
            bucket = f"{anime_key_from_bangumi(bangumi)}:{episode:g}"
            score = score_release(
                torrent.name, release, self.config,
                # SDK 的 Release 视图没有版本字段，自己从发布名解析（v2 > v1）
                revision=detect_revision(torrent.name),
            )
            rank = self._download_rank(bangumi.id)
            known = self._best.get(bucket)
            if known is not None and not outranks_with_rank(
                known[0], score, known[3], rank,
                allow_resolution_downgrade=self.config.allow_resolution_downgrade,
            ):
                return Verdict.reject(
                    f"已有不差于它的版本（{known[0].group_name}/{known[0].language}）"
                )
            # 库里已经有这一集：也要更好才放行（删了规则重建时不会再重头下）
            if self.config.known_library_guard:
                in_library = self._library.get(bucket)
                if in_library is not None and not outranks_library(
                    in_library[0], score,
                    allow_resolution_downgrade=self.config.allow_resolution_downgrade,
                ):
                    return Verdict.reject(f"已有不差于它的版本（库里已有 {in_library[1]}）")
            if known is not None or (
                self.config.known_library_guard and bucket in self._library
            ):
                logger.info(
                    "[%s] %s 比已知的都更好，放行：%s（%s/%s %sbit%s）",
                    PLUGIN_ID, bucket, torrent.name[:50],
                    score.group_name, score.language, score.bit_depth, score.container,
                )
            self._best[bucket] = (score, torrent.name, time.time(), rank)
            return Verdict.ok()
        except Exception as e:
            logger.debug("[%s] 择优判断失败（放行）：%s", PLUGIN_ID, e)
            return Verdict.ok()

    # ---------------- 3) 冲突替换（由宿主执行删除） ----------------
    @provider(points.CONFLICT_POLICY, id=PLUGIN_ID)
    def conflict_policy(self):
        plugin = self

        class TierConflictPolicy:
            def decide(self, request: ConflictRequest) -> ConflictDecision:
                if not (plugin.config.enabled and plugin.config.replace_on_conflict):
                    return ConflictDecision("hold", "质量择优插件未启用替换")
                try:
                    incoming = plugin._score_task(request.incoming)
                    owners = [
                        (task, plugin._score_task(task))
                        for task in request.owners
                    ]
                    owners = [(t, s) for t, s in owners if s is not None]
                    if incoming is None or not owners:
                        return ConflictDecision("hold", "版本身份解析不完整")
                    owner_task, owner_score = max(owners, key=lambda item: item[1].key())
                    if request.strict_upgrade or outranks(
                        owner_score, incoming,
                        allow_resolution_downgrade=plugin.config.allow_resolution_downgrade,
                    ):
                        return ConflictDecision(
                            "replace",
                            f"{incoming.group_name}/{incoming.language} 优于 "
                            f"{owner_score.group_name}/{owner_score.language}",
                        )
                    return ConflictDecision(
                        "hold",
                        f"保留现有 {owner_score.group_name}/{owner_score.language}"
                        f"（{owner_task.name[:48]}）",
                    )
                except Exception as e:
                    logger.debug("[%s] 冲突判断失败（保留）：%s", PLUGIN_ID, e)
                    return ConflictDecision("hold", "判断异常，保留现有文件")

        return TierConflictPolicy()

    def _score_task(self, task) -> Score | None:
        try:
            revision = getattr(getattr(task, "revision", None), "revision", 1)
            return score_release(task.name, None, self.config, revision=revision or 1)
        except Exception:
            return None

    # ---------------- 4) 定时巡检（默认 dry-run） ----------------
    @provider(points.SCHEDULED_TASK, id=PLUGIN_ID)
    def sweep_task(self) -> ScheduledTask:
        return ScheduledTask(
            run=self.sweep,
            interval=lambda: max(60, self.config.sweep_interval),
            initial_delay=180,
            enabled=lambda: self.config.enabled and self.config.sweep_enabled,
        )

    async def sweep(self) -> None:
        client = await self._open_downloader()
        if client is None:
            return
        try:
            infos = await self._torrent_infos(client)
            if not infos:
                return
            await self._prune_state(infos)
            await self._maybe_scan_library()
            plan = await self._plan_removals(client, infos)
            if not plan:
                return
            if self.config.sweep_dry_run:
                for item in plan:
                    logger.info(
                        "[%s] （dry-run）会清掉 %s（%s，第%s顺位），保留 %s（%s）",
                        PLUGIN_ID, item["loser_name"][:60], item["loser_group"],
                        item["order_rank"] + 1 if item["order_rank"] < 10_000 else "?",
                        item["winner_name"][:60], item["winner_group"],
                    )
                if self.config.notify:
                    await self._notify(preview_event(plan))
                return
            removed = 0
            for item in plan:
                if not await self._delete_torrent(client, item["loser_hash"]):
                    continue
                removed += 1
                self._best.pop(item["bucket"], None)
                logger.info(
                    "[%s] 已清理 %s，保留 %s",
                    PLUGIN_ID, item["loser_name"][:60], item["winner_name"][:60],
                )
                if self.config.notify:
                    await self._notify(
                        WashEvent(
                            title_text=str(item.get("title") or "未知番剧"),
                            episode=str(item.get("episode") or ""),
                            detail=f"{item['loser_group']} → {item['winner_group']}",
                        )
                    )
            await self._save_state()
            logger.info("[%s] 巡检结束：清理 %s 个", PLUGIN_ID, removed)
        except Exception as e:
            logger.warning("[%s] 巡检失败：%s", PLUGIN_ID, e)
        finally:
            await self._close_downloader(client)

    async def _plan_removals(
        self, client, infos: list[dict], titles: dict[int, str] | None = None
    ) -> list[dict]:
        """算出该清理哪些（不执行）。返回待删项列表。

        分桶键是「保存目录 + 季 + 集 + **作品身份**」——作品身份是硬要求：
        实测有一批老种子把 ``save_path`` 存成了根目录（``E:\\夸克动漫``），
        光按「目录 + 季 + 集」分桶会把**不同番剧**的第 1 集混在一起互相清理。
        身份优先用 ``ab:<id>`` 对应的番剧官方名（这样同一部番的不同字幕组能
        对上），没有标签时退回从发布名提取的作品名。
        """
        titles = titles if titles is not None else await self._bangumi_titles()
        buckets: dict[str, list[dict]] = {}
        for info in infos:
            name = str(info.get("name") or "")
            parsed = parse_episode(name)
            if parsed is None:
                continue
            season, episode = parsed
            files = await self._torrent_files(client, str(info.get("hash") or ""))
            media = [
                f for f in files
                if str(f.get("name") or "").lower().endswith(_MEDIA_SUFFIXES)
            ]
            if not media:
                continue
            save_path = str(info.get("save_path") or "")
            identity = self._identity(info, titles)
            bucket = (
                f"{save_path.replace(chr(92), '/').lower()}|{season}|{episode:g}"
                f"|{identity}"
            )
            score = score_release(
                name, None, self.config, revision=detect_revision(name)
            )
            # 种子里没有组名（AB 改写后的名字）→ 多半就是同一次下载，不算"可洗的另一份"
            if self.config.skip_unknown_group and score.group_name == UNKNOWN_GROUP:
                logger.debug(
                    "[%s] %s 没有可识别的字幕组，跳过洗版比较", PLUGIN_ID, name[:60]
                )
                continue
            buckets.setdefault(bucket, []).append(
                {
                    "info": info,
                    "name": name,
                    "file": str(media[0].get("name") or ""),
                    "score": score,
                    "season": season,
                    "episode": episode,
                    "bucket": bucket,
                    "save_path": save_path,
                    "identity": identity,
                }
            )

        now = time.time()
        plan: list[dict] = []
        for bucket, entries in buckets.items():
            if len(entries) < 2:
                continue
            entries.sort(
                key=lambda e: (
                    e["score"].key(),
                    -self._download_rank(parse_ab_tag(e["info"].get("tags"))),
                ),
                reverse=True,
            )
            winner = entries[0]
            # 首选卡住：没下完 + 没做种者 + 超过 stuck_hours → 清掉它，放次优进来
            if self.config.stuck_hours > 0 and self._is_stuck(
                winner["info"], now, self.config.stuck_hours
            ):
                # 卡住的是"最优的那份"：清掉它，让次优留下（报告里写清楚是谁代替谁）
                alternative = entries[1] if len(entries) > 1 else winner
                plan.append(self._plan_item(bucket, winner, alternative, stuck=True))
                continue
            # 除了"卡住的首选"，其余已经卡死（没下完 + 无做种 + 很久没动）的也顺手清掉：
            # 它们既占着下载顺序的位置，又永远下不来
            if self.config.stuck_hours > 0:
                for entry in entries[1:]:
                    if self._is_stuck(entry["info"], now, self.config.stuck_hours):
                        plan.append(self._plan_item(bucket, entry, winner, stuck=True))
            if not self._is_complete(winner["info"]):
                continue  # 更好的还没下完 → 一个都不动
            owners: dict[str, int] = {}
            for entry in entries:
                key = entry["file"].replace("\\", "/").lower()
                owners[key] = owners.get(key, 0) + 1
            for loser in entries[1:]:
                if not outranks(
                    loser["score"], winner["score"],
                    allow_resolution_downgrade=self.config.allow_resolution_downgrade,
                ):
                    continue  # 排序异常，保守跳过
                key = loser["file"].replace("\\", "/").lower()
                if owners.get(key, 0) > 1:
                    logger.info(
                        "[%s] %s 被 %s 个种子共用，跳过删除",
                        PLUGIN_ID, loser["file"], owners[key],
                    )
                    continue
                plan.append(self._plan_item(bucket, loser, winner))
        return plan

    def _plan_item(
        self, bucket: str, loser: dict, winner: dict, *, stuck: bool = False
    ) -> dict:
        rank = self._download_rank(parse_ab_tag(loser["info"].get("tags")))
        return {
            "bucket": bucket,
            "loser_hash": str(loser["info"].get("hash") or ""),
            "loser_name": loser["name"],
            "loser_group": loser["score"].group_name,
            "winner_name": winner["name"],
            "winner_group": winner["score"].group_name,
            "title": show_name(loser["save_path"]),
            "episode": f"第{loser['season']}季 第{loser['episode']:g}集",
            "stuck": stuck,
            "order_rank": rank,
        }

    @staticmethod
    def _is_complete(info: dict) -> bool:
        try:
            return float(info.get("progress") or 0) >= 1.0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _seeders(info: dict) -> int:
        try:
            return int(info.get("num_complete") or 0)
        except (TypeError, ValueError):
            return 0

    def _is_stuck(self, info: dict, now: float, hours: float) -> bool:
        if self._is_complete(info) or self._seeders(info) > 0:
            return False
        stamp = info.get("last_activity") or info.get("added_on") or 0
        try:
            idle = (now - float(stamp)) / 3600.0
        except (TypeError, ValueError):
            return False
        return idle >= hours

    # ---------------- 5) 下载顺序（编辑规则弹窗里的「下载顺序」标签页） ----------------
    async def _load_orders(self) -> None:
        try:
            raw = await self.ctx.kv.get("orders", {}) or {}
            self._orders = {
                str(key): [int(x) for x in value] for key, value in raw.items()
            }
        except Exception as e:
            logger.warning("[%s] 读取下载顺序失败：%s", PLUGIN_ID, e)
            self._orders = {}
        self._rebuild_rank_index()

    def _rebuild_rank_index(self) -> None:
        """规则 id → 它在下载顺序里的位置（越小越先取）。"""
        self._rank_by_rule = {}
        for order in self._orders.values():
            for index, rule_id in enumerate(order):
                current = self._rank_by_rule.get(rule_id)
                if current is None or index < current:
                    self._rank_by_rule[rule_id] = index

    async def _save_orders(self) -> None:
        self._rebuild_rank_index()
        try:
            await self.ctx.kv.set("orders", self._orders)
        except Exception as e:
            logger.debug("[%s] 保存下载顺序失败：%s", PLUGIN_ID, e)

    def _download_rank(self, rule_id: int | None) -> int:
        """规则在这部番的下载顺序里的位置；没设过就排在最后。"""
        if rule_id is None:
            return 10_000
        return self._rank_by_rule.get(int(rule_id), 10_000)

    async def _actual_groups(self, rule_ids: list[int]) -> dict[int, list[str]]:
        """每条规则**实际**收过哪些字幕组（按 qB 里 ab:<id> 标签统计）。

        AB 规则上的 group_name 只反映"建规则时第一条种子的组"（实测会认错），
        所以界面按这里的真实分布标注。
        """
        if not rule_ids:
            return {}
        wanted = {int(x) for x in rule_ids}
        counters: dict[int, collections.Counter] = {}
        try:
            client = await self._open_downloader()
            if client is None:
                return {}
            try:
                infos = await self._torrent_infos(client)
            finally:
                await self._close_downloader(client)
        except Exception as e:
            logger.debug("[%s] 统计实际字幕组失败：%s", PLUGIN_ID, e)
            return {}

        for info in infos:
            tags = info.get("tags") or ""
            if isinstance(tags, (list, tuple)):
                tags = ",".join(str(t) for t in tags)
            for rid in re.findall(r"ab:(\d+)", str(tags)):
                rule_id = int(rid)
                if rule_id not in wanted:
                    continue
                score = score_release(str(info.get("name") or ""), None, self.config)
                if score.group_name and score.group_name != UNKNOWN_GROUP:
                    counters.setdefault(rule_id, collections.Counter())[score.group_name] += 1
        return {rid: [g for g, _ in c.most_common(3)] for rid, c in counters.items()}

    async def _rules_of_anime(self, bangumi_id: int) -> tuple[str | None, list[dict]]:
        """同一部番的全部规则（按保存的下载顺序排列）。"""
        try:
            from module.database import Database

            async with Database() as db:
                anchor = await db.bangumi.search_id(bangumi_id)
                if anchor is None:
                    return None, []
                rules = await db.bangumi.search_all()
                detailed = []
                for rule in rules:
                    if hasattr(rule, "save_path"):
                        detailed.append(rule)
                    else:
                        full = await db.bangumi.search_id(getattr(rule, "id", None))
                        if full is not None:
                            detailed.append(full)
        except Exception as e:
            logger.warning("[%s] 读取规则列表失败：%s", PLUGIN_ID, e)
            return None, []

        key = anime_key(anchor)
        mine = [
            rule
            for rule in detailed
            if anime_key(rule) == key and not getattr(rule, "deleted", False)
        ]
        by_id = {getattr(rule, "id", None): rule for rule in mine}
        order = [rid for rid in (self._orders.get(key) or []) if rid in by_id]
        order += [
            rid
            for rid in sorted(
                by_id, key=lambda x: str(getattr(by_id[x], "group_name", "") or "")
            )
            if rid not in order
        ]
        actual = await self._actual_groups([int(x) for x in by_id])
        return key, [
            {
                "id": rid,
                "group_name": str(getattr(by_id[rid], "group_name", "") or ""),
                "actual_groups": actual.get(rid, []),
                "tags": rule_tags(by_id[rid]),
                "title": str(getattr(by_id[rid], "official_title", "") or ""),
            }
            for rid in order
        ]

    @provider(points.API_ROUTER, id=PLUGIN_ID)
    def order_router(self):
        from fastapi import APIRouter, HTTPException, Query

        router = APIRouter()

        @router.get("/order")
        async def read_order(bangumi_id: int = Query(..., ge=1)):
            key, rules = await self._rules_of_anime(bangumi_id)
            logger.info(
                "[%s] 前端读取下载顺序：bangumi_id=%s → %s 条规则%s",
                PLUGIN_ID,
                bangumi_id,
                len(rules),
                f"（{key.split('|')[0]}）" if key else "",
            )
            if key is None:
                raise HTTPException(status_code=404, detail="找不到这条规则")
            for rule in rules:
                rule["held"] = self._is_held(rule["id"])
            return {
                "anime": key.split("|")[0],
                "rules": rules,
                "rule_ids": [rule["id"] for rule in rules],
                "held": self._is_held(bangumi_id),
                "hold_enabled": bool(self.config.hold_new_subscriptions),
            }

        @router.put("/order")
        async def write_order(payload: OrderPayload):
            key, rules = await self._rules_of_anime(payload.bangumi_id)
            if key is None:
                raise HTTPException(status_code=404, detail="找不到这条规则")
            known = {rule["id"] for rule in rules}
            if set(payload.rule_ids) != known or len(payload.rule_ids) != len(known):
                raise HTTPException(
                    status_code=400, detail="规则清单已变化，请刷新页面后重试"
                )
            self._orders[key] = list(payload.rule_ids)
            await self._save_orders()
            # 保存了顺序 = 已经确认过这部番的字幕组顺序 → 一并放行
            for rule_id in set(payload.rule_ids) | {payload.bangumi_id}:
                await self.release_rule(rule_id)
            logger.info(
                "[%s] 已更新下载顺序：%s → %s",
                PLUGIN_ID,
                key.split("|")[0],
                " > ".join(
                    next(
                        (
                            rule["group_name"]
                            for rule in rules
                            if rule["id"] == rid
                        ),
                        str(rid),
                    )
                    for rid in payload.rule_ids
                ),
            )
            return {
                "ok": True,
                "anime": key.split("|")[0],
                "rule_ids": payload.rule_ids,
                "held": False,
            }

        @router.post("/order/release")
        async def release_order(payload: OrderPayload):
            """不调整顺序，直接放行（想用默认顺序时用这个）。"""
            await self.release_rule(payload.bangumi_id)
            logger.info(
                "[%s] 已放行规则 #%s（不调整顺序，按默认档位开始下载）",
                PLUGIN_ID,
                payload.bangumi_id,
            )
            return {"ok": True, "held": False}

        return router

    # ---------------- 宿主能力（本地插件，全部做兜底） ----------------
    async def _bangumi_titles(self) -> dict[int, str]:
        """``ab:<id>`` → 番剧官方名。"""
        try:
            from module.database import Database

            async with Database() as db:
                rules = await db.bangumi.search_all()
            return {
                int(rule.id): str(getattr(rule, "official_title", "") or "")
                for rule in rules
                if getattr(rule, "id", None) is not None
            }
        except Exception as e:
            if not self._titles_warned:
                self._titles_warned = True
                logger.warning(
                    "[%s] 读取番剧列表失败，跨字幕组洗版会退化成按作品名匹配：%s",
                    PLUGIN_ID,
                    e,
                )
            return {}

    def _identity(self, info: dict, titles: dict[int, str]) -> str:
        """作品身份：同一个 identity 才允许互相清理。

        优先用规则里的**官方名** —— 同一部番的两条规则（ANi 一条、喵萌一条）
        id 不同但官方名相同，这样跨字幕组才洗得动；规则被删了就用 id 兜底；
        完全没有 ``ab:`` 标签的种子退回从发布名提取的作品名。
        """
        bangumi_id = parse_ab_tag(info.get("tags"))
        if bangumi_id is not None:
            title = titles.get(bangumi_id)
            if title:
                return f"title:{title_key(title)}"
            return f"ab:{bangumi_id}"
        return f"title:{title_key(str(info.get('name') or ''))}"

    async def _open_downloader(self):
        try:
            from module.downloader import DownloadClient

            client = DownloadClient()
            await client.__aenter__()
            return client
        except Exception as e:
            logger.debug("[%s] 打开下载器失败：%s", PLUGIN_ID, e)
            return None

    async def _close_downloader(self, client) -> None:
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _torrent_infos(self, client) -> list[dict]:
        for name in ("get_torrent_info", "torrents_info", "get_torrents_info"):
            fn = getattr(client, name, None)
            if fn is None:
                continue
            try:
                return await fn(category=None, status_filter=None) or []
            except TypeError:
                try:
                    return await fn() or []
                except Exception:
                    return []
            except Exception:
                return []
        return []

    async def _torrent_files(self, client, torrent_hash: str) -> list[dict]:
        for name in ("get_torrent_files", "torrents_files"):
            fn = getattr(client, name, None)
            if fn is None:
                continue
            try:
                return await fn(torrent_hash) or []
            except Exception:
                return []
        return []

    async def _delete_torrent(self, client, torrent_hash: str) -> bool:
        for name in ("delete_torrent", "torrents_delete"):
            fn = getattr(client, name, None)
            if fn is None:
                continue
            try:
                return bool(await fn(torrent_hash, delete_files=True))
            except TypeError:
                try:
                    return bool(await fn(torrent_hash))
                except Exception:
                    return False
            except Exception:
                return False
        return False

    async def _notify(self, event: SystemEvent) -> None:
        try:
            self.ctx.bus.publish(event)
        except Exception as e:
            logger.debug("[%s] 发通知失败：%s", PLUGIN_ID, e)

    async def _prune_state(self, infos: list[dict]) -> None:
        """下载器里已经没有的候选，从状态里清掉（免得挡住新版）。"""
        names = {str(i.get("name") or "") for i in infos}
        stale = [k for k, item in self._best.items() if item[1] not in names]
        for bucket in stale:
            self._best.pop(bucket, None)
        if stale:
            await self._save_state()
