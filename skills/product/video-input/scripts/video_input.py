"""Portable core runtime for the video-input skill.

The Groq and OpenRouter routes use only the Python standard library. Media
work is delegated to standalone ffmpeg and ffprobe programs discovered at
runtime.
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_lib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


_TRANSCRIPT_HEADING = "## Complete Transcript"
_INSTRUCTIONS_HEADING = "## Actionable Instructions"
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _finite_number(value: object, label: str = "value") -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def safe_name(value: str) -> str:
    """Return a stable, portable directory name without path traversal."""
    if not isinstance(value, str):
        raise TypeError("name must be a string")
    cleaned = value.strip()
    parts = cleaned.replace("\\", "/").split("/")
    if any(part.strip() in {".", ".."} for part in parts):
        raise ValueError("name cannot contain a path traversal component")
    if any(part.strip().rstrip(".").split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES for part in parts):
        raise ValueError("name cannot be a Windows reserved device name")
    result = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    if not result or result in {".", ".."}:
        raise ValueError("name must contain letters or digits")
    return result


def parse_timestamp(value: str | float | int) -> float:
    """Parse seconds or an H:M:S timestamp into a finite non-negative value."""
    if isinstance(value, bool):
        raise TypeError("timestamp must be a number or string")
    if isinstance(value, (int, float)):
        result = _finite_number(value, "timestamp")
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp cannot be empty")
        fields = text.split(":")
        if not 1 <= len(fields) <= 3:
            raise ValueError("timestamp has too many fields")
        try:
            values = [float(field) for field in fields]
        except ValueError as exc:
            raise ValueError("timestamp contains non-numeric fields") from exc
        if not all(math.isfinite(field) for field in values):
            raise ValueError("timestamp must be finite")
        if len(values) == 1:
            result = values[0]
        elif len(values) == 2:
            minutes, seconds = values
            if not 0 <= seconds < 60:
                raise ValueError("seconds must be under 60")
            result = minutes * 60 + seconds
        else:
            hours, minutes, seconds = values
            if not 0 <= minutes < 60 or not 0 <= seconds < 60:
                raise ValueError("minutes and seconds must be under 60")
            result = hours * 3600 + minutes * 60 + seconds
    else:
        raise TypeError("timestamp must be a number or string")
    if result < 0:
        raise ValueError("timestamp cannot be negative")
    return result


def format_timestamp(seconds: float) -> str:
    value = _finite_number(seconds, "seconds")
    if value < 0:
        raise ValueError("seconds cannot be negative")
    milliseconds = int(math.floor(value * 1000 + 0.5))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def source_identity(path: Path) -> str:
    source = Path(path).resolve()
    stat = source.stat()
    return f"{source}|{stat.st_size}|{stat.st_mtime_ns}"


def build_workspace(video_path: Path, cwd: Path, output_name: str | None = None) -> Path:
    """Choose a contained job directory, refusing completed output collisions."""
    source = Path(video_path)
    name = safe_name(output_name if output_name is not None else source.stem)
    if name.casefold() == "video-input":
        raise ValueError("video-input is a reserved workspace name; use --output-name")
    root = Path(cwd).resolve()
    workspace = (root / name).resolve()
    if workspace.parent != root:
        raise ValueError("workspace escapes invocation root")
    if (
        (workspace / "brief.md").is_file()
        and (workspace / "images").is_dir()
        and not (workspace / ".work").exists()
        and not (workspace / ".work").is_symlink()
    ):
        raise FileExistsError("completed output already exists")
    return workspace
