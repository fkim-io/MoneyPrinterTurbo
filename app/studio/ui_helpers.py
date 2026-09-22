"""Safe local media binding and portable downloads for the Studio UI."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import zipfile


PROJECT_FILE_SCHEMA = "slapz-studio-ui/v1"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "video": {".mp4", ".mov", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
}


def store_upload(data: bytes, name: str, workspace: Path) -> dict:
    """Store only bytes supplied by this session, under a content-derived name."""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Choose a nonempty file smaller than 200 MB.")
    filename = Path(name.replace("\\", "/")).name
    suffix = Path(filename).suffix.lower()
    kind = next((key for key, values in EXTENSIONS.items() if suffix in values), None)
    if kind is None:
        raise ValueError(f"Unsupported media type: {suffix or 'no file extension'}")
    digest = sha256(data).hexdigest()
    asset_id = digest + suffix
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / asset_id
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("The upload destination is not a regular local file.")
    if not path.exists():
        path.write_bytes(data)
    return {
        "id": asset_id,
        "name": filename,
        "kind": kind,
        "path": str(path.resolve()),
        "sha256": digest,
        "bytes": len(data),
    }


def _references(project: dict):
    for scene in project.get("scenes", []):
        if scene.get("source_kind") != "color" and scene.get("source"):
            yield scene, "source"
    for key in ("narration_file", "music_file"):
        if project.get(key):
            yield project, key


def export_project(project: dict, assets: dict[str, dict]) -> bytes:
    """Export reusable asset IDs, never local server file paths."""
    portable = deepcopy(project)
    by_path = {asset["path"]: asset for asset in assets.values()}
    used = {}
    for container, key in _references(portable):
        asset = by_path.get(container[key])
        if asset is None:
            raise ValueError("A project asset is no longer available. Re-upload it first.")
        container[key] = "asset://" + asset["id"]
        used[asset["id"]] = {field: asset[field] for field in ("id", "name", "kind", "sha256")}
        used[asset["id"]]["generated"] = bool(asset.get("generated"))
    return json.dumps(
        {"schema": PROJECT_FILE_SCHEMA, "project": portable, "assets": list(used.values())},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def import_project(data: bytes, assets: dict[str, dict]) -> dict:
    """Resolve an exported project strictly against this session's uploads."""
    if len(data) > 1024 * 1024:
        raise ValueError("Project JSON must be smaller than 1 MB.")
    try:
        document = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("This file is not valid project JSON.") from exc
    if not isinstance(document, dict) or document.get("schema") != PROJECT_FILE_SCHEMA:
        raise ValueError("Choose a Slapz Studio project JSON downloaded from this page.")
    project = document.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("scenes"), list):
        raise ValueError("The project needs an ordered scenes list.")
    if not all(isinstance(scene, dict) for scene in project["scenes"]):
        raise ValueError("Each scene must be a JSON object.")
    project = deepcopy(project)
    saved_assets = document.get("assets", [])
    if not isinstance(saved_assets, list) or not all(isinstance(item, dict) for item in saved_assets):
        raise ValueError("Project asset metadata must be a list of objects.")
    metadata = {item.get("id"): item for item in saved_assets if isinstance(item.get("id"), str)}
    generated = set()
    for container, key in _references(project):
        reference = container[key]
        if not isinstance(reference, str) or not reference.startswith("asset://"):
            raise ValueError("Project media must reference uploaded assets, not server paths or URLs.")
        asset = assets.get(reference.removeprefix("asset://"))
        if asset is None:
            raise ValueError("Re-upload this project's original media first, then import the JSON again.")
        expected = "audio" if key != "source" else container.get("source_kind")
        if asset["kind"] != expected:
            raise ValueError("A project's media reference has the wrong media type.")
        saved = metadata.get(asset["id"], {})
        if saved.get("generated") is True and saved.get("sha256") == asset["sha256"]:
            generated.add(asset["id"])
        container[key] = asset["path"]
    for asset_id in generated:
        assets[asset_id]["generated"] = True
    return project


def local_output_path(path: str, output_dir: Path) -> Path:
    """Keep preview/download reads within the requested output directory."""
    root = output_dir.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("The output file is missing or outside this render's folder.")
    return candidate


def make_download_bundle(manifest: dict, output_dir: Path) -> bytes:
    """Download only declared render assets and a manifest with portable paths."""
    portable = deepcopy(manifest)
    buffer = BytesIO()
    seen = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for asset in portable.get("assets", []):
            path = local_output_path(asset["path"], output_dir)
            if path.name in seen:
                raise ValueError("Render output contains duplicate file names.")
            seen.add(path.name)
            archive.write(path, path.name)
            asset["path"] = path.name
        archive.writestr("manifest.json", json.dumps(portable, indent=2, ensure_ascii=False))
    return buffer.getvalue()
