#!/usr/bin/env python3
"""Generate offline card JSON and a schematic HTML preview; never connect to Feishu.

Only card_layout and reading are loaded from the plugin; plugin registration,
adapters and SDK clients are not used. Optional YAML requires PyYAML (already in
Hermes) and is read from the explicit --config path, with no profile discovery.
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
from pathlib import Path
import sys
import types
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
READING_KEYS = frozenset({"card_reading_preset", "card_timeline_expanded", "card_width_mode"})
PHASES = ("running", "waiting_approval", "waiting_input", "unknown", "segment", "done", "stopped", "blocked", "timeout", "failed")


def _plugin_module(name: str):
    # Give relative imports a package without executing __init__.py or registering a plugin.
    package = "_cardkit_offline_preview"
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(PLUGIN_ROOT)]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.{name}")


def read_public_config(path: Path) -> dict[str, Any]:
    """Extract the three supported reading fields; no raw config is returned or printed."""
    import yaml  # already supplied by Hermes; only needed when --config is used

    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise ValueError("YAML 解析失败；为保护配置内容，不输出原始解析错误。") from None
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError("配置根节点必须是映射。")
    platforms = content.get("platforms") or {}
    if not isinstance(platforms, dict):
        raise ValueError("platforms 必须是映射。")
    feishu = platforms.get("feishu") or {}
    if not isinstance(feishu, dict):
        raise ValueError("platforms.feishu 必须是映射。")
    return {key: value for key, value in feishu.items() if key in READING_KEYS}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _element_html(element: dict[str, Any]) -> str:
    tag = element.get("tag")
    if tag == "markdown":
        # Deliberately show escaped source: this is not a Feishu Markdown implementation.
        return '<pre class="markdown">' + _escape(element.get("content", "")) + "</pre>"
    if tag == "hr":
        return "<hr>"
    if tag == "button":
        return "<button disabled>" + _escape(element.get("text", {}).get("content", "")) + "</button>"
    if tag == "collapsible_panel":
        title = element.get("header", {}).get("title", {}).get("content", "")
        opened = " open" if element.get("expanded") else ""
        return (f"<details{opened}><summary>{_escape(title)}</summary>"
                + "".join(_element_html(child) for child in element.get("elements", [])) + "</details>")
    if tag == "table":
        columns = element.get("columns", [])
        cols = "".join(f'<col style="width:{_escape(column.get("width", "auto"))}">' for column in columns)
        header = "".join("<th>" + _escape(column.get("display_name", "")) + "</th>" for column in columns)
        rows = "".join("<tr>" + "".join("<td>" + _escape(row.get(column["name"], "")) + "</td>"
                                       for column in columns) + "</tr>" for row in element.get("rows", []))
        return f'<div class="table-wrap"><table><colgroup>{cols}</colgroup><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table></div>'
    return '<pre class="markdown">' + _escape(json.dumps(element, ensure_ascii=False)) + "</pre>"


def _index_html(cards: dict[str, dict[str, Any]], manifest: dict[str, Any]) -> str:
    pieces = []
    for phase, card in cards.items():
        header = card["header"]
        pieces.append(
            f'<article class="card {_escape(header["template"])}" id="{phase}"><header>'
            f'<h2>{_escape(header["subtitle"]["content"])}</h2><span>{_escape(header["title"]["content"])}</span>'
            f'<a href="{phase}.json">卡片 JSON</a></header><section>'
            + "".join(_element_html(element) for element in card["body"]["elements"])
            + "</section></article>")
    diagnostic = json.dumps(manifest["reading"], ensure_ascii=False, indent=2)
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>飞书卡片离线预览</title><style>
body{font:15px/1.6 system-ui,sans-serif;margin:0;background:#f3f5f8;color:#202633}
main{max-width:1000px;margin:auto;padding:24px}h1{font-size:26px}h2{font-size:18px;margin:0}
.intro{background:white;padding:20px;border-radius:12px;margin-bottom:24px}.grid{display:grid;gap:24px}
.card{background:white;border:1px solid #dfe3ea;border-radius:12px;overflow:hidden}
.card header{border-top:5px solid #4f83dd;background:#eef4ff;padding:16px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.card.green header{border-color:#3a9755;background:#edf8ef}.card.grey header{border-color:#8a939f;background:#f0f1f3}
.card.orange header{border-color:#cd912a;background:#fff8e9}.card.red header{border-color:#cd5353;background:#fff0f0}
header a{margin-left:auto}section{padding:18px 20px}.markdown{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}
details{background:#f6f7fa;padding:10px;border-radius:6px}summary{cursor:pointer}hr{border:0;border-top:1px solid #e0e4e9;margin:16px 0}
button{padding:6px 16px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;table-layout:fixed}
th,td{border:1px solid #dde2e9;padding:8px;overflow-wrap:anywhere;vertical-align:top}th{background:#f2f4f7}
.diagnostic{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#245bbb}
</style></head><body><main><div class="intro"><h1>飞书卡片离线预览</h1>
<p>全部数据为合成示例。未连接飞书、未调用模型、未发送消息。</p>
<p>HTML 仅示意结构，Markdown 以源码显示，表格展示全部行；实际排版、行高、分页和按钮行为需在客户端验收。</p>
<p>指定配置的来源为 <strong>file/next-load</strong>，只代表文件下次加载的解析结果，不是运行中 Gateway 的值。</p>
<details><summary>阅读配置与来源</summary><pre class="diagnostic">""" + _escape(diagnostic) + """</pre></details>
<p><a href="manifest.json">预览清单</a></p></div><div class="grid">""" + "".join(pieces) + "</div></main></body></html>"


