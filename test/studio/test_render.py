"""Exercise exported pixels, encoded timelines, and audible output with real FFmpeg."""

from array import array
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave
import zipfile

from PIL import Image
from pydantic import ValidationError
import pytest

from app.studio import RenderError, StudioProject, TextOverflowError, parse_srt, render_project
from app.studio.models import SIZE_DIMENSIONS


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Real rendering tests need FFmpeg and ffprobe")


@pytest.fixture(autouse=True)
def small_frames(monkeypatch):
    # Same layout/filter code, tiny exports to keep the real encoder tests inexpensive.
    monkeypatch.setitem(SIZE_DIMENSIONS, "portrait", (180, 320))
    monkeypatch.setitem(SIZE_DIMENSIONS, "feed", (216, 270))
    monkeypatch.setitem(SIZE_DIMENSIONS, "square", (180, 180))


def run(*args: str) -> bytes:
    return subprocess.run(args, check=True, capture_output=True, timeout=60).stdout


def metadata(path: Path) -> dict:
    return json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))


def pixel_at(path: Path, seconds: float = 0) -> tuple[int, int, int]:
    pixels = run("ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1")
    return tuple(pixels[:3])


def samples(path: Path) -> array:
    values = array("f")
    values.frombytes(run("ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"))
    return values


def rms(values) -> float:
    return math.sqrt(sum(value * value for value in values) / len(values))


def magnitude(values, frequency: float, start: float, end: float) -> float:
    section = values[round(start * 48000):round(end * 48000)]
    real = sum(value * math.cos(2 * math.pi * frequency * i / 48000) for i, value in enumerate(section))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / 48000) for i, value in enumerate(section))
    return 2 * math.hypot(real, imag) / len(section)


def tone(path: Path, duration: float, frequency=440, amplitude=0.25, active=lambda _: True):
    values = array("h", (round(amplitude * 32767 * math.sin(2 * math.pi * frequency * i / 48000)) if active(i / 48000) else 0 for i in range(round(duration * 48000))))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(values.tobytes())


@pytest.fixture
def presenter(tmp_path):
    """Two visibly/audibly different seconds make source_start errors observable."""
    target = tmp_path / "presenter.mp4"
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=180x320:r=30:d=1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-f", "lavfi", "-i", "color=blue:s=180x320:r=30:d=1", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1", "-filter_complex", "[0:v][1:a][2:v][3:a]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(target))
    return target


def test_carousel_exports_ordered_stills_zip_and_private_path_free_manifest(tmp_path):
    sources = []
    for color in ("red", "blue"):
        source = tmp_path / f"private-{color}.png"
        Image.new("RGB", (180, 320), color).save(source)
        sources.append(source)
    project = {"format": "carousel", "size": "feed", "creative_id": "demo-1", "scenes": [{"source_kind": "image", "source": str(source)} for source in sources]}
    output = tmp_path / "export"
    manifest = render_project(project, output)
    assert [asset["path"] for asset in manifest["assets"]] == ["scene-01.jpg", "scene-02.jpg", "carousel.zip"]
    for index, source in enumerate(sources, 1):
        with Image.open(output / f"scene-{index:02d}.jpg") as image:
            assert image.size == (216, 270)
            assert image.getpixel((108, 135))[0 if index == 1 else 2] > 240
        assert manifest["sources"][index - 1]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    with zipfile.ZipFile(output / "carousel.zip") as archive:
        assert archive.namelist() == ["scene-01.jpg", "scene-02.jpg"]
        assert archive.read("scene-01.jpg") == (output / "scene-01.jpg").read_bytes()
    assert manifest["review_status"] == "draft"
    assert str(tmp_path) not in json.dumps(manifest)
    assert json.loads((output / "manifest.json").read_text()) == manifest
    second = render_project(project, tmp_path / "export2")
    assert second == manifest  # no timestamps/ZIP nondeterminism in a static draft


