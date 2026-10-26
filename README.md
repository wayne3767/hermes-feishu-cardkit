# hermes-feishu-cardkit

Native streaming cards for the Feishu / Lark channel of [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Version **0.5.0** puts the tool line first, names what the turn is doing, and trims the footer to status · duration · model.

[English](#english) · [中文](#中文)

## English

**Feishu and Lark are the same product.** ByteDance ships it as Feishu (飞书, `feishu.cn`) inside China and as Lark (`larksuite.com`) everywhere else. Hermes's bundled adapter talks to both through one SDK; the `FEISHU_DOMAIN` setting (`feishu` or `lark`) selects the endpoint. This plugin works on both and picks its wording from that setting: Chinese card labels on Feishu, English on Lark (override with `FEISHU_CARD_LOCALE`).

### What it changes

Out of the box, Hermes delivers a Feishu/Lark reply as a plain message that gets edited over and over while the model writes. This plugin uses **native CardKit streaming cards**, preserving separate segments when Hermes pauses for interaction:

- The card appears the moment the model starts; text renders with the platform's typewriter animation as tokens arrive.
- The status word follows the turn: **Thinking** before anything arrives, **Working** once a tool call is recorded but no prose has streamed, **Generating** while text flows, then **Done** (plus waiting, stopped, failed and timed-out states). While the card is quiet the footer refreshes every few seconds so the elapsed time keeps moving — a long tool call never looks frozen.
- Every tool and skill call folds into **one line at the top of the card**, above the answer: "Thinking & tools · N tool calls". Expanding it lists the whole chain of tool and skill calls in order.
- The footer reads **status · elapsed · model**. The model is known before the first token and is replaced by the model the response actually served once that event arrives; per-turn tokens and context usage are one opt-in setting.
- Inline LaTeX becomes Unicode math text (`\delta_p` → δₚ, `\frac{a}{b}` → (a)/(b)); display formulas are typeset into images when `matplotlib` is available.
- Images the model attaches (`MEDIA:` lines) are placed inside the card where the model put them, with centred **"Figure N · title"** captions; markdown tables get a **"Table N · title"** heading, and tables longer than 5 rows become paged table components (10 rows per page) once the answer is done.
- A **Stop** button is bound to the original initiator, session and topic; unknown or expired cards have no usable control.
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
| `FEISHU_CARD_FOOTER_USAGE` / `platforms.feishu.card_footer_usage` | `false` | `true` adds provider, per-turn input/output tokens and context usage to the footer |
| `FEISHU_CARD_READING_PRESET` / `platforms.feishu.card_reading_preset` | `classic` | `classic`, `task` or `detailed` |
| `FEISHU_CARD_TIMELINE_EXPANDED` / `platforms.feishu.card_timeline_expanded` | preset | Explicit timeline expansion override |
| `FEISHU_CARD_WIDTH_MODE` / `platforms.feishu.card_width_mode` | preset | `default`, `compact` or `fill` |
| `display.platforms.feishu.tool_progress` | Hermes default `new` | `all` records every tool call in the timeline, without merging repeats |

Streaming as a whole is governed by Hermes's general `streaming.enabled` setting.

### Letting the model produce figures and table titles

The card can embed figures and caption them, but whether the model draws anything is up to your prompt. These are Feishu-only conventions, so put them in the Feishu platform hint rather than `SOUL.md` (other channels would follow them too) — a top-level `platform_hints:` → `feishu:` → `append: |` block in the profile's `config.yaml`, with rules like:

```
- When explaining distributions, curves or geometry, showing data trends or comparisons, or describing how a system works, draw a figure with Python matplotlib.
- Name the file after the figure's title: /tmp/hermes_fig_<title>_<timestamp>.png. Put the MEDIA:/tmp/….png line where the figure belongs in the text; the card adds "Figure N · title" itself, so do not write your own caption.
- Put a single line "Table N: title" above every markdown table. Tables longer than 5 rows show paged (10 rows per page, at most 5 such tables per card).
- Cards do not render LaTeX; the plugin converts it. Use $…$ for inline symbols (shown as Unicode text) and $$…$$ on their own lines for fractions, sums and integrals (typeset as images). Display formulas cannot contain CJK text or aligned / cases / matrix environments.
```

### Known limits

- Feishu pages markdown tables at 5 data rows; longer tables become table components (10 rows per page) in the finished card, at most 5 per card; while generating they show as markdown.
- Stop retains the exact original source, including topic and routed profile, the processing task/guard and the official agent-turn identity. It is revalidated inside the bundled chat lock. Ambiguous topic anchors, ended turns and missing observations hide or reject the control; the ordinary `/stop` command remains available.
- Subscripts without a Unicode form are written run-on (`$A_d$` → Ad, `$V_{daf}$` → Vdaf), as plain-text coal and chemistry notation does.
- Matrices, `cases`, multi-line alignment environments and formulas containing CJK text are not typeset as images; they fall back to Unicode.
- Card JSON is capped at 30 KB; the whole card is measured, tool lines give way first, and an oversized answer shows its head in the card while the full text goes out as ordinary messages. While streaming, an oversized answer shows its newest paragraphs.
- CardKit ends streaming mode after 10 minutes; longer turns keep updating the same card with full updates (no typewriter effect).
- `/stop` and `/new` affect only cards matched to that session. Five minutes without text means "No recent progress"; an observed human wait remains waiting. Interaction boundaries are labeled as closed segments, not completed tasks.
- Metrics use official events and exact inbound identity. Unsupported Hermes versions, ambiguous routing and some queued messages have no metrics: the footer then shows only status, elapsed time and (when `pre_llm_call` matched) the declared model. Missing values are hidden; input totals use canonical prompt tokens including cache buckets. Context usage describes the latest request rather than cumulative spend. Raw model reasoning is not exposed.
- Single-choice clarification cards use the original Hermes resolver. Multi-select/open-ended prompts and webhook mode keep the bundled text interaction. Approval handling remains bundled. Navigation requires a matching successful platform receipt; transport retries can still duplicate delivery after an ambiguous network failure.
- Optional observations and native clarification need Hermes's pinned routing identity and lifecycle hooks. Older versions retain basic cards and text interaction; uncertain profile ownership never borrows another profile's state or settings.
- Auto-height tables and widths are verified as JSON, not on every desktop/mobile client. Extreme widths, long Chinese cells and client height caps still require visual acceptance.
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

Hermes 的飞书通道默认把回复作为普通消息反复编辑。这个插件使用**原生飞书 CardKit 流式卡片**，遇到审批或澄清时按 Hermes 的实际流程保留前后片段：

- 模型开始生成时卡片立刻出现，文字以打字机效果逐步渲染
- 状态词跟着回合走：还没有任何输出时是“思考中”，已记录工具调用但还没有正文时是“调用工具中”，文字流动时是“生成中”，结束时是“已完成”（另有等待确认、等待输入、已停止、执行失败、已超时等状态）。卡片静默时页脚每几秒刷新一次时长，长时间的单个工具调用也不会显得卡死
- 每一次工具和技能调用都收进**正文最前、标题栏下的那一行**“思考与工具 · N 次工具调用”，点开即可看到完整的工具与技能调用链
- 页脚为**状态 · 时长 · 模型**：模型在首个 token 前即可显示，收到官方事件后换成实际服务的模型；本轮 token 与上下文占用改为可选配置
- 标题栏区分生成、等待、停止、失败和未知状态
- 行内 LaTeX 公式转成 Unicode 数学文本；块级公式在装了 matplotlib 时排版成图片
- 模型附带的图片（`MEDIA:` 行）嵌进卡片正文对应位置，带居中的"图N 标题"图注；表格上方加"表N 标题"表题；超过 5 行的表格在完成后改为分页表格组件（每页 10 行）
- "停止生成"按钮绑定原发起者、会话和话题；来源不明、越权或过期的点击不能控制其他回合
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
| `FEISHU_CARD_FOOTER_USAGE` / `platforms.feishu.card_footer_usage` | `false` | 设为 `true` 时页脚额外显示供应方、本轮输入/输出 token 与上下文占用 |
| `FEISHU_CARD_READING_PRESET` / `platforms.feishu.card_reading_preset` | `classic` | `classic` 默认布局、`task` 任务布局、`detailed` 展开过程 |
| `FEISHU_CARD_TIMELINE_EXPANDED` / `platforms.feishu.card_timeline_expanded` | 随预设 | 显式指定过程面板是否展开 |
| `FEISHU_CARD_WIDTH_MODE` / `platforms.feishu.card_width_mode` | 随预设 | `default`、`compact`、`fill` |
| `display.platforms.feishu.tool_progress` | Hermes 默认 `new` | 设为 `all` 时时间线记录每一次工具调用，连续同名调用不合并 |

流式本身受 Hermes 通用设置 `streaming.enabled` 控制。

### 让模型配图和写表题

卡片能嵌图、写图注和表题，但图从哪来由模型决定。这些是飞书专用约定，建议写进飞书平台提示而不是 `SOUL.md`（否则其他渠道也会照做）：在 profile 的 `config.yaml` 顶层写 `platform_hints:` → `feishu:` → `append: |`，后接类似下面的规则：

```
- 解释概率分布、函数曲线、几何关系，或给出数据趋势、对比、分布，或说明设备与系统原理时，用 Python matplotlib 画图。
- 脚本开头设置中文字体：plt.rcParams['font.family'] = ['Hiragino Sans GB', 'Arial Unicode MS']；plt.rcParams['axes.unicode_minus'] = False。
- 文件名就是图的中文标题：/tmp/hermes_fig_<中文标题>_<时间戳>.png。MEDIA:/tmp/…png 单独一行，放在正文中图应出现的位置。卡片会自动加"图N 标题"，不要自己写图注。
- 每个表格上方单独一行写"表N：标题"。超过 5 行的表格会分页显示（每页 10 行，一张卡片最多 5 个）。
- 卡片不渲染 LaTeX，由插件转换：行内符号用 $…$（转成 Unicode 文本），分式、求和、积分用单独成行的 $$…$$（转成公式图片）。块级公式里不写中文，不用 aligned、cases、matrix 等环境。
```

### 已知限制

- 飞书 Markdown 表格每页固定 5 行、超出分页；完成后的卡片里，超过 5 行的表格改为表格组件（每页 10 行），一张卡片最多 5 个；生成过程中仍是 Markdown 表格。
- "停止生成"绑定原始消息的话题、用户、路由 profile、处理任务与官方回合身份，在原生会话锁内再次核验。话题锚点有歧义、原任务已结束或缺少可靠回合记录时隐藏或拒绝控件，文字 `/stop` 仍沿用原流程。
- 没有 Unicode 下标字形的下标按连写处理（`$A_d$` → Ad，`$V_{daf}$` → Vdaf），与煤质、化学的纯文本写法一致。
- 矩阵、分段函数、多行对齐环境和含中文的公式不做图片排版，退回 Unicode 文本。
- 卡片 JSON 上限 30 KB；按整张卡片计量，先压缩工具时间线；仍超长的回答在卡片里显示开头部分，完整内容由 Hermes 按普通消息发出。生成过程中超长时显示最新几段。
- CardKit 流式模式 10 分钟后自动关闭；更长的回合继续用整卡更新刷新同一张卡片（没有打字机效果）。
- `/stop`、`/new` 只更新精确匹配会话的卡片。5 分钟无文本显示"暂未收到进展"；已观测到的人工等待保持等待状态。审批/澄清前的流式结束仅表示"本段已结束"，不会冒充整个任务完成。
- 模型和用量通过官方事件按原始消息、profile、会话和回合关联。缺少事件、归属有歧义或部分排队路径缺少准入事件时，隐藏用量统计；模型名优先取官方事件，取不到时用 `pre_llm_call` 的声明值（不是配置默认值）。输入总量使用包含缓存的 canonical prompt tokens；上下文占用取最近请求，不是本轮累计消耗。不显示模型原始思考。
- 单选澄清使用原生按钮与原 Hermes resolver，保留问题、选项和实际回答；多选、开放式问题、webhook 模式和来源无法证明的请求沿用原文本交互。迟到发送回执会补刷已超时或已回答的问题。审批继续使用 Hermes 原有流程。只有同一次成功送达回执中的链接或位置得到验证才显示"查看续答"；网络回执丢失时，上游重试仍可能重复投递。
- 新统计和原生澄清依赖上游固定的路由身份及生命周期事件。旧版 Hermes 保留基础卡片与文本交互，不显示无法证明归属的控件和统计。
- 原生表格使用自动行高、按内容分配列宽；超过 1,200 字符的表头/单元格、超过 50 列或不能可靠解析的整表保留为 Markdown。极宽表格、长中文单元格与客户端高度限制仍需实机检查，不以 JSON 测试代替。
- 定时任务卡片只在网关运行时生效；网关未运行、由独立发送器投递时仍是普通消息。带 `MEDIA:` 附件或超过卡片上限的输出也走普通消息。

### 开发与测试

`0.5.0` 默认仍为 `classic` 折叠布局；`task` 默认折叠并使用 `fill` 宽度，`detailed` 默认展开。显式字段覆盖预设，YAML 优先于当前 profile 的环境设置。适配器创建时保存读取结果；运行中的 `adapter.describe_card_config()` 只返回已加载的三个展示字段及来源，不输出凭据。profile 读取器失效时使用安全默认值，不回退到另一 profile 的进程级环境。

离线预览不加载插件、网关或 SDK，也不连接飞书：

```bash
python3 -B tools/preview_cards.py --output /tmp/cardkit-preview
# 可选：仅读取指定配置文件的三个展示字段（需要已有 PyYAML）
python3 -B tools/preview_cards.py --config /path/to/config.yaml --output /tmp/cardkit-preview
```

输出 10 种状态的卡片 JSON、`manifest.json` 和 `index.html`，共 12 个文件。HTML 是结构示意，不是飞书客户端截图。`--config` 来源标记为 `file/next-load`，不冒充进程已加载配置；不会修改配置或重启服务。

```bash
# 需要一个 Hermes 检出和带其依赖的 Python；默认用 ~/.hermes/hermes-agent
HERMES_AGENT_ROOT=/path/to/hermes-agent python -m pytest tests -q
```

### 致谢与许可

卡片的视觉结构（标题状态栏、"思考与工具"折叠面板、"已完成 · 时长 · 模型"页脚）借鉴了 [baileyh8/hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)（MIT）的界面设计，特此致谢。本项目未使用该项目的代码，而是基于 Hermes 上游的原生流式接口独立实现，不修改上游源码。

本项目以 MIT 许可发布（见 `LICENSE`）。它在运行时继承 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT，Copyright (c) 2025 Nous Research）的飞书适配器，但不包含或分发其代码。
