"""Local Pillow/FFmpeg composition with explicit ordering, timing, and audio policy.

No network, provider calls, bundled stock, generated motion, or publishing lives here.
Artifacts use relative paths; the portable manifest never contains source directories.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .models import SIZE_DIMENSIONS, StudioProject, StudioScene


FPS = 30
BACKGROUND = "#111114"
PINK = "#FF2D55"
WHITE = "#FFFFFF"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4"}


class RenderError(ValueError):
    """An input or composition could not be rendered safely as requested."""


class TextOverflowError(RenderError):
    """The complete text does not fit within the readable safe area."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _run(command: list[str], *, timeout: float = 180) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RenderError(f"Required executable is missing: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"{Path(command[0]).name} exceeded the local render time limit") from exc
    if result.returncode:
        # FFmpeg errors can contain local filenames. Keep full stderr out of portable artifacts.
        detail = result.stderr.strip()[-1500:]
        raise RenderError(f"{Path(command[0]).name} failed: {detail}")
    return result.stdout


def _probe(path: Path) -> dict:
    try:
        return json.loads(_run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,duration,sample_rate,channels",
            "-of", "json", str(path),
        ]))
    except (json.JSONDecodeError, KeyError) as exc:
        raise RenderError(f"Cannot read media metadata for {path.name}") from exc


