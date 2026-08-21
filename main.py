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
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star


SONGS_DIR_NAME = "songs"
GAME_SONG_COUNT = 8
# 单个曲库文件大小上限：防止误放入超大自定义 txt 在初始化时阻塞事件循环、
# 占用过多内存。内置最大曲库约 50KB，2MB 已留足余量。
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

# 已知音游曲库的官方显示名；未知 / 自定义曲库显示文件名。
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
# 曲库加载：模块级纯函数（扫描 / 过滤 / 解析）
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


def _scan_library_files(songs_dir: Path) -> list[str]:
    """扫描曲库目录，返回可用曲库文件名列表（按文件名排序）。

    只识别直接子级的 .txt 常规文件，扩展名大小写不敏感（.TXT / .Txt
    同样识别，兼容用户手动添加）；子目录不递归，非 txt 扩展名与
    "." 开头隐藏文件忽略，避免误加载无关文件。目录不存在或列举
    失败返回空列表，由调用方决定错误提示。
    """
    try:
        entries = list(songs_dir.iterdir())
    except OSError:
        return []
    names = [
        entry.name
        for entry in entries
        if entry.is_file()
        and not entry.name.startswith(".")
        and entry.suffix.lower() == ".txt"
        and entry.stem
    ]
    return sorted(names)


def _library_stem(filename: str) -> str:
    """曲库文件名（扩展名任意大小写）转曲库标识：去掉末尾的 .txt（4 个字符）。"""
    return filename[: -len(".txt")]


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
        # 曲库启用配置（enabled_libraries）在 _load_songs 中按三态语义读取；
        # 注入面板选项时需要访问 schema，因此保留引用。
        self._config = config
        # initialize() 扫描得到的曲库文件名列表；面板选项注入与曲库加载共用这一份结果。
        self._available_libraries: list[str] = []

    async def initialize(self):
        """插件初始化：扫描曲库 → 注入面板选项 → 按配置加载曲库。

        扫描只执行一次，配置选项与加载逻辑使用同一份结果；
        曲库或目录的任何异常都不会阻止插件本身加载。
        """
        available = _scan_library_files(self._songs_dir())
        self._available_libraries = available
        self._inject_library_options(available)
        self._load_songs(available)

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

    # ---- 曲库目录与配置选项注入 ----

    @staticmethod
    def _songs_dir() -> Path:
        return Path(__file__).resolve().parent / SONGS_DIR_NAME

    def _inject_library_options(self, library_files: list[str]) -> None:
        """把扫描到的曲库写入配置 schema 的 options / labels，供 WebUI 复选框展示。

        AstrBot v4.27.3 的 options 只能来自静态 _conf_schema.json，没有官方
        动态生成机制；此处改写 AstrBotConfig 实例持有的 schema dict（WebUI
        每次拉取配置读取同一引用），面板保存配置后 AstrBot 会热重载插件并
        重新执行 initialize 注入。该行为依赖 AstrBot 内部实现，全部包裹在
        try/except 中：失败仅记日志，面板退化为普通 list 手动填写，不影响
        游戏功能。
        """
        config = self._config
        if config is None or not library_files:
            return
        try:
            schema = getattr(config, "schema", None)
            item = schema.get("enabled_libraries") if isinstance(schema, dict) else None
            if not isinstance(item, dict):
                return
            stems = [_library_stem(name) for name in library_files]
            item["options"] = stems
            item["labels"] = [
                LIBRARY_DISPLAY_NAMES.get(stem, stem) for stem in stems
            ]
        except Exception:
            logger.debug("music_guess 曲库配置选项注入失败，面板将显示为手动填写列表")

    # ---- 曲库加载 ----

    def _load_songs(self, available_files: list[str]) -> None:
        """按用户配置合并启用曲库，构建歌曲池；对外只更新 song_pool / song_load_error。

        enabled_libraries 三态语义（不用 if not enabled 之类的合并判断）：
        - 键不存在：使用默认启用库 DEFAULT_ENABLED_LIBRARIES；
        - 键存在但类型非法：记警告后按默认处理；
        - 键存在且为字符串列表（含空列表）：完全按用户配置执行，
          空列表表示用户主动关闭所有曲库，不回退默认。
        曲库文件本身从不修改；跨曲库重复歌曲按答案匹配口径
        （_normalize_answer）在内存中去重，保留首次出现。
        """
        songs_dir = self._songs_dir()
        available_stems = [_library_stem(name) for name in available_files]
        if not available_stems:
            self.song_pool = []
            if songs_dir.is_dir():
                self.song_load_error = (
                    f"曲库目录 {SONGS_DIR_NAME}/ 中没有可用的 txt 曲库文件。"
                )
            else:
                self.song_load_error = f"曲库目录不存在：{SONGS_DIR_NAME}/"
            logger.warning(self.song_load_error)
            return

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
        enabled_files = [
            name
            for name, stem in zip(available_files, available_stems)
            if stem in enabled_set
        ]
        if not enabled_files:
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
        for name in enabled_files:
            titles = _read_library_titles(songs_dir / name)
            if titles is None:
                logger.warning(
                    f"music_guess 曲库 {name} 读取失败"
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
                summaries.append(f"{name}={kept}")
            else:
                logger.warning(
                    f"music_guess 曲库 {name} 没有可用曲目"
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
