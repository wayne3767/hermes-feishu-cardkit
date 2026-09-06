"""LaTeX → Unicode math text for Feishu cards.

Feishu card markdown renders neither ``$…$`` nor ``\\(…\\)`` (the component's syntax table has
no formula support), so a streamed answer that uses LaTeX shows raw backslashes.  This module
rewrites complete formulas into readable Unicode: Greek letters, operators, √(…), ⁿ/ₙ scripts,
(a)/(b) fractions and [a b; c d] matrices.  Unclosed delimiters are left alone, which keeps
partial streaming frames stable; code spans and fenced blocks are never touched.

The goal is legibility, not typesetting: anything unknown degrades to its bare command name
(``\\operatorname`` → ``operatorname``) rather than raising.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterator, List, Optional, Tuple

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_SYMBOLS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "ne": "≠", "neq": "≠", "approx": "≈", "sim": "∼", "simeq": "≃", "equiv": "≡", "propto": "∝", "infty": "∞",
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮", "partial": "∂", "nabla": "∇", "degree": "°",
    "circ": "∘", "bullet": "•", "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "emptyset": "∅", "angle": "∠", "perp": "⊥", "parallel": "∥", "star": "★",
    "ast": "∗", "prime": "′", "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "therefore": "∴",
    "because": "∵", "lVert": "‖", "rVert": "‖", "lvert": "|", "rvert": "|", "langle": "⟨", "rangle": "⟩",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "mid": "|", "colon": ":", "%": "%", "&": "&",
    "_": "_", "{": "{", "}": "}", "#": "#", "$": "$", "|": "‖", "\\": "\n", ",": " ", ";": " ", ":": " ",
    "!": "", " ": " ", "quad": "  ", "qquad": "    ", "displaystyle": "", "textstyle": "", "left": "", "right": "",
    "big": "", "Big": "", "bigg": "", "Bigg": "", "nonumber": "", "limits": "", "nolimits": "",
}
_FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "log",
    "ln", "lg", "exp", "max", "min", "sup", "inf", "lim", "det", "dim", "deg", "gcd", "arg", "ker", "Pr",
}
_TEXT_COMMANDS = {"text", "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "textbf", "textit", "textrm", "operatorname", "mathcal", "mathbb", "boldsymbol", "bm"}
_ACCENTS = {"hat": "̂", "bar": "̄", "overline": "̄", "vec": "⃗", "dot": "̇", "ddot": "̈", "tilde": "̃", "widehat": "̂", "widetilde": "̃"}
_SUPERSCRIPT = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUBSCRIPT = str.maketrans("0123456789+-=()aeoxhklmnpstijruv", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ")
_SUP_OK = set("0123456789+-=()ni")
_SUB_OK = set("0123456789+-=()aeoxhklmnpstijruv")
# Binary / relational symbols get a space on both sides (LaTeX does this itself).
_SPACED_SYMBOLS = set("=≈≠≤≥≡∝×·÷±∓→←⇒⇐↔⇔∈∉⊂⊆∪∩∼≃")
_OPERAND_END = set(")]}!′|%⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ")
_SCRIPT_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ"
_MATRIX_ENVS = {"matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "array", "cases", "aligned", "align", "align*", "equation", "equation*", "gather", "split"}
_MATRIX_BRACKETS = {"pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"), "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "cases": ("{", "")}

_CMD_RE = re.compile(r"\\([A-Za-z]+|.)")
# Pandoc-style inline math: no space just inside the dollars, closing dollar not followed by a digit.
_INLINE_DOLLAR_RE = re.compile(r"(?<![\\$\w])\$(?=\S)((?:\\.|[^$\n\\])+?)(?<=\S)\$(?![\d$])")
_BLOCK_DOLLAR_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_PAREN_RE = re.compile(r"\\\((.+?)\\\)", re.DOTALL)
_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
_CODE_RE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
_MATH_HINT_RE = re.compile(r"[\\^_]|[α-ωΑ-Ω∑∫√≤≥≠±×]")


class _Parser:
    """Recursive-descent walk over a LaTeX math string producing plain text."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.pos = 0

    # --- primitives ---
    def _peek(self) -> str:
        return self.src[self.pos] if self.pos < len(self.src) else ""

    def _group(self) -> str:
        """Parse one argument: a ``{…}`` group, a ``\\cmd`` or a single character."""
        while self._peek() == " ":
            self.pos += 1
        ch = self._peek()
        if ch == "{":
            self.pos += 1
            return self._sequence(stop="}")
        if ch == "\\":
            return self._command()
        self.pos += 1
        return ch

    def _raw_group(self) -> str:
        """Raw source of a ``{…}`` group (for \\text-like commands that must not be parsed)."""
        while self._peek() == " ":
            self.pos += 1
        if self._peek() != "{":
            return self._group()
        depth, start = 0, self.pos
        while self.pos < len(self.src):
            ch = self.src[self.pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    self.pos += 1
                    return self.src[start + 1:self.pos - 1]
            self.pos += 1
        return self.src[start + 1:]

    def _sequence(self, stop: str = "") -> str:
        out: List[str] = []

        def _emit(piece: str) -> None:
            """Append with LaTeX-like spacing: binary symbols spaced, unary minus kept tight."""
            if not piece:
                return
            if len(piece) == 1 and (piece in _SPACED_SYMBOLS or piece in "+-"):
                joined = "".join(out).rstrip()
                prev = joined[-1] if joined else ""
                binary = piece in _SPACED_SYMBOLS or (prev.isalnum() or prev in _OPERAND_END)
                if binary:
                    out[:] = [joined, " ", piece, " "]
                    return
            out.append(piece)

        while self.pos < len(self.src):
            ch = self.src[self.pos]
            if stop and ch == stop:
                self.pos += 1
                return "".join(out)
            if ch == "\\":
                _emit(self._command())
            elif ch in "^_":
                self.pos += 1
                out.append(_script(self._group(), superscript=ch == "^"))
            elif ch == "{":
                self.pos += 1
                out.append(self._sequence(stop="}"))
            elif ch == "}":
                self.pos += 1  # stray close brace
            else:
                self.pos += 1
                _emit(ch)
        return "".join(out)

    # --- commands ---
    def _command(self) -> str:
        m = _CMD_RE.match(self.src, self.pos)
        if not m:
            self.pos += 1
            return "\\"
        name = m.group(1)
        self.pos = m.end()
        if name in _GREEK:
            return _GREEK[name]
        if name in _TEXT_COMMANDS:
            return self._raw_group()
        if name == "frac" or name == "dfrac" or name == "tfrac":
            num, den = self._group(), self._group()
            return f"{_paren(num)}/{_paren(den)}"
        if name == "sqrt":
            index = ""
            if self._peek() == "[":
                end = self.src.find("]", self.pos)
                index = self.src[self.pos + 1:end] if end > 0 else ""
                self.pos = end + 1 if end > 0 else self.pos
            body = self._group()
            root = f"{_script(index, superscript=True)}√" if index else "√"
            return f"{root}({body})" if len(body) > 1 else f"{root}{body}"
        if name in _ACCENTS:
            body = self._group()
            return (body[0] + _ACCENTS[name] + body[1:]) if body else ""
        if name == "begin":
            return self._environment(self._raw_group())
        if name == "end":
            self._raw_group()
            return ""
        if name in _FUNCTIONS:
            return name
        if name in _SYMBOLS:
            return _SYMBOLS[name]
        if name == "underbrace" or name == "overbrace" or name == "underline":
            return self._group()
        if name == "boxed":
            return f"**{self._group().strip()}**"
        if name in {"binom", "dbinom"}:
            return f"C({self._group()}, {self._group()})"
        return name  # unknown command: keep its name, drop the backslash

    def _environment(self, env: str) -> str:
        end_marker = f"\\end{{{env}}}"
        end = self.src.find(end_marker, self.pos)
        body = self.src[self.pos:end if end >= 0 else len(self.src)]
        self.pos = end + len(end_marker) if end >= 0 else len(self.src)
        if env == "array" and self._peek() == "{":  # column spec
            self._raw_group()
        rows = [" ".join(_convert_expression(cell).strip() for cell in row.split("&")).strip()
                for row in body.split("\\\\") if row.strip()]
        if env not in _MATRIX_ENVS:
            return "; ".join(rows)
        if env in {"aligned", "align", "align*", "equation", "equation*", "gather", "split"}:
            return "\n".join(rows)
        left, right = _MATRIX_BRACKETS.get(env, ("[", "]"))
        return f"{left}{'; '.join(rows)}{right}"


def _paren(text: str) -> str:
    """Wrap a fraction operand unless it is a single token or already parenthesised."""
    text = text.strip()
    if re.fullmatch(rf"[\w.,°%!{_SCRIPT_CHARS}]+|[α-ωΑ-Ω∞π]", text) or (len(text) <= 2 and " " not in text):
        return text
    if re.fullmatch(r"\(.*\)!?", text) and _balanced(text[: text.rfind(")") + 1]):
        return text
    return f"({text})"


def _balanced(text: str) -> bool:
    """True when ``text`` is one parenthesised group: ``(…)`` whose first ( closes at the end."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def _script(text: str, *, superscript: bool) -> str:
    text = re.sub(r"\s*([=+\-])\s*", r"\1", text.strip())  # scripts stay tight: ₙ₌₀, not _(n = 0)
    if not text:
        return ""
    ok, table = (_SUP_OK, _SUPERSCRIPT) if superscript else (_SUB_OK, _SUBSCRIPT)
    if all(ch in ok for ch in text):
        return text.translate(table)
    mark = "^" if superscript else "_"
    if len(text) == 1 or re.fullmatch(r"[\w一-鿿]+", text):
        return f"{mark}{text}"
    return f"{mark}({text})"


def _convert_expression(latex: str) -> str:
    # Source newlines are whitespace in math mode; only an explicit \\ breaks a line.
    latex = re.sub(r"\s*\n\s*", " ", latex)
    text = _Parser(latex)._sequence()
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r" *\n *", "\n", text).strip()


def latex_to_unicode(latex: str) -> str:
    """Convert one formula body (without delimiters)."""
    try:
        return _convert_expression(latex)
    except Exception:  # never let a pathological formula break delivery
        return latex


def _looks_like_math(body: str) -> bool:
    return bool(_MATH_HINT_RE.search(body))


BlockRenderer = Callable[[str], Optional[str]]


def convert_math(markdown: str, block_renderer: Optional[BlockRenderer] = None) -> str:
    """Rewrite every complete formula in ``markdown``; code spans/blocks are preserved verbatim.

    ``block_renderer(latex)`` may return replacement markdown for a display formula (e.g. an
    embedded image); None falls back to the Unicode rewrite."""
    if "$" not in markdown and "\\(" not in markdown and "\\[" not in markdown:
        return markdown
    parts = _CODE_RE.split(markdown)
    for i in range(0, len(parts), 2):  # even indexes are outside code
        parts[i] = _convert_segment(parts[i], block_renderer)
    return "".join(parts)


def iter_block_formulas(markdown: str) -> Iterator[str]:
    """Bodies of every complete display formula outside code, in document order."""
    parts = _CODE_RE.split(markdown)
    for i in range(0, len(parts), 2):
        for pattern in (_BLOCK_DOLLAR_RE, _BRACKET_RE):
            for m in pattern.finditer(parts[i]):
                yield m.group(1).strip()


def _convert_segment(text: str, block_renderer: Optional[BlockRenderer] = None) -> str:
    def _render_block(m: "re.Match[str]") -> str:
        body = m.group(1).strip()
        if block_renderer is not None:
            replacement = block_renderer(body)
            if replacement:
                return "\n\n" + replacement + "\n\n"
        return _block(body)

    text = _BLOCK_DOLLAR_RE.sub(_render_block, text)
    text = _BRACKET_RE.sub(_render_block, text)
    text = _PAREN_RE.sub(lambda m: latex_to_unicode(m.group(1)), text)
    text = _INLINE_DOLLAR_RE.sub(lambda m: latex_to_unicode(m.group(1)) if _looks_like_math(m.group(1)) else m.group(0), text)
    # Prose spaces that hugged the original ``$$`` must not hang around the block's paragraph breaks.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)  # a block already surrounded by blank lines must not stack more


def _block(body: str) -> str:
    rendered = latex_to_unicode(body.strip())
    lines = [line.strip() for line in rendered.split("\n") if line.strip()]
    return "\n\n" + "\n".join(f"　　{line}" for line in lines) + "\n\n"


__all__ = ["convert_math", "iter_block_formulas", "latex_to_unicode"]
