# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""曲库自动更新测试（仅标准库；注入假 fetch 与临时目录，无真实网络）。

复用 test_dispatch 的 astrbot 桩模块加载 main.py，覆盖：
- disabled / github / gitee / auto（含非法配置值回退 auto）的源选择行为；
- auto 双源择优：版本高者优先、同版本内容一致优先 Gitee、内容不一致以
  GitHub 为准、首选源下载 / 校验失败后的跨源回退；
- 探测窗口与全局预算：挂起源不会被拖满单源超时，整体更新受总预算封顶；
- manifest / songs 校验矩阵（结构、hash、编码、BOM、数量、全重复）；
- 内容寻址缓存提交点：manifest 提交失败保留旧缓存、孤儿文件不影响加载
  且在下次成功提交后清理；
- 本地加载回退链：缓存 pair、自带 pair、损坏缓存成组清理自愈；
- initialize() 先本地后远程的接线；
- tools/make_manifest.py 的 version bump 行为。

无法覆盖真实 AstrBot 生命周期与真实网络，该部分按 README 人工验收步骤
在实机验证。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

import test_dispatch  # 复用其 astrbot 桩与 main 模块加载

main = test_dispatch.main
REPO_ROOT = test_dispatch.REPO_ROOT

GITHUB_URLS = main.UPDATE_SOURCES["github"]
GITEE_URLS = main.UPDATE_SOURCES["gitee"]

BUNDLED_TITLES = main._parse_song_titles(
    (REPO_ROOT / main.SONGS_FILENAME).read_text(encoding="utf-8-sig")
)


