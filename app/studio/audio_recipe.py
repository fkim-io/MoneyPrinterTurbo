"""Prepare three local audio excerpts as waveform clips; no network or paid API."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
import tempfile

from app.studio.recipes import bind_template
from app.studio.ui_helpers import EXTENSIONS, MAX_UPLOAD_BYTES, store_upload


FPS = 30
MIN_DURATION = 0.5
MAX_DURATION = 15.0
_FORMATS = "wav,mp3,mov,ogg,flac,aac"


def _run(args: list[str], timeout=90) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError as exc:
        raise ValueError("Waveform preparation needs ffmpeg and ffprobe on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Local waveform preparation exceeded its time limit.") from exc
    if result.returncode:
        raise ValueError("Could not prepare this audio locally. Check that it is a supported, readable audio file.")
    return result.stdout


def _number(value, minimum, maximum, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be a finite number from {minimum:g} to {maximum:g} seconds.")
    return float(value)


def _duration(path: Path) -> float:
    metadata = json.loads(_run([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", _FORMATS,
        "-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(path),
    ], timeout=15))
    stream = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "audio"), None)
    if stream is None:
        raise ValueError("The selected file has no audio stream.")
    try:
        duration = float(stream.get("duration", metadata.get("format", {}).get("duration")))
    except (ValueError, TypeError) as exc:
        raise ValueError("Cannot establish the selected audio's duration.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cannot establish the selected audio's duration.")
    return duration


def _source(asset: dict, source_root: Path) -> Path:
    value = asset.get("path")
    if not isinstance(value, str) or "://" in value or "\x00" in value:
        raise ValueError("Choose uploaded local audio from trusted Studio storage.")
    path = Path(value)
    if (asset.get("kind") != "audio" or path.suffix.lower() not in EXTENSIONS["audio"]
            or path.is_symlink() or not path.resolve().is_relative_to(source_root.resolve()) or not path.is_file()):
        raise ValueError("Choose uploaded local audio from trusted Studio storage.")
    if not 0 < path.stat().st_size <= MAX_UPLOAD_BYTES:
        raise ValueError("Choose nonempty audio smaller than 200 MB.")
    digest = sha256(path.read_bytes()).hexdigest()
    if asset.get("sha256") != digest or asset.get("id") != digest + path.suffix.lower():
        raise ValueError("The uploaded audio changed. Upload the original again.")
    return path.resolve()


def prepare_audio_choice(excerpts: list[dict], assets: dict[str, dict], workspace: Path, *, source_root: Path) -> tuple[dict, dict[str, dict]]:
    """Validate every excerpt first, then prepare three exact local clip assets.

    Excerpt shape: {asset_id, source_start, duration}. Durations are rounded to
    the nearest 30fps frame. Original audio remains a dependency of each clip.
    """
    if not isinstance(excerpts, list) or len(excerpts) != 3:
        raise ValueError("Choose exactly three audio excerpts.")
    checked = []
    durations = {}
    for excerpt in excerpts:
        if not isinstance(excerpt, dict) or set(excerpt) != {"asset_id", "source_start", "duration"}:
            raise ValueError("Each excerpt needs its audio asset, start and duration.")
        asset_id = excerpt["asset_id"]
        if not isinstance(asset_id, str) or asset_id not in assets:
            raise ValueError("Choose uploaded audio for every excerpt.")
        asset = assets[asset_id]
        if asset.get("id") != asset_id:
            raise ValueError("Choose audio by its uploaded asset ID.")
        path = _source(asset, source_root)
        start = _number(excerpt["source_start"], 0, 86400, "Excerpt start")
        requested = _number(excerpt["duration"], MIN_DURATION, MAX_DURATION, "Excerpt duration")
        frames = round(requested * FPS)
        duration = frames / FPS
        if asset_id not in durations:
            durations[asset_id] = _duration(path)
        if start + max(requested, duration) > durations[asset_id] + 0.001:
            raise ValueError(f"Excerpt exceeds {asset.get('name', 'audio')}'s {durations[asset_id]:g}-second duration. Choose an earlier start or shorter excerpt.")
        checked.append((asset, path, start, requested, duration, frames))
    workspace = Path(workspace)
    if workspace.is_symlink() or not workspace.resolve().is_relative_to(source_root.resolve()):
        raise ValueError("Prepared clips must stay inside trusted Studio storage.")
    workspace.mkdir(parents=True, exist_ok=True)
    prepared = {}
    bindings = {}
    for index, (original, path, start, requested, duration, frames) in enumerate(checked, 1):
        with tempfile.TemporaryDirectory(prefix="waveform-", dir=workspace) as temporary:
            output = Path(temporary) / "waveform.mp4"
            # Fixed filter graph; no user text, filenames or expressions enter it.
            graph = (
                f"[0:a:0]atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,asplit=2[audio][display];"
                "[display]showwaves=s=920x300:mode=cline:colors=0xff2d55:rate=30:scale=sqrt:draw=full,"
                "format=rgba,colorkey=0x000000:0.01:0[wave];"
                f"color=c=0x111114:s=1080x1920:r=30:d={duration:.9f}[background];"
                "[background][wave]overlay=80:1060:shortest=1,format=yuv420p[video]"
            )
            _run([
                "ffmpeg", "-v", "error", "-nostdin", "-n", "-protocol_whitelist", "file,pipe", "-format_whitelist", _FORMATS,
                "-ss", f"{start:.9f}", "-i", str(path), "-filter_complex", graph,
                "-map", "[video]", "-map", "[audio]", "-frames:v", str(frames), "-t", f"{duration:.9f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2", "-r", str(FPS), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                "-metadata", f"comment=audio-waveform/v1 source={original['sha256']} start={start:.9f}", str(output),
            ])
            clip = store_upload(output.read_bytes(), f"beat-{index}-waveform.mp4", workspace)
            clip["generated"] = bool(original.get("generated"))
            clip["derivation"] = {"kind": "audio-waveform/v1", "source_asset_id": original["id"], "source_start": start,
                                  "requested_duration": requested, "duration": duration, "fps": FPS}
            prepared[clip["id"]] = clip
            bindings[f"beat-{index}"] = clip["id"]
    project = bind_template("music-choice", bindings, prepared)
    for scene, (_, _, _, _, duration, _) in zip(project["scenes"], checked):
        scene["duration"] = duration
        scene["source_start"] = 0.0
    return project, prepared
