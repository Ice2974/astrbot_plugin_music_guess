# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""消息分发边界测试（仅标准库，不依赖真实 AstrBot 环境）。

通过最小桩模块导入 main.py，验证：
- 默认模式无游戏时仅认领「开字母」，快捷格式放行；
- 游戏进行中开字符 / 猜歌 / 结束被认领并 stop_event；
- 独占模式接管无法识别消息，/ 指令放行；
- A / B 群游戏状态隔离下相同格式的认领与放行；
- priority=1 声明在最底部装饰器上（源码级断言）；
- AtBotFilter.filter / _normalize_answer / _mask_title 的直接行为。

无法覆盖真实 AstrBot 的 handler 注册与多插件执行顺序，
该部分需按 README 人工验收步骤在实机验证。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = REPO_ROOT / "main.py"

GAME_TITLES = [
    "Credits",
    "World Vanquisher",
    "千本桜",
    "Fracture Ray",
    "Grievous Lady",
    "Tempestissimo",
    "Arghena",
    "Testify",
]


def _install_astrbot_stubs() -> None:
    """安装导入 main.py 所需的最小 astrbot 桩模块；已存在时跳过。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    components = types.ModuleType("astrbot.api.message_components")
    star = types.ModuleType("astrbot.api.star")

    class _Logger:
        def info(self, msg): ...
        def warning(self, msg): ...
        def exception(self, msg): ...
        def debug(self, msg): ...

    class CustomFilter:
        pass

    class EventMessageType:
        GROUP_MESSAGE = "group_message"

    class PlatformAdapterType:
        QQOFFICIAL = "qq_official"

    def _identity_decorator(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    class At:
        def __init__(self, qq):
            self.qq = qq

    class Plain:
        def __init__(self, text):
            self.text = text

    class Star:
        def __init__(self, context):
            self.context = context

    api.AstrBotConfig = dict
    api.logger = _Logger()
    event.AstrMessageEvent = object
    event.filter = filter_mod
    filter_mod.CustomFilter = CustomFilter
    filter_mod.EventMessageType = EventMessageType
    filter_mod.PlatformAdapterType = PlatformAdapterType
    filter_mod.custom_filter = _identity_decorator
    filter_mod.event_message_type = _identity_decorator
    filter_mod.platform_adapter_type = _identity_decorator
    components.At = At
    components.Plain = Plain
    star.Context = object
    star.Star = Star
    astrbot.api = api
    astrbot.api.event = event
    astrbot.api.event.filter = filter_mod
    astrbot.api.message_components = components
    astrbot.api.star = star
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.message_components": components,
            "astrbot.api.star": star,
        }
    )


def _load_main():
    _install_astrbot_stubs()
    spec = importlib.util.spec_from_file_location("music_guess_main", MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    # dataclass(slots=True) 会通过 sys.modules 查找所属模块，必须先注册。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


main = _load_main()


class StubEvent:
    """on_group_message 用到的最小事件接口。"""

    def __init__(self, text: str, group_id: str):
        self.message_str = text
        self._chain = [main.Plain(text)] if text else []
        self._group_id = group_id
        self.stopped = False

    def get_messages(self):
        return self._chain

    def get_group_id(self) -> str:
        return self._group_id

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> str:
        return text


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.MusicGuessPlugin(
            context=None, config={"exclusive_mode": False}
        )
        # 曲库桩：_start_game 依赖至少 8 首有效歌曲。
        self.plugin.song_pool = [f"Song {i}" for i in range(10)]
        self.plugin.song_load_error = None

    def _seed_game(self, group_id: str = "A") -> None:
        self.plugin.games[group_id] = main.GameState(
            songs=[main.RoundSong(title=title) for title in GAME_TITLES]
        )

    def _run_handler(self, text: str, group_id: str = "A"):
        event = StubEvent(text, group_id)

        async def drive():
            return [item async for item in self.plugin.on_group_message(event)]

        return event, asyncio.run(drive())

    # ---- 默认模式、无游戏 ----

    def test_public_no_game_start_claimed(self):
        reply = self.plugin._dispatch_public("A", "开字母")
        self.assertIsInstance(reply, str)
        self.assertIn("A", self.plugin.games)

    def test_public_no_game_quick_formats_pass(self):
        for text in [
            "开 A",
            "开A",
            "开7",
            "开 桜",
            "3 Credits",
            "1. Credits",
            "曲 3 Credits",
            "结束游戏",
            "结束开字母",
            "今天天气怎么样",
            "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(self.plugin._dispatch_public("A", text))

    def test_handler_no_game_passthrough_and_start(self):
        event, results = self._run_handler("开 A")
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

        event, results = self._run_handler("3 Credits")
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

        event, results = self._run_handler("结束游戏")
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

        event, results = self._run_handler("/help")
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)

        event, results = self._run_handler("开字母")
        self.assertEqual(len(results), 1)
        self.assertTrue(event.stopped)

    # ---- 默认模式、游戏进行中 ----

    def test_public_game_active_claims(self):
        self._seed_game()
        self.assertIsInstance(self.plugin._dispatch_public("A", "开 A"), str)
        self.assertIsInstance(self.plugin._dispatch_public("A", "3 Credits"), str)
        end_reply = self.plugin._dispatch_public("A", "结束游戏")
        self.assertIsInstance(end_reply, str)
        self.assertNotIn("A", self.plugin.games)

    def test_handler_game_active_claims_and_stops(self):
        self._seed_game()

        event, results = self._run_handler("开 A")
        self.assertTrue(event.stopped)
        self.assertEqual(len(results), 1)

        event, results = self._run_handler("1 Credits")
        self.assertTrue(event.stopped)
        self.assertIn("答对了", results[0])

        event, results = self._run_handler("今天天气怎么样")
        self.assertFalse(event.stopped)
        self.assertEqual(results, [])

        event, results = self._run_handler("/help")
        self.assertFalse(event.stopped)
        self.assertEqual(results, [])

    def test_public_game_all_guessed_auto_end(self):
        self._seed_game()
        for index, title in enumerate(GAME_TITLES, start=1):
            reply = self.plugin._dispatch_public("A", f"{index} {title}")
            self.assertIsInstance(reply, str)
        self.assertIn("全部歌曲都猜出来了", reply)
        self.assertNotIn("A", self.plugin.games)

    # ---- 独占模式 ----

    def _make_exclusive_plugin(self):
        return main.MusicGuessPlugin(
            context=None, config={"exclusive_mode": True}
        )

    def test_exclusive_no_game_takes_over(self):
        self.plugin = self._make_exclusive_plugin()

        event, results = self._run_handler("开 A")
        self.assertTrue(event.stopped)
        self.assertIn("没有进行中的开字母游戏", results[0])

        event, results = self._run_handler("3 Credits")
        self.assertTrue(event.stopped)
        self.assertIn("没有进行中的开字母游戏", results[0])

        event, results = self._run_handler("/help")
        self.assertFalse(event.stopped)
        self.assertEqual(results, [])

    def test_exclusive_game_active_unknown_taken_over(self):
        self.plugin = self._make_exclusive_plugin()
        self._seed_game()

        event, results = self._run_handler("今天天气怎么样")
        self.assertTrue(event.stopped)
        self.assertIn("无法识别", results[0])

        event, results = self._run_handler("/help")
        self.assertFalse(event.stopped)
        self.assertEqual(results, [])

    # ---- 群隔离路由：A 群有游戏、B 群无游戏 ----

    def test_group_isolation_same_guess_format(self):
        self._seed_game("A")

        # B 群无游戏：相同猜歌 / 开字符格式放行，不停止传播。
        event_b, results_b = self._run_handler("1 Credits", group_id="B")
        self.assertEqual(results_b, [])
        self.assertFalse(event_b.stopped)
        self.assertIsNone(self.plugin._dispatch_public("B", "开 A"))

        # B 群的放行操作不改变 A 群状态。
        self.assertIn("A", self.plugin.games)
        self.assertFalse(self.plugin.games["A"].songs[0].guessed)

        # A 群有游戏：相同格式被认领并停止传播。
        event_a, results_a = self._run_handler("1 Credits", group_id="A")
        self.assertTrue(event_a.stopped)
        self.assertIn("答对了", results_a[0])

    # ---- priority 声明位置（源码级断言） ----

    def test_priority_declared_on_bottom_decorator(self):
        source = MAIN_PY.read_text(encoding="utf-8")
        self.assertIn(
            "@filter.platform_adapter_type("
            "filter.PlatformAdapterType.QQOFFICIAL, priority=1)",
            source,
        )


# ---- AtBotFilter：@机器人 判定 ----


class FilterEvent:
    """AtBotFilter.filter 用到的最小事件接口。"""

    def __init__(self, self_id, segments):
        self._self_id = self_id
        self._segments = segments

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return self._segments


class AtBotFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = main.AtBotFilter()

    def _check(self, self_id, *segments):
        return self.filter.filter(FilterEvent(self_id, list(segments)), None)

    def test_at_self_passes(self):
        self.assertTrue(self._check("10001", main.At(qq="10001")))

    def test_int_qq_matches_string_self_id(self):
        self.assertTrue(self._check("10001", main.At(qq=10001)))

    def test_at_other_user_fails(self):
        self.assertFalse(self._check("10001", main.At(qq="10002")))

    def test_at_all_fails(self):
        self.assertFalse(self._check("10001", main.At(qq="all")))

    def test_plain_only_fails(self):
        self.assertFalse(self._check("10001", main.Plain("开字母")))

    def test_mixed_segments_at_self_passes(self):
        self.assertTrue(
            self._check(
                "10001", main.Plain("开 A"), main.At(qq="10001")
            )
        )

    def test_empty_self_id_fails(self):
        self.assertFalse(self._check("", main.At(qq="10001")))
        self.assertFalse(self._check(None, main.At(qq="10001")))


# ---- _normalize_answer：答案匹配口径 ----


class NormalizeAnswerTests(unittest.TestCase):
    normalize = staticmethod(main.MusicGuessPlugin._normalize_answer)

    def test_case_insensitive(self):
        self.assertEqual(self.normalize("Credits"), self.normalize("credits"))
        self.assertEqual(self.normalize("CREDITS"), self.normalize("credits"))

    def test_nfkc_fullwidth_equals_halfwidth(self):
        self.assertEqual(self.normalize("ｃｒｅｄｉｔｓ"), self.normalize("credits"))

    def test_curly_quotes_equal_straight(self):
        self.assertEqual(self.normalize("Lyrical ’94"), self.normalize("Lyrical '94"))
        self.assertEqual(self.normalize("“x”"), self.normalize('"x"'))
        self.assertEqual(self.normalize("‘x’"), self.normalize("'x'"))

    def test_whitespace_collapsed_and_stripped(self):
        self.assertEqual(self.normalize("  Alpha   Ray "), self.normalize("alpha ray"))

    def test_punctuation_must_match(self):
        self.assertNotEqual(self.normalize("Alpha Ray"), self.normalize("Alpha-Ray"))
        self.assertNotEqual(self.normalize("Track 7!"), self.normalize("Track 7"))

    def test_empty_and_none_are_safe(self):
        self.assertEqual(self.normalize(""), "")
        self.assertEqual(self.normalize(None), "")


# ---- _mask_title：遮罩规则 ----


class MaskTitleTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.MusicGuessPlugin(context=None)

    def mask(self, title, opened=None):
        return self.plugin._mask_title(title, set(opened or []))

    def test_letters_masked(self):
        self.assertEqual(self.mask("Credits"), "•••••••")

    def test_spaces_shown(self):
        self.assertEqual(self.mask("World Vanquisher"), "••••• ••••••••••")

    def test_cjk_masked(self):
        self.assertEqual(self.mask("千本桜"), "•••")

    def test_punctuation_shown(self):
        self.assertEqual(self.mask("LUDICROUS+"), "•••••••••+")

    def test_ascii_digits_masked(self):
        self.assertEqual(self.mask("Track 7"), "••••• •")

    def test_fullwidth_digits_shown(self):
        self.assertEqual(self.mask("Mix ２４"), "••• ２４")

    def test_opened_char_reveals_both_cases(self):
        self.assertEqual(self.mask("Alpha Beta", {"a"}), "A•••a •••a")

    def test_opened_cjk_char_revealed(self):
        self.assertEqual(self.mask("千本桜", {"桜"}), "••桜")


if __name__ == "__main__":
    unittest.main()
