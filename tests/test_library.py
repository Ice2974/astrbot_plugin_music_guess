# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""曲库加载测试（仅标准库，不依赖真实 AstrBot 环境）。

复用 test_dispatch 的 astrbot 桩模块导入 main.py，验证：
- songs/ 目录扫描：仅直接子级 txt、扩展名大小写不敏感、忽略子目录 / 其他扩展名 / 隐藏文件；
- 标题过滤：仅保留含 ASCII 英文字母或数字（A-Z / a-z / 0-9）的标题；
- 文件解析：UTF-8 / BOM / CRLF / 空行 / 文件内去重 / 非 UTF-8 文件跳过；
- enabled_libraries 三态：键缺失用默认、空列表不回退默认、非法类型回退默认；
- 跨曲库按答案匹配口径去重；曲池不足 8 首报错；
- 配置 schema options/labels 注入。

无法覆盖真实 AstrBot WebUI 的复选框渲染与热重载，该部分需按 README
人工验收步骤在实机验证。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import test_dispatch
from test_dispatch import main


def _titles(prefix: str, count: int) -> list[str]:
    return [f"{prefix} Song {index}" for index in range(1, count + 1)]


class SongLibraryTestCase(unittest.TestCase):
    """公共设施：临时 songs/ 目录 + _songs_dir 指向该目录的插件实例。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.songs_dir = Path(tmp.name) / "songs"
        self.songs_dir.mkdir()
        self.plugin = main.MusicGuessPlugin(context=None, config={})
        self.plugin._songs_dir = lambda: self.songs_dir  # type: ignore[method-assign]

    def write_library(self, filename: str, titles: list[str]) -> None:
        content = "\n".join(titles) + "\n"
        (self.songs_dir / filename).write_text(content, encoding="utf-8")

    def write_default_libraries(self) -> None:
        """默认四库 + 一个非默认库（chunithm），每库 10 首互不重复。"""
        self.write_library("phigros.txt", _titles("PHI", 10))
        self.write_library("arcaea.txt", _titles("ARC", 10))
        self.write_library("musedash.txt", _titles("MD", 10))
        self.write_library("maimai.txt", _titles("MAI", 10))
        self.write_library("chunithm.txt", _titles("CHU", 10))

    def load(self, config: dict | None = None) -> None:
        if config is not None:
            self.plugin._config = config
        self.plugin._load_songs(main._scan_library_files(self.songs_dir))


# ---- 标题过滤（ASCII 英文字母 / 数字口径） ----


class TitleFilterTests(unittest.TestCase):
    def test_titles_with_ascii_letters_or_digits_are_playable(self):
        for title in [
            "Credits",
            "BAD∞END∞NIGHT",
            "ニライカナイ (NiraicA_nai Mix)",
            "70 Minutes Fighters",
            "000 -Ain Soph Aur-",
            "LUDICROUS+",
            "2",
            "x",
            "Bangarang（feat. Sirah）",
        ]:
            with self.subTest(title=title):
                self.assertTrue(main._title_is_playable(title))

    def test_titles_without_ascii_letters_or_digits_are_filtered(self):
        for title in [
            "千本桜",
            "コズミックファンファーレ!!!!",
            "-+",
            "妄想♡ちゅー!!",
            "２４",  # 全角数字
            "Ⅻ",  # 罗马数字
            "①②③",  # 带圈数字
            "#病みカワ",
            "贝多芬祝福",
        ]:
            with self.subTest(title=title):
                self.assertFalse(main._title_is_playable(title))


# ---- 目录扫描 ----


class ScanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.songs_dir = Path(tmp.name)

    def test_scan_matches_txt_case_insensitively(self):
        for name in ["a.txt", "B.TXT", "c.Txt"]:
            (self.songs_dir / name).write_text("Song\n", encoding="utf-8")
        self.assertEqual(
            main._scan_library_files(self.songs_dir), ["B.TXT", "a.txt", "c.Txt"]
        )

    def test_scan_ignores_non_txt_subdirs_and_hidden(self):
        (self.songs_dir / "notes.md").write_text("x", encoding="utf-8")
        (self.songs_dir / "backup.bak").write_text("x", encoding="utf-8")
        (self.songs_dir / ".hidden.txt").write_text("x", encoding="utf-8")
        (self.songs_dir / ".txt").write_text("x", encoding="utf-8")
        sub = self.songs_dir / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("x", encoding="utf-8")
        self.assertEqual(main._scan_library_files(self.songs_dir), [])

    def test_scan_missing_dir_returns_empty(self):
        self.assertEqual(
            main._scan_library_files(self.songs_dir / "nope"), []
        )

    def test_library_stem_strips_any_case_extension(self):
        self.assertEqual(main._library_stem("phigros.txt"), "phigros")
        self.assertEqual(main._library_stem("MYLIB.TXT"), "MYLIB")
        self.assertEqual(main._library_stem("c.Txt"), "c")


# ---- 单文件读取与解析 ----


class ReadLibraryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_bom_crlf_blank_lines_and_duplicates(self):
        path = self.dir / "a.txt"
        path.write_bytes(
            b"\xef\xbb\xbfAlpha\r\n\r\nBeta\r\nAlpha\r\n\xE5\x8D\x83\xE6\x9C\xAC\xE6\xA1\x9C\r\n"
        )
        self.assertEqual(main._read_library_titles(path), ["Alpha", "Beta"])

    def test_all_filtered_titles_yield_empty_list(self):
        path = self.dir / "b.txt"
        path.write_text("千本桜\n-+\n", encoding="utf-8")
        self.assertEqual(main._read_library_titles(path), [])

    def test_non_utf8_file_returns_none(self):
        path = self.dir / "c.txt"
        path.write_bytes("純中文標題".encode("gbk"))
        self.assertIsNone(main._read_library_titles(path))

    def test_empty_file_returns_empty_list(self):
        path = self.dir / "d.txt"
        path.write_text("", encoding="utf-8")
        self.assertEqual(main._read_library_titles(path), [])


# ---- enabled_libraries 三态与合并去重 ----


class LoadSongsTests(SongLibraryTestCase):
    def test_missing_key_uses_default_libraries(self):
        self.write_default_libraries()
        self.load(config={"exclusive_mode": False})
        self.assertIsNone(self.plugin.song_load_error)
        self.assertEqual(len(self.plugin.song_pool), 40)
        self.assertFalse(
            any(title.startswith("CHU") for title in self.plugin.song_pool)
        )

    def test_invalid_type_falls_back_to_defaults(self):
        self.write_default_libraries()
        self.load(config={"enabled_libraries": "phigros"})
        self.assertIsNone(self.plugin.song_load_error)
        self.assertEqual(len(self.plugin.song_pool), 40)

    def test_empty_list_disables_all_without_fallback(self):
        self.write_default_libraries()
        self.load(config={"enabled_libraries": []})
        self.assertEqual(self.plugin.song_pool, [])
        self.assertIn("未启用任何曲库", self.plugin.song_load_error)
        self.assertIn("Phigros", self.plugin.song_load_error)

    def test_unknown_enabled_stems_are_ignored(self):
        self.write_library("phigros.txt", _titles("PHI", 10))
        self.load(config={"enabled_libraries": ["phigros", "ghost"]})
        self.assertIsNone(self.plugin.song_load_error)
        self.assertEqual(self.plugin.song_pool, _titles("PHI", 10))

    def test_explicit_selection_overrides_defaults(self):
        self.write_default_libraries()
        self.write_library("adofai.txt", _titles("ADO", 10))
        self.load(config={"enabled_libraries": ["adofai"]})
        self.assertEqual(self.plugin.song_pool, _titles("ADO", 10))

    def test_uppercase_extension_library_loads(self):
        self.write_library("MYLIB.TXT", _titles("UP", 10))
        self.load(config={"enabled_libraries": ["MYLIB"]})
        self.assertIsNone(self.plugin.song_load_error)
        self.assertEqual(self.plugin.song_pool, _titles("UP", 10))

    def test_cross_library_dedup_by_normalized_answer(self):
        self.write_library(
            "phigros.txt",
            ["Alpha Ray", "Lyrical ’94"] + _titles("PHI", 8),
        )
        self.write_library(
            "arcaea.txt",
            ["alpha  ray", "Lyrical '94"] + _titles("ARC", 8),
        )
        self.load(config={"enabled_libraries": ["phigros", "arcaea"]})
        self.assertEqual(len(self.plugin.song_pool), 18)
        # 按扫描排序 arcaea.txt 先加载，保留首次出现（arcaea 的写法）。
        self.assertIn("alpha  ray", self.plugin.song_pool)
        self.assertIn("Lyrical '94", self.plugin.song_pool)
        self.assertNotIn("Alpha Ray", self.plugin.song_pool)
        self.assertNotIn("Lyrical ’94", self.plugin.song_pool)

    def test_merged_pool_below_minimum_reports_error(self):
        self.write_library("tiny.txt", _titles("T", 5))
        self.load(config={"enabled_libraries": ["tiny"]})
        self.assertEqual(self.plugin.song_pool, [])
        self.assertIn("至少需要 8 首", self.plugin.song_load_error)

    def test_unreadable_library_is_skipped_but_others_load(self):
        (self.songs_dir / "gbklib.txt").write_bytes("純中文".encode("gbk"))
        self.write_library("good.txt", _titles("GOOD", 10))
        self.load(config={"enabled_libraries": ["gbklib", "good"]})
        self.assertIsNone(self.plugin.song_load_error)
        self.assertEqual(self.plugin.song_pool, _titles("GOOD", 10))

    def test_missing_songs_dir_reports_error(self):
        self.plugin._songs_dir = lambda: self.songs_dir.parent / "nope"  # type: ignore[method-assign]
        self.load()
        self.assertEqual(self.plugin.song_pool, [])
        self.assertIn("曲库目录不存在", self.plugin.song_load_error)

    def test_empty_songs_dir_reports_error(self):
        self.load()
        self.assertEqual(self.plugin.song_pool, [])
        self.assertIn("没有可用的 txt 曲库文件", self.plugin.song_load_error)


# ---- 配置 schema 选项注入 ----


class FakeConfig(dict):
    def __init__(self):
        super().__init__()
        self.schema: dict = {"enabled_libraries": {}}


class InjectOptionsTests(SongLibraryTestCase):
    def test_options_and_labels_are_injected(self):
        config = FakeConfig()
        self.plugin._config = config
        self.plugin._inject_library_options(
            ["phigros.txt", "mylib.txt", "MYLIB2.TXT"]
        )
        item = config.schema["enabled_libraries"]
        self.assertEqual(item["options"], ["phigros", "mylib", "MYLIB2"])
        self.assertEqual(item["labels"], ["Phigros", "mylib", "MYLIB2"])

    def test_plain_dict_config_is_harmless(self):
        self.plugin._config = {"enabled_libraries": {}}
        self.plugin._inject_library_options(["phigros.txt"])  # 不应抛异常

    def test_empty_scan_does_not_touch_schema(self):
        config = FakeConfig()
        self.plugin._config = config
        self.plugin._inject_library_options([])
        self.assertEqual(config.schema["enabled_libraries"], {})


# ---- initialize 接线：扫描一次，注入与加载共用结果 ----


class InitializeWiringTests(SongLibraryTestCase):
    def test_initialize_scans_injects_and_loads(self):
        self.write_default_libraries()
        self.plugin._config = {"enabled_libraries": ["phigros"]}
        asyncio.run(self.plugin.initialize())
        self.assertEqual(
            self.plugin._available_libraries,
            [
                "arcaea.txt",
                "chunithm.txt",
                "maimai.txt",
                "musedash.txt",
                "phigros.txt",
            ],
        )
        self.assertEqual(self.plugin.song_pool, _titles("PHI", 10))
        self.assertIsNone(self.plugin.song_load_error)


if __name__ == "__main__":
    unittest.main()
