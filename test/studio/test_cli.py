import json

import pytest

from app.studio.cli import load_project, main, starter_project


def test_starter_writes_a_valid_project(tmp_path):
    project = tmp_path / "project.json"
    assert main(["new", "--format", "carousel", "--output", str(project)]) == 0
    assert main(["validate", str(project)]) == 0


def test_cli_resolves_assets_relative_to_spec(tmp_path):
    project = starter_project("reel")
    project["scenes"][0]["source"] = "shot.mp4"
    project["scenes"][0]["source_kind"] = "video"
    project["music_file"] = "beat.mp3"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(project))
    loaded = load_project(str(path))
    assert loaded["scenes"][0]["source"] == str(tmp_path / "shot.mp4")
    assert loaded["music_file"] == str(tmp_path / "beat.mp3")


def test_invalid_project_is_actionable_error(tmp_path, capsys):
    project = tmp_path / "bad.json"
    project.write_text('{"format":"image", "scenes":[]}')
    assert main(["validate", str(project)]) == 1
    assert "Studio:" in capsys.readouterr().err


def test_budget_change_needs_explicit_approval(tmp_path, capsys):
    from app.studio.providers import ReplicateProvider

    workspace = str(tmp_path / "ledger")
    assert main(["budget", "--workspace", workspace, "--set-limit-usd", "5"]) == 1
    assert ReplicateProvider(workspace).budget_summary()["budget_limit_usd"] is None
    assert main(["budget", "--workspace", workspace, "--set-limit-usd", "5", "--approve-change"]) == 0
    assert float(ReplicateProvider(workspace).budget_summary()["budget_limit_usd"]) == 5


def test_cli_custom_model_plan_is_local_and_preserves_schema(tmp_path):
    specification = {"id": "example/custom", "role": "image", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}}
    spec_path, inputs_path, plan_path = (tmp_path / name for name in ("model.json", "inputs.json", "plan.json"))
    spec_path.write_text(json.dumps(specification))
    inputs_path.write_text(json.dumps({"prompt": "An original still"}))
    assert main(["plan-generation", "--model", "example/custom", "--model-spec", str(spec_path), "--inputs", str(inputs_path), "--max-cost-usd", "1", "--output", str(plan_path)]) == 0
    plan = json.loads(plan_path.read_text())
    assert plan["input_schema"] == specification["input_schema"]
    assert plan["inputs"] == {"prompt": "An original still"}


@pytest.mark.parametrize("project", [{"scenes": 3}, {"scenes": [1]}, {"scenes": [{"source": "https://example.com/a.mp4"}]}, {"scenes": [], "music_file": [1]}])
def test_invalid_cli_media_or_container_reports_error(tmp_path, project):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(project))
    assert main(["validate", str(path)]) == 1


def test_cli_srt_option_replaces_project_cues(tmp_path, monkeypatch):
    path, subtitle = tmp_path / "reel.json", tmp_path / "captions.srt"
    path.write_text(json.dumps(starter_project("reel")))
    subtitle.write_text("1\n00:00:00,500 --> 00:00:01,500\nA timed caption\n")
    received = []
    monkeypatch.setattr("app.studio.render_project", lambda project, output: received.append(project) or {"ok": True})
    assert main(["render", str(path), "--captions-srt", str(subtitle), "--output", str(tmp_path / "render")]) == 0
    assert received[0].captions[0].model_dump() == {"start": 0.5, "end": 1.5, "text": "A timed caption"}
