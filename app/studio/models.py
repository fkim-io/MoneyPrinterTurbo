"""The deliberately small, local-only Slapz composition contract."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SIZE_DIMENSIONS = {
    "portrait": (1080, 1920),
    "feed": (1080, 1350),
    "square": (1080, 1080),
}


def _local_reference(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip() or "\x00" in value or re.match(r"^[a-zA-Z][\w+.-]*://", value):
        raise ValueError("media must be a nonempty local file path, never a URL")
    return value


class StudioScene(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    source: str | None = None
    source_kind: Literal["image", "video", "color"] = "color"
    headline: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=4000)
    layout: Literal["boxed", "paragraph", "top", "minimal"] = "boxed"
    duration: float = Field(default=4.0, ge=0.2, le=60, allow_inf_nan=False)
    source_start: float = Field(default=0.0, ge=0, le=86400, allow_inf_nan=False)
    preserve_audio: bool = False
    text_position: Literal["top", "center", "bottom"] = "center"

    _source_is_local = field_validator("source")(_local_reference)

    @field_validator("headline", "body")
    @classmethod
    def printable_text(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("text may not contain control characters")
        return value.replace("\t", "    ").strip()

    @model_validator(mode="after")
    def appropriate_media_options(self) -> "StudioScene":
        if self.source_kind == "color":
            if self.source is not None:
                raise ValueError("color scenes must have source=null")
        elif self.source is None:
            raise ValueError(f"{self.source_kind} scenes require a local source file")
        if self.source_kind != "video" and (self.source_start or self.preserve_audio):
            raise ValueError("source_start and preserve_audio apply only to video scenes")
        return self


class StudioCaption(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    start: float = Field(ge=0, le=180, allow_inf_nan=False)
    end: float = Field(gt=0, le=180, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=300)

    @field_validator("text")
    @classmethod
    def printable_text(cls, value: str) -> str:
        value = StudioScene.printable_text(value)
        if not value:
            raise ValueError("caption text must not be empty")
        return value

    @model_validator(mode="after")
    def positive_interval(self) -> "StudioCaption":
        if self.end - self.start < 1 / 30 - 1e-9:
            raise ValueError("caption end must be at least one video frame after start")
        return self


class StudioProject(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    format: Literal["image", "carousel", "slideshow", "reel"] = "carousel"
    size: Literal["portrait", "feed", "square"] = "portrait"
    title: str = Field(default="Slapz creative", min_length=1, max_length=200)
    creative_id: str = Field(default="slapz-draft", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    scenes: list[StudioScene] = Field(min_length=1, max_length=20)
    narration_file: str | None = None
    music_file: str | None = None
    music_volume: float = Field(default=0.15, ge=0, le=1, allow_inf_nan=False)
    captions: list[StudioCaption] = Field(default_factory=list, max_length=100)

    _audio_is_local = field_validator("narration_file", "music_file")(_local_reference)

    @model_validator(mode="after")
    def appropriate_format(self) -> "StudioProject":
        if not self.title.strip():
            raise ValueError("title must contain visible text")
        if self.format == "image" and len(self.scenes) != 1:
            raise ValueError("image projects require exactly one scene")
        if self.format in {"image", "carousel"}:
            if any(scene.source_kind == "video" for scene in self.scenes):
                raise ValueError("still-image exports accept image or color scenes only")
            if self.narration_file or self.music_file:
                raise ValueError("still-image exports cannot contain narration or music")
            if self.captions:
                raise ValueError("timed captions apply only to slideshow or reel videos")
        elif sum(scene.duration for scene in self.scenes) > 180:
            raise ValueError("video projects may be at most 180 seconds long")
        timeline_duration = sum(round(scene.duration * 30) / 30 for scene in self.scenes)
        if self.format in {"slideshow", "reel"} and timeline_duration > 180:
            raise ValueError("video projects may be at most 180 seconds after rounding to 30 fps")
        previous_end = 0.0
        for index, caption in enumerate(self.captions, 1):
            if caption.start < previous_end:
                raise ValueError(f"Caption {index}: cues must be ordered and must not overlap")
            if caption.end > timeline_duration + 1e-9:
                raise ValueError(f"Caption {index}: end exceeds the rendered video timeline")
            previous_end = caption.end
        return self
