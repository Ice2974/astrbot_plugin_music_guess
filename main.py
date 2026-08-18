# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


PLUGIN_VERSION = "0.2.0"
SONGS_FILENAME = "songs.txt"
GAME_SONG_COUNT = 8

# QQ 官方客户端会把连续半角 * 按 Markdown 语法解释。
# 使用紧凑的 Bullet 字符 • 作为遮罩，既避开 Markdown，又能明显保留单词空格。
MASK_CHAR = "•"

START_COMMAND = "开字母"
END_COMMANDS = {"结束开字母", "结束游戏"}

OPEN_PATTERN = re.compile(r"^开\s+(.+?)\s*$")
GUESS_PATTERN = re.compile(r"^(?:曲\s*)?([1-8])\s+(.+?)\s*$")


@dataclass(slots=True)
class RoundSong:
    title: str
    guessed: bool = False


@dataclass(slots=True)
class GameState:
    songs: list[RoundSong]
    opened_chars: set[str] = field(default_factory=set)


class MusicGuessPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.games: dict[str, GameState] = {}
        self.song_pool: list[str] = []
        self.song_load_error: str | None = None
        self.exclusive_mode = (
            bool(config.get("exclusive_mode", False)) if config else False
        )

    async def initialize(self):
        """插件初始化：加载本地曲库。曲库异常不会阻止插件本身加载。"""
        self._load_songs()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    async def on_group_message(self, event: AstrMessageEvent):
        """
        处理 QQ 官方机器人群聊中的开字母游戏消息。

        - /xxx 始终保留给 AstrBot 内置/其他插件命令处理；
        - exclusive_mode=false（默认）：只处理明确的开字母命令，
          无法识别的消息直接放行，不回复、不停止传播；
        - exclusive_mode=true：其余普通群聊文本全部由本插件接管，
          无法识别时给出玩法提示，并 stop_event()，避免进入 LLM。
        """
        text = self._normalize_input(event.message_str)

        # 保留 AstrBot 的 /help、/plugin 等系统/插件命令。
        if text.startswith("/"):
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
        """exclusive_mode=false：只处理明确的开字母命令；返回 None 表示放行。"""
        if not text:
            return None

        if text == START_COMMAND:
            return self._start_game(group_id)

        if text in END_COMMANDS:
            return self._end_game(group_id)

        open_match = OPEN_PATTERN.fullmatch(text)
        if open_match:
            candidate = self._normalize_input(open_match.group(1))
            return self._open_character(group_id, candidate)

        # 猜歌只在已有进行中的游戏时才认定为插件消息，
        # 否则 "3 Credits" 这样可能是普通聊天的文本会被误拦截。
        if group_id in self.games:
            guess_match = GUESS_PATTERN.fullmatch(text)
            if guess_match:
                index = int(guess_match.group(1))
                answer = guess_match.group(2)
                return self._guess_song(group_id, index, answer)

        return None

    def _dispatch_exclusive(self, group_id: str, text: str) -> str:
        """exclusive_mode=true：所有非 / 普通群消息都由本插件接管。"""
        reply = self._dispatch_public(group_id, text)
        if reply is not None:
            return reply

        if not text:
            return self._usage_message(group_id)

        if group_id in self.games:
            # 游戏进行中：任何未识别普通文本都不要放到 LLM。
            return (
                "无法识别这条游戏消息。\n\n"
                "开字符：开 A / 开 7 / 开 桜\n"
                "猜歌曲：3 Credits（也可：曲 3 Credits）\n"
                "结束：结束开字母"
            )

        # 没有进行中的游戏：本插件也不承担 AI 对话。
        return (
            "本群当前没有进行中的开字母游戏。\n"
            "发送「开字母」开始一局。\n"
            "AstrBot 系统指令可使用 /help。"
        )

    def _usage_message(self, group_id: str) -> str:
        if group_id in self.games:
            return (
                "开字符：开 A / 开 7 / 开 桜\n"
                "猜歌曲：3 Credits（也可：曲 3 Credits）\n"
                "结束：结束开字母"
            )
        return "发送「开字母」开始一局。AstrBot 系统指令可使用 /help。"

    def _load_songs(self) -> None:
        """读取 songs.txt；出错时只记录友好错误，不抛出异常导致插件崩溃。"""
        path = Path(__file__).resolve().with_name(SONGS_FILENAME)

        try:
            if not path.is_file():
                self.song_pool = []
                self.song_load_error = f"曲库文件不存在：{SONGS_FILENAME}"
                logger.warning(self.song_load_error)
                return

            # utf-8-sig 同时兼容普通 UTF-8 和带 BOM 的 UTF-8 文本。
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            self.song_pool = []
            self.song_load_error = f"曲库文件读取失败：{exc}"
            logger.warning(self.song_load_error)
            return

        songs: list[str] = []
        seen: set[str] = set()

        for line in lines:
            title = line.strip()
            if not title or title in seen:
                continue
            seen.add(title)
            songs.append(title)

        self.song_pool = songs

        if len(songs) < GAME_SONG_COUNT:
            self.song_load_error = (
                f"曲库有效歌曲只有 {len(songs)} 首，"
                f"至少需要 {GAME_SONG_COUNT} 首。"
            )
            logger.warning(self.song_load_error)
            return

        self.song_load_error = None
        logger.info(f"music_guess 曲库加载完成：{len(songs)} 首")

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
            "开字符：开 A / 开 7 / 开 桜\n"
            "猜歌曲：3 Credits（也可：曲 3 Credits）\n"
            "结束：结束开字母"
        )

    def _open_character(self, group_id: str, char: str) -> str:
        state = self.games.get(group_id)
        if state is None:
            return "本群当前没有进行中的开字母游戏。发送「开字母」开始一局。"

        if not self._is_openable_char(char):
            return "一次只能开一个字母或数字，例如：开 A / 开 7 / 开 桜。"

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
    def _normalize_answer(text: str) -> str:
        """
        答案匹配规则：
        - Unicode NFKC 规范化
        - 英文字母大小写不敏感
        - 首尾空白忽略
        - 连续空白折叠成一个空格
        - 标点必须匹配
        """
        normalized = unicodedata.normalize("NFKC", text or "").casefold()
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
