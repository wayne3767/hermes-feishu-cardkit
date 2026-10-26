"""Reading preferences are scoped, explainable, and previewed without external calls."""

import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from feishu_cardkit.reading import resolve_reading


def test_default_is_unchanged_and_ignores_process_environment(monkeypatch):
    monkeypatch.setenv("FEISHU_CARD_READING_PRESET", "detailed")
    result = resolve_reading({})
    assert result["values"] == {"reading_preset": "classic", "width_mode": "default"}
    assert result["sources"]["reading_preset"] == "default"


@pytest.mark.parametrize("preset,expanded,width", [
    ("classic", False, "default"), ("task", False, "fill"), ("detailed", True, "fill"),
])
def test_reading_presets(preset, expanded, width):
    result = resolve_reading({"card_reading_preset": preset})
    assert result["values"] == {"reading_preset": preset, "width_mode": width}
    assert result["sources"]["reading_preset"] == "config:card_reading_preset"




def test_injected_profile_environments_stay_separate():
    env_a = {"FEISHU_CARD_READING_PRESET": "detailed", "FEISHU_CARD_WIDTH_MODE": "compact"}
    env_b = {"FEISHU_CARD_READING_PRESET": "task", "FEISHU_CARD_TIMELINE_EXPANDED": "true"}
    a = resolve_reading({}, env_get=env_a.get)
    b = resolve_reading({}, env_get=env_b.get)
    assert a["values"] == {"reading_preset": "detailed", "width_mode": "compact"}
    assert b["values"] == {"reading_preset": "task", "width_mode": "fill"}
    assert a["sources"]["width_mode"] == "profile-environment:FEISHU_CARD_WIDTH_MODE"
    assert resolve_reading({})["values"]["reading_preset"] == "classic"


def test_failed_profile_getter_never_falls_back_to_global_environment(monkeypatch):
    monkeypatch.setenv("FEISHU_CARD_READING_PRESET", "detailed")

    def unavailable(_key):
        raise RuntimeError("sensitive upstream failure")

    result = resolve_reading({}, env_get=unavailable)
    assert result["values"]["reading_preset"] == "classic"
    assert "unavailable" in result["sources"]["reading_preset"]
    assert "sensitive" not in json.dumps(result)


def test_invalid_values_fall_back_without_echoing_raw_content():
    result = resolve_reading({"card_reading_preset": "PRIVATE_INVALID_VALUE",
                              "card_width_mode": {"secret": "PRIVATE_INVALID_VALUE"},
                              "card_timeline_expanded": "PRIVATE_INVALID_VALUE"})
    assert result["values"] == {"reading_preset": "classic", "width_mode": "default"}
    assert all("invalid" in source for source in result["sources"].values())
    assert "PRIVATE_INVALID_VALUE" not in json.dumps(result)


@pytest.fixture
def preview():
    path = Path(__file__).resolve().parent.parent / "tools" / "preview_cards.py"
    spec = importlib.util.spec_from_file_location("preview_cards_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_preview_generates_all_states_without_network_or_secret_output(tmp_path, monkeypatch, preview):
    config = tmp_path / "config.yaml"
    config.write_text("""model:
  api_key: PRIVATE_MODEL_SECRET
platforms:
  feishu:
    app_id: PRIVATE_APP_ID
    app_secret: PRIVATE_APP_SECRET
    card_reading_preset: detailed
    card_timeline_expanded: false
    card_width_mode: compact
    card_unknown_setting: PRIVATE_UNKNOWN_SECRET
""", encoding="utf-8")
    original = config.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline preview attempted network access")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("FEISHU_CARD_READING_PRESET", "task")
    output = tmp_path / "preview"
    manifest = preview.write_previews(output, config_path=config)
    assert config.read_bytes() == original
    assert len(manifest["cards"]) == 10
    assert set(path.name for path in output.iterdir()) == {"index.html", "manifest.json", *manifest["cards"]}
    assert manifest["reading"]["values"] == {"reading_preset": "detailed", "width_mode": "compact"}
    assert all(source.startswith("file/next-load:") for source in manifest["reading"]["sources"].values())
    assert "not running gateway values" in manifest["configuration_scope"]
    generated = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert "PRIVATE_" not in generated
    html = (output / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "<script" not in html
    for phase in preview.PHASES:
        card = json.loads((output / f"{phase}.json").read_text(encoding="utf-8"))
        assert card["schema"] == "2.0" and card["config"]["width_mode"] == "compact"
        activity = card["body"]["elements"][0]
        assert activity["element_id"] == "hermes_activity"
        assert "\n" not in activity["content"]
        assert all(e["tag"] not in {"button", "collapsible_panel"} for e in card["body"]["elements"])
    assert not any(name.startswith("_cardkit_offline_preview.adapter") for name in sys.modules)


def test_malformed_yaml_does_not_echo_sensitive_content(tmp_path, preview):
    config = tmp_path / "broken.yaml"
    config.write_text("app_secret: [PRIVATE_SECRET\n", encoding="utf-8")
    with pytest.raises(ValueError, match="YAML 解析失败") as error:
        preview.read_public_config(config)
    assert "PRIVATE_SECRET" not in str(error.value)


def test_html_preview_escapes_content(preview):
    rendered = preview._element_html({"tag": "markdown", "content": '<script>alert("private")</script>'})
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered


def test_cli_defaults_generate_preview(tmp_path, capsys, preview):
    assert preview.main(["--output", str(tmp_path)]) == 0
    assert "10 张离线示例" in capsys.readouterr().out
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reading"]["values"]["reading_preset"] == "classic"


def test_preview_cli_in_fresh_process_never_imports_runtime_or_sdks(tmp_path):
    script = Path(__file__).resolve().parent.parent / "tools" / "preview_cards.py"
    guarded_run = """
import runpy, socket, sys
class BlockRuntimeImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'gateway', 'agent', 'hermes_cli', 'lark_oapi', 'anthropic', 'openai'}:
            raise AssertionError('preview imported runtime or SDK: ' + fullname)
def no_network(*args, **kwargs):
    raise AssertionError('preview attempted network access')
sys.meta_path.insert(0, BlockRuntimeImports())
socket.socket = no_network
socket.create_connection = no_network
sys.argv = [sys.argv[1], '--output', sys.argv[2]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-B", "-I", "-c", guarded_run, str(script), str(tmp_path)],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (tmp_path / "done.json").exists()
