"""LaTeX → Unicode rewrite used by Feishu streaming cards (cards render no LaTeX)."""

from __future__ import annotations

import pytest

from feishu_cardkit.math_text import convert_math, hide_open_formula, latex_to_unicode


@pytest.mark.parametrize("latex, expected", [
    (r"\delta_p = 1.45\ \text{g/cm}^3", "δₚ = 1.45 g/cm³"),
    (r"E_p = \frac{\delta_{75} - \delta_{25}}{2}", "Eₚ = (δ₇₅ - δ₂₅)/2"),
    (r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}", "x = (-b ± √(b² - 4ac))/2a"),
    (r"\sum_{i=1}^{n} \gamma_i A_i", "∑ᵢ₌₁ⁿ γᵢ Aᵢ"),
    (r"\int_0^{\infty} f(\delta)\,d\delta = 1", "∫₀^∞ f(δ) dδ = 1"),
    (r"\alpha \le \beta \ge \gamma \neq \infty", "α ≤ β ≥ γ ≠ ∞"),
    (r"10^{-3}, v_{\max}, \rho_{\text{介质}}", "10⁻³, vₘₐₓ, ρ_介质"),
    (r"\begin{bmatrix} a & b \\ c & d \end{bmatrix}", "[a b; c d]"),
    (r"\begin{pmatrix} 1 & 0 \\ 0 & 1 \end{pmatrix}", "(1 0; 0 1)"),
    (r"\hat{x}, \bar{y}, \vec{v}, \sqrt[3]{8}", "x̂, ȳ, v⃗, ³√8"),
    (r"\lim_{n \to \infty} a_n, \log_2 n, e^{i\pi} + 1 = 0", "lim_(n → ∞) aₙ, log₂ n, e^iπ + 1 = 0"),
    (r"\eta = \frac{\gamma_c (\beta - \alpha)}{\alpha (\beta - \theta)} \times 100\%", "η = (γc (β - α))/(α (β - θ)) × 100%"),
    (r"A_d = 12.5\%, V_{daf}, M_t, Q_{gr,d}, Q_{net,ar}", "Ad = 12.5%, Vdaf, Mₜ, Qgr,d, Qnet,ar"),
    (r"\varepsilon_{c,i} = \gamma_{c,i} / \gamma_{f,i}", "εc,i = γc,i / γf,i"),
    (r"\rho = 1.4\ \mathrm{g/cm^3}, \mathrm{kg/m^{3}}", "ρ = 1.4 g/cm³, kg/m³"),
    (r"f(x) = \begin{cases} a & x>0 \\ b & x\le 0 \end{cases}", "f(x) = {a，x>0; b，x ≤ 0"),
    (r"\mathrm{d}\rho / \mathrm{d}t \cdot \Delta \theta", "dρ / dt · Δ θ"),
    (r"\operatorname{sgn}(x) \quad \text{for } x \ne 0", "sgn(x) for x ≠ 0"),
    (r"\unknowncmd{x}", "unknowncmdx"),
])
def test_latex_to_unicode(latex: str, expected: str) -> None:
    assert latex_to_unicode(latex) == expected


class TestConvertMath:
    def test_inline_and_block_delimiters(self):
        out = convert_math("密度 $\\delta_p$ 与 $$E = mc^2$$ 以及 \\(a \\le b\\) 和 \\[\\sqrt{2}\\]")
        assert "δₚ" in out and "E = mc²" in out and "a ≤ b" in out and "√2" in out
        assert "$" not in out and "\\(" not in out

    def test_block_formula_gets_its_own_indented_paragraph(self):
        out = convert_math("前文$$x^2$$后文")
        assert out == "前文\n\n　　x²\n\n后文"

    def test_display_block_ignores_source_newlines_and_does_not_stack_blank_lines(self):
        src = "展开式为：\n\n\\[\n\\sin x\n= x-\\frac{x^3}{3!}+\\frac{x^5}{5!}\n-\\cdots\n\\]\n\n一般形式"
        assert convert_math(src) == "展开式为：\n\n　　sin x = x - x³/3! + x⁵/5! - ⋯\n\n一般形式"

    def test_prices_and_unclosed_formulas_are_left_alone(self):
        text = "价格 $5 和 $10；未闭合 $\\alpha + \\beta 保持"
        assert convert_math(text) == text

    def test_plain_dollar_text_without_math_hint_is_left_alone(self):
        assert convert_math("cost is $x$ dollars") == "cost is $x$ dollars"

    def test_code_spans_and_fences_are_untouched(self):
        text = "`$\\alpha$` 内联\n```\n$\\beta$\n```\n$\\gamma^2$"
        assert convert_math(text) == "`$\\alpha$` 内联\n```\n$\\beta$\n```\nγ²"

    def test_indentation_outside_formulas_survives(self):
        text = "- 一级\n  - 二级（价格 $5）\n    - 三级 $x^2$"
        assert convert_math(text) == "- 一级\n  - 二级（价格 $5）\n    - 三级 x²"

    def test_blanks_hugging_a_block_are_dropped(self):
        assert convert_math("前文  $$x^2$$  后文") == "前文\n\n　　x²\n\n后文"

    def test_streaming_prefix_stability(self):
        # A frame that ends mid-formula keeps the raw prefix; the next frame converts it.
        partial = "结果是 $\\frac{a}{b"
        assert convert_math(partial) == partial
        assert convert_math(partial + "}$。") == "结果是 a/b。"

    def test_no_dollar_fast_path(self):
        text = "no math here\\n"
        assert convert_math(text) is text

    def test_pathological_input_never_raises(self):
        assert isinstance(convert_math("$" + "{" * 50 + "\\frac" + "$"), str)


@pytest.mark.parametrize("frame, shown", [
    ("结果是 $\\frac{a}{b", "结果是"),
    ("推导：\n\n$$E = mc", "推导："),
    ("见 \\[ x^2", "见"),
    ("由 \\(a \\le", "由"),
    ("闭合 $x^2$ 后接 $\\alpha", "闭合 $x^2$ 后接"),
    ("价格 $5 和 $10", "价格 $5 和 $10"),
    ("```\n$\\alpha", "```\n$\\alpha"),
    ("完整 $$x$$ 结束", "完整 $$x$$ 结束"),
])
def test_hide_open_formula(frame: str, shown: str) -> None:
    assert hide_open_formula(frame) == shown