def test_long_words_and_paragraphs_fit_without_drawing_outside_safe_area(tmp_path):
    result = render_project({"format": "image", "scenes": [{"headline": "Camera roll\nchaos?", "body": "abcdefghijklmno" * 3, "layout": "paragraph"}]}, tmp_path / "image")
    with Image.open(tmp_path / "image" / result["assets"][0]["path"]) as image:
        assert image.size == (180, 320)
        # Panel/text remains well away from image borders (JPEG introduces ±2 RGB noise).
        for position in ((0, 0), (179, 0), (0, 319), (179, 319), (2, 160), (177, 160)):
            assert max(image.getpixel(position)) < 30


def test_text_overflow_rejects_without_partial_export(tmp_path):
    with pytest.raises(TextOverflowError, match="Scene 1.*split"):
        render_project({"format": "image", "scenes": [{"body": "Long paragraph with real information. " * 100}]}, tmp_path / "overflow")
    assert not (tmp_path / "overflow").exists()


def test_slideshow_is_static_ordered_cfr_with_exact_audio_video_duration(tmp_path):
    scenes = []
    for color in ("red", "blue"):
        source = tmp_path / f"{color}.png"
        Image.new("RGB", (180, 320), color).save(source)
        scenes.append({"source_kind": "image", "source": str(source), "duration": 0.5})
    manifest = render_project({"format": "slideshow", "scenes": scenes}, tmp_path / "slideshow")
    target = tmp_path / "slideshow/video.mp4"
    probe = metadata(target)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and audio["codec_name"] == "aac"
    assert video["r_frame_rate"] == "30/1"
    assert int(video["nb_frames"]) == 30
    assert float(video["start_time"]) == 0
    assert float(probe["format"]["duration"]) == pytest.approx(1, abs=0.005)
    assert pixel_at(target, 0.1)[0] > 240
    assert pixel_at(target, 0.7)[2] > 240
    assert manifest["assets"][0]["duration"] == 1
    assert rms(samples(target)) < 0.0001


def test_presenter_clip_start_and_audio_are_preserved_only_when_selected(tmp_path, presenter):
    for preserve in (True, False):
        output = tmp_path / str(preserve)
        result = render_project({"format": "reel", "scenes": [{"source_kind": "video", "source": str(presenter), "source_start": 1.2, "duration": 0.6, "preserve_audio": preserve}]}, output)
        video = output / "video.mp4"
        assert pixel_at(video, 0.1)[2] > 240  # chosen second (blue), not the first (red)
        assert result["audio"]["preserved_source_audio"] is preserve
        values = samples(video)
        if preserve:
            assert magnitude(values, 880, 0.05, 0.5) > 0.06
            assert magnitude(values, 440, 0.05, 0.5) < 0.003
        else:
            assert rms(values) < 0.0001


def test_music_loops_and_ducks_under_supplied_narration(tmp_path):
    narration, music = tmp_path / "voice.wav", tmp_path / "music.wav"
    tone(narration, 1.5, frequency=880, amplitude=0.5, active=lambda second: 0.5 <= second < 1.0)
    tone(music, 0.5, frequency=220, amplitude=0.4)
    render_project({"format": "reel", "scenes": [{"duration": 1.5}], "narration_file": str(narration), "music_file": str(music), "music_volume": 0.5, "captions": [{"start": 0.5, "end": 1.0, "text": "Voice"}]}, tmp_path / "mix")
    values = samples(tmp_path / "mix/video.mp4")
    quiet_music = magnitude(values, 220, 0.1, 0.4)
    ducked_music = magnitude(values, 220, 0.65, 0.9)
    assert quiet_music > 0.10
    assert ducked_music < quiet_music * 0.4
    assert magnitude(values, 880, 0.65, 0.9) > 0.4
    assert magnitude(values, 220, 1.3, 1.45) > 0.01  # the short music source looped
    assert max(abs(value) for value in values) < 0.99


