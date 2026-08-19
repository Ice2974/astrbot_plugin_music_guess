# 曲库数据来源

本文件用于记录 `songs.txt` 及未来结构化曲库的数据来源、许可条件和整理方式。

插件源代码的 `AGPL-3.0-or-later` 许可证仅适用于本项目代码，不代表第三方歌曲名称、游戏数据或其他资源自动采用相同许可证。

同样，第三方项目的代码许可证也不应自动理解为其收录的歌曲名称、游戏数据、曲绘、音频、谱面等内容采用相同许可证。

## 当前内置曲库

截至 2026-08-18，仓库内置的 `songs.txt` 为插件的离线 fallback 曲库，共约 540 首歌曲。

用途：

* 在远程曲库无法下载、本地缓存不可用等情况下保证插件仍可正常游戏。
* 仅保存歌曲标题，一行一首。
* 不保存音频、曲绘、谱面、角色素材或其他游戏资源。
* 不作为下列第三方数据库的完整镜像。
* 曲目经过人工筛选、合并和去重，仅保留适合“开字母”玩法使用的标题。

当前主要参考 Arcaea、Phigros、maimai DX 和 CHUNITHM 的公开曲目数据。

### 原有示例曲目

* 来源：项目早期人工整理。
* 数据类型：歌曲标题。
* 是否批量导入第三方数据库：否。
* 整理方式：人工添加。
* 备注：扩充 fallback 曲库时保留了原有曲目。

## Arcaea

* 游戏：Arcaea
* 参考数据源：`r-caea/arcaea-db`
* 原始项目：https://github.com/r-caea/arcaea-db
* 参考文件：`db/songs.json`
* 使用数据：歌曲标题。
* 整理方式：从参考数据中人工筛选部分曲目，只保留标题，并与其他来源合并去重。
* 是否完整镜像：否。
* 是否包含代码：否。
* 是否包含曲绘、音频、谱面等资源：否。

### 许可 / 使用条件

`r-caea/arcaea-db` 仓库包含 MIT License，但其 README 同时明确声明该仓库和软件包：

> intended for personal use only

该项目 README 还注明歌曲数据库来源于 BotArcAPI。

因此，本项目**不将 `r-caea/arcaea-db` 的 MIT License 直接解释为对 Arcaea 歌曲数据进行公开再分发的授权**。

当前 `songs.txt` 仅人工整理和保留部分歌曲标题，不复制其代码、完整数据库结构、曲绘、音频或谱面数据。

**待确认项：**

* 如果未来计划从该数据源自动批量同步或公开分发完整 Arcaea 曲库，应重新确认数据来源及其公开再分发条件。
* 在许可条件未明确前，不应将该数据源直接作为自动发布完整 Arcaea 数据库的依据。

Arcaea、歌曲及相关内容的权利归各自权利人所有。

## Phigros

* 游戏：Phigros
* 参考数据源：`Catrong/phi-plugin`
* 原始项目：https://github.com/Catrong/phi-plugin
* 参考文件：`resources/info/info.csv`
* 使用字段：`song`
* 使用数据：歌曲标题。
* 整理方式：从参考数据中人工筛选部分曲目，只保留标题，并与其他来源合并去重。
* 是否完整镜像：否。
* 是否包含代码：否。
* 是否包含曲绘、音频、谱面等资源：否。

### 许可 / 使用条件

`phi-plugin` 项目仓库使用 GNU General Public License v3.0（GPL-3.0）。

本项目没有复制或集成 `phi-plugin` 的程序代码，仅将其公开曲目数据作为人工核对歌曲标题的参考来源。

本项目不假定：

* `phi-plugin` 的 GPL-3.0 自动适用于所有 Phigros 歌曲名称或游戏数据；
* Phigros 的歌曲、曲绘、音频、谱面或其他游戏内容因此获得 GPL 授权。

当前内置曲库只保存人工整理后的歌曲标题。

Phigros、歌曲及相关内容的权利归各自权利人所有。

## maimai DX

* 游戏：maimai でらっくす / maimai DX
* 参考数据源：OTOGE DB
* 原始项目：https://github.com/zvuc/otoge-db
* 参考文件：`maimai/data/maimai_songs.json`
* 使用字段：`title`
* 使用数据：歌曲标题。
* 整理方式：人工筛选部分曲目，只保留标题，并与其他来源合并去重。
* 是否完整镜像：否。
* 是否包含代码：否。
* 是否包含曲绘、音频、谱面等资源：否。

### 数据来源说明

OTOGE DB README 说明，其基础曲目 JSON 数据来自 SEGA 官方网站公开的数据，并注明：

* `maimai/data/maimai_songs.json` 为 SEGA 提供的原始 JSON 数据副本；
* OTOGE DB 是非官方粉丝项目；
* OTOGE DB 与 SEGA 无隶属或官方认可关系。

### 许可 / 使用条件

OTOGE DB README 声明其仓库中的**代码**使用 MIT License。

该 MIT License 不应自动理解为：

* SEGA 官方提供的基础曲目数据采用 MIT License；
* maimai DX 歌曲名称、曲绘、音频、谱面等内容采用 MIT License。

