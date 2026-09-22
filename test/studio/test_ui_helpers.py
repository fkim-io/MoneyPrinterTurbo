import json
from io import BytesIO
from pathlib import Path
import zipfile

import pytest

from app.studio.ui_helpers import (
    export_project,
    import_project,
    local_output_path,
    make_download_bundle,
    store_upload,
)


def test_upload_filename_cannot_escape_workspace(tmp_path):
    asset = store_upload(b"owned upload", "../../somewhere/example.PNG", tmp_path / "uploads")
    assert asset["kind"] == "image"
    assert Path(asset["path"]).parent == tmp_path / "uploads"
    assert asset["name"] == "example.PNG"
    assert Path(asset["path"]).read_bytes() == b"owned upload"
    with pytest.raises(ValueError, match="Unsupported"):
        store_upload(b"code", "example.py", tmp_path)


def test_upload_rejects_existing_symlink(tmp_path):
    import hashlib

    upload = b"new image"
    private_file = tmp_path / "private.txt"
    private_file.write_text("private")
    destination = tmp_path / (hashlib.sha256(upload).hexdigest() + ".jpg")
    destination.symlink_to(private_file)
    with pytest.raises(ValueError, match="regular local file"):
        store_upload(upload, "image.jpg", tmp_path)
    assert private_file.read_text() == "private"


def test_portable_project_roundtrip_binds_identical_reuploaded_media(tmp_path):
    first = store_upload(b"photo", "photo.jpg", tmp_path / "first")
    second = store_upload(b"photo", "renamed.jpg", tmp_path / "second")
    voice = store_upload(b"voice", "voice.wav", tmp_path / "first")
    assets = {first["id"]: first, voice["id"]: voice}
    project = {"format": "reel", "scenes": [{"source": first["path"], "source_kind": "image", "headline": "Hello"}], "narration_file": voice["path"]}
    encoded = export_project(project, assets)
    assert str(tmp_path).encode() not in encoded
    rebound = import_project(encoded, {second["id"]: second, voice["id"]: voice})
    assert rebound["scenes"][0]["source"] == second["path"]
    assert rebound["narration_file"] == voice["path"]
    assert project["scenes"][0]["source"] == first["path"]


def test_project_roundtrip_retains_generated_provenance_and_captions(tmp_path):
    original = store_upload(b"generated", "scene.jpg", tmp_path / "original")
    original["generated"] = True
    project = {"format": "reel", "scenes": [{"source_kind": "image", "source": original["path"]}], "captions": [{"start": 0, "end": 2, "text": "A caption"}]}
    encoded = export_project(project, {original["id"]: original})
    rebound_asset = store_upload(b"generated", "scene.jpg", tmp_path / "reuploaded")
    assets = {rebound_asset["id"]: rebound_asset}
    rebound = import_project(encoded, assets)
    assert assets[original["id"]]["generated"] is True
    assert rebound["captions"] == project["captions"]
    document = json.loads(encoded)
    document["assets"][0]["generated"] = False
    import_project(json.dumps(document).encode(), assets)
    assert assets[original["id"]]["generated"] is True


@pytest.mark.parametrize("reference", ["/etc/passwd", "../../private.png", "https://example.com/photo.jpg"])
def test_import_rejects_unbound_server_paths_and_remote_urls(reference):
    document = {"schema": "slapz-studio-ui/v1", "project": {"scenes": [{"source_kind": "image", "source": reference}]}}
    with pytest.raises(ValueError, match="uploaded assets"):
        import_project(json.dumps(document).encode(), {})


def test_import_rejects_missing_assets_and_wrong_media_types(tmp_path):
    asset = store_upload(b"audio", "voice.mp3", tmp_path)
    project = {"schema": "slapz-studio-ui/v1", "project": {"scenes": [{"source_kind": "image", "source": "asset://" + asset["id"]}]}}
    with pytest.raises(ValueError, match="Re-upload"):
        import_project(json.dumps(project).encode(), {})
    with pytest.raises(ValueError, match="wrong media type"):
        import_project(json.dumps(project).encode(), {asset["id"]: asset})


def test_output_download_rejects_escape_and_symlink(tmp_path):
    root = tmp_path / "render"
    root.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (root / "symlink.jpg").symlink_to(outside)
    for path in ("../private.txt", str(outside), "symlink.jpg"):
        with pytest.raises(ValueError, match="outside"):
            local_output_path(path, root)


def test_download_bundle_contains_only_manifest_assets_with_portable_paths(tmp_path):
    image = tmp_path / "scene-01.jpg"
    image.write_bytes(b"rendered image")
    (tmp_path / "private.json").write_text("not an output")
    manifest = {"review_status": "draft", "assets": [{"path": str(image), "type": "image", "sha256": "example"}]}
    output = make_download_bundle(manifest, tmp_path)
    with zipfile.ZipFile(BytesIO(output)) as archive:
        assert set(archive.namelist()) == {"scene-01.jpg", "manifest.json"}
        saved = json.loads(archive.read("manifest.json"))
        assert saved["assets"][0]["path"] == "scene-01.jpg"
    assert manifest["assets"][0]["path"] == str(image)
