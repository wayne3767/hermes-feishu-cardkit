"""Pure rendering for Feishu streaming cards: card JSON, answer layout, captions.

Nothing here touches the network or adapter state; ``streaming.py`` owns the turn lifecycle and
calls into these functions with plain data.  Layout of a completed card:

    header      title = bot name, subtitle = 生成中 / 已完成 / 已停止, template blue / green / grey
    answer      prose markdown; each figure (image + ``图N　标题`` caption) is its own centred
                element; each table gets a centred ``表N　标题`` element above it
    timeline    collapsible panel "思考与工具 · N 次工具调用", one line per tool call
    hr
    footer      "⏳ 生成中 · N 次工具调用 · <latest tool>" → "✅ 已完成 · 32s · <model> · N 次工具调用"

While streaming, the answer is ONE markdown element (``ANSWER_ELEMENT_ID``) so the CardKit
text-stream API has a stable target; the split into figures / tables happens on the final
full update only.

Acknowledgement: the card's visual structure (header status colours, the collapsed
"思考与工具 · N 次工具调用" panel, the "已完成 · 时长 · 模型" footer) follows the UI of
baileyh8/hermes-feishu-streaming-card (MIT).  That project injects itself into the gateway
source; this module is an independent implementation on the adapter's native streaming
interface and shares no code with it beyond the wording and ``format_duration``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from gateway.platforms.base import MEDIA_TAG_CLEANUP_RE, BasePlatformAdapter
from .math_text import convert_math

ANSWER_ELEMENT_ID = "hermes_answer"
TIMELINE_PANEL_ID = "hermes_timeline"
TIMELINE_ELEMENT_ID = "hermes_timeline_md"
FOOTER_ELEMENT_ID = "hermes_footer"
DEFAULT_CARD_TITLE = "Hermes Agent"
SEED_PLACEHOLDER = "…"
# Feishu rejects card JSON above 30 KB.  CARD_JSON_MAX_BYTES caps the serialized card actually
# sent (envelope, timeline, footer and JSON escaping included); CARD_MAX_BYTES caps the raw answer.
CARD_JSON_MAX_BYTES = 28000
CARD_MAX_BYTES = 24000
TIMELINE_MAX_LINES = 40
TIMELINE_MAX_BYTES = 6000
# Timeline budget when the whole card is over CARD_JSON_MAX_BYTES (the timeline gives way first).
TIMELINE_SLIM_LINES = 8
TIMELINE_SLIM_BYTES = 1500
ACTIVITY_MAX_CHARS = 60

PHASE_RUNNING, PHASE_DONE, PHASE_STOPPED = "running", "done", "stopped"
_PHASE_TEMPLATE = {PHASE_DONE: "green", PHASE_STOPPED: "grey", PHASE_RUNNING: "blue"}


@dataclass(frozen=True)
class Labels:
    """User-visible wording; Feishu (China) shows Chinese, Lark (international) English."""
    running: str
    done: str
    stopped: str
    generating: str  # notification preview while streaming
    timeline_empty: str
    omitted: str  # "{n}" lines dropped from the timeline
    tools: str  # "{n}" tool calls
    figure: str  # "{n}" then a separator then the title
    table: str
    picture: str  # fallback figure title
    tool_only: str  # answer placeholder for a tool-only turn
    clipped_head: str  # heads a running answer that shows only its newest paragraphs
    scheduled: str  # card title / footer word for cron deliveries


ZH = Labels(running="生成中", done="已完成", stopped="已停止", generating="生成中…", timeline_empty="尚无工具调用",
            omitted="… 已省略 {n} 行", tools="{n} 次工具调用", figure="图{n}", table="表{n}", picture="图片", tool_only="✅",
            clipped_head="…（前文较长已省略，完整内容在生成结束后给出）", scheduled="定时任务")
EN = Labels(running="Generating", done="Done", stopped="Stopped", generating="Generating…", timeline_empty="No tool calls yet",
            omitted="… {n} earlier lines omitted", tools="{n} tool calls", figure="Figure {n}", table="Table {n}", picture="Image",
            tool_only="✅", clipped_head="… (earlier text omitted; the full answer follows when done)", scheduled="Scheduled task")
_LABELS = {"zh": ZH, "en": EN}


def labels_for(domain: str, locale: str = "") -> Labels:
    """``locale`` (zh / en) wins; otherwise the Lark international domain gets English."""
    return _LABELS.get((locale or "").lower()) or (EN if (domain or "").lower() == "lark" else ZH)


_FIGURE_BLOCK_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\n<font color=\"grey\">(?:图\d+|Figure \d+)　.*</font>$")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_CAPTION_RE = re.compile(r"^\s*\**\s*(?:表|Table)\s*\d+\s*[.:：、\-–—\s]*(.*?)\s*\**\s*$")
_FIGURE_CAPTION_RE = re.compile(r"^\s*(?:图|Figure|Fig\.?)\s*\d+\s*[.:：、\-–—\s]*(.*?)\s*$")
_FILENAME_NOISE_RE = re.compile(
    r"^(?:hermes_)?fig(?:ure)?[_\-]?|[_\-]?\d{6,}(?:[_\-]\d+)?$|[_\-]?\d{4}-\d{2}-\d{2}[T_\-]?[\d\-:]*$", re.IGNORECASE)
# Marker for a MEDIA line while the answer is re-rendered; replaced before anything is sent.
_MEDIA_PLACEHOLDER = "{{hermes-media-%d}}"
_MEDIA_PLACEHOLDER_RE = re.compile(r"\{\{hermes-media-(\d+)\}\}")

# (local path, image_key, alt text) for one delivered image
ImageItem = Tuple[str, str, str]
MarkdownRenderer = Callable[[str], str]


# --- Text helpers ------------------------------------------------------------------------------

def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Longest prefix of ``text`` whose UTF-8 encoding fits ``max_bytes`` (never splits a char)."""
    if utf8_len(text) <= max_bytes:
        return text
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def json_text_bytes(text: str) -> int:
    """Bytes ``text`` occupies inside the card JSON (quotes and escapes included)."""
    return utf8_len(json.dumps(text, ensure_ascii=False))


