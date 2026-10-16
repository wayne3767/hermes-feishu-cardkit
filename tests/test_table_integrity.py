"""Native tables preserve data and fall back intact at their representation limits."""

import pytest

from feishu_cardkit import card_layout as cl


def _markdown(headers, rows):
    def line(values):
        return "| " + " | ".join(values) + " |"

    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def _layout(text):
    return cl.layout_answer_elements(text, number_tables=False)


@pytest.mark.parametrize("count", range(1, 51))
def test_widths_cover_supported_column_counts(count):
    # Mixed lengths exercise rounding without relying on the allocation algorithm.
    text = _markdown([f"列{i}" for i in range(count)], [["说明" * (i + 1) for i in range(count)]])
    table = cl.table_component(text.splitlines())
    assert table is not None
    widths = [int(column["width"].removesuffix("%")) for column in table["columns"]]
    assert len(widths) == count and sum(widths) == 100
    assert all(0 < width <= (100 if count == 1 else 70) for width in widths)


def test_long_explanations_get_more_width_than_identifiers():
    rows = [[str(i), "处理措施：" + "检查筛面和喷水压力，记录处理结果。" * 4] for i in range(6)]
    table = _layout(_markdown(["编号", "处理措施"], rows))[0]
    assert table["tag"] == "table"
    assert int(table["columns"][1]["width"][:-1]) > int(table["columns"][0]["width"][:-1])
    assert table["columns"][0]["horizontal_align"] == "right"
    assert table["rows"][-1]["c1"] == rows[-1][1]


@pytest.mark.parametrize("location", ["header", "cell"])
@pytest.mark.parametrize("length", [1200, 1201])
def test_native_cell_limit_is_a_fallback_boundary_not_a_truncation(location, length):
    value = "煤" * length
    headers = [value if location == "header" else "指标", "备注"]
    rows = [[value if location == "cell" else str(i), "完整"] for i in range(6)]
    text = _markdown(headers, rows)
    elements = _layout(text)
    if length == 1201:
        assert len(elements) == 1
        assert elements[0]["tag"] == "markdown" and elements[0]["content"] == text
    else:
        assert elements[0]["tag"] == "table"
        actual = (elements[0]["columns"][0]["display_name"] if location == "header"
                  else elements[0]["rows"][0]["c0"])
        assert actual == value


@pytest.mark.parametrize("header_count", [2, 51])
def test_too_many_columns_preserve_original_markdown(header_count):
    text = _markdown([f"列{i}" for i in range(header_count)], [[f"内容{i}" for i in range(51)]] * 6)
    elements = _layout(text)
    assert len(elements) == 1 and elements[0]["tag"] == "markdown"
    assert elements[0]["content"] == text


def test_surplus_columns_are_preserved_including_the_last_supported_column():
    rows = [["少一列"], [f"数据{i}" for i in range(50)]] * 3
    table = _layout(_markdown(["项目", "说明"], rows))[0]
    assert table["tag"] == "table" and len(table["columns"]) == 50
    assert table["freeze_first_column"] is True
    assert table["rows"][0]["c49"] == "" and table["rows"][1]["c49"] == "数据49"


@pytest.mark.parametrize("separator", ["|---|", "|---|---|---|", "|---||", "|-- --|---|"])
def test_unsupported_separator_structure_stays_complete_markdown(separator):
    text = "| 项目 | 说明 |\n" + separator + "\n" + "\n".join(["| A | 原始内容 |"] * 6)
    elements = _layout(text)
    assert len(elements) == 1 and elements[0]["tag"] == "markdown"
    assert elements[0]["content"] == text


@pytest.mark.parametrize("lines", [[], ["| A |"], ["| A |", "|---|"]])
def test_incomplete_table_has_no_native_representation(lines):
    assert cl.table_component(lines) is None


def test_fallback_table_does_not_consume_native_component_budget():
    fallback = _markdown(["项目"], [["长" * 1201]] * 6)
    supported = _markdown(["项目"], [[str(i)] for i in range(6)])
    elements = _layout("\n\n".join([fallback] + [supported] * 6))
    assert [element["tag"] for element in elements] == ["markdown"] + ["table"] * 5 + ["markdown"]
    assert elements[0]["content"] == fallback and elements[-1]["content"] == supported


def test_pipe_escape_parity_preserves_literal_pipes_and_column_boundaries():
    assert cl._cells(r"| a \| b | c |") == ["a | b", "c"]
    assert cl._cells(r"| a \\| b |") == ["a " + "\\" * 2, "b"]
    assert cl._cells(r"| a \\\| b | c |") == [r"a \\| b", "c"]
