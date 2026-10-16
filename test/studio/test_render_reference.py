"""Reference's carousel look: crops for close-ups, the paper card, TikTok's ad safe zone and Instagram's feed margins."""

from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import ValidationError
import pytest

from app.studio import RenderError, StudioProject, render_project
from app.studio.models import SIZE_DIMENSIONS
from app.studio.render import REFERENCE_PAPER, _font_path


PAPER = tuple(int(REFERENCE_PAPER[i:i + 2], 16) for i in (1, 3, 5))


@pytest.fixture(autouse=True)
def small_frames(monkeypatch):
    monkeypatch.setitem(SIZE_DIMENSIONS, "portrait", (270, 480))
    monkeypatch.setitem(SIZE_DIMENSIONS, "feed", (270, 338))


@pytest.fixture
def reference_fonts(monkeypatch):
    # Any local TrueType font stands in for Libre Caslon, Instrument Sans and TikTok Sans here.
    regular, bold = _font_path(False), _font_path(True)
    monkeypatch.setenv("STUDIO_SERIF_FONT", str(bold))
    monkeypatch.setenv("STUDIO_SANS_FONT", str(regular))
    monkeypatch.setenv("STUDIO_SANS_BOLD_FONT", str(bold))
    monkeypatch.setenv("STUDIO_NATIVE_FONT", str(bold))
    monkeypatch.delenv("STUDIO_NATIVE_FONT_VARIATION", raising=False)


def quadrants(path: Path) -> Path:
    """A source whose four quadrants are red, green, blue and yellow, so a crop's position is visible."""
    image = Image.new("RGB", (400, 400))
    draw = ImageDraw.Draw(image)
    for (x, y), color in zip(((0, 0), (200, 0), (0, 200), (200, 200)), ("red", "lime", "blue", "yellow")):
        draw.rectangle((x, y, x + 199, y + 199), fill=color)
    image.save(path)
    return path


def near(pixel, color, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color))


def test_cover_fills_the_frame_and_zoom_narrows_to_the_focus(tmp_path, reference_fonts):
    source = quadrants(tmp_path / "quad.png")
    scenes = [
        {"source_kind": "image", "source": str(source), "fit": "cover"},
        {"source_kind": "image", "source": str(source), "fit": "cover", "zoom": 4, "focus": [0.75, 0.75]},
    ]
    render_project({"format": "carousel", "size": "portrait", "theme": "reference", "scenes": scenes}, tmp_path / "out")
    with Image.open(tmp_path / "out" / "scene-01.jpg") as image:
        rgb = image.convert("RGB")
        assert not near(rgb.getpixel((2, 2)), PAPER) and not near(rgb.getpixel((267, 477)), PAPER)  # no letterbox
    with Image.open(tmp_path / "out" / "scene-02.jpg") as image:
        rgb = image.convert("RGB")
        for point in ((5, 5), (135, 240), (264, 474)):
            assert near(rgb.getpixel(point), (255, 255, 0))  # only the yellow quadrant, edge to edge


def test_card_puts_paper_ground_ink_text_and_the_picture_below_the_text(tmp_path, reference_fonts):
    picture = tmp_path / "plate.png"
    Image.new("RGB", (300, 300), (20, 60, 200)).save(picture)
    scene = {"layout": "card", "source_kind": "image", "source": str(picture), "headline": "Dive", "body": "a turning bezel to time your air"}
    result = render_project({"format": "carousel", "size": "portrait", "theme": "reference", "safe_zone": "tiktok_ads", "scenes": [scene]}, tmp_path / "card")
    with Image.open(tmp_path / "card" / "scene-01.jpg") as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        assert near(rgb.getpixel((3, 3)), PAPER, 6)
        dark = [y for y in range(height) for x in range(width) if max(rgb.getpixel((x, y))) < 80]
        blue = [y for y in range(height) for x in range(0, width, 3) if near(rgb.getpixel((x, y)), (20, 60, 200), 30)]
        assert dark and blue
        assert min(dark) >= height * 0.125 - 1  # headline inside the ad safe zone
        assert max(dark) < min(blue)  # text above the picture
        assert max(blue) <= height * 0.80 + 1
    assert result["renderer"]["theme"] == "reference" and result["renderer"]["safe_zone"] == "tiktok_ads"


def test_card_places_a_drawn_phone_with_the_whole_screen_in_the_picture_box(tmp_path, reference_fonts):
    screen = tmp_path / "screen.png"
    Image.new("RGB", (120, 260), (255, 0, 0)).save(screen)
    scene = {"layout": "card", "phone_screen": str(screen), "headline": "Reference names it from a photo"}
    render_project({"format": "carousel", "size": "feed", "theme": "reference", "scenes": [scene]}, tmp_path / "phone")
    with Image.open(tmp_path / "phone" / "scene-01.jpg") as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        red = [(x, y) for x in range(width) for y in range(height) if near(rgb.getpixel((x, y)), (255, 0, 0), 50)]
        assert red
        ys = [y for _, y in red]
        assert min(ys) > height * 0.2 and max(ys) < height * 0.93  # below the headline, inside the feed margins
        assert near(rgb.getpixel((3, height - 3)), PAPER, 6)


def test_tiktok_ads_safe_zone_keeps_native_text_clear_of_the_caption_and_rail(tmp_path, reference_fonts):
    scenes = [
        {"layout": "native", "headline": "Name this watch from one detail", "text_position": "top"},
        {"layout": "native", "headline": "How many did you get?", "text_position": "bottom"},
    ]
    render_project({"format": "carousel", "size": "portrait", "theme": "reference", "safe_zone": "tiktok_ads", "scenes": scenes}, tmp_path / "ads")
    for index, check_rail in ((1, False), (2, True)):
        with Image.open(tmp_path / "ads" / f"scene-{index:02d}.jpg") as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            white = [(x, y) for x in range(width) for y in range(height) if min(rgb.getpixel((x, y))) > 225 and not near(rgb.getpixel((x, y)), PAPER, 6)]
            assert white
            xs, ys = [x for x, _ in white], [y for _, y in white]
            assert min(ys) >= height * 0.125 - 1 and max(ys) <= height * 0.656 + 2  # the rows overlap by one pixel
            assert min(xs) >= width * 0.111 - 1
            if check_rail:
                assert max(xs) <= width * 0.722 + 1  # beside the like/comment/share rail


def test_reference_theme_needs_its_fonts_and_rejects_impossible_crops(tmp_path, monkeypatch):
    for name in ("STUDIO_SERIF_FONT", "STUDIO_SANS_FONT", "STUDIO_SANS_BOLD_FONT", "STUDIO_NATIVE_FONT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RenderError, match="STUDIO_SANS_FONT"):
        render_project({"format": "image", "theme": "reference", "scenes": [{"layout": "card", "headline": "x"}]}, tmp_path / "nofont")
    with pytest.raises(ValidationError, match="fit, focus and zoom"):
        StudioProject.model_validate({"scenes": [{"zoom": 2}]})
    with pytest.raises(ValidationError, match="focus"):
        StudioProject.model_validate({"scenes": [{"source_kind": "image", "source": "/tmp/a.png", "focus": [1.5, 0.5]}]})
    with pytest.raises(ValidationError, match="either a picture or a phone screen"):
        StudioProject.model_validate({"scenes": [{"layout": "card", "source_kind": "image", "source": "/tmp/a.png", "phone_screen": "/tmp/b.png"}]})
