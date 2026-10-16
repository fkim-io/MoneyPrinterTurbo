"""Real local media tests for the repeatable three-audio recipe."""

from array import array
import json
import math
from pathlib import Path
import subprocess
import wave

import pytest

from app.studio.audio_recipe import prepare_audio_choice
from app.studio.recipes import load_recipe, save_recipe
from app.studio.ui_helpers import export_project, import_project, store_upload


def tone_asset(root):
    root.mkdir(parents=True, exist_ok=True)
    original = root / "two-tones.wav"
    rate = 24000
    samples = array("h", (round(10000 * math.sin(2 * math.pi * (220 if i < rate else 880) * i / rate)) for i in range(rate * 2)))
    with wave.open(str(original), "wb") as stream:
        stream.setparams((1, 2, rate, len(samples), "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    return store_upload(original.read_bytes(), original.name, root / "uploads")


def excerpts(asset, start=1.0, duration=0.5):
    return [{"asset_id": asset["id"], "source_start": start, "duration": duration} for _ in range(3)]


def test_real_excerpt_sound_timing_and_original_dependency_survive_recipe(tmp_path):
    asset = tone_asset(tmp_path)
    asset["generated"] = True
    original_bytes = Path(asset["path"]).read_bytes()
    project, clips = prepare_audio_choice(excerpts(asset), {asset["id"]: asset}, tmp_path / "prepared", source_root=tmp_path)
    assert all(scene["duration"] == 0.5 and scene["source_start"] == 0 and scene["preserve_audio"] for scene in project["scenes"])
    assert all(clip["generated"] for clip in clips.values())
    output = project["scenes"][0]["source"]
    metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", output]))
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1080, 1920, "30/1")
    assert float(video["duration"]) == pytest.approx(0.5, abs=0.001)
    assert int(video["nb_frames"]) == 15
    decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", output, "-map", "0:a:0", "-ac", "1", "-ar", "24000", "-f", "s16le", "-"])
    signal = array("h")
    signal.frombytes(decoded)
    # Discard codec edges, then distinguish the requested second (880Hz) from the first (220Hz).
    signal = signal[1000:10000]
    crossings = sum(left <= 0 < right for left, right in zip(signal, signal[1:]))
    measured_hz = crossings * 24000 / len(signal)
    assert 865 < measured_hz < 895
    all_assets = {asset["id"]: asset, **clips}
    encoded = export_project(project, all_assets)
    assert str(tmp_path).encode() not in encoded
    with pytest.raises(ValueError, match="original audio"):
        import_project(encoded, dict(clips))
    recipe = save_recipe("Three new beats", project, all_assets, tmp_path / "library", source_root=tmp_path)
    hashes = {key: Path(value["path"]).read_bytes() for key, value in all_assets.items()}
    for value in all_assets.values():
        Path(value["path"]).unlink()
    reopened, restored = load_recipe(recipe["id"], tmp_path / "library", tmp_path / "new-session")
    assert Path(restored[asset["id"]]["path"]).read_bytes() == original_bytes
    for key, value in restored.items():
        assert Path(value["path"]).read_bytes() == hashes[key]
    reopened_clip = next(item for item in restored.values() if item["kind"] == "video")
    assert reopened_clip["derivation"]["source_asset_id"] == asset["id"]
    assert reopened_clip["derivation"]["source_start"] == 1
    assert reopened["scenes"][0]["source_start"] == 0


@pytest.mark.parametrize("start,duration", [(1.75, 0.5), (-1, 0.5), (float("nan"), 0.5), (0, float("inf")), (0, 16), (0, True)])
def test_invalid_or_out_of_range_excerpt_creates_no_output(tmp_path, start, duration):
    asset = tone_asset(tmp_path)
    with pytest.raises(ValueError):
        prepare_audio_choice(excerpts(asset, start, duration), {asset["id"]: asset}, tmp_path / "prepared", source_root=tmp_path)
    assert not (tmp_path / "prepared").exists()


def test_preflights_all_three_ranges_before_any_preparation(tmp_path):
    asset = tone_asset(tmp_path)
    requested = excerpts(asset)
    requested[-1]["source_start"] = 2
    with pytest.raises(ValueError, match="exceeds"):
        prepare_audio_choice(requested, {asset["id"]: asset}, tmp_path / "prepared", source_root=tmp_path)
    assert not (tmp_path / "prepared").exists()


def test_preparation_rejects_paths_urls_symlinks_and_playlist_inputs(tmp_path):
    asset = tone_asset(tmp_path)
    for path in ["https://example.com/audio.wav", "/etc/passwd"]:
        with pytest.raises(ValueError, match="trusted Studio storage"):
            prepare_audio_choice(excerpts(asset), {asset["id"]: {**asset, "path": path}}, tmp_path / "prepared", source_root=tmp_path)
    link = tmp_path / "link.wav"
    link.symlink_to(asset["path"])
    with pytest.raises(ValueError, match="trusted Studio storage"):
        prepare_audio_choice(excerpts(asset), {asset["id"]: {**asset, "path": str(link)}}, tmp_path / "prepared", source_root=tmp_path)
    playlist = store_upload(b"ffconcat version 1.0\nfile https://example.com/audio.wav\n", "not-a-wave.wav", tmp_path / "uploads")
    with pytest.raises(ValueError, match="supported, readable audio"):
        prepare_audio_choice(excerpts(playlist), {playlist["id"]: playlist}, tmp_path / "prepared", source_root=tmp_path)
    with pytest.raises(ValueError, match="Prepared clips"):
        prepare_audio_choice(excerpts(asset), {asset["id"]: asset}, tmp_path.parent / "outside", source_root=tmp_path)


def test_missing_or_tampered_original_dependency_blocks_restore(tmp_path):
    asset = tone_asset(tmp_path)
    project, clips = prepare_audio_choice(excerpts(asset, 0, 0.5), {asset["id"]: asset}, tmp_path / "prepared", source_root=tmp_path)
    assert all(not item["generated"] for item in clips.values())
    recipe = save_recipe("Source preserved", project, {asset["id"]: asset, **clips}, tmp_path / "library", source_root=tmp_path)
    (tmp_path / "library" / "assets" / asset["id"]).write_bytes(b"tampered original")
    with pytest.raises(ValueError, match="media has changed"):
        load_recipe(recipe["id"], tmp_path / "library", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
