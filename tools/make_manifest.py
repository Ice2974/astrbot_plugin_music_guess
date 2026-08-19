# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""生成 / 更新仓库根目录的 manifest.json（曲库自动更新元数据）。

规则：
- 仅使用标准库，不调用 git；对当前工作树 songs.txt 的原始字节计算 sha256，
  与远端 raw 提供的字节保持同一口径；
- 曲目去重规则与 main.py 的 _parse_song_titles 保持一致（strip、跳过空行、
  完全相同的标题只保留一次）；
- 仅当内容 sha256 与现有 manifest 不一致时才递增 version 并重写 manifest；
  内容未变化时不 bump、不重写文件，只输出提示；
- 首次（无有效 manifest）从 version 1 开始。

用法：
    python tools/make_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SONGS_PATH = REPO_ROOT / "songs.txt"
MANIFEST_PATH = REPO_ROOT / "manifest.json"

# 与 main.py 的 GAME_SONG_COUNT 保持一致。
GAME_SONG_COUNT = 8


def parse_song_titles(text: str) -> list[str]:
    """与 main.py _parse_song_titles 相同的规则；独立实现以免依赖 AstrBot 环境。"""
    songs: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        title = line.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        songs.append(title)
    return songs


def load_manifest(path: Path) -> dict | None:
    """读取现有 manifest；结构非法时视为无有效 manifest（返回 None）。"""
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    version = obj.get("version")
    sha256 = obj.get("sha256")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    if not isinstance(sha256, str) or len(sha256) != 64:
        return None
    return obj


def generate(songs_path: Path, manifest_path: Path) -> str:
    """按工作树 songs.txt 生成 manifest；返回人读结果描述。

    仅在内容 sha256 与现有 manifest 不一致时递增 version 并重写 manifest。
    """
    try:
        data = songs_path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"错误：无法读取 {songs_path}：{exc}") from exc

    if b"\r\n" in data:
        print(
            "警告：songs.txt 含 CRLF 换行（仓库标准为 LF），"
            "请确认提交内容与哈希一致。",
            file=sys.stderr,
        )

    try:
        titles = parse_song_titles(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise SystemExit(f"错误：songs.txt 不是有效的 UTF-8 文本：{exc}") from exc

    if len(titles) < GAME_SONG_COUNT:
        raise SystemExit(
            f"错误：有效歌曲只有 {len(titles)} 首，少于 {GAME_SONG_COUNT}，不生成 manifest。"
        )

    sha256 = hashlib.sha256(data).hexdigest()
    existing = load_manifest(manifest_path)
    if existing is not None and existing.get("sha256") == sha256:
        return (
            f"songs.txt 内容未变化（sha256={sha256[:12]}…），"
            f"manifest 保持 v{existing['version']} 未重写。"
        )

    old_version = existing["version"] if existing is not None else 0
    manifest = {
        "version": old_version + 1,
        "song_count": len(titles),
        "sha256": sha256,
    }
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)
    return (
        f"manifest.json 已更新：v{old_version} → v{old_version + 1}，"
        f"{len(titles)} 首，sha256={sha256}"
    )


def main() -> None:
    print(generate(SONGS_PATH, MANIFEST_PATH))


if __name__ == "__main__":
    main()
