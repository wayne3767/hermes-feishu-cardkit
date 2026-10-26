# Changelog

## 0.5.0 — 2026-10-27

- Move the tool panel to the top of the card body, directly under the header and above the answer,
  so it reads as one line ("思考与工具 · N 次工具调用") until the reader expands it. The expanded
  panel still lists every tool and skill call (`skill_view`, `terminal`, …) in order.
- Show what the turn is actually doing: 思考中 before anything arrives, 调用工具中 once a tool call
  is recorded but no prose has streamed, 生成中 while text flows, plus the existing waiting,
  stopped, failed and timed-out states. While the card is quiet the footer refreshes every few
  seconds, so the elapsed time keeps moving during a long tool call. The header subtitle is
  refreshed through a full update only while no answer text exists, so a status change never
  interrupts the typewriter.
- Simplify the footer to status · elapsed · model, matching the card style this plugin follows.
  The model is known before the first token (from `pre_llm_call`) and is replaced by the model the
  response actually served once that event arrives. Per-turn tokens and context usage move behind
  `FEISHU_CARD_FOOTER_USAGE` / `platforms.feishu.card_footer_usage` (default off).
- Offline preview and tests updated for the new layout and wording.

## 0.4.0 — 2026-10-16

- Keep the native platform plugin architecture; no sidecar, Hermes source patches or model calls.
- Preserve native table content with automatic row height, content-weighted widths, extra data
  columns, and whole-table Markdown fallback for unsupported structures or cells over 1,200
  characters. Numeric alignment, 10-row paging and five-table budget remain.
- Observe official Hermes lifecycle events using exact admitted message, profile, session and
  agent-turn identities. Show waiting, declined, timed-out and failed states; five-minute text
  silence now means "No recent progress", not "Stopped". An interaction boundary finalizes a
  segment, not the entire turn; closed segments keep observing real resolution and retire safely.
- Display actual model/provider, deduplicated per-turn input/output and latest request context
  usage. Missing/ambiguous observations stay hidden; configured defaults are not execution proof.
- Bind Stop to the original card, initiator, routed profile and topic through the bundled guards.
  Unknown, foreign and expired clicks cannot stop another session. Missing upstream seams hide
  controls without blocking normal message delivery.
- Require the core-pinned runtime home and exact session identity for observations and clarification
  controls. Bind Stop to the original processing guard, revalidate after the real chat lock, reject
  terminal turns immediately, and fail closed on ambiguous topic anchors. A broken profile setting
  reader no longer falls back to another profile's process environment.
- Add native single-choice clarification cards using Hermes's original resolver, retaining
  question/options and real answers. Timeout/cancel removes controls; multi-select, open-ended
  and unproven routes use the native text path. Verified same-receipt navigation is optional;
  no link is fabricated when the platform omits message position/link metadata.
- Refresh settled clarification cards after late send acknowledgements; never update an empty
  message id. Webhook transports retain the native text path because the bundled webhook handler
  does not return card updates. Older Hermes without pinned identity keeps basic cards, without
  optional controls or usage observations that cannot be safely attributed.
- Add classic/task/detailed reading presets, explicit width/timeline overrides and redacted
  display-setting provenance. An offline CLI generates ten state examples without SDK or network
  imports; HTML is schematic, not a claim of client rendering acceptance.
- Retain the bundled approval workflow and delivery retries. No exactly-once network-delivery
  guarantee is added. Desktop/mobile display and real clicks still need deployment-time acceptance.

## 0.3.0 — 2026-09-26

- Stop button: generating cards carry a "停止生成 / Stop" button; a click is dispatched as `/stop`
  from the clicker through the bundled guarded pipeline (authorization unchanged) and answered
  with a toast.  `FEISHU_CARD_STOP_BUTTON=false` hides it; it is also hidden when the bundled
  adapter lacks the card-callback seams.
- Long tables: in completed cards (and cron cards) markdown tables with more than 5 data rows
  become Card JSON 2.0 `table` components (10 rows per page, alignment from the separator row,
  numeric columns right-aligned, first column frozen when wide), up to 5 per card.
