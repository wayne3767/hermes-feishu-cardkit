# hermes-feishu-cardkit

Native streaming cards for [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s Feishu / Lark channel.

Hermes 的飞书通道默认把回复作为普通消息反复编辑。这个插件把每一轮回复变成**一张飞书 CardKit 流式卡片**：

- 模型开始生成时卡片立刻出现，文字以打字机效果逐步渲染
- 标题栏显示状态（生成中 / 已完成 / 已停止），页脚显示用时、模型、工具调用次数
- 工具调用收进折叠面板"思考与工具 · N 次工具调用"，不刷屏
- 行内 LaTeX 公式转成 Unicode 数学文本；块级公式在装了 matplotlib 时排版成图片
- 模型附带的图片（`MEDIA:` 行）嵌进卡片正文对应位置，带居中的"图N 标题"图注；表格上方加"表N 标题"表题
- 任一环节失败都退回 Hermes 原有的投递方式，文字和图片不会丢

**不修改 Hermes 的任何源码文件。** 插件在运行时用 Hermes 平台注册表的"后注册者优先"规则接管 `feishu` 平台条目，适配器是内置飞书适配器的子类。`hermes update` 不会撤销安装；上游改内部实现时，插件只依赖适配器的流式接口和少数几个稳定接缝，接缝缺失时自动退回内置适配器。

## 安装

前提：Hermes Agent 已装好并配置了飞书通道（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`），飞书应用在开放平台已授予 **`cardkit:card:write`** 权限（没有这个权限，插件首次尝试后会记录警告并退回原有行为）。

```bash
hermes plugins install https://github.com/wayne3767/hermes-feishu-cardkit
hermes plugins enable feishu-cardkit
hermes gateway restart
```

多 profile 用户对每个需要的 profile 分别执行（`hermes -p <profile> plugins install …`）。

可选：块级公式排版成图片需要 `matplotlib` 这个包存在于 Hermes 自己的 Python 环境（`~/.hermes/hermes-agent/venv`）里。没有它插件照常工作，只是公式以 Unicode 文本显示。

## 配置

| 环境变量 / config.yaml 键 | 默认 | 作用 |
|---|---|---|
| `FEISHU_STREAMING_CARD` / `platforms.feishu.streaming_card` | `true` | 关掉即回到 Hermes 内置行为 |
| `FEISHU_CARD_MATH_IMAGES` / `platforms.feishu.card_math_images` | `true` | 块级公式渲染成图片（需 matplotlib） |
| `FEISHU_CARD_LOCALE` / `platforms.feishu.card_locale` | 空 | 卡片文案语言 `zh` / `en`；默认 feishu 域中文、lark 域英文 |
| `display.platforms.feishu.tool_progress` | Hermes 默认 `new` | 设为 `all` 时时间线记录每一次工具调用，连续同名调用不合并 |

流式本身受 Hermes 通用设置 `streaming.enabled` 控制。

## 让模型配图和写表题

卡片能嵌图、写图注和表题，但图从哪来由模型决定。把下面这段放进 profile 的 `SOUL.md`，模型就会在合适的时候用 matplotlib 画图、用约定的文件名给图命名、在表格上方写表题：

```
- 解释概率分布、函数曲线、几何关系，或给出数据趋势、对比、分布，或说明设备与系统原理时，用 Python matplotlib 画图。
- 脚本开头设置中文字体：plt.rcParams['font.family'] = ['Hiragino Sans GB', 'Arial Unicode MS']；plt.rcParams['axes.unicode_minus'] = False。
- 文件名就是图的中文标题：/tmp/hermes_fig_<中文标题>_<时间戳>.png。MEDIA:/tmp/…png 单独一行，放在正文中图应出现的位置。卡片会自动加"图N 标题"，不要自己写图注。
- 每个表格上方单独一行写"表N：标题"；表格数据不超过 5 行（飞书卡片的限制）。
```

## 已知限制

- 飞书卡片 Markdown 表格最多 5 行数据、每个元素最多 4 个表格，超出会被飞书截断。
- 矩阵、分段函数、多行对齐环境和含中文的公式不做图片排版，退回 Unicode 文本。
- 卡片 JSON 上限 30 KB；超长回答的卡片显示开头部分，其余由 Hermes 按普通消息发出。
- 模型的思考过程和 token 用量 Hermes 不暴露给适配器，卡片里没有这两项。

## 开发与测试

```bash
# 需要一个 Hermes 检出和带其依赖的 Python；默认用 ~/.hermes/hermes-agent
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests -q
```

## 致谢与许可

卡片的视觉结构（标题状态栏、"思考与工具"折叠面板、"已完成 · 时长 · 模型"页脚）借鉴了 [baileyh8/hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)（MIT）的界面设计，特此致谢。本项目未使用该项目的代码，而是基于 Hermes 上游的原生流式接口独立实现，不修改上游源码。

本项目以 MIT 许可发布（见 `LICENSE`）。它在运行时继承 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT，Copyright (c) 2025 Nous Research）的飞书适配器，但不包含或分发其代码。