def _load_manifest_tool():
    spec = importlib.util.spec_from_file_location(
        "make_manifest_tool", REPO_ROOT / "tools" / "make_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_manifest_tool = _load_manifest_tool()


def make_titles(count: int, prefix: str = "Song") -> list[str]:
    return [f"{prefix} {index}" for index in range(count)]


def make_songs_bytes(titles: list[str]) -> bytes:
    return ("\n".join(titles) + "\n").encode("utf-8")


def make_manifest_bytes(
    songs_bytes: bytes, version: int, song_count: int | None = None
) -> bytes:
    if song_count is None:
        song_count = len(main._parse_song_titles(songs_bytes.decode("utf-8-sig")))
    return json.dumps(
        {
            "version": version,
            "song_count": song_count,
            "sha256": hashlib.sha256(songs_bytes).hexdigest(),
        }
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_valid_cache(data_dir: Path, titles: list[str], version: int) -> bytes:
    """向数据目录预置一份有效缓存 pair，返回 manifest 字节。"""
    songs = make_songs_bytes(titles)
    manifest = make_manifest_bytes(songs, version)
    (data_dir / main.MANIFEST_FILENAME).write_bytes(manifest)
    (data_dir / f"songs-{sha256_hex(songs)}.txt").write_bytes(songs)
    return manifest


class FakeFetch:
    """按 URL 返回预设响应；可注入延迟和异常，并记录所有调用。"""

    def __init__(self):
        self.routes: dict[str, bytes | Exception] = {}
        self.delays: dict[str, float] = {}
        self.calls: list[str] = []

    def route(self, url: str, payload: bytes | Exception, delay: float = 0.0):
        self.routes[url] = payload
        self.delays[url] = delay
        return self

    async def __call__(self, url: str, timeout_s: float) -> bytes:
        self.calls.append(url)
        delay = self.delays.get(url, 0.0)
        if delay:
            await asyncio.sleep(delay)
        payload = self.routes.get(url)
        if payload is None:
            raise OSError(f"no route configured for {url}")
        if isinstance(payload, Exception):
            raise payload
        return payload


class UpdateTestCase(unittest.TestCase):
    """公共设施：临时数据目录、插件构造与更新驱动。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "plugin_data"
        self.data_dir.mkdir()

    def _fresh_data_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def make_plugin(self, config: dict | None = None, data_dir: Path | None = None):
        plugin = main.MusicGuessPlugin(
            context=None, config={} if config is None else config
        )
        plugin._data_dir = self.data_dir if data_dir is None else data_dir
        return plugin

    def run_update(self, plugin, fetch: FakeFetch, **kwargs) -> bool:
        return asyncio.run(plugin._check_song_updates(fetch=fetch, **kwargs))

    def cache_files(self) -> list[str]:
        return sorted(item.name for item in self.data_dir.iterdir())


class UpdateSourceModeTests(UpdateTestCase):
    """disabled / 单源模式与非法配置值的行为。"""

    def test_disabled_makes_no_requests(self):
        fetch = FakeFetch()
        plugin = self.make_plugin({"songs_update_source": "disabled"})
        self.assertFalse(self.run_update(plugin, fetch))
        self.assertEqual(fetch.calls, [])

    def test_github_mode_never_falls_back_to_gitee(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(10, "Mirror"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 2))
        fetch.route(GITHUB_URLS["songs"], OSError("connection reset"))
        # Gitee 路由完好：若实现错误地跨源，更新会成功。
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 2))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin({"songs_update_source": "github"})
        self.assertFalse(self.run_update(plugin, fetch))

        called = set(fetch.calls)
        self.assertEqual(
            called, {GITHUB_URLS["manifest"], GITHUB_URLS["songs"]}
        )
        self.assertEqual(self.cache_files(), [])

    def test_gitee_mode_never_falls_back_to_github(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(10, "Mirror"))
        fetch = FakeFetch()
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 2))
        fetch.route(GITEE_URLS["songs"], OSError("connection reset"))
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 2))
        fetch.route(GITHUB_URLS["songs"], github_songs)

        plugin = self.make_plugin({"songs_update_source": "gitee"})
        self.assertFalse(self.run_update(plugin, fetch))

        called = set(fetch.calls)
        self.assertEqual(called, {GITEE_URLS["manifest"], GITEE_URLS["songs"]})
        self.assertEqual(self.cache_files(), [])

    def test_invalid_config_value_behaves_like_auto(self):
        remote_songs = make_songs_bytes(make_titles(10, "Remote"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(remote_songs, 1))
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(remote_songs, 1))

        plugin = self.make_plugin({"songs_update_source": "bananas"})
        self.assertEqual(plugin.songs_update_source, main.UPDATE_SOURCE_AUTO)
        # 远端 v1 与自带 v1 相同 → 已是最新，但仍探测了两个源。
        self.assertFalse(self.run_update(plugin, fetch))
        self.assertEqual(
            set(fetch.calls), {GITHUB_URLS["manifest"], GITEE_URLS["manifest"]}
        )

    def test_unhashable_config_values_fall_back_to_auto(self):
        # 手工编辑配置可能写出不可哈希值；必须在 __init__ 安全回退 auto，
        # 不得抛 TypeError 导致插件加载失败。
        for bad in ("bananas", None, 123, [], {}):
            with self.subTest(value=bad):
                plugin = main.MusicGuessPlugin(
                    context=None, config={"songs_update_source": bad}
                )
                self.assertEqual(
                    plugin.songs_update_source, main.UPDATE_SOURCE_AUTO
                )

    def test_missing_data_dir_skips_update_and_uses_bundled(self):
        fetch = FakeFetch()
        plugin = self.make_plugin()
        plugin._data_dir = None
        self.assertFalse(self.run_update(plugin, fetch))
        self.assertEqual(fetch.calls, [])

        plugin._load_songs()
        self.assertEqual(plugin.song_pool, BUNDLED_TITLES)
        self.assertIsNone(plugin.song_load_error)

    def test_resolve_data_dir_failure_returns_none(self):
        original = main.StarTools
        main.StarTools = types.SimpleNamespace()  # 无 get_data_dir 属性
        try:
            plugin = self.make_plugin()
            self.assertIsNone(plugin._resolve_data_dir())
        finally:
            main.StarTools = original


class AutoSelectionTests(UpdateTestCase):
    """auto 模式的择优、回退与等待上限。"""

    def test_auto_prefers_higher_version(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(11, "Gitee"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 2))
        fetch.route(GITHUB_URLS["songs"], github_songs)
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 3))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(11, "Gitee"))

    def test_auto_same_version_same_content_prefers_gitee(self):
        songs = make_songs_bytes(make_titles(10, "Same"))
        manifest = make_manifest_bytes(songs, 2)
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], manifest)
        fetch.route(GITHUB_URLS["songs"], songs)
        fetch.route(GITEE_URLS["manifest"], manifest)
        fetch.route(GITEE_URLS["songs"], songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        self.assertIn(GITEE_URLS["songs"], fetch.calls)
        self.assertNotIn(GITHUB_URLS["songs"], fetch.calls)

    def test_auto_same_version_different_sha_prefers_github(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(10, "Gitee"))
        fetch = FakeFetch()
        fetch.route(
            GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 3)
        )
        fetch.route(GITHUB_URLS["songs"], github_songs)
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 3))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        self.assertIn(GITHUB_URLS["songs"], fetch.calls)
        self.assertNotIn(GITEE_URLS["songs"], fetch.calls)
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(10, "GH"))

    def test_auto_uses_single_available_source(self):
        songs = make_songs_bytes(make_titles(10, "Only"))
        for good, bad in (
            (GITHUB_URLS, GITEE_URLS),
            (GITEE_URLS, GITHUB_URLS),
        ):
            with self.subTest(source=good["manifest"]):
                data_dir = self._fresh_data_dir()
                fetch = FakeFetch()
                fetch.route(good["manifest"], make_manifest_bytes(songs, 2))
                fetch.route(good["songs"], songs)
                fetch.route(bad["manifest"], OSError("unreachable"))

                plugin = self.make_plugin(data_dir=data_dir)
                self.assertTrue(self.run_update(plugin, fetch))
                plugin._load_songs()
                self.assertEqual(plugin.song_pool, make_titles(10, "Only"))

    def test_auto_both_sources_unavailable_keeps_local(self):
        fetch = FakeFetch()  # 无任何路由 → 全部失败
        plugin = self.make_plugin()
        self.assertFalse(self.run_update(plugin, fetch))
        self.assertEqual(self.cache_files(), [])
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, BUNDLED_TITLES)

    def test_auto_falls_back_when_preferred_download_fails(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(9, "Gitee"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 3))
        fetch.route(GITHUB_URLS["songs"], OSError("download failed"))
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 2))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        self.assertIn(GITHUB_URLS["songs"], fetch.calls)
        self.assertIn(GITEE_URLS["songs"], fetch.calls)
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(9, "Gitee"))

    def test_auto_falls_back_when_preferred_validation_fails(self):
        github_songs = make_songs_bytes(make_titles(10, "GH"))
        gitee_songs = make_songs_bytes(make_titles(9, "Gitee"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(github_songs, 3))
        # 返回内容与 manifest hash 不一致 → 校验失败 → 回退 Gitee。
        fetch.route(
            GITHUB_URLS["songs"], make_songs_bytes(make_titles(10, "Tampered"))
        )
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 2))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(9, "Gitee"))

    def test_auto_probe_window_bounds_hanging_source(self):
        gitee_songs = make_songs_bytes(make_titles(10, "Gitee"))
        github_manifest = make_manifest_bytes(
            make_songs_bytes(make_titles(10, "GH")), 2
        )
        fetch = FakeFetch()
        # GitHub manifest 挂起 5s，探测窗口只有 0.5s：不得等待 GitHub 完整超时。
        fetch.route(GITHUB_URLS["manifest"], github_manifest, delay=5.0)
        fetch.route(GITEE_URLS["manifest"], make_manifest_bytes(gitee_songs, 2))
        fetch.route(GITEE_URLS["songs"], gitee_songs)

        plugin = self.make_plugin()
        start = time.monotonic()
        updated = self.run_update(
            plugin,
            fetch,
            probe_timeout_s=0.5,
            download_timeout_s=2.0,
            total_budget_s=10.0,
        )
        elapsed = time.monotonic() - start

        self.assertTrue(updated)
        self.assertLess(elapsed, 2.0)
        self.assertIn(GITEE_URLS["songs"], fetch.calls)
        self.assertNotIn(GITHUB_URLS["songs"], fetch.calls)

    def test_global_budget_caps_hanging_download(self):
        songs = make_songs_bytes(make_titles(10, "Slow"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(songs, 2))
        fetch.route(GITHUB_URLS["songs"], songs, delay=5.0)

        plugin = self.make_plugin({"songs_update_source": "github"})
        start = time.monotonic()
        updated = self.run_update(
            plugin,
            fetch,
            total_budget_s=1.0,
            probe_timeout_s=0.5,
            download_timeout_s=10.0,
        )
        elapsed = time.monotonic() - start

        self.assertFalse(updated)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(self.cache_files(), [])

    def test_no_download_when_remote_not_newer(self):
        # 自带曲库为 v1，远端也是 v1：应判定已是最新，不下载也不生成缓存。
        remote_songs = make_songs_bytes(make_titles(12, "Remote"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(remote_songs, 1))
        fetch.route(GITHUB_URLS["songs"], remote_songs)

        plugin = self.make_plugin({"songs_update_source": "github"})
        self.assertFalse(self.run_update(plugin, fetch))
        self.assertEqual(fetch.calls, [GITHUB_URLS["manifest"]])
        self.assertEqual(self.cache_files(), [])


class ValidationTests(UpdateTestCase):
    """manifest 与 songs 的校验矩阵（github 单源模式）。"""

    def _make_plugin(self, data_dir: Path | None = None):
        return self.make_plugin(
            {"songs_update_source": "github"}, data_dir=data_dir
        )

    def test_manifest_invalid_json_rejected(self):
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], b"{not json")
        self.assertFalse(self.run_update(self._make_plugin(), fetch))
        self.assertEqual(fetch.calls, [GITHUB_URLS["manifest"]])
        self.assertEqual(self.cache_files(), [])

    def test_manifest_invalid_fields_rejected(self):
        songs = make_songs_bytes(make_titles(10))
        good = json.loads(make_manifest_bytes(songs, 2))
        cases = {
            "missing_version": {k: v for k, v in good.items() if k != "version"},
            "missing_song_count": {
                k: v for k, v in good.items() if k != "song_count"
            },
            "missing_sha256": {k: v for k, v in good.items() if k != "sha256"},
            "bool_version": {**good, "version": True},
            "float_version": {**good, "version": 2.0},
            "version_below_one": {**good, "version": 0},
            "song_count_below_minimum": {**good, "song_count": 7},
            "sha_not_hex": {**good, "sha256": "z" * 64},
            "sha_wrong_length": {**good, "sha256": good["sha256"][:32]},
            "not_an_object": [good],
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                data_dir = self._fresh_data_dir()
                fetch = FakeFetch()
                fetch.route(
                    GITHUB_URLS["manifest"], json.dumps(payload).encode("utf-8")
                )
                self.assertFalse(self.run_update(self._make_plugin(data_dir), fetch))
                self.assertEqual(sorted(item.name for item in data_dir.iterdir()), [])

    def test_songs_hash_mismatch_rejected(self):
        expected = make_songs_bytes(make_titles(10, "Expected"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(expected, 2))
        fetch.route(
            GITHUB_URLS["songs"], make_songs_bytes(make_titles(10, "Actual"))
        )
        self.assertFalse(self.run_update(self._make_plugin(), fetch))
        self.assertEqual(self.cache_files(), [])

    def test_songs_utf8_with_bom_accepted(self):
        songs = b"\xef\xbb\xbf" + make_songs_bytes(make_titles(10, "Bom"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(songs, 2))
        fetch.route(GITHUB_URLS["songs"], songs)

        plugin = self._make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(10, "Bom"))

    def test_songs_invalid_utf8_rejected(self):
        songs = b"\xff\xfe\x00 not utf-8"
        fetch = FakeFetch()
        # 歌曲字节无法解码：song_count 需显式给出（自动计算依赖解码）。
        fetch.route(
            GITHUB_URLS["manifest"],
            make_manifest_bytes(songs, 2, song_count=10),
        )
        fetch.route(GITHUB_URLS["songs"], songs)
        self.assertFalse(self.run_update(self._make_plugin(), fetch))
        self.assertEqual(self.cache_files(), [])

    def test_songs_content_invalid_rejected(self):
        cases = {
            # (songs 字节, manifest 声明的 song_count)
            "empty": (b"", 8),
            "too_few": (make_songs_bytes(make_titles(3)), 8),
            "all_duplicates": ((("\n".join(["Same"]) * 10) + "\n").encode(), 8),
            "count_mismatch": (make_songs_bytes(make_titles(10)), 12),
        }
        for name, (songs, count) in cases.items():
            with self.subTest(case=name):
                data_dir = self._fresh_data_dir()
                fetch = FakeFetch()
                fetch.route(
                    GITHUB_URLS["manifest"],
                    make_manifest_bytes(songs, 2, song_count=count),
                )
                fetch.route(GITHUB_URLS["songs"], songs)
                self.assertFalse(self.run_update(self._make_plugin(data_dir), fetch))
                self.assertEqual(sorted(item.name for item in data_dir.iterdir()), [])


class CacheLoadTests(UpdateTestCase):
    """本地加载回退链与缓存 pair 语义。"""

    def test_valid_cache_preferred_over_bundled_on_tie(self):
        titles = make_titles(10, "Cache")
        write_valid_cache(self.data_dir, titles, version=1)  # 与自带 v1 并列

        plugin = self.make_plugin()
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, titles)

    def test_cache_higher_version_preferred(self):
        titles = make_titles(10, "Cache")
        write_valid_cache(self.data_dir, titles, version=5)

        plugin = self.make_plugin()
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, titles)

    def test_bundled_newer_version_wins_over_cache(self):
        write_valid_cache(self.data_dir, make_titles(10, "Cache"), version=1)

        plugin = self.make_plugin()
        plugin._read_bundled_library = lambda: (99, make_titles(12, "Bundled"))
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(12, "Bundled"))

    def test_cache_pair_hash_mismatch_discarded_and_bundled_used(self):
        real_songs = make_songs_bytes(make_titles(10, "Real"))
        manifest = make_manifest_bytes(real_songs, 2)
        (self.data_dir / main.MANIFEST_FILENAME).write_bytes(manifest)
        # 歌曲文件字节与 manifest hash 不一致（例如半文件 / 新旧错位）。
        (self.data_dir / f"songs-{sha256_hex(real_songs)}.txt").write_bytes(
            make_songs_bytes(make_titles(10, "Corrupt"))
        )

        plugin = self.make_plugin()
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, BUNDLED_TITLES)
        # 无效缓存成组清理。
        self.assertEqual(self.cache_files(), [])

    def test_cache_manifest_without_songs_file_discarded(self):
        ghost_songs = make_songs_bytes(make_titles(10, "Ghost"))
        (self.data_dir / main.MANIFEST_FILENAME).write_bytes(
            make_manifest_bytes(ghost_songs, 2)
        )

        plugin = self.make_plugin()
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, BUNDLED_TITLES)
        self.assertEqual(self.cache_files(), [])

    def test_update_failure_keeps_existing_cache(self):
        titles = make_titles(10, "Cache")
        manifest_bytes = write_valid_cache(self.data_dir, titles, version=3)

        fetch = FakeFetch()  # 两个 manifest 都失败
        plugin = self.make_plugin()
        self.assertFalse(self.run_update(plugin, fetch))

        self.assertEqual(
            (self.data_dir / main.MANIFEST_FILENAME).read_bytes(), manifest_bytes
        )
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, titles)

    def test_updated_library_loads_and_starts_game(self):
        new_titles = make_titles(10, "New")
        songs = make_songs_bytes(new_titles)
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(songs, 2))
        fetch.route(GITHUB_URLS["songs"], songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, new_titles)
        self.assertIsNone(plugin.song_load_error)

        reply = plugin._start_game("A")
        self.assertIsInstance(reply, str)
        state = plugin.games["A"]
        self.assertEqual(len(state.songs), main.GAME_SONG_COUNT)


class CommitPointTests(UpdateTestCase):
    """内容寻址缓存：manifest.json 是唯一提交点。"""

    def test_manifest_commit_failure_preserves_old_cache(self):
        old_manifest = write_valid_cache(
            self.data_dir, make_titles(10, "Old"), version=2
        )
        new_songs = make_songs_bytes(make_titles(11, "New"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(new_songs, 3))
        fetch.route(GITHUB_URLS["songs"], new_songs)

        real_atomic_write = main._atomic_write

        def flaky_atomic_write(path: Path, data: bytes) -> None:
            if path.name == main.MANIFEST_FILENAME:
                raise OSError("simulated manifest replace failure")
            return real_atomic_write(path, data)

        main._atomic_write = flaky_atomic_write
        try:
            plugin = self.make_plugin({"songs_update_source": "github"})
            self.assertFalse(self.run_update(plugin, fetch))
        finally:
            main._atomic_write = real_atomic_write

        # 旧 cache manifest 原样保留并继续有效。
        self.assertEqual(
            (self.data_dir / main.MANIFEST_FILENAME).read_bytes(), old_manifest
        )
        plugin._load_songs()
        self.assertEqual(plugin.song_pool, make_titles(10, "Old"))
        # 新歌曲文件成为孤儿：存在，但不影响加载。
        self.assertTrue(
            (self.data_dir / f"songs-{sha256_hex(new_songs)}.txt").is_file()
        )

    def test_orphan_cleaned_after_next_successful_update(self):
        write_valid_cache(self.data_dir, make_titles(10, "Old"), version=2)
        orphan_songs = make_songs_bytes(make_titles(9, "Orphan"))
        (self.data_dir / f"songs-{sha256_hex(orphan_songs)}.txt").write_bytes(
            orphan_songs
        )

        new_songs = make_songs_bytes(make_titles(11, "New"))
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], make_manifest_bytes(new_songs, 4))
        fetch.route(GITHUB_URLS["songs"], new_songs)

        plugin = self.make_plugin()
        self.assertTrue(self.run_update(plugin, fetch))
        # 目录只剩提交点与新内容文件。
        self.assertEqual(
            self.cache_files(),
            [main.MANIFEST_FILENAME, f"songs-{sha256_hex(new_songs)}.txt"],
        )


class InitializeWiringTests(UpdateTestCase):
    """initialize()：先加载本地，再远程更新，成功后重载。"""

    def _run_initialize(self, plugin, fetch: FakeFetch):
        original_http = main._http_download
        main._http_download = fetch
        try:
            asyncio.run(plugin.initialize())
        finally:
            main._http_download = original_http

    def test_initialize_updates_and_reloads(self):
        write_valid_cache(self.data_dir, make_titles(10, "Old"), version=2)
        new_songs = make_songs_bytes(make_titles(12, "New"))
        manifest = make_manifest_bytes(new_songs, 3)
        fetch = FakeFetch()
        fetch.route(GITHUB_URLS["manifest"], manifest)
        fetch.route(GITEE_URLS["manifest"], manifest)
        fetch.route(GITEE_URLS["songs"], new_songs)  # 同版本同内容 → Gitee 胜出

        plugin = self.make_plugin()
        plugin._resolve_data_dir = lambda: self.data_dir
        self._run_initialize(plugin, fetch)
        self.assertEqual(plugin.song_pool, make_titles(12, "New"))

    def test_initialize_disabled_keeps_local_library(self):
        fetch = FakeFetch()
        plugin = self.make_plugin({"songs_update_source": "disabled"})
        plugin._resolve_data_dir = lambda: self.data_dir
        self._run_initialize(plugin, fetch)
        self.assertEqual(fetch.calls, [])
        self.assertEqual(plugin.song_pool, BUNDLED_TITLES)


class MakeManifestToolTests(unittest.TestCase):
    """tools/make_manifest.py 的 version bump 行为。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.songs_path = self.root / "songs.txt"
        self.manifest_path = self.root / "manifest.json"

    def _write_songs(self, titles: list[str]) -> None:
        self.songs_path.write_bytes(make_songs_bytes(titles))

    def _read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def test_first_manifest_starts_at_version_one(self):
        self._write_songs(make_titles(10))
        result = make_manifest_tool.generate(self.songs_path, self.manifest_path)
        self.assertIn("v0 → v1", result)
        manifest = self._read_manifest()
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["song_count"], 10)
        self.assertEqual(
            manifest["sha256"], sha256_hex(make_songs_bytes(make_titles(10)))
        )

    def test_no_bump_when_unchanged(self):
        self._write_songs(make_titles(10))
        make_manifest_tool.generate(self.songs_path, self.manifest_path)
        before = self.manifest_path.read_bytes()

        result = make_manifest_tool.generate(self.songs_path, self.manifest_path)
        self.assertIn("未变化", result)
        self.assertEqual(self.manifest_path.read_bytes(), before)
        self.assertEqual(self._read_manifest()["version"], 1)

    def test_bumps_on_change(self):
        self._write_songs(make_titles(10))
        make_manifest_tool.generate(self.songs_path, self.manifest_path)

        self._write_songs(make_titles(11))
        result = make_manifest_tool.generate(self.songs_path, self.manifest_path)
        self.assertIn("v1 → v2", result)
        manifest = self._read_manifest()
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["song_count"], 11)
        self.assertEqual(
            manifest["sha256"], sha256_hex(make_songs_bytes(make_titles(11)))
        )

    def test_rejects_too_few_songs(self):
        self._write_songs(make_titles(3))
        with self.assertRaises(SystemExit):
            make_manifest_tool.generate(self.songs_path, self.manifest_path)
        self.assertFalse(self.manifest_path.exists())

    def test_existing_invalid_manifest_errors_and_preserves_file(self):
        # 模拟「原本代表高版本（v5）、后被误编辑损坏」：所有用例的
        # sha256 都与当前 songs.txt 完全一致。结构非法必须报错退出，
        # 不得误判为“内容未变化”，更不得当作首次生成把文件重置为 v1。
        titles = make_titles(10)
        self._write_songs(titles)
        valid = json.loads(make_manifest_bytes(make_songs_bytes(titles), 5))
        cases = {
            "invalid_json": b"{not json",
            "not_an_object": b"[1, 2, 3]",
            "version_missing": {k: v for k, v in valid.items() if k != "version"},
            "version_bool": {**valid, "version": True},
            "version_zero": {**valid, "version": 0},
            "song_count_missing": {
                k: v for k, v in valid.items() if k != "song_count"
            },
            "song_count_bool": {**valid, "song_count": True},
            "song_count_below_minimum": {**valid, "song_count": 7},
            "sha_not_hex": {**valid, "sha256": "z" * 64},
            "sha_uppercase_hex": {**valid, "sha256": valid["sha256"].upper()},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                raw = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8")
                )
                self.manifest_path.write_bytes(raw)
                with self.assertRaises(SystemExit):
                    make_manifest_tool.generate(self.songs_path, self.manifest_path)
                # 原文件不得被覆盖（尤其不得被重置为 v1）。
                self.assertEqual(self.manifest_path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
