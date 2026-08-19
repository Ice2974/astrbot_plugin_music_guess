# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import ssl
import unicodedata
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools


SONGS_FILENAME = "songs.txt"
MANIFEST_FILENAME = "manifest.json"
GAME_SONG_COUNT = 8

# QQ 官方客户端会把连续半角 * 按 Markdown 语法解释。
# 使用紧凑的 Bullet 字符 • 作为遮罩，既避开 Markdown，又能明显保留单词空格。
MASK_CHAR = "•"

START_COMMAND = "开字母"
END_COMMANDS = {"结束开字母", "结束游戏"}

# 开字符：空格后跟任意单个字符；ASCII 字母 / 数字允许紧凑格式（开A、开7）。
# 中文等文字字符必须带空格，避免把「开心」这类普通词语误识别为开字符指令。
OPEN_PATTERN = re.compile(r"^开(?:\s+(.+?)|([A-Za-z0-9]))\s*$")
# 猜歌：编号与曲名之间允许空格、点号（与题板「1. 」样式一致）或顿号分隔。
GUESS_PATTERN = re.compile(r"^(?:曲\s*)?([1-8])[\s.、]+(.+?)\s*$")

# ---- 曲库自动更新 ----
# 曲库更新源配置（songs_update_source）的内部取值。
UPDATE_SOURCE_AUTO = "auto"
UPDATE_SOURCE_GITHUB = "github"
UPDATE_SOURCE_GITEE = "gitee"
UPDATE_SOURCE_DISABLED = "disabled"
VALID_UPDATE_SOURCES = frozenset(
    {
        UPDATE_SOURCE_AUTO,
        UPDATE_SOURCE_GITHUB,
        UPDATE_SOURCE_GITEE,
        UPDATE_SOURCE_DISABLED,
    }
)
DEFAULT_UPDATE_SOURCE = UPDATE_SOURCE_AUTO

# GitHub 为曲库主上游，Gitee 为国内镜像（GitHub → Gitee 单向同步）。
# Gitee raw 地址已人工验证会 302 到 raw.giteeusercontent.com，urllib 自动跟随。
UPDATE_SOURCES: dict[str, dict[str, str]] = {
    "github": {
        "manifest": "https://raw.githubusercontent.com/Ice2974/astrbot_plugin_music_guess/main/manifest.json",
        "songs": "https://raw.githubusercontent.com/Ice2974/astrbot_plugin_music_guess/main/songs.txt",
    },
    "gitee": {
        "manifest": "https://gitee.com/Ice2974/astrbot_plugin_music_guess/raw/main/manifest.json",
        "songs": "https://gitee.com/Ice2974/astrbot_plugin_music_guess/raw/main/songs.txt",
    },
}

# 整个远程更新流程的全局时间预算；探测 / 下载阶段各有分项超时并按剩余预算钳制，
# 外层 asyncio.wait_for(总预算) 作硬兜底，避免不可达源拖慢插件初始化。
UPDATE_TOTAL_BUDGET_S = 20.0
UPDATE_PROBE_TIMEOUT_S = 6.0
UPDATE_DOWNLOAD_TIMEOUT_S = 12.0
UPDATE_THREAD_GRACE_S = 2.0
UPDATE_MAX_BYTES = 8 * 1024 * 1024
UPDATE_USER_AGENT = (
    "astrbot_plugin_music_guess (+https://github.com/Ice2974/astrbot_plugin_music_guess)"
)

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(slots=True)
class RoundSong:
    title: str
    guessed: bool = False


@dataclass(slots=True)
class GameState:
    songs: list[RoundSong]
    opened_chars: set[str] = field(default_factory=set)


class AtBotFilter(filter.CustomFilter):
    """仅当消息链中存在指向当前机器人自身的 At 消息段时通过。

    @其他用户、@全体成员、仅回复机器人、仅 wake 前缀均不通过。
    在 WakingCheck 的 handler filter 阶段生效：未 @机器人时
    本插件 handler 不会进入 activated_handlers。
    """

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        self_id = event.get_self_id()
        if not self_id:
            return False
        self_id = str(self_id)
        return any(
            isinstance(seg, At) and str(seg.qq) == self_id
            for seg in event.get_messages()
        )


