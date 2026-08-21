# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star


GAME_SONG_COUNT = 8
# 单个内置曲库文件大小上限：避免异常文件在初始化时占用过多资源。
MAX_LIBRARY_FILE_BYTES = 2 * 1024 * 1024

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

# 默认启用的曲库（值为曲库文件名去掉 .txt 后缀）。
DEFAULT_ENABLED_LIBRARIES = ["phigros", "arcaea", "musedash", "maimai"]

# 插件内置曲库的固定位置。不扫描 songs/，只有此处列出的文件会被识别。
BUILTIN_LIBRARY_FILES = {
    "phigros": "songs/phigros.txt",
    "arcaea": "songs/arcaea.txt",
    "musedash": "songs/musedash.txt",
    "maimai": "songs/maimai.txt",
    "cytus": "songs/cytus.txt",
    "cytus2": "songs/cytus2.txt",
    "deemo": "songs/deemo.txt",
    "djmax": "songs/djmax.txt",
    "lanota": "songs/lanota.txt",
    "rotaeno": "songs/rotaeno.txt",
    "chunithm": "songs/chunithm.txt",
    "soundvoltex": "songs/soundvoltex.txt",
    "pjsekai": "songs/pjsekai.txt",
    "adofai": "songs/adofai.txt",
    "beatsaber": "songs/beatsaber.txt",
}

# 内置音游曲库的官方显示名。
LIBRARY_DISPLAY_NAMES = {
    "phigros": "Phigros",
    "arcaea": "Arcaea",
    "musedash": "Muse Dash",
    "maimai": "maimai",
    "cytus": "Cytus",
    "cytus2": "Cytus II",
    "deemo": "Deemo",
    "djmax": "DJMAX",
    "lanota": "Lanota",
    "rotaeno": "Rotaeno",
    "chunithm": "CHUNITHM",
    "soundvoltex": "SOUND VOLTEX",
    "pjsekai": "Project SEKAI",
    "adofai": "A Dance of Fire and Ice",
    "beatsaber": "Beat Saber",
}

# 曲目标题必须至少含一个 ASCII 英文字母或数字（A-Z / a-z / 0-9）才适合开字母玩法。
# 全角数字、罗马数字、带圈数字、纯中文、纯日文、纯符号等标题不含上述字符，一律过滤。
_PLAYABLE_PATTERN = re.compile(r"[A-Za-z0-9]")

@dataclass(slots=True)
class RoundSong:
    title: str
    guessed: bool = False


@dataclass(slots=True)
class GameState:
    songs: list[RoundSong]
    opened_chars: dict[str, str] = field(default_factory=dict)


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
# 曲库加载：模块级纯函数（过滤 / 解析）
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


def _title_is_playable(title: str) -> bool:
    """标题是否适合开字母玩法：至少含一个 ASCII 英文字母（A-Z / a-z）或数字（0-9）。

    纯中文、纯日文、仅全角数字 / 罗马数字 / 带圈数字、纯符号等标题返回
    False，加载时过滤。保留的标题必然含遮罩字符（_is_secret_char），
    玩法才能成立；被过滤的标题本就无法有效开局。
    """
    return bool(_PLAYABLE_PATTERN.search(title))


def _read_library_titles(path: Path) -> list[str] | None:
    """读取单个曲库文件并过滤，返回适合玩法的曲名列表。

    文件不可读、非 UTF-8（兼容 BOM）或超过 MAX_LIBRARY_FILE_BYTES
    返回 None，由调用方跳过该库；空行忽略、完全相同的重复行只保留
    一次（与单文件曲库时代一致）。
    """
    try:
        if path.stat().st_size > MAX_LIBRARY_FILE_BYTES:
            return None
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    return [title for title in _parse_song_titles(text) if _title_is_playable(title)]


class MusicGuessPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.games: dict[str, GameState] = {}
        self.song_pool: list[str] = []
        self.song_load_error: str | None = None
        self.exclusive_mode = (
            bool(config.get("exclusive_mode", False)) if config else False
        )
        # AstrBot 对每条消息创建独立协程，同群消息会并发进入 handler。
        # 每个群一把锁，把「状态修改 → 回复经管道实际发出」整体串行，
        # 后一条同群消息必须等前一条的回复发出后才开始处理。
        # 锁与持有者标记按 group_id 永久保留、不淘汰：等待中的协程持有
        # 旧锁对象引用，淘汰后同群可能同时出现两把锁、互斥失效；单条
        # 目只有一个 asyncio.Lock，增长上界为被 @ 过的群数，插件重载
        # 即重置。
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, object] = {}
        # 曲库启用配置（enabled_libraries）在 _load_songs 中按三态语义读取。
        self._config = config

    async def initialize(self):
        """插件初始化：按固定的内置曲库路径加载歌曲。"""
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

        同群串行化：AstrBot 为每条消息创建独立协程（event_bus 的
        create_task），同群两条消息可能并发进入本 handler，导致后一条
        消息的题板混入前一条刚写入、回复尚未发出的状态，且回复顺序与
        处理顺序颠倒。因此用每群一把锁把「dispatch → 回复经管道实际
        发出」整体作为临界区：回复仍走 yield 结果，由管道的
        ResultDecorate / RespondStage 完成装饰（@发送者、引用回复、
        回复前缀、t2i 等）、OnDecoratingResult / OnAfterMessageSent
        钩子与实际发送；洋葱模型下发送在 handler 挂起期间完成，锁覆盖
        到生成器恢复并停止传播之后，同群下一条消息才开始 dispatch。

        顺序边界：锁保证的是「消息进入本 handler 的顺序」内严格串行、
        互斥、状态不混入。消息进入 handler 前若被管道前置阶段重排
        （如内容安全远程检查、限流等待等可变延迟挂起），处理顺序可能
        不等于平台接收顺序；此时互斥与状态不混入仍然成立。插件层无法
        获得可靠的上游接收序号，不承诺平台绝对接收顺序。不同群使用
        不同的锁，互不阻塞。

        stop_event() 在 yield 恢复后（即回复发送完成、生成器收尾前）
        调用，与官方内置插件同款顺序：star_request 在 handler 生成器
        结束后检查 is_stopped 并跳过后续 handler，ProcessStage 的 LLM
        兜底被 stop_event 写入的空 STOP 结果拦截，认领 / 放行消息的
        拦截语义与原先一致。

        锁释放：正常路径由 finally 释放；若调度器在 handler 挂起期间
        不再恢复生成器（事件被外部 stop、装饰钩子中断）或任务被取消，
        由管道任务完成回调按持有者标记确定释放，不依赖异步生成器
        finalizer。
        """
        text = self._normalize_input(event.message_str)

        # 保留 AstrBot 的 /help、/plugin 等系统/插件命令。
        if self._normalize_input(self._chain_plain_text(event)).startswith("/"):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        lock = self._group_lock(group_id)
        await lock.acquire()
        token = object()
        self._holders[group_id] = token
        # 兜底释放：管道任务结束时若锁仍由本消息持有（生成器被调度器
        # 遗弃、任务取消），由完成回调确定释放。task 为 None（未被
        # Task 驱动）时不存在遗弃路径，仅靠 finally。
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(lambda _task: self._release_lock(group_id, token))

        try:
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

            if reply:
                # 洋葱模型：本次挂起期间管道完成装饰、钩子与实际发送；
                # 锁必须覆盖到发送完成（生成器恢复），否则同群下一条
                # 消息的题板会混入本条刚写入、尚未发出的状态。
                yield event.plain_result(reply)

            # 回复发送完成后（生成器恢复）再停止传播：被认领的消息不会
            # 继续进入其它 handler / Agent / LLM。
            event.stop_event()
        finally:
            self._release_lock(group_id, token)

    def _group_lock(self, group_id: str) -> asyncio.Lock:
        """获取指定群的串行化锁；单事件循环内懒创建，无需额外保护。"""
        lock = self._group_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_locks[group_id] = lock
        return lock

    def _release_lock(self, group_id: str, token: object) -> None:
        """释放群锁，仅当当前持有者仍是该 token 时生效。

        正常路径由 handler 的 finally 调用；生成器被调度器遗弃或任务
        取消时由管道任务完成回调调用。两条路径可能都触发（回调也可能
        晚于新消息接管锁），token 判重保证恰好释放一次、不误释放后继
        消息的锁。
        """
        if self._holders.get(group_id) is not token:
            return
        del self._holders[group_id]
        lock = self._group_locks.get(group_id)
        if lock is not None and lock.locked():
            lock.release()

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
                "开字符：开 A / 开 7 / 开 少\n"
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
                "开字符：开 A / 开 7 / 开 少\n"
                "猜歌曲：3 Credits（或 曲 3 Credits）\n"
                "结束：结束开字母"
            )
        return "请 @机器人 后发送「开字母」开始一局。AstrBot 系统指令可使用 /help。"

    # ---- 曲库路径 ----

    @staticmethod
    def _plugin_dir() -> Path:
        return Path(__file__).resolve().parent

    # ---- 曲库加载 ----

    def _load_songs(self) -> None:
        """按用户配置合并启用曲库，构建歌曲池；对外只更新 song_pool / song_load_error。

        enabled_libraries 三态语义（不用 if not enabled 之类的合并判断）：
        - 键不存在：使用默认启用库 DEFAULT_ENABLED_LIBRARIES；
        - 键存在但类型非法：记警告后按默认处理；
        - 键存在且为字符串列表（含空列表）：完全按用户配置执行，
          空列表表示用户主动关闭所有曲库，不回退默认。
        曲库文件本身从不修改；跨曲库重复歌曲按答案匹配口径
        （_normalize_answer）在内存中去重，保留首次出现。
        """
        available_stems = list(BUILTIN_LIBRARY_FILES)

        enabled_cfg = (
            self._config.get("enabled_libraries")
            if self._config is not None
            else None
        )
        if enabled_cfg is None:
            enabled = list(DEFAULT_ENABLED_LIBRARIES)
        elif not isinstance(enabled_cfg, list) or not all(
            isinstance(stem, str) for stem in enabled_cfg
        ):
            logger.warning(
                "music_guess 配置项 enabled_libraries 类型非法，按默认曲库处理"
            )
            enabled = list(DEFAULT_ENABLED_LIBRARIES)
        else:
            enabled = enabled_cfg

        unknown = [stem for stem in enabled if stem not in available_stems]
        if unknown:
            logger.warning(
                "music_guess 配置启用了不存在的曲库，已忽略：" + ", ".join(unknown)
            )

        enabled_set = set(enabled)
        enabled_stems = [stem for stem in available_stems if stem in enabled_set]
        if not enabled_stems:
            self.song_pool = []
            display = "、".join(
                LIBRARY_DISPLAY_NAMES.get(stem, stem) for stem in available_stems
            )
            self.song_load_error = (
                "未启用任何曲库。请在 AstrBot 插件配置中勾选「启用曲库」"
                f"后重载插件（可用曲库：{display}）。"
            )
            logger.warning(self.song_load_error)
            return

        pool: list[str] = []
        seen_keys: set[str] = set()
        duplicates = 0
        summaries: list[str] = []
        plugin_dir = self._plugin_dir()
        for stem in enabled_stems:
            relative_path = BUILTIN_LIBRARY_FILES[stem]
            titles = _read_library_titles(plugin_dir / relative_path)
            if titles is None:
                logger.warning(
                    f"music_guess 曲库 {relative_path} 读取失败"
                    "（不可读、非 UTF-8 或超过 2MB 大小上限），已跳过"
                )
                continue
            kept = 0
            for title in titles:
                key = self._normalize_answer(title)
                if key in seen_keys:
                    duplicates += 1
                    continue
                seen_keys.add(key)
                pool.append(title)
                kept += 1
            if kept:
                summaries.append(f"{relative_path}={kept}")
            else:
                logger.warning(
                    f"music_guess 曲库 {relative_path} 没有可用曲目"
                    "（全部被过滤、为空或与其他曲库重复），已跳过"
                )

        if len(pool) < GAME_SONG_COUNT:
            self.song_pool = []
            self.song_load_error = (
                f"启用曲库合并后有效歌曲只有 {len(pool)} 首，"
                f"至少需要 {GAME_SONG_COUNT} 首。"
            )
            logger.warning(self.song_load_error)
            return

        self.song_pool = pool
        self.song_load_error = None
        logger.info(
            "music_guess 曲库加载完成："
            + ", ".join(summaries)
            + f"；跨曲库去重丢弃 {duplicates} 首，共 {len(pool)} 首"
        )

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
            "开字符：开 A / 开 7 / 开 少\n"
            "猜歌曲：3 Credits（或 曲 3 Credits）\n"
            "结束：结束开字母"
        )

    def _open_character(self, group_id: str, char: str) -> str:
        state = self.games.get(group_id)
        if state is None:
            return "本群当前没有进行中的开字母游戏。请 @机器人 后发送「开字母」开始一局。"

        if not self._is_openable_char(char):
            return "一次只能开一个字母或数字，例如：@机器人 开 A / 开 7 / 开 少。"

        key = char.casefold()
        display_char = char.upper() if char.isalpha() else char

        if key in state.opened_chars:
            return (
                f"{display_char} 已经开过了。\n\n"
                f"{self._render_opened_chars(state)}"
            )

        state.opened_chars[key] = display_char

        exists = any(
            any(
                self._is_secret_char(ch) and ch.casefold() == key
                for ch in song.title
            )
            for song in state.songs
        )

        if not exists:
            return (
                f"本局没有 {display_char}。\n\n"
                f"{self._render_opened_chars(state)}"
            )

        return (
            f"已开启 {display_char}：\n\n{self._render_board(state)}\n\n"
            f"{self._render_opened_chars(state)}"
        )

    def _guess_song(self, group_id: str, index: int, answer: str) -> str:
        state = self.games.get(group_id)
        if state is None:
            return "本群当前没有进行中的开字母游戏。"

        song = state.songs[index - 1]

        if song.guessed:
            return f"第 {index} 首已经猜出来了：{song.title}"

        if self._normalize_guess_answer(answer) != self._normalize_guess_answer(
            song.title
        ):
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
    def _normalize_guess_answer(text: str) -> str:
        """猜歌专用匹配口径：保留基础答案规范化，并忽略所有 Unicode 空白。

        跨曲库去重仍直接使用 _normalize_answer，不应用此处的空格增删
        宽松匹配，避免改变现有曲池合并行为。
        """
        return MusicGuessPlugin._normalize_answer(text).replace(" ", "")

    @staticmethod
    def _is_secret_char(ch: str) -> bool:
        """参与遮罩和开字母的字符：所有 Unicode 字母（含英文、中文、日文），以及 ASCII 数字 0-9。"""
        return ch.isalpha() or (ch.isascii() and ch.isdigit())

    def _is_openable_char(self, text: str) -> bool:
        return len(text) == 1 and self._is_secret_char(text)

    def _mask_title(self, title: str, opened_chars: Collection[str]) -> str:
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
    def _render_opened_chars(state: GameState) -> str:
        return "开字母历史：\n" + " ".join(state.opened_chars.values())

    @staticmethod
    def _render_answers(state: GameState) -> str:
        return "\n".join(
            f"{index}. {song.title}"
            for index, song in enumerate(state.songs, start=1)
        )

    async def terminate(self):
        """插件卸载/停用/重载时清理内存中的游戏状态。"""
        self.games.clear()