本项目仅使用人工整理后的部分歌曲标题，不复制曲绘、音频、谱面或其他游戏资源。

maimai、maimai でらっくす、歌曲及相关内容的权利归 SEGA 和/或各自权利人所有。

## CHUNITHM

* 游戏：CHUNITHM
* 参考数据源：OTOGE DB
* 原始项目：https://github.com/zvuc/otoge-db
* 参考文件：`chunithm/data/music.json`
* 使用字段：`title`
* 使用数据：歌曲标题。
* 整理方式：人工筛选部分曲目，只保留标题，并与其他来源合并去重。
* 是否完整镜像：否。
* 是否包含代码：否。
* 是否包含曲绘、音频、谱面等资源：否。

### 数据来源说明

OTOGE DB README 说明，其基础曲目数据建立在 SEGA 官方网站公开数据之上，并将基础 JSON 文件作为 SEGA 官方数据的副本保存。

### 许可 / 使用条件

OTOGE DB 的代码使用 MIT License，但本项目不将该许可证解释为 SEGA 官方曲目数据或歌曲相关内容采用 MIT License。

本项目仅使用人工整理后的部分歌曲标题，不复制曲绘、音频、谱面或其他游戏资源。

CHUNITHM、歌曲及相关内容的权利归 SEGA 和/或各自权利人所有。

## 曲库整理规则

当前 fallback `songs.txt` 采用以下整理规则：

1. 一行保存一个歌曲标题。
2. 文件使用 UTF-8 编码。
3. 去除空行和首尾多余空白。
4. 完全相同的标题只保留一次。
5. 同时按照插件答案规范化规则检查容易产生等价答案的重复项，包括：

   * Unicode NFKC 规范化；
   * 英文字母大小写归一；
   * 常见弯引号与直引号统一；
   * 连续空白归一。
6. 同一歌曲同时收录于多个音游时，如果最终标题完全一致，只在 `songs.txt` 中保留一次。
7. 不添加来源游戏、作者、别名、曲包、难度等结构化信息。

当前曲库仍采用简单的：

```text
songs.txt
```

一行一个标题的格式。

只有实际需要游戏筛选、别名、多语言标题、同名歌曲区分等功能时，才考虑升级为结构化曲库格式。

## 曲库分发与自动更新

`songs.txt` 通过以下渠道分发，内容完全一致，GitHub 为主上游：

* GitHub 主仓库 raw：`https://raw.githubusercontent.com/Ice2974/astrbot_plugin_music_guess/main/songs.txt`
* Gitee 镜像 raw：`https://gitee.com/Ice2974/astrbot_plugin_music_guess/raw/main/songs.txt`（GitHub → Gitee 单向同步）

仓库根目录的 `manifest.json` 是曲库自动更新元数据（`version` / `song_count` / `sha256`），不属于歌曲数据，也不改变 `songs.txt` 一行一首的格式。

manifest 维护流程：

1. 修改 `songs.txt`；
2. 运行 `python tools/make_manifest.py`：仅当内容 sha256 与现有 manifest 不一致时递增 version 并重写 manifest，内容未变化时不 bump、不重写，仅输出提示；首次（无有效 manifest）从 version 1 开始；
3. 提交并推送 GitHub，等待 Gitee 同步。

脚本只对当前工作树 `songs.txt` 的原始字节计算哈希（与远端 raw 提供的字节同口径），不调用 git。注意仓库换行标准为 LF：若本地工作树被转换为 CRLF，脚本会输出警告，此时哈希与远端实际内容会不一致，应先恢复 LF 再生成。

通过上述渠道分发的内容与仓库内置 `songs.txt` 完全相同，其数据来源、整理规则与许可说明仍以上文各节为准；经 Gitee 镜像分发不改变任何来源的许可条件。

## 未来在线曲库

未来远程自动更新曲库可能继续加入其他音乐游戏。

新增或自动同步第三方数据源时，应至少在本文件记录：

* 游戏名称；
* 数据源名称；
* 原始项目或官方网站；
* 实际使用的文件或 API；
* 使用的数据字段；
* 数据源许可证；
* 游戏数据本身的使用条件；
* 是否经过人工整理；
* 是否进行自动同步；
* 数据转换或过滤方式。

如果某个第三方数据源：

* 没有明确许可证；
* 明确限制公开再分发；
* 许可证只覆盖程序代码而未明确覆盖数据；
* 数据来源或权利状态无法确认；

则不应仅因为技术上可以抓取，就直接加入自动公开发布流程。

此类情况应记录为：

`待确认项`

并在确认使用条件后再决定是否作为正式在线曲库来源。

## 权利声明

本项目是非官方开源插件，与 Arcaea、Phigros、SEGA、maimai、CHUNITHM 及相关游戏、发行商、开发商或音乐版权方不存在官方隶属或认可关系。

歌曲标题、游戏名称、商标以及其他相关内容的权利归各自权利人所有。

如发现曲库来源、署名、许可说明或收录内容存在问题，可通过项目仓库 Issue 提出更正或移除请求。
