"""Offline composition; importing this package never loads a provider or upstream config."""

from .captions import parse_srt
from .models import StudioCaption, StudioProject, StudioScene
from .render import RenderError, RenderProject, TextOverflowError, render_project

__all__ = [
    "StudioProject",
    "StudioScene",
    "StudioCaption",
    "parse_srt",
    "RenderError",
    "TextOverflowError",
    "render_project",
    "RenderProject",
]
