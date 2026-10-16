"""Local, byte-preserving recipes with portable asset references.

Only assets already held in the trusted Studio storage tree can be saved. A
saved JSON file never selects a server path; loading resolves content-addressed
filenames within the library and verifies their hashes before rebinding them.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4

from app.studio.models import StudioProject
from app.studio.ui_helpers import EXTENSIONS, MAX_UPLOAD_BYTES, export_project, import_project, store_upload


SCHEMA = "slapz-studio-recipe/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_ASSET = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|jpeg|webp|mp4|mov|webm|m4v|mp3|wav|m4a|aac|ogg|flac)\Z")


def _scene(headline, duration=4, **overrides):
    return {"source_kind": "color", "headline": headline, "layout": "top", "text_position": "top", "duration": duration, **overrides}


_TEMPLATES = [
    {
        "id": "headphone-reaction", "name": "Headphone reaction",
        "description": "A selected reaction photo or clip, a real beat, and one short opening hook. Original reaction-clip audio is muted.",
        "slots": [
            {"id": "reaction", "label": "Reaction photo or clip", "kinds": ["image", "video"], "scene": 0},
            {"id": "beat", "label": "Beat audio", "kinds": ["audio"], "field": "music_file"},
        ],
        "project": {"title": "Headphone reaction", "creative_id": "slapz-headphone-draft", "format": "reel", "size": "portrait", "music_volume": 0.8,
                    "scenes": [_scene("when the beat is flirting\nharder than he is", 6)]},
    },
    {
        "id": "music-choice", "name": "Three-beat choice",
        "description": "Compare three real beats. Prepare local waveform clips from audio excerpts, or use existing videos with their original sound.",
        "slots": [{"id": f"beat-{i}", "label": f"Beat {i} clip", "kinds": ["video"], "scene": i - 1, "preserve_audio": True} for i in range(1, 4)],
        "project": {"title": "Three-beat choice", "creative_id": "slapz-beat-choice-draft", "format": "reel", "size": "portrait",
                    "scenes": [_scene("the beat changes\nthe whole mood.", 4, body=f"{i:02d} / mood {i}", layout="minimal") for i in range(1, 4)]},
    },
    {
        "id": "creation-demo", "name": "Real app creation demo",
        "description": "Real app recordings show the idea, creation step and audible result. Use authorized footage and performances; adjust timings to match what the app actually does.",
        "slots": [
            {"id": "idea", "label": "Idea / starting point clip", "kinds": ["video"], "scene": 0},
            {"id": "create", "label": "Creation step clip", "kinds": ["video"], "scene": 1},
            {"id": "result", "label": "Result clip with sound", "kinds": ["video"], "scene": 2, "preserve_audio": True},
        ],
        "project": {"title": "Real app creation demo", "creative_id": "slapz-creation-demo-draft", "format": "reel", "size": "portrait",
                    "scenes": [_scene("give your idea\nsomewhere to start.", 3), _scene("make it yours.", 4), _scene("sound on.", 5)]},
    },
]


def templates() -> list[dict]:
    return deepcopy(_TEMPLATES)


def bind_template(template_id: str, bindings: dict[str, str], assets: dict[str, dict]) -> dict:
    template = next((item for item in templates() if item["id"] == template_id), None)
    if template is None:
        raise ValueError("Unknown recipe template.")
    if set(bindings) != {slot["id"] for slot in template["slots"]}:
        raise ValueError("Choose media for every template slot.")
    project = template["project"]
    for slot in template["slots"]:
        asset = assets.get(bindings[slot["id"]])
        if asset is None or asset.get("kind") not in slot["kinds"]:
            raise ValueError(f"Choose valid uploaded media for {slot['label']}.")
        if "scene" in slot:
            scene = project["scenes"][slot["scene"]]
            scene.update(source=asset["path"], source_kind=asset["kind"], preserve_audio=slot.get("preserve_audio", False))
        else:
            project[slot["field"]] = asset["path"]
    return StudioProject.model_validate(project).model_dump(mode="json")


def _child(root: Path, name: str, *, file=False) -> Path:
    """Reject symlinks at the requested entry as well as resolved escapes."""
    candidate = root / name
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("Recipe files must stay inside their local library.")
    if file and not candidate.is_file():
        raise ValueError("Recipe media is missing. Restore the original library backup.")
    return candidate


def save_recipe(name: str, project: dict, assets: dict[str, dict], library_root: Path, *, source_root: Path) -> dict:
    """Save a new immutable recipe version, retaining exact source media bytes."""
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Recipe name must contain 1–120 visible characters.")
    validated = StudioProject.model_validate(project).model_dump(mode="json")
    portable = json.loads(export_project(validated, assets))
    media = []
    for saved in portable["assets"]:
        asset_id = saved["id"]
        if not _ASSET.fullmatch(asset_id):
            raise ValueError("Recipe media needs a content-addressed asset ID.")
        path = Path(assets[asset_id]["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(source_root.resolve()) or not path.is_file():
            raise ValueError("Recipe source media must be inside trusted Studio storage.")
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Recipe media must be smaller than 200 MB.")
        content = path.read_bytes()
        if not content or sha256(content).hexdigest() != saved["sha256"] or not asset_id.startswith(saved["sha256"] + "."):
            raise ValueError("Recipe source media changed; import it again before saving.")
        media.append((saved, content))
    library_root = Path(library_root)
    if library_root.is_symlink():
        raise ValueError("Recipe library must be a regular local directory.")
    library_root.mkdir(parents=True, exist_ok=True)
    media_root = _child(library_root, "assets")
    media_root.mkdir(exist_ok=True)
    for saved, content in media:
        destination = _child(media_root, saved["id"])
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != content:
                raise ValueError("Saved recipe media has changed; restore the library backup.")
        else:
            with destination.open("xb") as stream:
                stream.write(content)
    document = {"schema": SCHEMA, "id": uuid4().hex, "name": name.strip(), "created_at": datetime.now(timezone.utc).isoformat(), "document": portable}
    encoded = json.dumps(document, ensure_ascii=False, indent=2).encode()
    if len(encoded) > 1024 * 1024:
        raise ValueError("Recipe JSON must be smaller than 1 MB.")
    with _child(library_root, document["id"] + ".json").open("xb") as stream:
        stream.write(encoded)
    return {key: document[key] for key in ("id", "name", "created_at")}


def _read_recipe(recipe_id: str, library_root: Path) -> dict:
    if not isinstance(recipe_id, str) or not _ID.fullmatch(recipe_id):
        raise ValueError("Invalid saved recipe ID.")
    path = _child(Path(library_root), recipe_id + ".json", file=True)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Recipe JSON must be smaller than 1 MB.")
    document = json.loads(path.read_text())
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("id") != recipe_id
            or not isinstance(document.get("name"), str) or not isinstance(document.get("created_at"), str)
            or not isinstance(document.get("document"), dict)):
        raise ValueError("Invalid saved recipe document.")
    return document


def list_recipes(library_root: Path) -> list[dict]:
    result = []
    for path in Path(library_root).glob("*.json"):
        document = _read_recipe(path.stem, library_root)
        result.append({key: document[key] for key in ("id", "name", "created_at")})
    return sorted(result, key=lambda item: item["created_at"], reverse=True)


def load_recipe(recipe_id: str, library_root: Path, upload_workspace: Path) -> tuple[dict, dict[str, dict]]:
    """Verify saved media, copy into this session, then bind the editable project."""
    document = _read_recipe(recipe_id, library_root)["document"]
    metadata = document.get("assets")
    if not isinstance(metadata, list) or len(metadata) > 42:
        raise ValueError("Invalid recipe media list.")
    media_root = _child(Path(library_root), "assets")
    contents = []
    for saved in metadata:
        if not isinstance(saved, dict) or not isinstance(saved.get("id"), str) or not _ASSET.fullmatch(saved["id"]):
            raise ValueError("Invalid recipe media reference.")
        asset_id = saved["id"]
        kind = saved.get("kind")
        if kind not in EXTENSIONS or Path(asset_id).suffix not in EXTENSIONS[kind]:
            raise ValueError("Invalid recipe media type.")
        path = _child(media_root, asset_id, file=True)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Recipe media must be smaller than 200 MB.")
        content = path.read_bytes()
        if not content or sha256(content).hexdigest() != saved.get("sha256") or not asset_id.startswith(saved["sha256"] + "."):
            raise ValueError("Saved recipe media has changed; restore the library backup.")
        contents.append((saved, content))
    # Resolve and validate all project references before writing any session files.
    verified_assets = {saved["id"]: {**saved, "path": str(media_root / saved["id"])} for saved, _ in contents}
    portable = json.dumps(document).encode()
    StudioProject.model_validate(import_project(portable, verified_assets))
    rebound = {}
    for saved, content in contents:
        asset = store_upload(content, saved["id"], upload_workspace)
        asset["name"] = Path(str(saved.get("name", saved["id"])).replace("\\", "/")).name
        asset["generated"] = bool(saved.get("generated"))
        rebound[asset["id"]] = asset
    project = StudioProject.model_validate(import_project(portable, rebound)).model_dump(mode="json")
    return project, rebound