def write_previews(output: Path, *, config_path: Path | None = None) -> dict[str, Any]:
    cl = _plugin_module("card_layout")
    reading = _plugin_module("reading")
    extra = read_public_config(config_path) if config_path else {}
    resolved = reading.resolve_reading(extra, source_label="file/next-load")
    answer = ("## 离线示例数据（非生产数据）\n\n说明：请结合实际记录核查。\n\n"
              "| 项目 | 数量 | 处理措施 |\n|---|---:|---|\n"
              + "\n".join(f"| 示例 {i} | {i * 10} | 检查记录、确认原因并补充说明；此处用于观察较长文字的列宽与换行。 |" for i in range(1, 7)))
    timeline = "✓ 示例资料读取完成\n✓ 示例计算完成\n等待或结束状态见卡片标题"
    metrics = {"model": "example-model", "provider": "offline-example",
               "tokens": {"input_tokens": 1200, "output_tokens": 320},
               "context": {"used_tokens": 1800, "max_tokens": 16000}}
    cards = {}
    for phase in PHASES:
        footer = cl.render_footer(phase=phase, tool_count=2, elapsed=42, metrics=metrics)
        cards[phase] = cl.build_stream_card(
            title="Hermes · 离线示例", markdown=answer, timeline_md=timeline,
            tool_count=2, footer=footer, phase=phase, stop_button=True, **resolved["values"],
        )
    scope = "file/next-load" if config_path else "defaults/preview"
    manifest = {"mode": "offline-preview", "configuration_scope": f"{scope}; not running gateway values",
                "reading": resolved, "cards": [f"{phase}.json" for phase in PHASES]}
    output.mkdir(parents=True, exist_ok=True)
    for phase, card in cards.items():
        (output / f"{phase}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "index.html").write_text(_index_html(cards, manifest), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="显式指定 Hermes YAML；只读取 platforms.feishu 的阅读配置")
    parser.add_argument("--output", type=Path, required=True, help="预览输出目录")
    args = parser.parse_args(argv)
    try:
        manifest = write_previews(args.output, config_path=args.config)
    except ImportError:
        parser.error("离线模块依赖不可用；读取 YAML 配置时需要 PyYAML，可使用 Hermes 的 Python。")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f'已生成 {len(manifest["cards"])} 张离线示例：{(args.output / "index.html").resolve()}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