def card_json_bytes(card: Dict[str, Any]) -> int:
    """Size of the card exactly as serialized for CardKit (``ensure_ascii=False``)."""
    return utf8_len(json.dumps(card, ensure_ascii=False))


def _fence_open(text: str) -> bool:
    return sum(line.lstrip().startswith("```") for line in text.split("\n")) % 2 == 1


def clip_markdown_head(text: str, max_bytes: int, note: str) -> str:
    """The beginning of ``text`` within ``max_bytes``, cut at a paragraph (else line) boundary,
    an unclosed ``` fence or ``$$`` block closed, then ``note``."""
    if utf8_len(text) <= max_bytes:
        return text
    head = truncate_utf8(text, max(0, max_bytes - utf8_len(note) - 8))  # room for closers + note
    for boundary in ("\n\n", "\n"):
        cut = head.rfind(boundary)
        if cut >= len(head) // 2:
            head = head[:cut]
            break
    head = head.rstrip()
    if _fence_open(head):
        head += "\n```"
    elif head.count("$$") % 2:
        head += "$$"
    return f"{head}\n\n{note}" if head else note


def clip_markdown_tail(text: str, max_bytes: int, note: str) -> str:
    """``note`` then the end of ``text`` within ``max_bytes``, starting at a paragraph (else line)
    boundary; a ``` fence or ``$$`` block open at the cut is reopened so the tail renders alike."""
    if utf8_len(text) <= max_bytes:
        return text
    data = text.encode("utf-8")
    keep = max(0, max_bytes - utf8_len(note) - 8)  # room for the note and a reopener
    tail = data[len(data) - keep:].decode("utf-8", errors="ignore") if keep else ""
    for boundary in ("\n\n", "\n"):
        cut = tail.find(boundary)
        if 0 <= cut <= len(tail) // 2:
            tail = tail[cut + len(boundary):]
            break
    prefix = text[:len(text) - len(tail)]
    opener = "```\n" if _fence_open(prefix) else "$$\n" if prefix.count("$$") % 2 else ""
    return f"{note}\n\n{opener}" + tail.lstrip("\n")


def format_duration(seconds: float) -> str:
    """``32s`` / ``2m57s`` / ``1h2m3s`` (same shape as hermes-feishu-streaming-card's footer)."""
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def render_answer(markdown: str) -> str:
    """Answer markdown as the streaming card shows it: complete LaTeX formulas become Unicode
    math text; an unclosed formula at the end of a frame stays raw until it closes."""
    return convert_math(markdown) if markdown else markdown