def _duration(probe: dict, kind: str) -> float:
    stream = next((stream for stream in probe.get("streams", []) if stream.get("codec_type") == kind), None)
    if stream is None:
        raise RenderError(f"Input has no {kind} stream")
    raw = stream.get("duration", probe.get("format", {}).get("duration"))
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise RenderError(f"Cannot establish the {kind} stream duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RenderError(f"Invalid {kind} stream duration")
    return duration


def _local_file(value: str, extensions: set[str], role: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise RenderError(f"{role}: local input file does not exist: {path.name}")
    if path.suffix.lower() not in extensions:
        raise RenderError(f"{role}: unsupported media extension {path.suffix!r}")
    return path


def _open_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as original:
            if getattr(original, "is_animated", False):
                raise RenderError(f"Animated images are unsupported; supply a video: {path.name}")
            if original.width * original.height > 40_000_000:
                raise RenderError(f"Image exceeds the 40 megapixel input limit: {path.name}")
            original.load()
            # EXIF orientation matters for uploaded phone photos; no metadata is copied on export.
            return ImageOps.exif_transpose(original).convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RenderError(f"Invalid image input: {path.name}") from exc


def _font_path(bold: bool) -> Path:
    configured = os.environ.get("STUDIO_BOLD_FONT" if bold else "STUDIO_FONT")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RenderError("Configured Studio font does not exist")
        return path
    names = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for name in names:
        if Path(name).is_file():
            return Path(name)
    raise RenderError("No supported font found. Set STUDIO_FONT and STUDIO_BOLD_FONT to local TrueType fonts.")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Pixel-based wrapping, retaining paragraphs and splitting overlong words without loss."""
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if font.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                if font.getlength(char) > width:
                    raise TextOverflowError("A text glyph is wider than the safe area")
                if font.getlength(current + char) > width:
                    lines.append(current)
                    current = char
                else:
                    current += char
        lines.append(current)
    return lines


def _line_metrics(lines: list[str], font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    if not lines:
        return 0, 0
    ink_height = max(font.getbbox(line or "Ag", anchor="lt")[3] for line in lines)
    step = max(ink_height + 2, math.ceil(font.size * 1.22))
    return step, (len(lines) - 1) * step + ink_height


def _text_overlay(scene: StudioScene, dimensions: tuple[int, int], fonts: tuple[Path, Path], *, reserve_captions: bool = False) -> Image.Image:
    width, height = dimensions
    overlay = Image.new("RGBA", dimensions, (0, 0, 0, 0))
    if not scene.headline and not scene.body:
        return overlay
    margin = max(6, round(width * 0.06))
    pad = max(4, round(width * 0.035))
    available_width = width - 2 * (margin + pad)
    layout_height = height - round(height * 0.26) if reserve_captions else height
    max_height = int(layout_height * 0.72) - 2 * pad
    headline_start = round(width * (0.066 if scene.layout == "paragraph" else 0.078))
    body_start = round(width * (0.054 if scene.layout == "paragraph" else 0.049))
    minimum_headline, minimum_body = max(8, round(width * 0.034)), max(7, round(width * 0.027))
    gap = round(width * 0.024) if scene.headline and scene.body else 0
    fitted = None
    for delta in range(max(headline_start, body_start) + 1):
        title_font = ImageFont.truetype(str(fonts[1]), max(minimum_headline, headline_start - delta))
        body_font = ImageFont.truetype(str(fonts[0]), max(minimum_body, body_start - delta))
        title_lines = _wrap(scene.headline, title_font, available_width)
        body_lines = _wrap(scene.body, body_font, available_width)
        title_step, title_height = _line_metrics(title_lines, title_font)
        body_step, body_height = _line_metrics(body_lines, body_font)
        content_height = title_height + gap + body_height
        # getlength is advance width; italic/diacritic ink can be wider, so verify actual bounds too.
        all_fit = all(
            font.getbbox(line or " ", anchor="lt")[2] - min(0, font.getbbox(line or " ", anchor="lt")[0]) <= available_width
            for lines, font in ((title_lines, title_font), (body_lines, body_font)) for line in lines
        )
        if content_height <= max_height and all_fit:
            fitted = (title_font, body_font, title_lines, body_lines, title_step, body_step, title_height, content_height)
            break
        if title_font.size == minimum_headline and body_font.size == minimum_body:
            break
    if fitted is None:
        raise TextOverflowError("Text does not fit at the minimum readable font size; shorten it or split it into more scenes")
    title_font, body_font, title_lines, body_lines, title_step, body_step, title_height, content_height = fitted
    panel_height = content_height + 2 * pad
    safe_y = max(margin, round(height * 0.07))
    position = "top" if scene.layout == "top" else scene.text_position
    y = safe_y if position == "top" else layout_height - safe_y - panel_height if position == "bottom" else (layout_height - panel_height) // 2
    draw = ImageDraw.Draw(overlay)
    if scene.layout != "minimal":
        opacity = 238 if scene.layout == "boxed" else 220
        draw.rounded_rectangle((margin, y, width - margin, y + panel_height), radius=round(width * 0.024), fill=(17, 17, 20, opacity))
        if scene.layout == "boxed":
            draw.rounded_rectangle((margin, y, margin + max(3, round(width * 0.009)), y + panel_height), radius=3, fill=PINK)
    centered = scene.layout in {"boxed", "minimal"}
    for lines, font, step, start_y, color in (
        (title_lines, title_font, title_step, y + pad, PINK if scene.layout != "minimal" else WHITE),
        (body_lines, body_font, body_step, y + pad + title_height + gap, WHITE),
    ):
        for line_index, line in enumerate(lines):
            bounds = font.getbbox(line or " ", anchor="lt")
            ink_width = bounds[2] - bounds[0]
            x = (width - ink_width) / 2 - bounds[0] if centered else margin + pad - bounds[0]
            draw.text((x, start_y + line_index * step), line, font=font, fill=color, anchor="lt", stroke_width=1 if scene.layout == "minimal" else 0, stroke_fill=BACKGROUND)
    return overlay


def _caption_overlay(text: str, dimensions: tuple[int, int], font_path: Path) -> tuple[Image.Image, int]:
    """A cropped white-on-dark caption panel above the bottom social-interface area."""
    width, height = dimensions
    pad = max(4, round(width * 0.025))
    safe_width = round(width * 0.84)
    text_width = safe_width - 2 * pad
    max_height = round(height * 0.17) - 2 * pad
    minimum = max(7, round(width * 0.034))
    for size in range(max(minimum, round(width * 0.051)), minimum - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        lines = _wrap(text, font, text_width)
        step, ink_height = _line_metrics(lines, font)
        if ink_height <= max_height and all(font.getbbox(line or " ", anchor="lt")[2] - font.getbbox(line or " ", anchor="lt")[0] <= text_width for line in lines):
            panel = Image.new("RGBA", (safe_width, ink_height + 2 * pad))
            draw = ImageDraw.Draw(panel)
            draw.rounded_rectangle((0, 0, panel.width - 1, panel.height - 1), radius=max(3, round(width * 0.015)), fill=(17, 17, 20, 238))
            for index, line in enumerate(lines):
                bounds = font.getbbox(line or " ", anchor="lt")
                x = (safe_width - (bounds[2] - bounds[0])) / 2 - bounds[0]
                draw.text((x, pad + index * step), line, font=font, fill=WHITE, anchor="lt")
            return panel, height - round(height * 0.10) - panel.height
    raise TextOverflowError("Caption does not fit at the minimum readable font size; shorten it or split it into more cues")


def _still(scene: StudioScene, dimensions: tuple[int, int], source: Path | None, overlay: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", dimensions, BACKGROUND)
    if source:
        image = ImageOps.contain(_open_image(source), dimensions, Image.Resampling.LANCZOS)
        canvas.alpha_composite(image, ((dimensions[0] - image.width) // 2, (dimensions[1] - image.height) // 2))
    canvas.alpha_composite(overlay)
    return canvas.convert("RGB")


def _render_scene(scene: StudioScene, source: Path | None, overlay: Image.Image, target: Path, dimensions: tuple[int, int], work: Path, index: int) -> float:
    width, height = dimensions
    frames = round(scene.duration * FPS)
    duration = frames / FPS
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if scene.source_kind == "video":
        overlay_path = work / f"overlay-{index}.png"
        overlay.save(overlay_path)
        command += ["-protocol_whitelist", "file,pipe", "-ss", str(scene.source_start), "-i", str(source), "-i", str(overlay_path)]
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={BACKGROUND},setsar=1,fps={FPS}"
        video_filter = f"[0:v:0]{scale}[base];[base][1:v:0]overlay=0:0:format=auto,format=yuv420p[v]"
        audio_input = "0:a:0" if scene.preserve_audio else "2:a:0"
    else:
        still = work / f"still-{index}.png"
        _still(scene, dimensions, source, overlay).save(still)
        command += ["-loop", "1", "-framerate", str(FPS), "-i", str(still), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        video_filter = "[0:v:0]setsar=1,format=yuv420p[v]"
        audio_input = "1:a:0"
    filters = f"{video_filter};[{audio_input}]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,apad,atrim=duration={duration}[a]"
    command += ["-filter_complex_threads", "1", "-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-frames:v", str(frames), "-t", str(duration), "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target)]
    _run(command, timeout=max(120, duration * 20))
    return duration


def _render_video(project: StudioProject, sources: list[Path | None], overlays: list[Image.Image], dimensions: tuple[int, int], work: Path, target: Path, narration: Path | None, music: Path | None, fonts: tuple[Path, Path]) -> float:
    segments = []
    durations = []
    for index, (scene, source, overlay) in enumerate(zip(project.scenes, sources, overlays)):
        segment = work / f"segment-{index:02d}.mp4"
        durations.append(_render_scene(scene, source, overlay, segment, dimensions, work, index))
        segments.append(segment)
    duration = sum(durations)
    # Concat decoded streams with explicit timestamps. Stream-copying MP4 segments shifts
    # video by AAC priming and accumulates audio padding; that breaks presenter lip sync.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    for segment in segments:
        command += ["-i", str(segment)]
    filters = []
    for index, length in enumerate(durations):
        filters.append(f"[{index}:v:0]trim=duration={length},setpts=PTS-STARTPTS[v{index}]")
        filters.append(f"[{index}:a:0]atrim=duration={length},asetpts=PTS-STARTPTS[a{index}]")
    filters.append("".join(f"[v{index}][a{index}]" for index in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=1[video][sourcevoice]")
    next_input = len(segments)
    if narration:
        command += ["-protocol_whitelist", "file,pipe", "-i", str(narration)]
        filters.append(f"[{next_input}:a:0]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[narration]")
        filters.append("[sourcevoice][narration]amix=inputs=2:duration=longest:normalize=0[voice]")
        next_input += 1
    else:
        filters.append("[sourcevoice]anull[voice]")
    if music:
        command += ["-protocol_whitelist", "file,pipe", "-i", str(music)]
        music_samples = max(1, math.ceil(min(_duration(_probe(music), "audio"), duration) * 48000))
        filters += [
            f"[{next_input}:a:0]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={duration},aloop=loop=-1:size={music_samples},volume={project.music_volume},atrim=duration={duration},asetpts=PTS-STARTPTS[music]",
            "[voice]asplit=2[voiceout][duckkey]",
            "[music][duckkey]sidechaincompress=threshold=0.015:ratio=8:attack=10:release=300[ducked]",
            "[voiceout][ducked]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=0:latency=1[audio]",
        ]
    else:
        filters.append("[voice]alimiter=limit=0.95:level=0:latency=1[audio]")
    if music:
        next_input += 1
    video_label = "video"
    for index, cue in enumerate(project.captions):
        panel, y = _caption_overlay(cue.text, dimensions, fonts[1])
        caption_path = work / f"caption-{index:03d}.png"
        panel.save(caption_path)
        panel.close()
        command += ["-threads", "1", "-i", str(caption_path)]
        next_label = f"captioned{index}"
        # Compare integer frame indices: floating t can be 0.899999 at the 0.9s boundary.
        # A half-open interval shows the cue on the first frame at/after its start.
        start_frame = math.ceil(cue.start * FPS - 1e-9)
        end_frame = math.ceil(cue.end * FPS - 1e-9)
        filters.append(f"[{video_label}][{next_input}:v:0]overlay=x=(W-w)/2:y={y}:enable='gte(n,{start_frame})*lt(n,{end_frame})'[{next_label}]")
        video_label = next_label
        next_input += 1
    command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", "[audio]", "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "18", "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", str(duration), "-map_metadata", "-1", "-movflags", "+faststart", str(target)]
    _run(command, timeout=max(120, duration * 15))
    return duration


def _asset(path: Path, kind: str, dimensions: tuple[int, int], duration: float | None = None) -> dict:
    return {"path": path.name, "type": kind, "width": dimensions[0], "height": dimensions[1], "duration": duration, "sha256": _sha256(path)}


def render_project(project: StudioProject | dict, output_dir: str | Path) -> dict:
    """Validate and render an offline draft; return a portable manifest also saved on disk.

    Existing output files are never overwritten. Video durations are rounded to the nearest
    30 fps frame, listed individually in the manifest. Video source ranges must exist; only
    still images are held. Music loops and ducks under narration / retained presenter audio.
    """
    project = StudioProject.model_validate(project.model_dump() if isinstance(project, StudioProject) else project)
    dimensions = SIZE_DIMENSIONS[project.size]
    fonts = (_font_path(False), _font_path(True))
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise RenderError("Output directory must be new or empty so previous drafts cannot be overwritten")
    sources: list[Path | None] = []
    source_records = []
    overlays = []
    for index, scene in enumerate(project.scenes, 1):
        source = None
        record = {"scene": index, "kind": scene.source_kind, "source_start": scene.source_start, "requested_duration": scene.duration, "duration": round(scene.duration * FPS) / FPS, "preserve_audio": scene.preserve_audio}
        if scene.source is not None:
            source = _local_file(scene.source, VIDEO_EXTENSIONS if scene.source_kind == "video" else IMAGE_EXTENSIONS, f"Scene {index}")
            if scene.source_kind == "video":
                probe = _probe(source)
                source_duration = _duration(probe, "video")
                requested_end = scene.source_start + max(scene.duration, record["duration"])
                if requested_end > source_duration + 0.001:
                    raise RenderError(f"Scene {index}: requested end {requested_end:.3f}s exceeds source duration {source_duration:.3f}s; shorten the scene or source_start")
                if scene.preserve_audio and not any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])):
                    raise RenderError(f"Scene {index}: preserve_audio is enabled but the video has no audio stream")
            else:
                _open_image(source).close()
            record.update({"name": source.name, "sha256": _sha256(source)})
        sources.append(source)
        source_records.append(record)
        try:
            overlays.append(_text_overlay(scene, dimensions, fonts, reserve_captions=bool(project.captions)))
        except TextOverflowError as exc:
            raise TextOverflowError(f"Scene {index}: {exc}") from exc
    for index, cue in enumerate(project.captions, 1):
        try:
            panel, _ = _caption_overlay(cue.text, dimensions, fonts[1])
            panel.close()
        except TextOverflowError as exc:
            raise TextOverflowError(f"Caption {index}: {exc}") from exc
    audio_files = {}
    audio_records = {}
    for role, value in (("narration", project.narration_file), ("music", project.music_file)):
        if value:
            path = _local_file(value, AUDIO_EXTENSIONS, role)
            audio_duration = _duration(_probe(path), "audio")
            if role == "narration" and audio_duration > sum(record["duration"] for record in source_records) + 0.05:
                raise RenderError("Narration is longer than the video timeline; extend the scenes or trim the supplied narration")
            audio_files[role] = path
            audio_records[role] = {"name": path.name, "sha256": _sha256(path), "duration": audio_duration}
    destination.mkdir(parents=True, exist_ok=True)
    # Build separately, and publish files only after all rendering succeeded.
    with tempfile.TemporaryDirectory(prefix=".studio-", dir=destination.parent) as temporary:
        work = Path(temporary)
        assets = []
        if project.format in {"image", "carousel"}:
            for index, (scene, source, overlay) in enumerate(zip(project.scenes, sources, overlays), 1):
                path = work / f"scene-{index:02d}.jpg"
                _still(scene, dimensions, source, overlay).save(path, "JPEG", quality=95, subsampling=0, optimize=False)
                assets.append(_asset(path, "image", dimensions))
            if project.format == "carousel":
                archive = work / "carousel.zip"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                    for asset in assets:
                        info = zipfile.ZipInfo(asset["path"], date_time=(2020, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o644 << 16
                        handle.writestr(info, (work / asset["path"]).read_bytes())
                assets.append(_asset(archive, "archive", dimensions))
        else:
            target = work / "video.mp4"
            duration = _render_video(project, sources, overlays, dimensions, work, target, audio_files.get("narration"), audio_files.get("music"), fonts)
            assets.append(_asset(target, "video", dimensions, duration))
        portable_project = project.model_dump()
        for scene, record in zip(portable_project["scenes"], source_records):
            if scene["source"]:
                scene["source"] = {"name": record["name"], "sha256": record["sha256"]}
        for role, field in (("narration", "narration_file"), ("music", "music_file")):
            portable_project[field] = audio_records.get(role)
        manifest = {
            "schema_version": 1, "creative_id": project.creative_id, "title": project.title,
            "format": project.format, "size": project.size, "review_status": "draft",
            "project_schema_digest": _json_digest(StudioProject.model_json_schema()),
            "project_digest": _json_digest(portable_project), "assets": assets, "sources": source_records,
            "captions": {"count": len(project.captions), "digest": _json_digest(portable_project["captions"]), "cues": portable_project["captions"]},
            "audio": {"inputs": audio_records, "music_volume": project.music_volume, "music_ducking": bool(audio_files.get("music")), "preserved_source_audio": any(scene.preserve_audio for scene in project.scenes)},
            "renderer": {"name": "slapz-studio", "version": 1, "fps": FPS if project.format in {"slideshow", "reel"} else None, "fit": "contain", "automatic_zoom": False, "fonts": [{"name": path.name, "sha256": _sha256(path)} for path in fonts]},
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for filename in [asset["path"] for asset in assets] + ["manifest.json"]:
            shutil.copyfile(work / filename, destination / filename)
    return manifest


RenderProject = render_project