- Math: formulas still being typed are held back from streaming frames (no raw LaTeX flash);
  subscripts without a Unicode form are written run-on (A_d → Ad, V_{daf} → Vdaf, Q_{gr,d} →
  Qgr,d); `\mathrm{g/cm^3}` → g/cm³ (math-font commands parse their argument); `cases` rows
  read "value，condition".
- Fixed: any `$` in an answer stripped the indentation of every line (nested lists flattened).
- Fixed: the cron card footer showed the server clock's zone (UTC on servers) instead of Hermes's
  configured `timezone`.
- Test guarding that the plugin's `feishu` registration keeps every field the bundled entry has.

## 0.2.2 — 2026-09-26

- Cron cards use a violet header instead of blue, so scheduled pushes no longer look like a chat
  turn still generating (chat cards keep blue / green / grey for running / done / stopped).
  Failures stay red.  `FEISHU_CRON_CARD_TEMPLATE` (`platforms.feishu.cron_card_template`) picks
  another Feishu card template; unknown names fall back to violet.

## 0.2.1 — 2026-09-26

- Cron cards strip Hermes's default English wrapper ("Cronjob Response: <name> / (job_id: …) /
  -----" and the "To stop or manage this job…" trailer, on unless `cron.wrap_response: false`).
  The job name moves to the footer ("⏰ 定时任务 · <name> · MM-DD HH:MM") and titles cards whose
  text has no title line.  0.2.0 showed the wrapper line as the card title.

## 0.2.0 — 2026-09-26

- Cron deliveries as cards: when Hermes delivers a scheduled job's output (`metadata["job_id"]`),
  the text goes out as one static Card JSON 2.0 message instead of a `post`.  The first line (or
  a leading `# heading`) becomes the header, a trailing `（…）` its subtitle, and an H1 that
  restates the title line is dropped; the body keeps headings and tables (not numbered); the
  footer reads "⏰ 定时任务 · MM-DD HH:MM".  Headers mentioning a failure are red.
- Output with `MEDIA:` lines, over the 28 000-byte card budget, or rejected by Feishu goes out
  through the bundled `send` unchanged.  `FEISHU_CRON_CARD=false` (`platforms.feishu.cron_card`)
  turns the feature off.

## 0.1.1 — 2026-09-24

Reliability fixes for long turns and failed card updates (no layout changes).

- Final seal no longer leaves a card "生成中": the completed-layout `card.update` is retried
  (1 s, 3 s), then a slim card (one plain answer element, tool lines reduced to their count) is
  tried, and as a last resort the footer is rewritten and streaming mode switched off.
- Long turns: the abandon watchdog is idle-based (no frame for 5 min) instead of a fixed 10 min
  after the card opened, so active turns are never sealed "已停止" mid-run; past CardKit's
  10-minute streaming window frames go out as full updates of the same card instead of being
  dropped.  A frame for an idle-sealed turn revives the same card rather than opening a second one.
- `/stop` and `/new`: `interrupt_session_activity` marks the chat's open cards, which are sealed
  "已停止" once silent for 5 s (the consumer sends no final frame for a cancelled native stream).
- Card size: the whole serialized card is measured against a 28 000-byte budget; the tool
  timeline shrinks first, then the answer is cut at a paragraph boundary with open code fences /
  `$$` blocks closed.  An answer that outgrows the card mid-stream shows its newest paragraphs
  under a note instead of freezing.
- Card-open failures: three non-permission failures in a row pause streaming cards for 10 min
  (then one trial open) instead of disabling them until restart; missing `cardkit:card:write`
  (99991672) still disables them.
- Full-update, footer and close failures are logged at WARNING with the Feishu error code (no
  content); intermediate frame failures stay at DEBUG.
- Post-stream images whose response matches no card only go into the chat's single card sealed
  within 10 s; otherwise they are sent as ordinary messages rather than guessed into a card.
- The watchdog no longer cancels itself before its seal completes.

## 0.1.0 — 2026-09-06

First release: CardKit streaming card per turn (typewriter answer, status header, collapsed tool
timeline, footer), LaTeX → Unicode, optional typeset formula images (matplotlib), in-card `MEDIA:`
figures at their positions with numbered captions, titled tables, zh / en wording, and full
fallback to the bundled adapter on any failure. Validated against Hermes 0.21.0 (2026-08-31) and
origin/main (2026-09-06).
