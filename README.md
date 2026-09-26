# hermes-feishu-cardkit

Native streaming cards for the Feishu / Lark channel of [Hermes Agent](https://github.com/NousResearch/hermes-agent).

[English](#english) · [中文](#中文)

## English

**Feishu and Lark are the same product.** ByteDance ships it as Feishu (飞书, `feishu.cn`) inside China and as Lark (`larksuite.com`) everywhere else. Hermes's bundled adapter talks to both through one SDK; the `FEISHU_DOMAIN` setting (`feishu` or `lark`) selects the endpoint. This plugin works on both and picks its wording from that setting: Chinese card labels on Feishu, English on Lark (override with `FEISHU_CARD_LOCALE`).

### What it changes

Out of the box, Hermes delivers a Feishu/Lark reply as a plain message that gets edited over and over while the model writes. With this plugin every turn becomes **one CardKit streaming card**:

- The card appears the moment the model starts; text renders with the platform's typewriter animation as tokens arrive.
- A status header (Generating / Done / Stopped) and a footer with elapsed time, model and tool-call count.
- Tool calls fold into a collapsed **"Thinking & tools · N tool calls"** panel instead of a stream of progress messages.
- Inline LaTeX becomes Unicode math text (`\delta_p` → δₚ, `\frac{a}{b}` → (a)/(b)); display formulas are typeset into images when `matplotlib` is available.
- Images the model attaches (`MEDIA:` lines) are placed inside the card where the model put them, with centred **"Figure N · title"** captions; markdown tables get a **"Table N · title"** heading, and tables longer than 5 rows become paged table components (10 rows per page) once the answer is done.
- A **Stop** button on the generating card sends `/stop` for whoever clicks it.
- Inline LaTeX still being typed is held back until it closes, so raw `$\frac{…` never flashes in the card.
- Cron deliveries (scheduled job output) go out as **one static card**: the first line becomes the header (a trailing `(…)` the subtitle), the rest keeps its headings and tables, Hermes's English "Cronjob Response" wrapper is removed and the job name goes into a scheduled-task footer; the header is violet (chat cards keep blue / green / grey for their progress) and red on failures.
- Every step degrades to Hermes's normal delivery on failure (missing CardKit scope, upload error, oversized answer, an incompatible Hermes build). Text and images are never lost.

### How it works — no core patches

Hermes already has a native streaming pipeline that its DingTalk and WeCom adapters use; the Feishu adapter simply never implemented it. This plugin subclasses the bundled Feishu adapter and implements that interface (`send_stream_frame`, `supports_native_streaming`, plus the `extract_media` / `send_multiple_images` hooks for attachments). At startup it re-registers the `feishu` platform entry; Hermes's platform registry is last-writer-wins and user plugins load after bundled ones, so **no file in the Hermes checkout is modified** and `hermes update` cannot undo the install. Registration itself is lazy: the bundled adapter (about 200 ms to import) is only loaded when the gateway actually builds it, never on plain `hermes` startup. If a future Hermes drops one of the few adapter seams the plugin relies on, it logs a warning and hands the platform back to the bundled adapter.

### Install

Requirements: Hermes Agent with the Feishu/Lark channel configured (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`), and the app granted the **`cardkit:card:write`** scope in the developer console (`open.feishu.cn` or `open.larksuite.com`). Without that scope the first attempt logs a warning and the plugin falls back to the bundled behaviour.

```bash
hermes plugins install https://github.com/wayne3767/hermes-feishu-cardkit
hermes plugins enable feishu-cardkit
hermes gateway restart
```

Repeat per profile if you run several (`hermes -p <profile> plugins install …`).

Optional: display formulas are typeset as images when the `matplotlib` package is present in Hermes's own Python environment (`~/.hermes/hermes-agent/venv`). Without it the plugin works unchanged and formulas stay Unicode text.

### Configuration

| Env var / `config.yaml` key | Default | Effect |
|---|---|---|
| `FEISHU_STREAMING_CARD` / `platforms.feishu.streaming_card` | `true` | `false` restores the bundled behaviour |
| `FEISHU_CARD_MATH_IMAGES` / `platforms.feishu.card_math_images` | `true` | Typeset display formulas as images (needs matplotlib) |
| `FEISHU_CARD_LOCALE` / `platforms.feishu.card_locale` | empty | Card wording `zh` / `en`; default follows `FEISHU_DOMAIN` |
| `FEISHU_CRON_CARD` / `platforms.feishu.cron_card` | `true` | `false` sends cron output as ordinary messages again |
| `FEISHU_CRON_CARD_TEMPLATE` / `platforms.feishu.cron_card_template` | `violet` | Header colour of cron cards (a Feishu card template name); failures stay `red` |
| `FEISHU_CARD_STOP_BUTTON` / `platforms.feishu.card_stop_button` | `true` | Stop button on generating cards |
| `display.platforms.feishu.tool_progress` | Hermes default `new` | `all` records every tool call in the timeline, without merging repeats |

Streaming as a whole is governed by Hermes's general `streaming.enabled` setting.

### Letting the model produce figures and table titles

The card can embed figures and caption them, but whether the model draws anything is up to your prompt. These are Feishu-only conventions, so put them in the Feishu platform hint rather than `SOUL.md` (other channels would follow them too) — in the profile's `config.yaml`, `agent.platform_hints.feishu.append: |` followed by rules like:

```
- When explaining distributions, curves or geometry, showing data trends or comparisons, or describing how a system works, draw a figure with Python matplotlib.
- Name the file after the figure's title: /tmp/hermes_fig_<title>_<timestamp>.png. Put the MEDIA:/tmp/….png line where the figure belongs in the text; the card adds "Figure N · title" itself, so do not write your own caption.
- Put a single line "Table N: title" above every markdown table. Tables longer than 5 rows show paged (10 rows per page, at most 5 such tables per card).
- Cards do not render LaTeX; the plugin converts it. Use $…$ for inline symbols (shown as Unicode text) and $$…$$ on their own lines for fractions, sums and integrals (typeset as images). Display formulas cannot contain CJK text or aligned / cases / matrix environments.
```

### Known limits

- Feishu pages markdown tables at 5 data rows; longer tables become table components (10 rows per page) in the finished card, at most 5 per card; while generating they show as markdown.
- The Stop button stops the clicker's own session; in topic groups it may miss a turn started inside a topic (card callbacks carry no topic).
- Subscripts without a Unicode form are written run-on (`$A_d$` → Ad, `$V_{daf}$` → Vdaf), as plain-text coal and chemistry notation does.
- Matrices, `cases`, multi-line alignment environments and formulas containing CJK text are not typeset as images; they fall back to Unicode.
- Card JSON is capped at 30 KB; the whole card is measured, tool lines give way first, and an oversized answer shows its head in the card while the full text goes out as ordinary messages. While streaming, an oversized answer shows its newest paragraphs.
- CardKit ends streaming mode after 10 minutes; longer turns keep updating the same card with full updates (no typewriter effect).
- `/stop` and `/new` mark the chat's open card "Stopped" within a few seconds; a turn that sends nothing for 5 minutes is marked "Stopped" too and returns to "Generating" if it resumes.
- Hermes does not expose the model's reasoning or token usage to adapters, so the card has neither.
- Cron cards apply only when the gateway delivers the job; the standalone sender (gateway down) still sends ordinary messages. Output with `MEDIA:` attachments or over the card size limit also goes out as ordinary messages.

### Development

```bash
# needs a Hermes checkout and a Python with its dependencies; defaults to ~/.hermes/hermes-agent
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests -q
```

### Acknowledgements and license

The card's visual structure (status header, collapsed "Thinking & tools" panel, "Done · duration · model" footer) follows the UI of [baileyh8/hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card) (MIT). No code from that project is used; this is an independent implementation on Hermes's native streaming interface that patches nothing upstream.

MIT licensed (see `LICENSE`). At runtime the plugin subclasses the Feishu adapter of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT, Copyright (c) 2025 Nous Research) but does not include or redistribute its code.

## 中文

Hermes 的飞书通道默认把回复作为普通消息反复编辑。这个插件把每一轮回复变成**一张飞书 CardKit 流式卡片**：

- 模型开始生成时卡片立刻出现，文字以打字机效果逐步渲染
- 标题栏显示状态（生成中 / 已完成 / 已停止），页脚显示用时、模型、工具调用次数
- 工具调用收进折叠面板"思考与工具 · N 次工具调用"，不刷屏
- 行内 LaTeX 公式转成 Unicode 数学文本；块级公式在装了 matplotlib 时排版成图片
- 模型附带的图片（`MEDIA:` 行）嵌进卡片正文对应位置，带居中的"图N 标题"图注；表格上方加"表N 标题"表题；超过 5 行的表格在完成后改为分页表格组件（每页 10 行）
- 生成中的卡片带"停止生成"按钮，点击即替点击者发送 `/stop`
- 生成中尚未闭合的行内公式先不显示，不会闪出原始 LaTeX
- 定时任务（cron）的输出以**一张静态卡片**发出：首行作标题（末尾括号内容作副标题），其余保留标题层级和表格，Hermes 默认加的英文外壳（"Cronjob Response…"）会去掉，任务名放进"定时任务"页脚；标题栏为紫色（聊天卡片的蓝 / 绿 / 灰表示进度，两者分开），标题含"失败"等字样时为红色
- 任一环节失败都退回 Hermes 原有的投递方式，文字和图片不会丢

**不修改 Hermes 的任何源码文件。** 插件在运行时用 Hermes 平台注册表的"后注册者优先"规则接管 `feishu` 平台条目，适配器是内置飞书适配器的子类。`hermes update` 不会撤销安装；上游改内部实现时，插件只依赖适配器的流式接口和少数几个稳定接缝，接缝缺失时自动退回内置适配器。

### 安装

前提：Hermes Agent 已装好并配置了飞书通道（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`），飞书应用在开放平台已授予 **`cardkit:card:write`** 权限（没有这个权限，插件首次尝试后会记录警告并退回原有行为）。

```bash
hermes plugins install https://github.com/wayne3767/hermes-feishu-cardkit
hermes plugins enable feishu-cardkit
hermes gateway restart
```

多 profile 用户对每个需要的 profile 分别执行（`hermes -p <profile> plugins install …`）。

可选：块级公式排版成图片需要 `matplotlib` 这个包存在于 Hermes 自己的 Python 环境（`~/.hermes/hermes-agent/venv`）里。没有它插件照常工作，只是公式以 Unicode 文本显示。

### 配置

| 环境变量 / config.yaml 键 | 默认 | 作用 |
|---|---|---|
| `FEISHU_STREAMING_CARD` / `platforms.feishu.streaming_card` | `true` | 关掉即回到 Hermes 内置行为 |
| `FEISHU_CARD_MATH_IMAGES` / `platforms.feishu.card_math_images` | `true` | 块级公式渲染成图片（需 matplotlib） |
| `FEISHU_CARD_LOCALE` / `platforms.feishu.card_locale` | 空 | 卡片文案语言 `zh` / `en`；默认 feishu 域中文、lark 域英文 |
| `FEISHU_CRON_CARD` / `platforms.feishu.cron_card` | `true` | 设为 `false` 时定时任务输出恢复为普通消息 |
| `FEISHU_CRON_CARD_TEMPLATE` / `platforms.feishu.cron_card_template` | `violet` | 定时任务卡片标题栏颜色（飞书卡片模板名）；失败始终为 `red` |
| `FEISHU_CARD_STOP_BUTTON` / `platforms.feishu.card_stop_button` | `true` | 生成中卡片的"停止生成"按钮 |
| `display.platforms.feishu.tool_progress` | Hermes 默认 `new` | 设为 `all` 时时间线记录每一次工具调用，连续同名调用不合并 |

流式本身受 Hermes 通用设置 `streaming.enabled` 控制。

### 让模型配图和写表题

卡片能嵌图、写图注和表题，但图从哪来由模型决定。这些是飞书专用约定，建议写进飞书平台提示而不是 `SOUL.md`（否则其他渠道也会照做）：在 profile 的 `config.yaml` 里写 `agent.platform_hints.feishu.append: |`，后接类似下面的规则：

```
- 解释概率分布、函数曲线、几何关系，或给出数据趋势、对比、分布，或说明设备与系统原理时，用 Python matplotlib 画图。
- 脚本开头设置中文字体：plt.rcParams['font.family'] = ['Hiragino Sans GB', 'Arial Unicode MS']；plt.rcParams['axes.unicode_minus'] = False。
- 文件名就是图的中文标题：/tmp/hermes_fig_<中文标题>_<时间戳>.png。MEDIA:/tmp/…png 单独一行，放在正文中图应出现的位置。卡片会自动加"图N 标题"，不要自己写图注。
- 每个表格上方单独一行写"表N：标题"。超过 5 行的表格会分页显示（每页 10 行，一张卡片最多 5 个）。
- 卡片不渲染 LaTeX，由插件转换：行内符号用 $…$（转成 Unicode 文本），分式、求和、积分用单独成行的 $$…$$（转成公式图片）。块级公式里不写中文，不用 aligned、cases、matrix 等环境。
```

### 已知限制

- 飞书 Markdown 表格每页固定 5 行、超出分页；完成后的卡片里，超过 5 行的表格改为表格组件（每页 10 行），一张卡片最多 5 个；生成过程中仍是 Markdown 表格。
- "停止生成"按钮停止的是点击者自己的会话；话题群里在话题内发起的回合可能停不到（卡片回调不带话题信息）。
- 没有 Unicode 下标字形的下标按连写处理（`$A_d$` → Ad，`$V_{daf}$` → Vdaf），与煤质、化学的纯文本写法一致。
- 矩阵、分段函数、多行对齐环境和含中文的公式不做图片排版，退回 Unicode 文本。
- 卡片 JSON 上限 30 KB；按整张卡片计量，先压缩工具时间线；仍超长的回答在卡片里显示开头部分，完整内容由 Hermes 按普通消息发出。生成过程中超长时显示最新几段。
- CardKit 流式模式 10 分钟后自动关闭；更长的回合继续用整卡更新刷新同一张卡片（没有打字机效果）。
- `/stop`、`/new` 后几秒内卡片标为"已停止"；回合 5 分钟没有任何输出也会标为"已停止"，恢复输出后自动回到"生成中"。
- 模型的思考过程和 token 用量 Hermes 不暴露给适配器，卡片里没有这两项。
- 定时任务卡片只在网关运行时生效；网关未运行、由独立发送器投递时仍是普通消息。带 `MEDIA:` 附件或超过卡片上限的输出也走普通消息。

### 开发与测试

```bash
# 需要一个 Hermes 检出和带其依赖的 Python；默认用 ~/.hermes/hermes-agent
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests -q
```

### 致谢与许可

卡片的视觉结构（标题状态栏、"思考与工具"折叠面板、"已完成 · 时长 · 模型"页脚）借鉴了 [baileyh8/hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)（MIT）的界面设计，特此致谢。本项目未使用该项目的代码，而是基于 Hermes 上游的原生流式接口独立实现，不修改上游源码。

本项目以 MIT 许可发布（见 `LICENSE`）。它在运行时继承 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT，Copyright (c) 2025 Nous Research）的飞书适配器，但不包含或分发其代码。
