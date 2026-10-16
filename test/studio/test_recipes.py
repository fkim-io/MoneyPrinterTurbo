"""Private library persistence and the upload-only asset boundary."""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from app.studio.recipes import bind_template, list_recipes, load_recipe, save_recipe, templates
from app.studio.ui_helpers import store_upload


def fixture_project(tmp_path):
    storage = tmp_path / "studio"
    asset = store_upload(b"source video bytes", "reaction.mp4", storage / "session-one")
    asset["generated"] = True
    audio = store_upload(b"original beat", "beat.wav", storage / "session-one")
    assets = {item["id"]: item for item in (asset, audio)}
    project = bind_template("headphone-reaction", {"reaction": asset["id"], "beat": audio["id"]}, assets)
    project["scenes"][0].update(source_start=1.5, duration=4.0)
    project["captions"] = [{"start": 0, "end": 1.5, "text": "Listen to this."}]
    return storage, assets, project


def test_library_retains_exact_bytes_and_rebinds_after_restart(tmp_path):
    storage, assets, project = fixture_project(tmp_path)
    library = storage / "recipes"
    saved = save_recipe("My reaction", project, assets, library, source_root=storage)
    encoded = (library / (saved["id"] + ".json")).read_bytes()
    assert str(tmp_path).encode() not in encoded
    assert list_recipes(library) == [saved]
    # A new browser session gets fresh paths, with no dependency on old uploads.
    for asset in assets.values():
        Path(asset["path"]).unlink()
    reopened, rebound = load_recipe(saved["id"], library, storage / "session-two")
    assert reopened["scenes"][0]["source"] != project["scenes"][0]["source"]
    assert reopened["scenes"][0]["source_start"] == 1.5
    assert reopened["scenes"][0]["duration"] == 4
    assert reopened["captions"] == project["captions"]
    assert reopened["music_volume"] == 0.8
    assert all(Path(asset["path"]).parent == storage / "session-two" for asset in rebound.values())
    assert rebound[next(key for key in assets if key.endswith(".mp4"))]["generated"] is True
    for asset_id, asset in rebound.items():
        assert sha256(Path(asset["path"]).read_bytes()).hexdigest() == assets[asset_id]["sha256"]
    # Editing a loaded draft and swapping its media creates a new version.
    replacement = store_upload(b"new take", "another.mp4", storage / "session-two")
    rebound[replacement["id"]] = replacement
    reopened["scenes"][0]["source"] = replacement["path"]
    second = save_recipe("My reaction v2", reopened, rebound, library, source_root=storage)
    assert second["id"] != saved["id"]
    original, _ = load_recipe(saved["id"], library, storage / "session-three")
    assert Path(original["scenes"][0]["source"]).read_bytes() == b"source video bytes"


def test_save_rejects_external_sources_and_modified_media(tmp_path):
    storage, assets, project = fixture_project(tmp_path)
    with pytest.raises(ValueError, match="trusted Studio storage"):
        save_recipe("No", project, assets, storage / "recipes", source_root=tmp_path / "other")
    source = Path(project["scenes"][0]["source"])
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source media changed"):
        save_recipe("No", project, assets, storage / "recipes", source_root=storage)


@pytest.mark.parametrize("reference", ["/etc/passwd", "../../private.jpg", "https://example.com/hidden.jpg"])
def test_saved_document_cannot_select_server_files_or_urls(tmp_path, reference):
    storage, assets, project = fixture_project(tmp_path)
    library = storage / "recipes"
    saved = save_recipe("Draft", project, assets, library, source_root=storage)
    path = library / (saved["id"] + ".json")
    doc = json.loads(path.read_text())
    doc["document"]["project"]["scenes"][0]["source"] = reference
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="uploaded assets"):
        load_recipe(saved["id"], library, storage / "new-session")
    assert not (storage / "new-session").exists()


def test_load_rejects_asset_traversal_and_corruption(tmp_path):
    storage, assets, project = fixture_project(tmp_path)
    library = storage / "recipes"
    saved = save_recipe("Draft", project, assets, library, source_root=storage)
    asset_id = next(iter(assets))
    (library / "assets" / asset_id).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="media has changed"):
        load_recipe(saved["id"], library, storage / "new-session")
    path = library / (saved["id"] + ".json")
    doc = json.loads(path.read_text())
    doc["document"]["assets"][0]["id"] = "../../private.mp4"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="media reference"):
        load_recipe(saved["id"], library, storage / "new-session")


def test_library_symlink_cannot_read_or_overwrite_outside_media(tmp_path):
    storage, assets, project = fixture_project(tmp_path)
    library = storage / "recipes"
    library.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (library / "assets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside their local library"):
        save_recipe("Draft", project, assets, library, source_root=storage)
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError, match="recipe ID"):
        load_recipe("../../secret", library, storage / "new-session")


def test_templates_require_media_types_and_preserve_beat_audio(tmp_path):
    storage, assets, _ = fixture_project(tmp_path)
    video = next(asset for asset in assets.values() if asset["kind"] == "video")
    audio = next(asset for asset in assets.values() if asset["kind"] == "audio")
    for template in templates():
        bindings = {slot["id"]: audio["id"] if slot["kinds"] == ["audio"] else video["id"] for slot in template["slots"]}
        project = bind_template(template["id"], bindings, assets)
        assert project["format"] == "reel"
        if template["id"] == "music-choice":
            assert all(scene["preserve_audio"] for scene in project["scenes"])
        if template["id"] == "creation-demo":
            assert project["scenes"][-1]["preserve_audio"] is True
    with pytest.raises(ValueError, match="every template slot"):
        bind_template("headphone-reaction", {}, assets)
    with pytest.raises(ValueError, match="valid uploaded media"):
        bind_template("headphone-reaction", {"reaction": audio["id"], "beat": audio["id"]}, assets)
    image = store_upload(b"portrait", "photo.jpg", storage / "session-one")
    assets[image["id"]] = image
    assert bind_template("headphone-reaction", {"reaction": image["id"], "beat": audio["id"]}, assets)["scenes"][0]["source_kind"] == "image"