def summary_for(markdown: str, phase: str, labels: Labels = ZH) -> str:
    """Notification preview: first answer line once done, a status word otherwise."""
    if phase == PHASE_RUNNING:
        return labels.generating
    prose = (line.strip() for line in markdown.splitlines()
             if line.strip() and not line.lstrip().startswith(("![", "<font", "|", "```")))
    first = next(prose, "")
    return first[:60] if first else labels.done


# --- Timeline / footer ----------------------------------------------------------------------------

def render_timeline(lines: List[str], labels: Labels = ZH, *, max_lines: int = TIMELINE_MAX_LINES,
                    max_bytes: int = TIMELINE_MAX_BYTES) -> str:
    """Timeline markdown: newest ``max_lines`` lines within ``max_bytes`` (0 lines: the count only)."""
    if not lines:
        return labels.timeline_empty
    kept = list(lines[-max_lines:]) if max_lines > 0 else []
    while len(kept) > 1 and utf8_len("\n".join(kept)) > max_bytes:
        kept.pop(0)
    if len(kept) < len(lines):
        kept.insert(0, labels.omitted.format(n=len(lines) - len(kept)))
    return "\n".join(kept)


def timeline_title(tool_count: int, labels: Labels = ZH) -> str:
    prefix = "思考与工具" if labels is ZH else "Thinking & tools"
    return f"{prefix} · {labels.tools.format(n=tool_count)}"


def render_footer(*, phase: str, tool_count: int, elapsed: float, model: str = "", activity: str = "",
                  labels: Labels = ZH) -> str:
    tools = labels.tools.format(n=tool_count)
    if phase == PHASE_DONE:
        parts = [f"✅ {labels.done}", format_duration(elapsed)] + ([model] if model else []) + [tools]
    elif phase == PHASE_STOPPED:
        parts = [f"⏹ {labels.stopped}", format_duration(elapsed)]
    else:
        parts = [f"⏳ {labels.running}", tools] + ([activity] if activity else [])
    return " · ".join(parts)


# --- Figures ----------------------------------------------------------------------------------------

