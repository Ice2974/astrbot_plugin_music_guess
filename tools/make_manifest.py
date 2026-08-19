# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Ice2974

"""生成 / 更新仓库根目录的 manifest.json（曲库自动更新元数据）。

规则：
- 仅使用标准库，不调用 git；对当前工作树 songs.txt 的原始字节计算 sha256，
  与远端 raw 提供的字节保持同一口径；
- 曲目去重规则与 main.py 的 _parse_song_titles 保持一致（strip、跳过空行、
  完全相同的标题只保留一次）；
- 仅当内容 sha256 与现有合法 manifest 不一致时才递增 version 并重写 manifest；
  内容未变化时不 bump、不重写文件，只输出提示；
- manifest.json 不存在时视为首次生成，从 version 1 开始；
- manifest.json 已存在但结构非法时报错退出：不覆盖原文件、不当作首次
  生成、绝不重置为 v1，以维护 version 单调递增协议。

用法：
    python tools/make_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SONGS_PATH = REPO_ROOT / "songs.txt"
MANIFEST_PATH = REPO_ROOT / "manifest.json"

# 与 main.py 的 GAME_SONG_COUNT 保持一致。
GAME_SONG_COUNT = 8

# 与 main.py 运行时 _validate_manifest 相同的 sha256 口径：64 位小写十六进制。
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


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


def _is_valid_manifest(obj: object) -> bool:
    """与 main.py 运行时 _validate_manifest 一致的结构校验；未知字段忽略。"""
    if not isinstance(obj, dict):
        return False
    version = obj.get("version")
    song_count = obj.get("song_count")
    sha256 = obj.get("sha256")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return False
    if (
        not isinstance(song_count, int)
        or isinstance(song_count, bool)
        or song_count < GAME_SONG_COUNT
    ):
        return False
    if not isinstance(sha256, str) or not _SHA256_HEX.fullmatch(sha256):
        return False
    return True


def load_manifest(path: Path) -> tuple[str, dict | None]:
    """读取现有 manifest，区分三种情况。

    返回 (状态, 解析结果)：
    - "missing"：文件不存在（首次生成），结果为 None；
    - "valid"：文件存在且结构完整合法，结果为解析后的 dict；
    - "invalid"：文件存在但非法（不可读 / 坏 JSON / 字段不符），结果为 None。
    """
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    try:
        obj = json.loads(raw)
    except ValueError:
        return "invalid", None
    if not _is_valid_manifest(obj):
        return "invalid", None
    return "valid", obj


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
    state, existing = load_manifest(manifest_path)
    if state == "invalid":
        # 已存在的 manifest 非法：绝不当作首次生成（那会把 version 重置为
        # v1，破坏单调递增协议），而是报错退出，保留原文件待人工修复。
        raise SystemExit(
            f"错误：{manifest_path} 已存在但结构非法，为避免重置 version "
            "不会自动重新生成。请先人工修复（如 git checkout -- "
            f"{manifest_path.name}）后重试。"
        )
    if state == "valid" and existing.get("sha256") == sha256:
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