def test_timed_captions_obey_half_open_cue_boundaries(tmp_path):
    cues = [{"start": 0.3, "end": 0.6, "text": "WIDE WORDS"}, {"start": 0.6, "end": 0.9, "text": "I"}]
    manifest = render_project({"format": "reel", "scenes": [{"duration": 1.2}], "captions": cues}, tmp_path / "captions")
    target = tmp_path / "captions/video.mp4"

    def bright_pixels_at(second):
        png = run("ffmpeg", "-v", "error", "-ss", str(second), "-i", str(target), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1")
        with Image.open(BytesIO(png)) as frame:
            return sum(min(pixel[:3]) > 210 for pixel in frame.crop((0, 220, 180, 310)).getdata())

    assert bright_pixels_at(0.2) == 0
    wide_count, narrow_count = bright_pixels_at(0.3), bright_pixels_at(0.6)
    assert wide_count > narrow_count * 3
    assert narrow_count > 0
    assert bright_pixels_at(0.9) == 0
    assert manifest["captions"]["cues"] == cues
    assert len(manifest["captions"]["digest"]) == 64


def test_srt_parser_and_caption_schema_reject_bad_timelines():
    parsed = parse_srt("\ufeff1\r\n00:00:00,100 --> 00:00:00,500\r\n<i>Hello</i> &amp; goodbye\r\n\r\n2\r\n00:00:00,500 --> 00:00:01,000\r\nSecond line\r\n")
    assert parsed == [{"start": 0.1, "end": 0.5, "text": "Hello & goodbye"}, {"start": 0.5, "end": 1.0, "text": "Second line"}]
    with pytest.raises(ValueError, match="timestamp"):
        parse_srt("1\n00:99:00,000 --> 00:00:01,000\nBad")
    with pytest.raises(ValueError, match="overlap"):
        parse_srt("1\n00:00:00,000 --> 00:00:01,000\nOne\n\n2\n00:00:00,500 --> 00:00:02,000\nTwo")
    with pytest.raises(ValidationError, match="timeline"):
        StudioProject.model_validate({"format": "reel", "scenes": [{"duration": 0.5}], "captions": parsed})
    with pytest.raises(ValidationError, match="only"):
        StudioProject.model_validate({"format": "image", "scenes": [{}], "captions": parsed})
    with pytest.raises(ValidationError, match="overlap"):
        StudioProject.model_validate({"format": "reel", "scenes": [{}], "captions": list(reversed(parsed))})


def test_preserved_audio_keeps_intentional_initial_delay(tmp_path):
    source = tmp_path / "delayed.mp4"
    run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=blue:s=180x320:r=30:d=1", "-itsoffset", "0.3", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=0.7", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source))
    render_project({"format": "reel", "scenes": [{"source_kind": "video", "source": str(source), "duration": 1, "preserve_audio": True}]}, tmp_path / "delayed")
    values = samples(tmp_path / "delayed/video.mp4")
    assert rms(values[1000:10000]) < 0.001
    assert magnitude(values, 880, 0.4, 0.8) > 0.06


def test_invalid_ranges_media_and_output_collision_fail_clearly(tmp_path, presenter):
    with pytest.raises(RenderError, match="exceeds source duration"):
        render_project({"format": "reel", "scenes": [{"source_kind": "video", "source": str(presenter), "source_start": 1.5, "duration": 1}]}, tmp_path / "range")
    with pytest.raises(RenderError, match="does not exist"):
        render_project({"format": "image", "scenes": [{"source_kind": "image", "source": str(tmp_path / "absent.jpg")}]}, tmp_path / "missing")
    invalid = tmp_path / "corrupt.jpg"
    invalid.write_text("not an image")
    with pytest.raises(RenderError, match="Invalid image"):
        render_project({"format": "image", "scenes": [{"source_kind": "image", "source": str(invalid)}]}, tmp_path / "invalid")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "existing.txt").write_text("keep me")
    with pytest.raises(RenderError, match="previous drafts"):
        render_project({"format": "image", "scenes": [{}]}, protected)
    assert (protected / "existing.txt").read_text() == "keep me"


@pytest.mark.parametrize("project", [
    {"scenes": [{"duration": float("nan")}]},
    {"scenes": [{"duration": 61}]},
    {"scenes": [{}] * 21},
    {"format": "image", "scenes": [{}, {}]},
    {"format": "reel", "scenes": [{"duration": 60}] * 4},
    {"format": "image", "scenes": [{"source_kind": "video", "source": "local.mp4"}]},
    {"scenes": [{"source_kind": "image", "source": "https://example.com/a.jpg"}]},
    {"scenes": [{"source_kind": "color", "source": "/private/image.png"}]},
    {"scenes": [{"preserve_audio": True}]},
    {"scenes": [{}], "music_file": "song.mp3"},
    {"scenes": [{}], "unrecognized": "ignored?"},
])
def test_schema_rejects_ambiguous_or_unbounded_requests(project):
    with pytest.raises(ValidationError):
        StudioProject.model_validate(project)


