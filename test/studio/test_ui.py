"""Regression coverage for the local editor and the paid-generation boundary."""

from pathlib import Path

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

import app.studio as studio
import app.studio.providers as providers


PAGE = Path(__file__).resolve().parents[2] / "webui" / "pages" / "1_Slapz_Studio.py"


def button(app, label):
    return next(item for item in app.button if item.label == label)


def field(elements, label):
    return next(item for item in elements if item.label == label)


@pytest.fixture(autouse=True)
def no_paid_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A UI test attempted a paid generation")

    monkeypatch.setattr(providers.ReplicateProvider, "submit", forbidden)


def test_real_starter_carousel_has_draft_preview_and_download_bytes():
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    button(app, "Render draft").click().run()
    assert not app.exception and not app.error
    result = app.session_state["slapz_result"]
    assert result["manifest"]["review_status"] == "draft"
    assert len([asset for asset in result["manifest"]["assets"] if asset["type"] == "image"]) == 3
    assert result["zip"].startswith(b"PK")


def test_paid_approval_is_invalidated_when_inputs_change():
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    button(app, "Prepare generation plan").click().run()
    paid = "Generate with Replicate · paid"
    assert button(app, paid).disabled
    next(item for item in app.checkbox if item.label.startswith("I approve")).check().run()
    assert not button(app, paid).disabled
    field(app.text_area, "Model inputs (JSON)").set_value('{"prompt":"A changed scene"}').run()
    assert button(app, paid).disabled
    assert any("fresh plan" in warning.value for warning in app.warning)
    assert not app.exception


def test_manual_and_srt_captions_survive_render_and_still_format_validation(monkeypatch):
    def stub_render(project, output_dir):
        output_dir.mkdir(parents=True)
        Image.new("RGB", (32, 32), "black").save(output_dir / "preview.jpg")
        return {"format": project.format, "review_status": "draft", "assets": [{"path": "preview.jpg", "type": "image"}]}

    monkeypatch.setattr(studio, "render_project", stub_render)
    app = AppTest.from_file(str(PAGE), default_timeout=30).run()
    app.radio[0].set_value("slideshow").run()
    button(app, "Add timed caption").click().run()
    field(app.text_area, "Caption text").set_value("Real caption text").run()
    button(app, "Render draft").click().run()
    assert app.session_state["slapz_result"]["project"]["captions"][0]["text"] == "Real caption text"
    field(app.text_area, "Or paste SRT text").set_value("1\n00:00:00,000 --> 00:00:01,500\nImported caption\n").run()
    button(app, "Replace captions from SRT").click().run()
    assert app.session_state["slapz_editor"]["captions"] == [{"start": 0.0, "end": 1.5, "text": "Imported caption"}]
    app.radio[0].set_value("carousel").run()
    button(app, "Render draft").click().run()
    assert any("timed captions" in error.value for error in app.error)
    button(app, "Remove timed captions from this project").click().run()
    button(app, "Render draft").click().run()
    assert not app.exception and not app.error
    assert app.session_state["slapz_result"]["project"]["captions"] == []
