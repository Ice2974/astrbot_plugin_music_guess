# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""游戏进度持久化测试（仅标准库，不依赖真实 AstrBot 环境）。

复用 test_dispatch 的 astrbot 桩模块导入 main.py，验证：
- 往返一致性：存档落盘 → 新实例恢复，题板 / 已猜标记 / 已开字符
  顺序（含 ß / İ / ı 等多码点 casefold 键值）保持一致；
- 恢复校验：歌曲数量严格 8 首、版本严格 int（排除 bool）、单群
  结构校验与清洗写回；
- 隔离分类：损坏（非 UTF-8 / 非法 JSON / 顶层非 dict / games 非
  dict / 超大小上限）与版本不受支持分别隔离，隔离失败降级内存；
- 异常降级：数据目录创建失败、读取 OSError（保留原文件）、写入
  失败与写入超限（保留旧快照、不关闭持久化）均不阻断游戏；
- 状态变更接线：开局 / 开字符（含本局没有该字符）/ 猜对 / 主动
  结束 / 全部猜完写盘，无状态变化分支不写盘（spy 计数断言）；
- 生命周期：initialize 经 StarTools 恢复、terminate 兜底落盘、
  停用后重新启用恢复。

无法覆盖真实 AstrBot 的 StarTools 实现与进程生命周期（插件重载 /
重启 / 强杀），该部分需按 README 人工验收步骤在实机验证。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_dispatch
from test_dispatch import GAME_TITLES, main

STATE_FILE = "games.json"
TMP_FILE = "games.json.tmp"
# 桩 star 模块由 test_dispatch 安装；StarTools 的类级状态在用例间共享，
# 每个用到桩的用例必须以 reset_star_stub 收尾。
STAR_STUB = sys.modules["astrbot.api.star"]


def reset_star_stub() -> None:
    STAR_STUB.StarTools.data_dir = None
    STAR_STUB.StarTools.error = RuntimeError


def make_state(opened: dict[str, str] | None = None, guessed_count: int = 0):
    """构造 8 首题板的 GameState，前 guessed_count 首标记为已猜出。"""
    state = main.GameState(
        songs=[main.RoundSong(title=title) for title in GAME_TITLES]
    )
    for song in state.songs[:guessed_count]:
        song.guessed = True
    for key, value in (opened or {}).items():
        state.opened_chars[key] = value
    return state


def song_entries(titles, guessed=False):
    return [{"title": title, "guessed": guessed} for title in titles]


