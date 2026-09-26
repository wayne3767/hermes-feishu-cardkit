# Changelog

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
