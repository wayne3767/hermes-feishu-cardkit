"""Display-formula images for Feishu cards: mathtext preprocessing (no matplotlib needed) and,
when matplotlib is importable, a real render."""

from __future__ import annotations

import pytest

from feishu_cardkit import math_image as mi


class TestPrepare:
    def test_rewrites_commands_mathtext_lacks(self):
        assert mi.prepare_for_mathtext(r"\boxed{a \le b}") == r"a \leq b"
        assert mi.prepare_for_mathtext(r"\displaystyle\dfrac{1}{2}\ge\tfrac{1}{4}\ne 0") == r"\frac{1}{2}\geq\frac{1}{4}\neq 0"
        assert mi.prepare_for_mathtext(r"\underline{x}+\overline{y}") == "x+y"
        assert mi.prepare_for_mathtext(r"\textbf{v}\boldsymbol{\beta}") == r"\mathrm{v}\mathbf{\beta}"

    def test_source_newlines_collapse(self):
        assert mi.prepare_for_mathtext("a\n  +\n b") == "a + b"

    @pytest.mark.parametrize("latex", [
        r"\begin{bmatrix} a & b \\ c & d \end{bmatrix}",
        r"\begin{cases} 1 & x>0 \\ 0 \end{cases}",
        "a \\\\ b",
        r"{a \over b}",
        r"\underbrace{a+b}_{c}",
        r"A_{\text{入料}}",
        "",
    ])
    def test_unsupported_input_returns_none(self, latex: str):
        assert mi.prepare_for_mathtext(latex) is None

    def test_unwrap_is_nesting_aware(self):
        assert mi._unwrap(r"\boxed{\frac{a}{b}} + c", "boxed") == r"\frac{a}{b} + c"
        assert mi._unwrap(r"\textcolor[rgb]{1,0,0}{x}", "textcolor", skip=1) == "x"
        assert mi.prepare_for_mathtext(r"\color{red} y + \textcolor{blue}{z}") == "y + z"

    def test_digest_is_stable(self):
        assert mi.formula_digest("x^2") == mi.formula_digest("x^2") and len(mi.formula_digest("x^2")) == 16


class TestRender:
    def test_unavailable_or_unsupported_gives_none(self, monkeypatch):
        monkeypatch.setattr(mi, "_available", False)
        assert mi.render_formula_png(r"\frac{1}{2}") is None
        monkeypatch.setattr(mi, "_available", True)
        assert mi.render_formula_png(r"\begin{cases} 1 \end{cases}") is None

    @pytest.mark.skipif(not mi.mathtext_available(), reason="matplotlib not installed")
    def test_real_render_produces_png_and_caches(self):
        png = mi.render_formula_png(r"f(x)=\frac{1}{\sigma\sqrt{2\pi}}\exp\left[-\frac{(x-\mu)^2}{2\sigma^2}\right]")
        assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert mi.render_formula_png(r"f(x)=\frac{1}{\sigma\sqrt{2\pi}}\exp\left[-\frac{(x-\mu)^2}{2\sigma^2}\right]") is png

    @pytest.mark.skipif(not mi.mathtext_available(), reason="matplotlib not installed")
    def test_parse_error_gives_none_not_exception(self):
        assert mi.render_formula_png(r"\frac{1}{") is None


@pytest.mark.skipif(not mi.mathtext_available(), reason="matplotlib not installed")
def test_concurrent_renders_are_serialised():
    """mathtext's parser is not thread-safe; parallel formulas from the SDK pool must all succeed."""
    import concurrent.futures
    formulas = [r"X\sim N(\mu,\sigma^2)", r"f(x)=\frac{1}{\sigma\sqrt{2\pi}}e^{-x^2/2}", r"\sum_{n=0}^{\infty} x^n", r"\int_0^1 t\,dt"] * 2
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = [f.result() for f in [pool.submit(mi.render_formula_png, f) for f in formulas]]
    assert all(results)
