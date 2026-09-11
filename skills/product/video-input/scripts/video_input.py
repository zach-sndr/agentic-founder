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