# ---------------------------------------------------------------------------
# 曲库自动更新：模块级纯函数（解析 / 校验 / 下载 / 原子写 / 候选排序）
# ---------------------------------------------------------------------------


def _parse_song_titles(text: str) -> list[str]:
    """按行解析曲名：strip、跳过空行、完全相同的标题只保留一次（保持首次出现顺序）。"""
    songs: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        title = line.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        songs.append(title)
    return songs


def _validate_manifest(obj: object) -> dict | None:
    """manifest.json 结构校验，不合法返回 None；未知字段忽略以保持向前兼容。"""
    if not isinstance(obj, dict):
        return None
    version = obj.get("version")
    song_count = obj.get("song_count")
    sha256 = obj.get("sha256")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    if (
        not isinstance(song_count, int)
        or isinstance(song_count, bool)
        or song_count < GAME_SONG_COUNT
    ):
        return None
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        return None
    return {"version": version, "song_count": song_count, "sha256": sha256}


def _validate_library_pair(
    manifest_bytes: bytes, songs_bytes: bytes
) -> tuple[int, list[str]] | None:
    """整组校验 manifest + songs：结构、大小、SHA-256、编码、去重数量。

    任一环节失败返回 None；只有整组有效，其 manifest version 才参与版本比较。
    """
    try:
        info = _validate_manifest(json.loads(manifest_bytes.decode("utf-8-sig")))
    except ValueError:  # 含 UnicodeDecodeError / json.JSONDecodeError
        return None
    if info is None:
        return None
    if len(songs_bytes) > UPDATE_MAX_BYTES:
        return None
    if hashlib.sha256(songs_bytes).hexdigest() != info["sha256"]:
        return None
    try:
        titles = _parse_song_titles(songs_bytes.decode("utf-8-sig"))
    except UnicodeDecodeError:
        return None
    if len(titles) < GAME_SONG_COUNT or len(titles) != info["song_count"]:
        return None
    return info["version"], titles


def _blocking_http_get(url: str, timeout_s: float) -> bytes:
    """阻塞式 GET（在工作线程中运行）；非 2xx、超时、超过大小上限都会抛异常。"""
    request = urllib.request.Request(url, headers={"User-Agent": UPDATE_USER_AGENT})
    with urllib.request.urlopen(
        request, timeout=timeout_s, context=ssl.create_default_context()
    ) as response:
        data = response.read(UPDATE_MAX_BYTES + 1)
    if len(data) > UPDATE_MAX_BYTES:
        raise ValueError(f"响应超过 {UPDATE_MAX_BYTES} 字节上限")
    return data


