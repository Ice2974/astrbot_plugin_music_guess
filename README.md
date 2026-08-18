# 音游开字母

一个简单、轻量的 AstrBot QQ 群音游开字母猜曲插件。

插件内部名：`astrbot_plugin_music_guess`

当前版本：`0.2.0`

## 功能

- 每局随机抽取 8 首歌曲
- 隐藏 Unicode 字母（英文字母、中文汉字、日文假名等）和 ASCII 数字 0-9
- 支持开英文字母、数字与中文 / 日文等文字字符
- 支持按编号猜歌
- 猜完全部歌曲后自动结束
- 可主动结束并公布答案
- 每个 QQ 群游戏状态完全独立
- 不调用 LLM
- 不使用数据库
- 不依赖外部 API
- 无第三方 Python 依赖

## 支持环境

- AstrBot `>=4.27.3,<5`
- QQ 官方机器人（`qq_official`）

当前版本以 AstrBot `v4.27.3` 为主要开发和测试环境。

## 安装

### AstrBot WebUI

将本插件的 ZIP 包上传到 AstrBot WebUI 的插件管理页面安装。

### GitHub 仓库

仓库地址：

`https://github.com/Ice2974/astrbot_plugin_music_guess`

如果从源码手动安装，请确保插件目录最终为：

```text
data/plugins/astrbot_plugin_music_guess/
```

## 使用方法

开始：

```text
开字母
```

开字符：

```text
开 A
开 7
开 桜
```

猜歌：

```text
3 Credits
曲 3 Credits
```

结束：

```text
结束开字母
结束游戏
```

AstrBot 自身的 `/help` 等 `/` 开头指令始终会继续放行，不受插件影响。

### 默认模式（exclusive_mode=false）

插件只处理明确属于开字母游戏的消息，例如 `开字母`、`开 A`、`结束游戏`，以及游戏进行中的 `3 Credits` 猜歌消息。

普通聊天（如 `你好`、`abcdef`）不会被本插件拦截，会正常交给 AstrBot 的其他插件或 AI 对话处理，适合大多数公开 Bot。

### 独占模式（exclusive_mode=true）

开启后，除 `/` 开头的 AstrBot 指令外，机器人收到的普通群消息都会由本插件接管：无法识别时返回玩法提示，并停止事件传播，不进入 LLM。

适合只用于开字母游戏的专用 Bot。默认关闭，可在 AstrBot WebUI 的插件配置中开启。

## 曲名隐藏规则

Unicode 字母和 ASCII 数字 0-9 使用 `•` 隐藏：

```text
Credits
•••••••

World Vanquisher
••••• ••••••••••

千本桜
•••
```

规则：

- 所有 Unicode 字母（英文字母、中文汉字、日文假名等）都参与隐藏，可通过 `开 桜` 等方式开启
- 英文字母大小写视为同一字符
- ASCII 数字 0-9 默认隐藏，也可通过 `开 7` 等方式开启；非 ASCII 数字（如全角 `９`）不隐藏，直接显示
- 空格直接显示
- 标点和符号直接显示
- 已猜中的歌曲显示完整原标题

## 答案匹配规则

- Unicode NFKC 规范化
- 英文字母忽略大小写
- 忽略首尾空白
- 连续空白折叠为一个空格
- 标点必须正确
- 当前不支持别名
- 当前不支持模糊匹配

## 曲库

`songs.txt`：

- UTF-8 编码
- 一行一首歌曲
- 空行自动忽略
- 完全相同的重复行自动去重
- 至少需要 8 首有效歌曲

修改 `songs.txt` 后重载插件即可重新读取。

当前仓库附带的曲库只是开发测试用示例。正式多音游曲库的数据来源和许可信息请记录在 [`SOURCES.md`](SOURCES.md)。

## 游戏状态

游戏状态只保存在 AstrBot 进程内存中：

```text
group_id -> GameState
```

因此：

- 不同 QQ 群互不影响
- AstrBot 重启、插件重载或插件停用后，正在进行的游戏会丢失

这是当前版本的有意设计，不需要数据库。

## 当前未实现

- 曲名别名
- 来源游戏字段
- 同名歌曲区分
- 排行榜 / 积分
- 游戏状态持久化
- 在线更新曲库
- WebUI 游戏配置
- 自定义每局歌曲数量
- 模糊匹配

## 配置

插件只有一个配置项，可在 AstrBot WebUI 的插件配置页面修改：

- `exclusive_mode`（独占模式，默认 `false`）：开启后，除 `/` 开头的 AstrBot 指令外，普通群消息全部由本插件接管，不再进入后续 LLM 或其他消息处理流程，适合仅用于开字母游戏的专用 Bot。普通公开 Bot 推荐保持默认关闭。

## 项目结构

```text
astrbot_plugin_music_guess/
├── .gitignore
├── LICENSE
├── README.md
├── SOURCES.md
├── _conf_schema.json
├── main.py
├── metadata.yaml
└── songs.txt
```

## 数据与许可证

插件源代码使用 `AGPL-3.0-or-later`。

歌曲名称以及未来可能引入的第三方数据，其权利和许可条件应按各自来源处理，不因为插件代码采用 AGPL 就自动视为同一许可证。详见 `SOURCES.md`。