def test_native_text_is_black_on_white_boxes_inside_the_photo_mode_safe_zone(tmp_path):
    result = render_project({"format": "image", "size": "portrait", "scenes": [{"headline": "things i wish i knew", "body": "before i started writing again", "layout": "native"}]}, tmp_path / "native")
    with Image.open(tmp_path / "native" / result["assets"][0]["path"]) as image:
        pixels = image.convert("RGB").load()
        width, height = image.size
        white = [(x, y) for x in range(width) for y in range(height) if min(pixels[x, y]) > 225]
        dark_in_box = [(x, y) for x in range(width) for y in range(height) if max(pixels[x, y]) < 40 and any(min(pixels[x + dx, y]) > 225 for dx in (-3, 3) if 0 <= x + dx < width)]
        assert white and dark_in_box  # boxes exist and carry dark ink
        xs, ys = [x for x, _ in white], [y for _, y in white]
        assert min(ys) >= height * 0.13 - 1 and max(ys) <= height * 0.78 + 1  # clear of tabs and caption
        assert max(xs) <= width * 0.86  # clear of the action rail


def test_phone_screen_is_drawn_on_a_phone_when_the_photo_has_no_green_screen(tmp_path):
    screen = tmp_path / "private-screen.png"
    Image.new("RGB", (120, 260), (255, 0, 0)).save(screen)
    result = render_project({"format": "carousel", "size": "portrait", "scenes": [{"phone_screen": str(screen)}]}, tmp_path / "drawn")
    with Image.open(tmp_path / "drawn" / "scene-01.jpg") as image:
        red, green, blue = image.convert("RGB").getpixel((90, round(320 * 0.62)))
        assert red > 200 and green < 60 and blue < 60
    assert result["sources"][0]["phone_screen"]["sha256"] == hashlib.sha256(screen.read_bytes()).hexdigest()
    assert result["sources"][0]["phone_screen"]["name"] == "private-screen.png"
    assert str(tmp_path) not in json.dumps(result)


def test_phone_screen_is_warped_into_a_green_screen_and_leaves_no_green(tmp_path):
    photo = tmp_path / "hand.png"
    base = Image.new("RGB", (180, 320), (90, 70, 60))
    from PIL import ImageDraw
    ImageDraw.Draw(base).polygon([(60, 90), (125, 100), (118, 230), (52, 220)], fill=(0, 255, 0))
    base.save(photo)
    screen = tmp_path / "screen.png"
    Image.new("RGB", (120, 260), (0, 0, 255)).save(screen)
    render_project({"format": "carousel", "size": "portrait", "scenes": [{"source_kind": "image", "source": str(photo), "phone_screen": str(screen)}]}, tmp_path / "green")
    with Image.open(tmp_path / "green" / "scene-01.jpg") as image:
        rgb = image.convert("RGB")
        assert rgb.getpixel((88, 160))[2] > 200  # centre of the quad now shows the screenshot
        assert rgb.getpixel((20, 20))[0] > 70  # photo outside the screen untouched
        assert not any(g > 150 and g > r + 60 and g > b + 60 for r, g, b in rgb.getdata())


def test_phone_screen_is_local_and_only_for_still_scenes(tmp_path):
    with pytest.raises(ValidationError, match="URL"):
        StudioProject.model_validate({"scenes": [{"phone_screen": "https://example.com/a.png"}]})
    with pytest.raises(ValidationError, match="phone_screen"):
        StudioProject.model_validate({"format": "reel", "scenes": [{"source_kind": "video", "source": "/tmp/a.mp4", "phone_screen": "/tmp/a.png"}]})
    with pytest.raises(RenderError, match="phone screen"):
        render_project({"format": "image", "scenes": [{"phone_screen": str(tmp_path / "missing.png")}]}, tmp_path / "missing")
