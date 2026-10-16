"""Local Pillow/FFmpeg composition with explicit ordering, timing, and audio policy.

No network, provider calls, bundled stock, generated motion, or publishing lives here.
Artifacts use relative paths; the portable manifest never contains source directories.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from .models import SIZE_DIMENSIONS, StudioProject, StudioScene


FPS = 30
BACKGROUND = "#111114"
PINK = "#FF2D55"
WHITE = "#FFFFFF"
# Reference's paper and ink (brand constants shared with its end cards and studio plates, #F3F0E8 ground).
REFERENCE_PAPER, REFERENCE_INK, REFERENCE_INK_SOFT, REFERENCE_PAPER_SOFT = "#F3F0E8", "#1F1D19", "#625D54", "#CFC9BD"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4"}


class RenderError(ValueError):
    """An input or composition could not be rendered safely as requested."""


class TextOverflowError(RenderError):
    """The complete text does not fit within the readable safe area."""


@dataclass(frozen=True)
class Style:
    """The fonts, colours and text safe zone one project renders with."""

    regular: Path
    bold: Path
    serif: Path
    native: Path
    native_variation: str | None
    ground: str
    paper: str
    ink: str
    ink_soft: str
    paper_soft: str
    safe_zone: str = "photo_mode"


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


def _env_font(name: str, fallback: Path | None = None) -> Path:
    configured = os.environ.get(name)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise RenderError(f"{name} does not point to a font file")
        return path
    if fallback is None:
        raise RenderError(f"The reference theme needs {name} set to a local TrueType font")
    return fallback


def _style(project: StudioProject) -> Style:
    if project.theme == "reference":
        sans = _env_font("STUDIO_SANS_FONT")
        sans_bold = _env_font("STUDIO_SANS_BOLD_FONT", sans)
        return Style(
            regular=sans, bold=sans_bold, serif=_env_font("STUDIO_SERIF_FONT"), native=_env_font("STUDIO_NATIVE_FONT", sans_bold),
            native_variation=os.environ.get("STUDIO_NATIVE_FONT_VARIATION") or None, ground=REFERENCE_PAPER, paper=REFERENCE_PAPER,
            ink=REFERENCE_INK, ink_soft=REFERENCE_INK_SOFT, paper_soft=REFERENCE_PAPER_SOFT, safe_zone=project.safe_zone,
        )
    regular, bold = _font_path(False), _font_path(True)
    return Style(regular=regular, bold=bold, serif=bold, native=bold, native_variation=None, ground=BACKGROUND, paper=BACKGROUND,
                 ink=WHITE, ink_soft="#C8C8CC", paper_soft="#3A3A40", safe_zone=project.safe_zone)


def _load_font(path: Path, size: int, variation: str | None = None) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(path), size)
    if variation:
        try:
            font.set_variation_by_name(variation)
        except (OSError, ValueError) as exc:
            raise RenderError(f"Font {path.name} has no {variation!r} style") from exc
    return font


def _safe_area(dimensions: tuple[int, int], safe_zone: str) -> tuple[int, int, int, int, int, int]:
    """Where text may sit: (left, top, right, bottom, rail_top, rail_right).

    Below rail_top a block must also stay left of rail_right (TikTok's like/comment/share rail).
    Feed and square stills are Instagram's: only the 3:4 grid crop and the slide counter intrude.
    """
    width, height = dimensions
    if height / width < 1.6:
        box = (0.08, 0.07, 0.92, 0.93, 0.93, 0.92)
    elif safe_zone == "tiktok_ads":
        # TikTok's ad safe zone on 1080x1920: 240 px top, 660 px bottom, 120 px sides, 300 px right below y~840.
        box = (0.111, 0.125, 0.889, 0.656, 0.4375, 0.722)
    else:
        box = (0.07, 0.13, 0.85, 0.78, 0.78, 0.85)
    left, top, right, bottom, rail_top, rail_right = box
    return round(width * left), round(height * top), round(width * right), round(height * bottom), round(height * rail_top), round(width * rail_right)


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


def _balanced_wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap to the fewest lines, then narrow the measure until one more pixel would add a line, so no word is left alone."""
    lines = _wrap(text, font, width)
    low, high = max(1, width // 3), width
    while low < high:
        middle = (low + high) // 2
        try:
            fits = len(_wrap(text, font, middle)) <= len(lines)
        except TextOverflowError:
            fits = False
        low, high = (low, middle) if fits else (middle + 1, high)
    return _wrap(text, font, low)


def _native_overlay(scene: StudioScene, dimensions: tuple[int, int], fonts: tuple[Path, Path], style: Style | None = None) -> Image.Image:
    """TikTok's own text-tool look: black text on white rounded boxes that hug each line.

    Kept inside the safe zone: clear of the top tabs, the bottom caption and the right-hand
    action rail, so the post reads like a person typed it in the app.
    """
    width, height = dimensions
    overlay = Image.new("RGBA", dimensions, (0, 0, 0, 0))
    safe_left, safe_top, wide_right, safe_bottom, rail_top, rail_right = _safe_area(dimensions, style.safe_zone if style else "photo_mode")
    font_path, variation = (style.native, style.native_variation) if style else (fonts[1], None)
    pad_x = max(3, round(width * 0.022))
    pad_y = max(2, round(width * 0.012))
    max_height = safe_bottom - safe_top
    placed = None
    # Fit to the full width first; a block that would reach down beside the action rail is refitted narrower.
    for safe_right in dict.fromkeys((wide_right, rail_right)):
        available_width = safe_right - safe_left - 2 * pad_x
        fitted = None
        for size in range(max(9, round(width * 0.056)), max(8, round(width * 0.034)) - 1, -1):
            title_font = _load_font(font_path, size, variation)
            body_font = _load_font(font_path, max(8, round(size * 0.82)), variation)
            blocks = [(line, font) for text, font in ((scene.headline, title_font), (scene.body, body_font)) for line in _balanced_wrap(text, font, available_width)]
            rows = [(line, font, font.getbbox(line or "Ag", anchor="lt")) for line, font in blocks]
            row_height = [max(font.getbbox("Ag", anchor="lt")[3], bounds[3]) + 2 * pad_y for _, font, bounds in rows]
            total = sum(row_height)
            if total <= max_height and all(bounds[2] - bounds[0] <= available_width for _, _, bounds in rows):
                fitted = (rows, row_height, total)
                break
        if fitted is None:
            continue
        rows, row_height, total = fitted
        y = safe_top if scene.text_position == "top" else safe_bottom - total if scene.text_position == "bottom" else safe_top + (max_height - total) // 2
        placed = (rows, row_height, y, safe_right)
        if y + total <= rail_top or safe_right == rail_right:
            break
    if placed is None:
        raise TextOverflowError("Text does not fit at the minimum readable font size; shorten it or split it into more scenes")
    rows, row_height, y, safe_right = placed
    center = (safe_left + safe_right) / 2
    draw = ImageDraw.Draw(overlay)
    radius = max(3, round(width * 0.014))
    for (line, font, bounds), line_height in zip(rows, row_height):
        if line:
            ink = bounds[2] - bounds[0]
            left = round(center - ink / 2 - pad_x)
            # Rows touch, as in the app; the one-pixel overlap stops a seam between boxes.
            draw.rounded_rectangle((left, y, left + ink + 2 * pad_x, y + line_height + 1), radius=radius, fill=(255, 255, 255, 255))
            draw.text((left + pad_x - bounds[0], y + pad_y), line, font=font, fill=(0, 0, 0, 255), anchor="lt")
        y += line_height
    return overlay


def _card_layout(scene: StudioScene, dimensions: tuple[int, int], style: Style, *, has_picture: bool) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Reference's card: a serif headline over a sans line on paper (or ink), centred, with the box left for the picture.

    Text stays inside the safe area; the picture may run lower, under the caption area, because
    nothing on it has to be read there. Returns the text overlay and the picture box.
    """
    width, height = dimensions
    left, top, right, bottom, rail_top, rail_right = _safe_area(dimensions, style.safe_zone)
    portrait = height / width >= 1.6
    picture_top, picture_bottom = (round(height * 0.10), round(height * 0.80)) if portrait else (top, bottom)
    overlay = Image.new("RGBA", dimensions, (0, 0, 0, 0))
    ink, soft = (style.ink, style.ink_soft) if scene.tone == "paper" else (style.paper, style.paper_soft)
    if not scene.headline and not scene.body:
        return overlay, (left, picture_top, right, picture_bottom)
    gap = round(width * 0.03)
    max_block = round((bottom - top) * (0.46 if has_picture else 1.0))
    placed = None
    for text_right in dict.fromkeys((right, rail_right)):
        available = text_right - left
        fitted = None
        for step in range(0, 41):
            scale = 1.0 - step * 0.012
            head_size = max(9, round(width * 0.086 * scale))
            body_size = max(8, round(width * 0.045 * scale))
            if head_size < round(width * 0.05) or body_size < round(width * 0.03):
                break
            head_font, body_font = _load_font(style.serif, head_size), _load_font(style.regular, body_size)
            try:
                head_lines, body_lines = _balanced_wrap(scene.headline, head_font, available), _balanced_wrap(scene.body, body_font, available)
            except TextOverflowError:
                continue
            head_step, body_step = round(head_size * 1.1), round(body_size * 1.34)
            head_height = len(head_lines) * head_step if head_lines else 0
            body_height = len(body_lines) * body_step if body_lines else 0
            block = head_height + (gap if head_lines and body_lines else 0) + body_height
            if block <= max_block and all(font.getbbox(line or " ", anchor="lt")[2] - font.getbbox(line or " ", anchor="lt")[0] <= available for lines, font in ((head_lines, head_font), (body_lines, body_font)) for line in lines):
                fitted = (head_font, body_font, head_lines, body_lines, head_step, body_step, head_height, block)
                break
        if fitted is None:
            continue
        y = bottom - fitted[-1] if scene.text_position == "bottom" else top if has_picture or scene.text_position == "top" else top + (bottom - top - fitted[-1]) // 2
        placed = (fitted, y, text_right)
        if y + fitted[-1] <= rail_top or text_right == rail_right:
            break
    if placed is None:
        raise TextOverflowError("Text does not fit at the minimum readable font size; shorten it or split it into more scenes")
    (head_font, body_font, head_lines, body_lines, head_step, body_step, head_height, block), y, text_right = placed
    draw = ImageDraw.Draw(overlay)
    center = (left + text_right) / 2
    for lines, font, step, start, color in ((head_lines, head_font, head_step, y, ink), (body_lines, body_font, body_step, y + head_height + (gap if head_lines and body_lines else 0), soft)):
        for index, line in enumerate(lines):
            bounds = font.getbbox(line or " ", anchor="lt")
            draw.text((center - (bounds[2] - bounds[0]) / 2 - bounds[0], start + index * step), line, font=font, fill=color, anchor="lt")
    if scene.text_position == "bottom":
        box = (left, picture_top, right, y - 2 * gap)
    else:
        box = (left, y + block + 2 * gap, right, picture_bottom)
    if box[3] - box[1] < round(height * 0.12):
        raise TextOverflowError("The text leaves no room for the picture; shorten it")
    return overlay, box


def _text_overlay(scene: StudioScene, dimensions: tuple[int, int], fonts: tuple[Path, Path], *, reserve_captions: bool = False, style: Style | None = None) -> Image.Image:
    width, height = dimensions
    overlay = Image.new("RGBA", dimensions, (0, 0, 0, 0))
    if scene.layout == "card":
        if style is None:
            raise RenderError("The card layout needs a project style")
        return _card_layout(scene, dimensions, style, has_picture=bool(scene.source or scene.phone_screen))[0]
    if not scene.headline and not scene.body:
        return overlay
    if scene.layout == "native":
        return _native_overlay(scene, dimensions, fonts, style)
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


def _green_screen(image: Image.Image) -> tuple[Image.Image, list[tuple[float, float]]] | None:
    """Find a chroma-green phone screen: its mask and corners (top-left, top-right, bottom-right, bottom-left)."""
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    mask = (green > 120) & (green > red + 60) & (green > blue + 60)
    if mask.sum() < image.width * image.height * 0.01:
        return None
    ys, xs = np.nonzero(mask)
    corners = [
        (xs[np.argmin(xs + ys)], ys[np.argmin(xs + ys)]),
        (xs[np.argmax(xs - ys)], ys[np.argmax(xs - ys)]),
        (xs[np.argmax(xs + ys)], ys[np.argmax(xs + ys)]),
        (xs[np.argmin(xs - ys)], ys[np.argmin(xs - ys)]),
    ]
    # Grow the mask slightly so green fringe at the bezel edge is covered too.
    soft = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(5))
    return soft, [(float(x), float(y)) for x, y in corners]


def _despill(canvas: Image.Image, mask: Image.Image, reach: int) -> None:
    """Remove green light a chroma screen casts on nearby fingers and bezel: green never exceeds red or blue there."""
    band = np.asarray(mask.filter(ImageFilter.MaxFilter(reach * 2 + 1))) > 0
    pixels = np.asarray(canvas).copy()
    rgb = pixels[..., :3].astype(np.int16)
    limit = np.maximum(rgb[..., 0], rgb[..., 2])
    spill = band & (rgb[..., 1] > limit)
    pixels[..., 1] = np.where(spill, limit, rgb[..., 1]).astype(np.uint8)
    canvas.paste(Image.fromarray(pixels, canvas.mode))


def _perspective(screen: Image.Image, corners: list[tuple[float, float]], size: tuple[int, int]) -> Image.Image:
    source = [(0, 0), (screen.width, 0), (screen.width, screen.height), (0, screen.height)]
    rows, values = [], []
    # Solve for the output-to-input mapping Pillow's PERSPECTIVE transform expects.
    for (x, y), (u, v) in zip(corners, source):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values += [u, v]
    coefficients = np.linalg.solve(np.array(rows, dtype=float), np.array(values, dtype=float))
    return screen.convert("RGBA").transform(size, Image.Transform.PERSPECTIVE, tuple(coefficients), Image.Resampling.BICUBIC)


def _crop_to(image: Image.Image, aspect: float, focus: tuple[float, float], zoom: float) -> Image.Image:
    """The largest crop with this width/height aspect, narrowed by zoom, centred on focus and kept inside the image."""
    width, height = image.size
    crop_width, crop_height = (height * aspect, height) if width / height > aspect else (width, width / aspect)
    crop_width, crop_height = crop_width / zoom, crop_height / zoom
    x = min(max(focus[0] * width - crop_width / 2, 0), width - crop_width)
    y = min(max(focus[1] * height - crop_height / 2, 0), height - crop_height)
    return image.crop((round(x), round(y), round(x + crop_width), round(y + crop_height)))


def _picture(scene: StudioScene, source: Path, region: tuple[int, int]) -> Image.Image:
    image = _open_image(source)
    if scene.fit == "cover":
        return _crop_to(image, region[0] / region[1], scene.focus, scene.zoom).resize(region, Image.Resampling.LANCZOS)
    if scene.zoom != 1.0:
        image = _crop_to(image, image.width / image.height, scene.focus, scene.zoom)
    return ImageOps.contain(image, region, Image.Resampling.LANCZOS)


def _drawn_phone(screen: Image.Image, dimensions: tuple[int, int], box: tuple[int, int, int, int] | None = None) -> tuple[Image.Image, tuple[int, int]]:
    """A plain phone, bezel and shadow, for photos without a green screen (or inside a card's picture box)."""
    width, height = dimensions
    phone_height = round(height * 0.5)
    if box is not None:
        room = max(3, round(width * 0.016)) * 5
        phone_height = min(box[3] - box[1] - room, round((box[2] - box[0] - room) * screen.height / screen.width))
        if phone_height < 8:
            raise RenderError("No room for the phone in this card")
    screen = ImageOps.fit(screen.convert("RGBA"), (round(phone_height * screen.width / screen.height), phone_height), Image.Resampling.LANCZOS)
    bezel = max(3, round(width * 0.016))
    radius = round(screen.width * 0.13)
    layer = Image.new("RGBA", (screen.width + 2 * bezel, screen.height + 2 * bezel), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), radius=radius + bezel, fill=(12, 12, 14, 255))
    rounded = Image.new("L", screen.size, 0)
    ImageDraw.Draw(rounded).rounded_rectangle((0, 0, screen.width - 1, screen.height - 1), radius=radius, fill=255)
    layer.paste(screen, (bezel, bezel), rounded)
    shadow = Image.new("RGBA", (layer.width + 8 * bezel, layer.height + 8 * bezel), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((4 * bezel, 5 * bezel, 4 * bezel + layer.width, 5 * bezel + layer.height), radius=radius + bezel, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(2 * bezel))
    shadow.alpha_composite(layer, (4 * bezel, 4 * bezel))
    if box is not None:
        return shadow, ((box[0] + box[2] - shadow.width) // 2, (box[1] + box[3] - shadow.height) // 2)
    return shadow, ((width - shadow.width) // 2, round(height * 0.62) - shadow.height // 2)


def _still(scene: StudioScene, dimensions: tuple[int, int], source: Path | None, overlay: Image.Image, style: Style | None = None) -> Image.Image:
    card = scene.layout == "card"
    if card and style is None:
        raise RenderError("The card layout needs a project style")
    ground = (style.paper if scene.tone == "paper" else style.ink) if card else (style.ground if style else BACKGROUND)
    canvas = Image.new("RGBA", dimensions, ground)
    box = _card_layout(scene, dimensions, style, has_picture=bool(source or scene.phone_screen))[1] if card else (0, 0, *dimensions)
    region = (box[2] - box[0], box[3] - box[1])
    if source:
        image = _picture(scene, source, region)
        offset = (box[0] + (region[0] - image.width) // 2, box[1] + (region[1] - image.height) // 2)
        if card:
            # Rounded corners: invisible on a plate shot against the same paper, tidy on any other photo.
            mask = Image.new("L", image.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=round(dimensions[0] * 0.03), fill=255)
            image.putalpha(ImageChops.multiply(image.getchannel("A"), mask))
        canvas.alpha_composite(image, offset)
    if scene.phone_screen:
        screen = _open_image(Path(scene.phone_screen))
        found = _green_screen(image) if source and not card else None
        if found:
            mask, corners = found
            # Warp a little past the detected edge so the grown mask never exposes green fringe.
            cx, cy = sum(x for x, _ in corners) / 4, sum(y for _, y in corners) / 4
            grown = [(x + (x - cx) * 0.03 + math.copysign(3, x - cx) + offset[0], y + (y - cy) * 0.03 + math.copysign(3, y - cy) + offset[1]) for x, y in corners]
            warped = _perspective(screen, grown, dimensions)
            placed = Image.new("L", dimensions, 0)
            placed.paste(mask, offset)
            canvas.paste(warped, (0, 0), ImageChops.multiply(placed, warped.getchannel("A")))
            _despill(canvas, placed, max(4, round(dimensions[0] * 0.07)))
        else:
            phone, position = _drawn_phone(screen, dimensions, box if card else None)
            canvas.alpha_composite(phone, position)
    canvas.alpha_composite(overlay)
    return canvas.convert("RGB")


def _render_scene(scene: StudioScene, source: Path | None, overlay: Image.Image, target: Path, dimensions: tuple[int, int], work: Path, index: int, style: Style | None = None) -> float:
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
        _still(scene, dimensions, source, overlay, style).save(still)
        command += ["-loop", "1", "-framerate", str(FPS), "-i", str(still), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        video_filter = "[0:v:0]setsar=1,format=yuv420p[v]"
        audio_input = "1:a:0"
    filters = f"{video_filter};[{audio_input}]aresample=48000:async=1:first_pts=0,aformat=channel_layouts=stereo,apad,atrim=duration={duration}[a]"
    command += ["-filter_complex_threads", "1", "-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-frames:v", str(frames), "-t", str(duration), "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(target)]
    _run(command, timeout=max(120, duration * 20))
    return duration


def _render_video(project: StudioProject, sources: list[Path | None], overlays: list[Image.Image], dimensions: tuple[int, int], work: Path, target: Path, narration: Path | None, music: Path | None, fonts: tuple[Path, Path], style: Style | None = None) -> float:
    segments = []
    durations = []
    for index, (scene, source, overlay) in enumerate(zip(project.scenes, sources, overlays)):
        segment = work / f"segment-{index:02d}.mp4"
        durations.append(_render_scene(scene, source, overlay, segment, dimensions, work, index, style))
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
    style = _style(project)
    fonts = (style.regular, style.bold)
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
        if scene.phone_screen is not None:
            screen = _local_file(scene.phone_screen, IMAGE_EXTENSIONS, f"Scene {index} phone screen")
            _open_image(screen).close()
            record["phone_screen"] = {"name": screen.name, "sha256": _sha256(screen)}
        sources.append(source)
        source_records.append(record)
        try:
            overlays.append(_text_overlay(scene, dimensions, fonts, reserve_captions=bool(project.captions), style=style))
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
                _still(scene, dimensions, source, overlay, style).save(path, "JPEG", quality=95, subsampling=0, optimize=False)
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
            duration = _render_video(project, sources, overlays, dimensions, work, target, audio_files.get("narration"), audio_files.get("music"), fonts, style)
            assets.append(_asset(target, "video", dimensions, duration))
        portable_project = project.model_dump()
        for scene, record in zip(portable_project["scenes"], source_records):
            if scene["source"]:
                scene["source"] = {"name": record["name"], "sha256": record["sha256"]}
            if scene["phone_screen"]:
                scene["phone_screen"] = record["phone_screen"]
        for role, field in (("narration", "narration_file"), ("music", "music_file")):
            portable_project[field] = audio_records.get(role)
        manifest = {
            "schema_version": 1, "creative_id": project.creative_id, "title": project.title,
            "format": project.format, "size": project.size, "review_status": "draft",
            "project_schema_digest": _json_digest(StudioProject.model_json_schema()),
            "project_digest": _json_digest(portable_project), "assets": assets, "sources": source_records,
            "captions": {"count": len(project.captions), "digest": _json_digest(portable_project["captions"]), "cues": portable_project["captions"]},
            "audio": {"inputs": audio_records, "music_volume": project.music_volume, "music_ducking": bool(audio_files.get("music")), "preserved_source_audio": any(scene.preserve_audio for scene in project.scenes)},
            "renderer": {"name": "slapz-studio", "version": 1, "fps": FPS if project.format in {"slideshow", "reel"} else None, "fit": "per-scene", "automatic_zoom": False, "theme": project.theme, "safe_zone": project.safe_zone, "fonts": [{"name": path.name, "sha256": _sha256(path)} for path in dict.fromkeys((*fonts, style.serif, style.native))]},
        }
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for filename in [asset["path"] for asset in assets] + ["manifest.json"]:
            shutil.copyfile(work / filename, destination / filename)
    return manifest


RenderProject = render_project