async def _http_download(url: str, timeout_s: float) -> bytes:
    """异步下载：阻塞请求放到工作线程执行，外层 wait_for 兜底。

    取消时工作线程至多残留到 socket 超时，不会阻塞事件循环。
    """
    return await asyncio.wait_for(
        asyncio.to_thread(_blocking_http_get, url, timeout_s),
        timeout=timeout_s + UPDATE_THREAD_GRACE_S,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace，保证单个文件的原子替换。

    跨文件没有事务保证；曲库缓存依赖 manifest.json 作为唯一提交点
    （见 _commit_cache）。
    """
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        # replace 成功时 tmp 已不存在；失败时清理残留。
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _cache_files(data_dir: Path) -> list[Path]:
    """列出数据目录中的曲库缓存文件（manifest、songs-*.txt 与 *.tmp 残留）。"""
    return [
        item
        for item in data_dir.iterdir()
        if item.is_file()
        and (
            item.name == MANIFEST_FILENAME
            or (item.name.startswith("songs-") and item.name.endswith(".txt"))
            or item.name.endswith(".tmp")
        )
    ]


def _sort_update_candidates(entries: list[dict]) -> list[dict]:
    """候选源排序：version 降序。

    - 同 version 且内容（sha256）相同：优先 Gitee（国内可达性）；
    - 同 version 但 sha256 不一致：警告并以 GitHub 主上游为准。
    """
    ordered = sorted(entries, key=lambda entry: entry["version"], reverse=True)
    result: list[dict] = []
    index = 0
    while index < len(ordered):
        end = index
        while (
            end < len(ordered) and ordered[end]["version"] == ordered[index]["version"]
        ):
            end += 1
        group = ordered[index:end]
        if len(group) > 1:
            if len({entry["sha256"] for entry in group}) == 1:
                group.sort(key=lambda entry: entry["source"] != "gitee")
            else:
                logger.warning(
                    "music_guess 更新源 manifest 同 version 但 sha256 不一致，以 GitHub 主上游为准"
                )
                group.sort(key=lambda entry: entry["source"] != "github")
        result.extend(group)
        index = end
    return result


class MusicGuessPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.games: dict[str, GameState] = {}
        self.song_pool: list[str] = []
        self.song_load_error: str | None = None
        self.exclusive_mode = (
            bool(config.get("exclusive_mode", False)) if config else False
        )
        raw_source = (
            config.get("songs_update_source", DEFAULT_UPDATE_SOURCE)
            if config
            else DEFAULT_UPDATE_SOURCE
        )
        # 旧版本配置或手工编辑产生的非法值（含 list/dict 等不可哈希值）
        # 一律按 auto 处理，不能在 __init__ 阶段抛异常导致插件加载失败。
        self.songs_update_source = (
            raw_source
            if isinstance(raw_source, str)
            and raw_source in VALID_UPDATE_SOURCES
            else DEFAULT_UPDATE_SOURCE
        )
        # AstrBot 插件数据目录（缓存位置），initialize() 中通过 StarTools 解析；
        # None 表示不可用：跳过远程更新，仅使用插件自带曲库。
        self._data_dir: Path | None = None

    async def initialize(self):
        """插件初始化：先加载本地有效曲库，再在限时预算内尝试远程曲库更新。

        本地曲库在更新开始前就已可用；曲库、缓存或网络的任何异常
        都不会阻止插件本身加载和游戏。
        """
        self._data_dir = self._resolve_data_dir()
        self._load_songs()
        if await self._check_song_updates():
            self._load_songs()

    @filter.custom_filter(AtBotFilter)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL, priority=1)
    async def on_group_message(self, event: AstrMessageEvent):
        """
        处理 QQ 官方机器人群聊中 @机器人 的开字母游戏消息。

        - 只有消息链中包含指向本机器人的 At 消息段时 handler 才会被激活
          （见 AtBotFilter）；未 @机器人 的群消息不进入本插件；
        - / 开头的 AstrBot 指令始终放行：WakingCheck 会在 handler 之前
          剥离 message_str 的 wake 前缀（默认 "/"），因此用未被改写的
          消息链 Plain 文本判断；
        - priority=1 让游戏进行中已被认领的消息先于默认优先级（0）的
          其他插件处理；priority 必须声明在最底部的装饰器上，否则
          AstrBot 注册时会被忽略；
        - exclusive_mode=false（默认）：只认领「开字母」入口和进行中
          游戏的开字符 / 猜歌 / 结束操作，其余 @机器人 消息直接放行，
          不回复、不停止传播；
        - exclusive_mode=true：@机器人 但无法识别的消息返回玩法提示，
          并 stop_event()，避免进入 LLM。
        """
        text = self._normalize_input(event.message_str)

        # 保留 AstrBot 的 /help、/plugin 等系统/插件命令。
        if self._normalize_input(self._chain_plain_text(event)).startswith("/"):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        try:
            if self.exclusive_mode:
                reply = self._dispatch_exclusive(group_id, text)
            else:
                reply = self._dispatch_public(group_id, text)
                if reply is None:
                    # 普通模式下无法明确识别为本插件命令的消息放行，
                    # 交给 AstrBot 后续的插件 / LLM 正常处理。
                    return
        except Exception:
            logger.exception("music_guess 处理游戏消息时发生未预期异常")
            reply = "游戏处理失败，请联系管理员查看 AstrBot 日志。"

        # 先停止传播，再返回消息结果。
        # 这样被本插件认领的消息不会继续进入其它 handler / Agent / LLM。
        event.stop_event()

        if reply:
            yield event.plain_result(reply)

    def _dispatch_public(self, group_id: str, text: str) -> str | None:
        """exclusive_mode=false：只认领「开字母」入口和进行中游戏的操作；返回 None 表示放行。"""
        if not text:
            return None

        if text == START_COMMAND:
            return self._start_game(group_id)

        # 游戏未开始时，开字符 / 猜歌 / 结束等快捷格式不属于本插件，
        # 放行给其他插件 / LLM，避免抢占编号选择等同类消息。
        if group_id not in self.games:
            return None

        if text in END_COMMANDS:
            return self._end_game(group_id)

        open_match = OPEN_PATTERN.fullmatch(text)
        if open_match:
            candidate = self._normalize_input(
                open_match.group(1) or open_match.group(2)
            )
            return self._open_character(group_id, candidate)

        guess_match = GUESS_PATTERN.fullmatch(text)
        if guess_match:
            index = int(guess_match.group(1))
            answer = guess_match.group(2)
            return self._guess_song(group_id, index, answer)

        return None

    def _dispatch_exclusive(self, group_id: str, text: str) -> str:
        """exclusive_mode=true：@机器人 的非 / 群消息都由本插件接管。"""
        reply = self._dispatch_public(group_id, text)
        if reply is not None:
            return reply

        if not text:
            return self._usage_message(group_id)

        if group_id in self.games:
            # 游戏进行中：任何未识别普通文本都不要放到 LLM。
            return (
                "无法识别这条游戏消息。\n\n"
                "以下操作均需 @机器人：\n"
                "开字符：开 A / 开 7 / 开 桜\n"
                "猜歌曲：3 Credits（或 曲 3 Credits）\n"
                "结束：结束开字母"
            )

        # 没有进行中的游戏：本插件也不承担 AI 对话。
        return (
            "本群当前没有进行中的开字母游戏。\n"
            "请 @机器人 后发送「开字母」开始一局。\n"
            "AstrBot 系统指令可使用 /help。"
        )

    def _usage_message(self, group_id: str) -> str:
        if group_id in self.games:
            return (
                "以下操作均需 @机器人：\n"
                "开字符：开 A / 开 7 / 开 桜\n"
                "猜歌曲：3 Credits（或 曲 3 Credits）\n"
                "结束：结束开字母"
            )
        return "请 @机器人 后发送「开字母」开始一局。AstrBot 系统指令可使用 /help。"

    # ---- 曲库加载与远程更新 ----

    def _resolve_data_dir(self) -> Path | None:
        """获取 AstrBot 官方插件数据目录；失败时返回 None（跳过更新，仅用本地曲库）。"""
        try:
            return StarTools.get_data_dir("astrbot_plugin_music_guess")
        except Exception:
            logger.warning("music_guess 无法获取插件数据目录，跳过曲库自动更新")
            return None

    def _load_songs(self) -> None:
        """加载本地曲库：缓存 pair 与插件自带曲库按 manifest version 择高，并列取缓存。

        缓存无效时成组清理（自愈）并回退插件自带曲库；自带曲库永不删除。
        对外只更新 song_pool / song_load_error，不抛出异常。
        """
        cache_pair = self._read_cache_pair()
        if cache_pair is None:
            self._discard_cache()

        bundled_version, bundled_titles = self._read_bundled_library()
        bundled_ok = (
            bundled_titles is not None and len(bundled_titles) >= GAME_SONG_COUNT
        )

        if bundled_ok and (cache_pair is None or bundled_version > cache_pair[0]):
            source, version, titles = "自带", bundled_version, bundled_titles
        elif cache_pair is not None:
            source, version, titles = "缓存", cache_pair[0], cache_pair[1]
        else:
            self.song_pool = []
            if bundled_titles is None:
                self.song_load_error = f"曲库文件不存在：{SONGS_FILENAME}"
            else:
                self.song_load_error = (
                    f"曲库有效歌曲只有 {len(bundled_titles)} 首，"
                    f"至少需要 {GAME_SONG_COUNT} 首。"
                )
            logger.warning(self.song_load_error)
            return

        self.song_pool = titles
        self.song_load_error = None
        logger.info(f"music_guess 曲库加载完成：{source} v{version}，{len(titles)} 首")

    def _read_cache_pair(self) -> tuple[int, list[str]] | None:
        """读取并整组校验缓存（manifest.json + songs-<sha256>.txt）；无效返回 None。"""
        data_dir = self._data_dir
        if data_dir is None:
            return None
        try:
            manifest_bytes = (data_dir / MANIFEST_FILENAME).read_bytes()
            info = _validate_manifest(json.loads(manifest_bytes.decode("utf-8-sig")))
            if info is None:
                return None
            songs_path = data_dir / f"songs-{info['sha256']}.txt"
            if not songs_path.is_file():
                return None
            return _validate_library_pair(manifest_bytes, songs_path.read_bytes())
        except OSError:
            return None
        except ValueError:  # 含 UnicodeDecodeError / json.JSONDecodeError
            return None

    def _read_bundled_library(self) -> tuple[int, list[str] | None]:
        """读取插件自带曲库。

        manifest + songs 整组有效时返回其 version 与曲名；manifest 缺失或整组
        无效时 version 按 0 处理，但 songs.txt 本身仍可作为最后手段使用。
        """
        plugin_dir = Path(__file__).resolve().parent
        try:
            songs_bytes = (plugin_dir / SONGS_FILENAME).read_bytes()
        except OSError:
            return 0, None
        try:
            pair = _validate_library_pair(
                (plugin_dir / MANIFEST_FILENAME).read_bytes(), songs_bytes
            )
            if pair is not None:
                return pair[0], pair[1]
        except OSError:
            pass
        except ValueError:
            pass
        try:
            return 0, _parse_song_titles(songs_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return 0, None

    def _discard_cache(self) -> None:
        """best-effort 丢弃无效缓存残留；插件自带曲库不受影响。"""
        data_dir = self._data_dir
        if data_dir is None:
            return
        try:
            leftovers = _cache_files(data_dir)
        except OSError:
            return
        if not leftovers:
            return
        logger.warning("music_guess 本地曲库缓存无效，清理残留并回退插件自带曲库")
        for item in leftovers:
            try:
                item.unlink()
            except OSError:
                pass

    def _local_library_version(self) -> int:
        """当前本地有效曲库版本：缓存 pair 与自带 pair 的较大值，无效按 0。"""
        versions = [0]
        cache_pair = self._read_cache_pair()
        if cache_pair is not None:
            versions.append(cache_pair[0])
        bundled_version, _ = self._read_bundled_library()
        versions.append(bundled_version)
        return max(versions)

    async def _check_song_updates(
        self,
        fetch: Callable[[str, float], Awaitable[bytes]] | None = None,
        total_budget_s: float = UPDATE_TOTAL_BUDGET_S,
        probe_timeout_s: float = UPDATE_PROBE_TIMEOUT_S,
        download_timeout_s: float = UPDATE_DOWNLOAD_TIMEOUT_S,
    ) -> bool:
        """按配置尝试远程曲库更新；成功提交缓存返回 True。

        所有网络、镜像、校验或远程文件异常都只记录日志并返回 False，
        绝不抛出异常，也不影响已加载的本地曲库。
        """
        if fetch is None:
            fetch = _http_download
        if self._data_dir is None or self.songs_update_source == UPDATE_SOURCE_DISABLED:
            return False
        try:
            return await asyncio.wait_for(
                self._run_update(fetch, total_budget_s, probe_timeout_s, download_timeout_s),
                timeout=total_budget_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"music_guess 曲库更新超出 {total_budget_s:.0f}s 总预算，继续使用本地曲库"
            )
            return False
        except Exception:
            logger.exception("music_guess 曲库更新发生未预期异常，继续使用本地曲库")
            return False

    async def _run_update(
        self,
        fetch: Callable[[str, float], Awaitable[bytes]],
        total_budget_s: float,
        probe_timeout_s: float,
        download_timeout_s: float,
    ) -> bool:
        """更新主流程：探测 manifest → 择优 → 与本地版本比较 → 下载校验 → 提交缓存。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + total_budget_s

        def remaining() -> float:
            return deadline - loop.time()

        source_names = (
            ["github", "gitee"]
            if self.songs_update_source == UPDATE_SOURCE_AUTO
            else [self.songs_update_source]
        )

        # 阶段一：manifest 探测（auto 模式并发，受整体探测窗口约束，
        # 不会因为一个源挂起而拖满另一个源的超时时间）。
        probe_window = min(probe_timeout_s, remaining())
        entries = await self._probe_manifests(fetch, source_names, probe_window)
        if not entries:
            logger.warning("music_guess 未取得任何有效的远程 manifest，继续使用本地曲库")
            return False

        candidates = _sort_update_candidates(entries)
        local_version = self._local_library_version()
        best = candidates[0]
        if best["version"] <= local_version:
            logger.info(
                f"music_guess 曲库已是最新（本地 v{local_version}，"
                f"远端最高 v{best['version']}）"
            )
            return False

        # 阶段二：按序下载并整组校验；auto 模式允许跨到下一个已取得
        # 有效 manifest 的源，github / gitee 单源模式禁止跨源。
        problems: list[str] = []
        for entry in candidates:
            if entry["version"] <= local_version:
                # 候选按 version 降序，其后都不会比本地新，不降级。
                break
            budget_left = remaining()
            if budget_left <= 0:
                problems.append("更新预算耗尽")
                break
            timeout = min(download_timeout_s, budget_left)
            try:
                songs_bytes = await fetch(entry["urls"]["songs"], timeout)
                pair = _validate_library_pair(entry["manifest_bytes"], songs_bytes)
            except Exception as exc:
                problems.append(f"{entry['source']} songs.txt 下载失败：{exc}")
                logger.warning(f"music_guess 曲库更新：{problems[-1]}")
                continue
            if pair is None:
                problems.append(f"{entry['source']} songs.txt 校验未通过")
                logger.warning(f"music_guess 曲库更新：{problems[-1]}")
                continue
            if self._commit_cache(entry["manifest_bytes"], songs_bytes, entry["sha256"]):
                logger.info(
                    f"music_guess 曲库已更新：{entry['source']} v{pair[0]}，"
                    f"{len(pair[1])} 首"
                )
                return True
            problems.append(f"{entry['source']} 缓存写入失败")
            logger.warning(f"music_guess 曲库更新：{problems[-1]}")

        logger.warning(
            f"music_guess 曲库更新失败（{'; '.join(problems)}），继续使用本地曲库"
        )
        return False

    async def _probe_manifests(
        self,
        fetch: Callable[[str, float], Awaitable[bytes]],
        source_names: list[str],
        window_s: float,
    ) -> list[dict]:
        """在探测窗口内并发获取并校验各源 manifest；窗口结束取消未完成任务。"""
        if window_s <= 0:
            return []
        tasks = [
            asyncio.ensure_future(self._fetch_manifest(fetch, name, window_s))
            for name in source_names
        ]
        done, pending = await asyncio.wait(tasks, timeout=window_s)
        for task in pending:
            task.cancel()
        if pending:
            # 等待取消完成，避免任务销毁警告。
            await asyncio.gather(*pending, return_exceptions=True)
        entries = []
        for task in done:
            try:
                entry = task.result()
            except Exception:
                entry = None
            if entry is not None:
                entries.append(entry)
        return entries

    async def _fetch_manifest(
        self,
        fetch: Callable[[str, float], Awaitable[bytes]],
        source_name: str,
        timeout_s: float,
    ) -> dict | None:
        """获取并校验单个源的 manifest；失败只记日志并返回 None。"""
        urls = UPDATE_SOURCES[source_name]
        try:
            manifest_bytes = await fetch(urls["manifest"], timeout_s)
            obj = json.loads(manifest_bytes.decode("utf-8-sig"))
        except Exception as exc:
            logger.warning(f"music_guess {source_name} manifest 获取失败：{exc}")
            return None
        info = _validate_manifest(obj)
        if info is None:
            logger.warning(f"music_guess {source_name} manifest 校验未通过")
            return None
        return {
            "source": source_name,
            "urls": urls,
            "manifest_bytes": manifest_bytes,
            **info,
        }

    def _commit_cache(
        self, manifest_bytes: bytes, songs_bytes: bytes, sha256_hex: str
    ) -> bool:
        """内容寻址提交缓存：先写 songs-<sha256>.txt，最后原子替换 manifest.json。

        manifest.json 是唯一提交点：替换成功之前，旧 manifest 始终指向旧的
        有效歌曲文件，因此歌曲写入失败、manifest 替换失败、甚至更新流程在两步
        之间被超时取消，都不会破坏旧有效缓存。两个 os.replace 各自仅保证
        单文件原子，不构成跨文件事务。
        """
        data_dir = self._data_dir
        if data_dir is None:
            return False
        try:
            _atomic_write(data_dir / f"songs-{sha256_hex}.txt", songs_bytes)
            _atomic_write(data_dir / MANIFEST_FILENAME, manifest_bytes)
        except Exception:
            logger.exception("music_guess 曲库缓存写入失败")
            return False
        self._cleanup_orphan_cache(sha256_hex)
        return True

    def _cleanup_orphan_cache(self, current_sha256: str) -> None:
        """提交成功后 best-effort 清理未被 manifest 引用的缓存文件。"""
        data_dir = self._data_dir
        if data_dir is None:
            return
        keep = f"songs-{current_sha256}.txt"
        try:
            leftovers = _cache_files(data_dir)
        except OSError:
            return
        for item in leftovers:
            if item.name in (MANIFEST_FILENAME, keep):
                continue
            try:
                item.unlink()
            except OSError:
                pass

    def _start_game(self, group_id: str) -> str:
        if group_id in self.games:
            return "本群已经有一局开字母正在进行。"

        if self.song_load_error:
            return f"无法开始游戏：{self.song_load_error}"

        if len(self.song_pool) < GAME_SONG_COUNT:
            return (
                f"无法开始游戏：曲库有效歌曲只有 {len(self.song_pool)} 首，"
                f"至少需要 {GAME_SONG_COUNT} 首。"
            )

        selected = random.sample(self.song_pool, GAME_SONG_COUNT)
        state = GameState(songs=[RoundSong(title=title) for title in selected])
        self.games[group_id] = state

        return (
            "开字母开始！\n\n"
            f"{self._render_board(state)}\n\n"
            "以下操作均需 @机器人：\n"
            "开字符：开 A / 开 7 / 开 桜\n"
            "猜歌曲：3 Credits（或 曲 3 Credits）\n"
            "结束：结束开字母"
        )

    def _open_character(self, group_id: str, char: str) -> str:
        state = self.games.get(group_id)
        if state is None:
            return "本群当前没有进行中的开字母游戏。请 @机器人 后发送「开字母」开始一局。"

        if not self._is_openable_char(char):
            return "一次只能开一个字母或数字，例如：@机器人 开 A / 开 7 / 开 桜。"

        key = char.casefold()
        display_char = char.upper() if char.isalpha() else char

        if key in state.opened_chars:
            return f"{display_char} 已经开过了。"

        state.opened_chars.add(key)

        exists = any(
            any(
                self._is_secret_char(ch) and ch.casefold() == key
                for ch in song.title
            )
            for song in state.songs
        )

        if not exists:
            return f"本局没有 {display_char}。"

        return f"已开启 {display_char}：\n\n{self._render_board(state)}"

    def _guess_song(self, group_id: str, index: int, answer: str) -> str:
        state = self.games.get(group_id)
        if state is None:
            return "本群当前没有进行中的开字母游戏。"

        song = state.songs[index - 1]

        if song.guessed:
            return f"第 {index} 首已经猜出来了：{song.title}"

        if self._normalize_answer(answer) != self._normalize_answer(song.title):
            return "不对。"

        song.guessed = True

        if all(item.guessed for item in state.songs):
            answers = self._render_answers(state)
            del self.games[group_id]
            return (
                f"答对了！\n\n{index}. ✓ {song.title}\n\n"
                "全部歌曲都猜出来了，游戏结束！\n\n"
                f"本局答案：\n{answers}"
            )

        return f"答对了！\n\n{self._render_board(state)}"

    def _end_game(self, group_id: str) -> str:
        state = self.games.pop(group_id, None)
        if state is None:
            return "本群当前没有进行中的开字母游戏。"

        return f"本局结束，答案：\n\n{self._render_answers(state)}"

    @staticmethod
    def _normalize_input(text: str) -> str:
        """用于指令解析：兼容常见全角字符，并去除首尾空白。"""
        return unicodedata.normalize("NFKC", text or "").strip()

    @staticmethod
    def _chain_plain_text(event: AstrMessageEvent) -> str:
        """拼接消息链顶层 Plain 段文本；WakingCheck 不会改写消息链。"""
        return "".join(
            seg.text for seg in event.get_messages() if isinstance(seg, Plain)
        )

    @staticmethod
    def _normalize_answer(text: str) -> str:
        """
        答案匹配规则：
        - Unicode NFKC 规范化
        - 英文字母大小写不敏感
        - 首尾空白忽略
        - 连续空白折叠成一个空格
        - 弯引号 ’ ‘ “ ” 视为对应直引号（手机输入法会自动转换）
        - 其余标点必须匹配
        """
        normalized = unicodedata.normalize("NFKC", text or "").casefold()
        for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"')):
            normalized = normalized.replace(curly, straight)
        return " ".join(normalized.split())

    @staticmethod
    def _is_secret_char(ch: str) -> bool:
        """参与遮罩和开字母的字符：所有 Unicode 字母（含英文、中文、日文），以及 ASCII 数字 0-9。"""
        return ch.isalpha() or (ch.isascii() and ch.isdigit())

    def _is_openable_char(self, text: str) -> bool:
        return len(text) == 1 and self._is_secret_char(text)

    def _mask_title(self, title: str, opened_chars: set[str]) -> str:
        result: list[str] = []

        for ch in title:
            if self._is_secret_char(ch):
                if ch.casefold() in opened_chars:
                    result.append(ch)
                else:
                    result.append(MASK_CHAR)
            else:
                # 空格、标点、符号，以及非 ASCII 数字等其他字符直接显示。
                result.append(ch)

        return "".join(result)

    def _render_board(self, state: GameState) -> str:
        lines: list[str] = []

        for index, song in enumerate(state.songs, start=1):
            if song.guessed:
                display = f"✓ {song.title}"
            else:
                display = self._mask_title(song.title, state.opened_chars)
            lines.append(f"{index}. {display}")

        return "\n".join(lines)

    @staticmethod
    def _render_answers(state: GameState) -> str:
        return "\n".join(
            f"{index}. {song.title}"
            for index, song in enumerate(state.songs, start=1)
        )

    async def terminate(self):
        """插件卸载/停用/重载时清理内存中的游戏状态。"""
        self.games.clear()
