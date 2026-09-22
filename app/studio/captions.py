"""Strict, dependency-free SRT input; captions remain plain text, never filter syntax."""

import html
import re

from .models import StudioCaption


_TIME = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_TIMING = re.compile(rf"^{_TIME}\s*-->\s*{_TIME}$")


def parse_srt(text: str) -> list[dict]:
    """Return ordered {start, end, text} cues; malformed/overlapping SRT is an error.

    StudioProject additionally checks the final cue against its specific timeline.
    Simple SRT bold/italic/underline tags become plain text, and entities are decoded.
    """
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("SRT is empty")
    cues = []
    previous_end = 0.0
    for block_index, block in enumerate(re.split(r"\n\s*\n", normalized), 1):
        lines = block.split("\n")
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"SRT block {block_index}: expected cue number, timestamps, and text")
        match = _TIMING.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError(f"SRT block {block_index}: invalid timestamp line")
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        content = html.unescape(re.sub(r"</?(?:b|i|u)>", "", "\n".join(lines[2:]), flags=re.IGNORECASE))
        cue = StudioCaption(start=start, end=end, text=content)
        if cue.start < previous_end:
            raise ValueError(f"SRT block {block_index}: cues must be ordered and must not overlap")
        previous_end = cue.end
        cues.append(cue.model_dump())
        if len(cues) > 100:
            raise ValueError("SRT may contain at most 100 cues")
    return cues
