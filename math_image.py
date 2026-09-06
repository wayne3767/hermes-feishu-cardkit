"""Typeset display formulas for Feishu cards with matplotlib's mathtext engine.

Unicode math text (math_text.py) is legible but flat: it cannot stack a fraction or nest an
exponent.  For ``$$…$$`` / ``\\[…\\]`` blocks the card can do better — render the formula to a
PNG, upload it as a message image and embed it with ``![公式](image_key)``.  matplotlib is an
optional dependency (``pip install matplotlib``); without it, or for input mathtext cannot parse
(environments, line breaks, CJK glyphs), callers fall back to the Unicode rewrite.

Rendering is CPU-only and ~10 ms per formula once the engine is warm; the first import costs a
few hundred ms, so callers run this off the event loop.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import re
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger("hermes_feishu_cardkit")

MATH_IMAGE_FONTSIZE = 15
MATH_IMAGE_DPI = 200
# Feishu scales an embedded image to the card width, so a short formula on a tight crop shows up
# huge.  Every formula is therefore centred on a canvas of this fixed width (inches at DPI): the
# glyph size on screen is then the same for "P(A)=0.01" and a long integral alike.
MATH_IMAGE_CANVAS_INCHES = 7.0
MATH_IMAGE_MIN_HEIGHT_INCHES = 0.45
_CACHE_MAX = 64
_CJK_RE = re.compile("[　-鿿＀-￯]")  # CJK punctuation, ideographs, full-width forms
_UNSUPPORTED_RE = re.compile(r"\\begin\{|\\end\{|\\\\|\\over(?![a-zA-Z])|\\underbrace|\\overbrace|\\stackrel|\\substack|\\mbox|\\tag")
_REWRITES = (
    (re.compile(r"\\(?:displaystyle|textstyle|scriptstyle|nonumber|limits|nolimits)\b"), ""),
    (re.compile(r"\\(?:dfrac|tfrac)\b"), r"\\frac"),
    (re.compile(r"\\le\b"), r"\\leq"), (re.compile(r"\\ge\b"), r"\\geq"), (re.compile(r"\\ne\b"), r"\\neq"),
    (re.compile(r"\\(?:textbf|textit|textrm|bm)\b"), r"\\mathrm"),
    (re.compile(r"\\boldsymbol\b"), r"\\mathbf"),
    (re.compile(r"\\(?:qquad)\b"), r"\\quad\\quad"),
)
# command → (brace groups to drop first, keep the last group's content)
_UNWRAP = {"boxed": (0, True), "underline": (0, True), "overline": (0, True), "textcolor": (1, True), "color": (1, False)}

_cache: "OrderedDict[str, Optional[bytes]]" = OrderedDict()
_cache_lock = threading.Lock()
# mathtext's parser keeps module-level state and is NOT thread-safe: two formulas parsed at the
# same time from the SDK worker pool corrupt each other.  Rendering is ~10 ms, so serialise it.
_render_lock = threading.Lock()
_available: Optional[bool] = None


def mathtext_available() -> bool:
    """Is matplotlib importable?  Probed once, never imports the package here."""
    global _available
    if _available is None:
        _available = importlib.util.find_spec("matplotlib") is not None
    return _available


def _group_end(latex: str, pos: int) -> int:
    """Index of the ``}`` closing the group that opens at ``pos`` (or len(latex))."""
    depth = 0
    for i in range(pos, len(latex)):
        if latex[i] == "{":
            depth += 1
        elif latex[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(latex)


def _unwrap(latex: str, name: str, *, skip: int = 0, keep: bool = True) -> str:
    """Drop ``\\name`` with its ``[opt]`` and ``skip`` leading brace groups; the last group is
    replaced by its content when ``keep`` (``\\boxed{x}`` → ``x``), else removed (``\\color{red}``)."""
    marker = f"\\{name}"
    while True:
        start = latex.find(marker)
        if start < 0:
            return latex
        pos = start + len(marker)
        if pos < len(latex) and latex[pos] == "[":
            pos = latex.find("]", pos) + 1 or len(latex)
        for _ in range(skip):
            if pos < len(latex) and latex[pos] == "{":
                pos = _group_end(latex, pos) + 1
        if pos >= len(latex) or latex[pos] != "{":
            latex = latex[:start] + latex[pos:]
            continue
        end = _group_end(latex, pos)
        content = latex[pos + 1:end] if keep else ""
        latex = latex[:start] + content + latex[end + 1:]


def prepare_for_mathtext(latex: str) -> Optional[str]:
    """Normalise a LaTeX body for mathtext, or None when it cannot be typeset."""
    body = re.sub(r"\s*\n\s*", " ", latex).strip()
    if not body or _UNSUPPORTED_RE.search(body) or _CJK_RE.search(body):
        return None
    for name, (skip, keep) in _UNWRAP.items():
        body = _unwrap(body, name, skip=skip, keep=keep)
    for pattern, replacement in _REWRITES:
        body = pattern.sub(replacement, body)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body or None


def formula_digest(latex: str) -> str:
    return hashlib.sha1(latex.encode("utf-8")).hexdigest()[:16]


def render_formula_png(latex: str) -> Optional[bytes]:
    """PNG bytes for one formula body, or None (unsupported input / mathtext parse error)."""
    body = prepare_for_mathtext(latex)
    if body is None or not mathtext_available():
        return None
    key = formula_digest(body)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with _render_lock:
        png = _render(body)
    with _cache_lock:
        _cache[key] = png
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return png


def _render(body: str) -> Optional[bytes]:
    """Draw with the object-oriented Agg API: no pyplot global state, safe from a worker thread."""
    global _available
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except Exception as exc:  # present but broken: stop trying for the life of the process
        logger.warning("matplotlib is installed but cannot be imported (%s); formula images disabled", exc)
        _available = False
        return None
    try:
        with rc_context({"mathtext.fontset": "cm", "text.color": "#1f2329"}):
            # Measure first, then lay the formula out centred on the fixed-width canvas.
            probe = Figure(figsize=(0.01, 0.01), dpi=MATH_IMAGE_DPI)
            canvas = FigureCanvasAgg(probe)
            text = probe.text(0, 0, f"${body}$", fontsize=MATH_IMAGE_FONTSIZE)
            bbox = text.get_window_extent(canvas.get_renderer())
            width_in, height_in = bbox.width / probe.dpi, bbox.height / probe.dpi
            canvas_w = max(MATH_IMAGE_CANVAS_INCHES, width_in + 0.4)
            canvas_h = max(MATH_IMAGE_MIN_HEIGHT_INCHES, height_in + 0.3)
            fig = Figure(figsize=(canvas_w, canvas_h), dpi=MATH_IMAGE_DPI, facecolor="white")
            FigureCanvasAgg(fig)
            fig.text(0.5, 0.5, f"${body}$", fontsize=MATH_IMAGE_FONTSIZE, ha="center", va="center")
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=MATH_IMAGE_DPI, facecolor="white")
        return buf.getvalue()
    except Exception as exc:  # mathtext parse errors and the like: caller falls back to Unicode
        logger.debug("mathtext render failed for %.60r: %s", body, exc)
        return None


__all__ = ["mathtext_available", "prepare_for_mathtext", "render_formula_png", "formula_digest"]