class PersistenceTestCase(unittest.TestCase):
    """公共设施：独立临时数据目录 + 直连该目录的插件实例。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.plugin = main.MusicGuessPlugin(context=None, config={})
        self.plugin._data_dir = self.data_dir

    # ---- 存档读写辅助 ----

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILE

    @property
    def tmp_path(self) -> Path:
        return self.data_dir / TMP_FILE

    def write_state_file(self, data, ensure_ascii: bool = False) -> None:
        self.state_path.write_text(
            json.dumps(data, ensure_ascii=ensure_ascii), encoding="utf-8"
        )

    def read_state_file(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def valid_archive(self) -> dict:
        return {
            "version": main.STATE_FILE_VERSION,
            "games": {
                "10001": {
                    "songs": song_entries(GAME_TITLES),
                    "opened_chars": {"a": "A"},
                }
            },
        }

    def fresh_plugin(self) -> main.MusicGuessPlugin:
        """新插件实例：从同一数据目录恢复，模拟插件重载 / 重启。"""
        fresh = main.MusicGuessPlugin(context=None, config={})
        fresh._data_dir = self.data_dir
        fresh._restore_games()
        return fresh


# ---- 1. 往返一致性与多群 / 顺序 / 特殊字符 ----


class RoundTripTests(PersistenceTestCase):
    def test_full_state_roundtrip(self):
        self.plugin.games["10001"] = make_state(
            opened={"a": "A", "7": "7", "少": "少"}, guessed_count=3
        )
        self.plugin._persist_games()

        fresh = self.fresh_plugin()
        self.assertEqual(list(fresh.games), ["10001"])
        restored = fresh.games["10001"]
        self.assertEqual([song.title for song in restored.songs], GAME_TITLES)
        self.assertEqual(
            [song.guessed for song in restored.songs],
            [True, True, True, False, False, False, False, False],
        )
        self.assertEqual(
            list(restored.opened_chars.items()),
            [("a", "A"), ("7", "7"), ("少", "少")],
        )

    def test_multiple_groups_and_partial_update_roundtrip(self):
        self.plugin.games["10001"] = make_state(opened={"a": "A"})
        self.plugin.games["20002"] = make_state(guessed_count=1)
        self.plugin._persist_games()

        fresh = self.fresh_plugin()
        # 修改一个群的状态，不应丢失其他群的局面。
        fresh._open_character("10001", "c")
        second = self.fresh_plugin()
        self.assertEqual(
            list(second.games["10001"].opened_chars.items()), [("a", "A"), ("c", "C")]
        )
        self.assertTrue(second.games["20002"].songs[0].guessed)
        self.assertEqual(second.games["20002"].opened_chars, {})

    def test_opened_chars_order_roundtrip(self):
        self.plugin.games["10001"] = make_state()
        for char in "world3":
            self.plugin._open_character("10001", char)
        self.plugin._persist_games()

        fresh = self.fresh_plugin()
        self.assertEqual(
            list(fresh.games["10001"].opened_chars.items()),
            [("w", "W"), ("o", "O"), ("r", "R"), ("l", "L"), ("d", "D"), ("3", "3")],
        )

    def test_special_casefold_entries_roundtrip(self):
        # 开字符的 casefold()/upper() 对部分字符产生多码点或大小写
        # 不对称的键值，持久化必须原样往返。
        self.plugin.games["10001"] = make_state()
        for char in ("ß", "İ", "ı"):
            self.plugin._open_character("10001", char)

        expected = {"ss": "SS", "i\u0307": "İ", "ı": "I"}
        self.assertEqual(self.plugin.games["10001"].opened_chars, expected)
        self.plugin._persist_games()

        fresh = self.fresh_plugin()
        self.assertEqual(fresh.games["10001"].opened_chars, expected)


# ---- 2. 恢复歌曲数量必须严格等于 8 ----


class SongCountTests(PersistenceTestCase):
    def test_restore_requires_exactly_eight_songs(self):
        cases = {
            0: GAME_TITLES[:0],
            7: GAME_TITLES[:7],
            8: GAME_TITLES[:8],
            9: GAME_TITLES + ["Extra Song"],
        }
        for count, titles in cases.items():
            with self.subTest(count=count):
                self.write_state_file(
                    {
                        "version": main.STATE_FILE_VERSION,
                        "games": {
                            "10001": {"songs": song_entries(titles), "opened_chars": {}}
                        },
                    }
                )
                self.plugin._restore_games()
                expected_groups = 1 if count == 8 else 0
                self.assertEqual(len(self.plugin.games), expected_groups)
                self.state_path.unlink()


# ---- 3. 版本严格校验（排除 bool；未知版本优先于 games 校验） ----


class VersionTests(PersistenceTestCase):
    def test_bool_version_is_not_accepted(self):
        data = self.valid_archive()
        data["version"] = True
        self.write_state_file(data)

        self.plugin._restore_games()

        self.assertEqual(self.plugin.games, {})
        self.assertFalse(self.state_path.exists())
        self.assertEqual(
            len(list(self.data_dir.glob("games.unsupported-vunknown.*.json"))), 1
        )

    def test_unknown_version_quarantined_before_games_check(self):
        # 未知版本不检查 games 结构：即使 games 非法也按 unsupported
        # 隔离，不会被误判为 corrupt。
        self.write_state_file({"version": 2, "games": "not-a-dict"})

        self.plugin._restore_games()

        self.assertEqual(self.plugin.games, {})
        self.assertFalse(self.state_path.exists())
        self.assertEqual(
            len(list(self.data_dir.glob("games.unsupported-v2.*.json"))), 1
        )
        self.assertEqual(len(list(self.data_dir.glob("games.corrupt.*.json"))), 0)

    def test_missing_version_is_unsupported(self):
        data = self.valid_archive()
        del data["version"]
        self.write_state_file(data)

        self.plugin._restore_games()

        self.assertEqual(self.plugin.games, {})
        self.assertEqual(
            len(list(self.data_dir.glob("games.unsupported-vunknown.*.json"))), 1
        )


# ---- 4. 损坏文件隔离与单群清洗写回 ----


class CorruptFileTests(PersistenceTestCase):
    def assert_quarantined_as_corrupt(self):
        self.assertEqual(self.plugin.games, {})
        self.assertFalse(self.state_path.exists())
        self.assertEqual(len(list(self.data_dir.glob("games.corrupt.*.json"))), 1)

    def test_non_utf8_quarantined(self):
        self.state_path.write_bytes(b"\xff\xfe\x00\x01")
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_invalid_json_quarantined(self):
        self.state_path.write_text('{"version": 1,', encoding="utf-8")
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_huge_integer_quarantined(self):
        # 超长整数使 json.loads 抛 ValueError；文件远小于大小上限。
        raw = '{"version": 1, "n": ' + "9" * 5000 + "}"
        self.state_path.write_text(raw, encoding="utf-8")
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_deeply_nested_quarantined(self):
        # 动态加深嵌套直到当前解释器的 json.loads 抛 RecursionError
        # （不依赖固定深度）；每次构造前检查预计字节数不超过大小
        # 上限（纯 ASCII，字节数即字符数），未来解释器不在预期深度
        # 抛错时以 fail 终止而非无限循环。
        depth = 100
        while 2 * depth + 32 < main.MAX_STATE_FILE_BYTES:
            nested = "[" * depth + "]" * depth
            try:
                json.loads(nested)
            except RecursionError:
                break
            depth *= 2
        else:
            self.fail(
                "嵌套深度的预计字节数已达到 MAX_STATE_FILE_BYTES 仍未触发 "
                "RecursionError，无法构造本测试用例"
            )
        raw = '{"version": 1, "x": ' + nested + "}"
        self.assertLess(len(raw.encode("utf-8")), main.MAX_STATE_FILE_BYTES)
        self.state_path.write_text(raw, encoding="utf-8")
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_top_level_array_quarantined(self):
        self.state_path.write_text("[1, 2]", encoding="utf-8")
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_games_not_dict_quarantined(self):
        self.write_state_file({"version": main.STATE_FILE_VERSION, "games": []})
        self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_oversized_file_quarantined(self):
        with mock.patch.object(main, "MAX_STATE_FILE_BYTES", 8):
            self.write_state_file(self.valid_archive())
            self.plugin._restore_games()
        self.assert_quarantined_as_corrupt()

    def test_single_bad_group_dropped_and_file_cleaned(self):
        self.write_state_file(
            {
                "version": main.STATE_FILE_VERSION,
                "games": {
                    "10001": self.valid_archive()["games"]["10001"],
                    "20002": {
                        "songs": song_entries(GAME_TITLES[:7]),
                        "opened_chars": {},
                    },
                },
            }
        )

        self.plugin._restore_games()

        # 坏一群只丢一群，其他群正常恢复。
        self.assertEqual(list(self.plugin.games), ["10001"])
        self.assertEqual(self.plugin.games["10001"].opened_chars, {"a": "A"})
        # 清洗写回：存档不再包含被丢弃的群。
        self.assertEqual(list(self.read_state_file()["games"]), ["10001"])

    def test_missing_state_file_starts_empty(self):
        self.plugin._restore_games()
        self.assertEqual(self.plugin.games, {})
        self.assertFalse(self.state_path.exists())


# ---- 5. 单群条目结构校验 ----


class EntryValidationTests(PersistenceTestCase):
    def restore_single_group(self, entry) -> int:
        self.write_state_file(
            {"version": main.STATE_FILE_VERSION, "games": {"10001": entry}}
        )
        self.plugin._restore_games()
        return len(self.plugin.games)

    def test_all_guessed_group_rejected(self):
        entry = {
            "songs": song_entries(GAME_TITLES, guessed=True),
            "opened_chars": {},
        }
        self.assertEqual(self.restore_single_group(entry), 0)

    def test_duplicate_answer_titles_rejected(self):
        titles = [GAME_TITLES[0]] + GAME_TITLES[1:]
        titles[-1] = GAME_TITLES[0].upper()
        entry = {"songs": song_entries(titles), "opened_chars": {}}
        self.assertEqual(self.restore_single_group(entry), 0)

    def test_opened_key_not_casefold_rejected(self):
        entry = {
            "songs": song_entries(GAME_TITLES),
            "opened_chars": {"A": "A"},
        }
        self.assertEqual(self.restore_single_group(entry), 0)

    def test_opened_empty_value_rejected(self):
        entry = {
            "songs": song_entries(GAME_TITLES),
            "opened_chars": {"a": ""},
        }
        self.assertEqual(self.restore_single_group(entry), 0)

    def test_non_dict_entry_rejected(self):
        self.assertEqual(self.restore_single_group("not-a-dict"), 0)

    def test_unencodable_title_rejected(self):
        # JSON 转义可表达孤立代理字符；ensure_ascii=True 写出纯 ASCII
        # 文本，恢复时按非法条目丢弃。
        songs = song_entries(GAME_TITLES)
        songs[0] = {"title": "\udc80", "guessed": False}
        raw = json.dumps(
            {
                "version": main.STATE_FILE_VERSION,
                "games": {"10001": {"songs": songs, "opened_chars": {}}},
            },
            ensure_ascii=True,
        )
        self.state_path.write_text(raw, encoding="utf-8")

        self.plugin._restore_games()

        self.assertEqual(self.plugin.games, {})


# ---- 6. 隔离失败与读取失败降级 ----


class QuarantineAndReadErrorTests(PersistenceTestCase):
    def test_quarantine_failure_degrades_to_memory_mode(self):
        self.state_path.write_text("{invalid", encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            self.plugin._restore_games()

        # 隔离失败：保留原文件原位、本次运行不再写盘覆盖。
        self.assertEqual(self.plugin.games, {})
        self.assertTrue(self.state_path.exists())
        self.assertIsNone(self.plugin._data_dir)

    def test_quarantine_overwrites_existing_same_name_file(self):
        self.state_path.write_text("{invalid", encoding="utf-8")
        fixed = "20260101-000000-000000"
        quarantine_path = self.data_dir / f"games.corrupt.{fixed}.json"
        quarantine_path.write_text("old", encoding="utf-8")

        with mock.patch.object(main, "datetime") as dt:
            dt.now.return_value.strftime.return_value = fixed
            self.plugin._restore_games()

        self.assertFalse(self.state_path.exists())
        self.assertEqual(quarantine_path.read_text(encoding="utf-8"), "{invalid")
        self.assertEqual(self.plugin.games, {})
        self.assertIsNotNone(self.plugin._data_dir)

    def test_read_oserror_keeps_file_and_degrades(self):
        self.write_state_file(self.valid_archive())
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.plugin._restore_games()

        # 读取失败不等于损坏：保留原文件，降级为内存模式。
        self.assertEqual(self.plugin.games, {})
        self.assertTrue(self.state_path.exists())
        self.assertEqual(len(list(self.data_dir.glob("games.corrupt.*.json"))), 0)
        self.assertIsNone(self.plugin._data_dir)


# ---- 7. 数据目录创建失败降级为内存模式 ----


class DataDirDegradationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_star_stub)
        self.plugin = main.MusicGuessPlugin(context=None, config={})
        # 隔离曲库加载：数据目录降级不得阻断曲库初始化之外的流程。
        self.plugin._load_songs = lambda: None

    def test_runtime_error_degrades_to_memory_mode(self):
        # 桩默认 data_dir=None，get_data_dir 抛 RuntimeError。
        asyncio.run(self.plugin.initialize())

        self.assertIsNone(self.plugin._data_dir)
        self.memory_game_still_works()

    def test_value_error_degrades_to_memory_mode(self):
        STAR_STUB.StarTools.error = ValueError
        asyncio.run(self.plugin.initialize())

        self.assertIsNone(self.plugin._data_dir)
        self.memory_game_still_works()

    def memory_game_still_works(self):
        self.plugin.games["10001"] = make_state()
        reply = self.plugin._open_character("10001", "c")
        self.assertIsInstance(reply, str)
        self.assertEqual(self.plugin.games["10001"].opened_chars, {"c": "C"})
        # 内存模式下持久化静默跳过，不抛异常。
        self.plugin._persist_games()
        self.assertEqual(self.plugin.games["10001"].opened_chars, {"c": "C"})


# ---- 8. 写入失败与写入超限 ----


class WriteFailureTests(PersistenceTestCase):
    def test_os_replace_failure_keeps_old_snapshot(self):
        self.plugin.games["10001"] = make_state()
        self.plugin._persist_games()
        old = self.state_path.read_bytes()

        with mock.patch("os.replace", side_effect=OSError("disk error")):
            reply = self.plugin._open_character("10001", "c")

        # 游戏操作仍成功，内存状态已更新。
        self.assertIsInstance(reply, str)
        self.assertEqual(self.plugin.games["10001"].opened_chars, {"c": "C"})
        # 磁盘保留完整旧快照，临时文件被清理。
        self.assertEqual(self.state_path.read_bytes(), old)
        self.assertFalse(self.tmp_path.exists())

        # 写入失败不关闭持久化：后续状态变化可再次尝试。
        self.plugin._persist_games()
        self.assertEqual(self.read_state_file()["games"]["10001"]["opened_chars"], {"c": "C"})

    def test_oversize_write_keeps_old_snapshot_and_recovers(self):
        self.plugin.games["10001"] = make_state()
        self.plugin._persist_games()
        old = self.state_path.read_bytes()

        with mock.patch.object(main, "MAX_STATE_FILE_BYTES", 16):
            reply = self.plugin._open_character("10001", "c")

        # 超限：不写临时文件、不替换旧存档、不关闭持久化。
        self.assertIsInstance(reply, str)
        self.assertEqual(self.state_path.read_bytes(), old)
        self.assertFalse(self.tmp_path.exists())
        self.assertIsNotNone(self.plugin._data_dir)

        # 上限恢复（或状态缩小）后，持久化自动恢复。
        self.plugin._persist_games()
        self.assertEqual(self.read_state_file()["games"]["10001"]["opened_chars"], {"c": "C"})


# ---- 9. 生命周期接线（StarTools 桩 / terminate 兜底 / 停用再启用） ----


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.addCleanup(reset_star_stub)
        STAR_STUB.StarTools.data_dir = self.data_dir

    def make_plugin(self) -> main.MusicGuessPlugin:
        plugin = main.MusicGuessPlugin(context=None, config={})
        plugin._load_songs = lambda: None
        asyncio.run(plugin.initialize())
        return plugin

    def test_initialize_restores_via_star_tools(self):
        songs = song_entries(GAME_TITLES)
        songs[0]["guessed"] = True
        self.data_dir.joinpath(STATE_FILE).write_text(
            json.dumps(
                {
                    "version": main.STATE_FILE_VERSION,
                    "games": {
                        "10001": {
                            "songs": songs,
                            "opened_chars": {"a": "A"},
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plugin = self.make_plugin()

        self.assertEqual(plugin._data_dir, self.data_dir)
        state = plugin.games["10001"]
        self.assertTrue(state.songs[0].guessed)
        self.assertEqual(state.opened_chars, {"a": "A"})

    def test_terminate_persists_before_clear(self):
        plugin = self.make_plugin()
        # 绕过状态变更点直接放入内存，验证 terminate 的兜底落盘。
        plugin.games["10001"] = make_state(opened={"a": "A"})

        asyncio.run(plugin.terminate())

        self.assertEqual(plugin.games, {})
        data = json.loads(
            self.data_dir.joinpath(STATE_FILE).read_text(encoding="utf-8")
        )
        self.assertIn("10001", data["games"])

    def test_disable_then_reenable_restores(self):
        first = self.make_plugin()
        first.games["10001"] = make_state(opened={"a": "A"})
        asyncio.run(first.terminate())

        second = self.make_plugin()
        asyncio.run(second.initialize())

        self.assertEqual(
            list(second.games["10001"].opened_chars.items()), [("a", "A")]
        )


# ---- 10. 状态变更点接线（spy 计数断言） ----


class StateChangeWiringTests(PersistenceTestCase):
    def setUp(self):
        super().setUp()
        self.plugin.song_pool = [f"Song {i}" for i in range(10)]
        self.plugin.song_load_error = None

    def seed_game(self):
        self.plugin.games["10001"] = make_state()

    def test_start_game_persists(self):
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._start_game("10001")
        self.assertIn("开字母开始", reply)
        spy.assert_called_once()

    def test_duplicate_start_not_persists(self):
        self.plugin._start_game("10001")
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._start_game("10001")
        self.assertIn("已经有一局", reply)
        spy.assert_not_called()

    def test_open_new_character_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._open_character("10001", "c")
        self.assertIn("已开启", reply)
        spy.assert_called_once()

    def test_open_missing_character_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._open_character("10001", "8")
        self.assertIn("本局没有", reply)
        spy.assert_called_once()

    def test_duplicate_open_not_persists(self):
        self.seed_game()
        self.plugin._open_character("10001", "c")
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._open_character("10001", "C")
        self.assertIn("已经开过了", reply)
        spy.assert_not_called()

    def test_invalid_open_not_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._open_character("10001", "ab")
        self.assertIn("一次只能开一个", reply)
        spy.assert_not_called()

    def test_open_without_game_not_persists(self):
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._open_character("10001", "c")
        self.assertIn("没有进行中的开字母游戏", reply)
        spy.assert_not_called()

    def test_guess_wrong_not_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._guess_song("10001", 1, "Totally Wrong")
        self.assertEqual(reply, "不对。")
        spy.assert_not_called()

    def test_guess_correct_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._guess_song("10001", 1, GAME_TITLES[0])
        self.assertIn("答对了", reply)
        spy.assert_called_once()

    def test_guess_already_guessed_not_persists(self):
        self.plugin.games["10001"] = make_state(guessed_count=1)
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._guess_song("10001", 1, GAME_TITLES[0])
        self.assertIn("已经猜出来了", reply)
        spy.assert_not_called()

    def test_guess_without_game_not_persists(self):
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._guess_song("10001", 1, GAME_TITLES[0])
        self.assertIn("没有进行中的开字母游戏", reply)
        spy.assert_not_called()

    def test_final_guess_persists_exactly_once(self):
        self.plugin.games["10001"] = make_state(guessed_count=7)
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._guess_song("10001", 8, GAME_TITLES[7])
        self.assertIn("全部歌曲都猜出来了", reply)
        self.assertNotIn("10001", self.plugin.games)
        spy.assert_called_once()

    def test_end_game_persists(self):
        self.seed_game()
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._end_game("10001")
        self.assertIn("本局结束", reply)
        spy.assert_called_once()

    def test_end_without_game_not_persists(self):
        with mock.patch.object(self.plugin, "_persist_games") as spy:
            reply = self.plugin._end_game("10001")
        self.assertIn("没有进行中的开字母游戏", reply)
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