def title_from_filename(path: str, labels: Labels = ZH) -> str:
    """``/tmp/hermes_fig_正态分布密度曲线_1725600000.png`` → ``正态分布密度曲线``."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = _FILENAME_NOISE_RE.sub("", _FILENAME_NOISE_RE.sub("", stem))  # prefix, then suffix
    return stem.replace("_", " ").replace("-", " ").strip() or labels.picture


_TITLE_SAFE = str.maketrans({"[": "［", "]": "］", "(": "（", ")": "）", "\n": " ", "<": "‹", ">": "›"})


def image_block(number: int, title: str, image_key: str, labels: Labels = ZH) -> str:
    """Image plus its caption line; ``layout_answer_elements`` recognises and centres this shape.
    Brackets in the title would end the markdown image syntax early, so they become full-width."""
    title = title.translate(_TITLE_SAFE).strip() or labels.picture
    return f"![{title}]({image_key})\n<font color=\"grey\">{labels.figure.format(n=number)}　{title}</font>"


def append_images(markdown: str, items: List[ImageItem], labels: Labels = ZH) -> str:
    """Images whose position is unknown: numbered figure blocks after the text."""
    blocks = [image_block(n, (alt or "").strip() or title_from_filename(path, labels), key, labels)
              for n, (path, key, alt) in enumerate(items, start=1)]
    return markdown.rstrip() + "\n\n" + "\n\n".join(blocks)


def place_images(raw: str, items: List[ImageItem], render: MarkdownRenderer, labels: Labels = ZH) -> Optional[str]:
    """Rebuild the answer from the raw response so each image sits where its ``MEDIA:`` line
    stood: tags → placeholders, ``render`` (math etc.) on the cleaned text, placeholders → figure
    blocks.  A ``图N：…`` line right after a tag becomes that figure's title.  Files the raw text
    never mentioned are appended.  None when no delivered file is referenced at all."""
    by_name = {os.path.basename(path): index for index, (path, _key, _alt) in enumerate(items)}
    numbers: Dict[int, int] = {}  # item index → figure number
    explicit: Dict[int, str] = {}

    def _swap(match: "re.Match[str]") -> str:
        tag_path = os.path.expanduser(match.group("path").strip("`\"'"))
        index = by_name.get(os.path.basename(tag_path))
        if index is None or index in numbers:
            return match.group(0)
        numbers[index] = len(numbers) + 1
        return _MEDIA_PLACEHOLDER % index

    lines = MEDIA_TAG_CLEANUP_RE.sub(_swap, raw).split("\n")
    if not numbers:
        return None
    for i, line in enumerate(lines):
        match = _MEDIA_PLACEHOLDER_RE.search(line)
        if not match:
            continue
        for j in range(i + 1, min(i + 3, len(lines))):  # caption may follow after one blank line
            if not lines[j].strip():
                continue
            caption = _FIGURE_CAPTION_RE.match(lines[j].strip("* "))
            if caption and caption.group(1):
                explicit[int(match.group(1))] = caption.group(1)
                lines[j] = ""
            break
    text = render(BasePlatformAdapter.strip_media_directives_for_display("\n".join(lines)))
    for index in range(len(items)):
        if index not in numbers:
            numbers[index] = len(numbers) + 1
            text = text.rstrip() + "\n\n" + _MEDIA_PLACEHOLDER % index

    def _fill(match: "re.Match[str]") -> str:
        index = int(match.group(1))
        path, key, alt = items[index]
        title = (explicit.get(index) or alt or "").strip() or title_from_filename(path, labels)
        return image_block(numbers[index], title, key, labels)

    return re.sub(r"\n{3,}", "\n\n", _MEDIA_PLACEHOLDER_RE.sub(_fill, text)).strip()


# --- Element layout ---------------------------------------------------------------------------------

def _is_table(lines: List[str], start: int) -> bool:
    return (len(lines) > start + 1 and bool(_TABLE_LINE_RE.match(lines[start]))
            and set(lines[start + 1].strip()) <= set("|:- ") and "-" in lines[start + 1])


def layout_answer_elements(markdown: str, labels: Labels = ZH, *, number_tables: bool = True) -> List[Dict[str, Any]]:
    """Completed-card body: prose stays markdown; a figure block becomes a centred element (caption
    centred under the image); a table gets a centred ``表N　标题`` element above it, the title taken
    from a ``表N：…`` line the model wrote just before the table (same or previous paragraph).
    ``number_tables=False`` (cron cards): no ``表N`` numbering, a caption line only when one was written."""
    elements: List[Dict[str, Any]] = []
    prose: List[str] = []
    table_no = 0

    def _flush_prose() -> None:
        text = "\n\n".join(prose).strip("\n")
        prose.clear()
        if text.strip():
            elements.append({"tag": "markdown", "content": text})

    def _pop_caption(head: List[str]) -> str:
        """Take a ``表N：标题`` line off the end of ``head`` or off the previous paragraph."""
        if head and _TABLE_CAPTION_RE.match(head[-1]):
            return _TABLE_CAPTION_RE.match(head.pop()).group(1)
        if not head and prose and _TABLE_CAPTION_RE.match(prose[-1].strip()) and "\n" not in prose[-1].strip():
            return _TABLE_CAPTION_RE.match(prose.pop().strip()).group(1)
        return ""

    in_fence = False
    for block in re.split(r"\n{2,}", markdown.strip("\n")):
        fenced = in_fence  # a paragraph inside an open ``` block is code, never a table or figure
        if block.count("```") % 2:
            in_fence = not in_fence
        if fenced or block.lstrip().startswith("```"):
            prose.append(block)
            continue
        if _FIGURE_BLOCK_RE.match(block.strip()):
            _flush_prose()
            elements.append({"tag": "markdown", "text_align": "center", "content": block.strip()})
            continue
        lines = block.split("\n")
        start = next((i for i in range(len(lines)) if _is_table(lines, i)), None)
        if start is None:
            prose.append(block)
            continue
        head, table = lines[:start], lines[start:]
        title = _pop_caption(head)
        if head:
            prose.append("\n".join(head))
        _flush_prose()
        table_no += 1
        caption = labels.table.format(n=table_no) + (f"　{title}" if title else "") if number_tables else title
        if caption:
            elements.append({"tag": "markdown", "text_align": "center", "content": f"<font color=\"grey\">{caption}</font>"})
        elements.append({"tag": "markdown", "content": "\n".join(table)})
    _flush_prose()
    for index, element in enumerate(elements):
        element["element_id"] = ANSWER_ELEMENT_ID if index == 0 else f"{ANSWER_ELEMENT_ID}_{index}"
    return elements or [{"tag": "markdown", "element_id": ANSWER_ELEMENT_ID, "content": SEED_PLACEHOLDER}]


def build_stream_card(
    *, title: str, markdown: str, timeline_md: str, tool_count: int, footer: str, phase: str, summary: str = "",
    labels: Labels = ZH, streaming: Optional[bool] = None, plain: bool = False,
) -> Dict[str, Any]:
    """Card JSON 2.0 for one turn.  ``markdown`` is the answer exactly as it should show (math and
    images already rendered); streaming mode is on only while ``phase`` is running (``streaming=False``
    keeps it off: a running card past CardKit's streaming window).  ``plain`` keeps a finished
    answer in one element instead of the figure / table layout (the slim fallback card)."""
    subtitle = {PHASE_DONE: labels.done, PHASE_STOPPED: labels.stopped}.get(phase, labels.running)
    template = _PHASE_TEMPLATE[phase]
    running = phase == PHASE_RUNNING
    streaming = running and streaming is not False
    body = markdown or SEED_PLACEHOLDER
    elements = ([{"tag": "markdown", "element_id": ANSWER_ELEMENT_ID, "content": body}] if running or plain
                else layout_answer_elements(body, labels))
    if running or tool_count:
        elements.append({
            "tag": "collapsible_panel", "element_id": TIMELINE_PANEL_ID, "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": timeline_title(tool_count, labels)},
                       "vertical_align": "center", "padding": "4px 0px 4px 8px"},
            "border": {"color": "grey", "corner_radius": "8px"},
            "elements": [{"tag": "markdown", "element_id": TIMELINE_ELEMENT_ID, "content": timeline_md}],
        })
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "element_id": FOOTER_ELEMENT_ID, "text_size": "notation", "content": footer})
    config: Dict[str, Any] = {"streaming_mode": streaming, "update_multi": True}
    if streaming:
        config["streaming_config"] = {"print_frequency_ms": {"default": 50}, "print_step": {"default": 2}, "print_strategy": "fast"}
    if summary:
        config["summary"] = {"content": summary}
    return {
        "schema": "2.0",
        "config": config,
        "header": {"title": {"tag": "plain_text", "content": title},
                   "subtitle": {"tag": "plain_text", "content": subtitle}, "template": template},
        "body": {"elements": elements},
    }


def timeline_update_actions(tool_count: int, timeline_md: str, footer: str, labels: Labels = ZH) -> List[Dict[str, Any]]:
    """CardKit batch actions refreshing the panel title, its body and the footer in one call."""
    def _partial(element_id: str, partial: Dict[str, Any]) -> Dict[str, Any]:
        return {"action": "partial_update_element", "params": {"element_id": element_id, "partial_element": partial}}
    return [
        _partial(TIMELINE_ELEMENT_ID, {"content": timeline_md}),
        _partial(TIMELINE_PANEL_ID, {"header": {"title": {"tag": "plain_text", "content": timeline_title(tool_count, labels)}}}),
        _partial(FOOTER_ELEMENT_ID, {"content": footer}),
    ]


def footer_update_actions(footer: str) -> List[Dict[str, Any]]:
    """Batch action rewriting the footer alone (last-resort seal when full updates keep failing)."""
    return [{"action": "partial_update_element", "params": {"element_id": FOOTER_ELEMENT_ID, "partial_element": {"content": footer}}}]


__all__ = [
    "ANSWER_ELEMENT_ID", "TIMELINE_PANEL_ID", "TIMELINE_ELEMENT_ID", "FOOTER_ELEMENT_ID", "DEFAULT_CARD_TITLE",
    "SEED_PLACEHOLDER", "CARD_JSON_MAX_BYTES", "CARD_MAX_BYTES", "TIMELINE_MAX_LINES", "TIMELINE_MAX_BYTES",
    "TIMELINE_SLIM_LINES", "TIMELINE_SLIM_BYTES",
    "ACTIVITY_MAX_CHARS", "PHASE_RUNNING", "PHASE_DONE", "PHASE_STOPPED", "ImageItem", "Labels", "ZH", "EN", "labels_for",
    "utf8_len", "truncate_utf8", "json_text_bytes", "card_json_bytes", "clip_markdown_head", "clip_markdown_tail",
    "format_duration", "render_answer", "summary_for", "render_timeline",
    "timeline_title", "render_footer", "title_from_filename", "image_block", "append_images", "place_images",
    "layout_answer_elements", "build_stream_card", "timeline_update_actions", "footer_update_actions",
]
