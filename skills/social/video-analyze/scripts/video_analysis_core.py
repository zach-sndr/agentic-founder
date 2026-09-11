"""Deterministic support code for the Gemini video analyzer.

This module deliberately has no Gemini dependency. Keeping media inspection,
artifact persistence, retry policy, and coverage validation independent makes
the public CLI testable without an API key or network access.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar


MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_RETRY_ATTEMPTS = 3

T = TypeVar("T")


class VideoAnalysisError(RuntimeError):
    """Base error for an actionable analysis failure."""


class PrerequisiteError(VideoAnalysisError):
    """Raised when an external tool or required dependency is missing."""


class IncompleteAnalysisError(VideoAnalysisError):
    """Raised when coverage recovery cannot reach the source video end."""


def utc_now() -> str:
    """Return a stable, timezone-aware timestamp for persisted diagnostics."""
    return datetime.now(UTC).isoformat()


def safe_name(value: str) -> str:
    """Make a readable, cross-platform directory component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return cleaned[:80] or "video"


def source_identity(video_path: Path) -> str:
    """Return a fast stable identity for a particular source file revision."""
    stat = video_path.stat()
    material = f"{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def build_artifact_dir(video_path: Path, cwd: Path | None = None) -> Path:
    """Return the single durable workspace for this source video revision."""
    root = (cwd or Path.cwd()).resolve() / "video-analyze"
    return root / f"{safe_name(video_path.stem)}--{source_identity(video_path)[:8]}"


def json_default(value: Any) -> str:
    """Serialize SDK dates and paths without leaking unsupported objects."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read JSON, returning a caller-supplied empty shape when absent/corrupt."""
    if not path.exists():
        return dict(default or {})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default or {})
    return loaded if isinstance(loaded, dict) else dict(default or {})


def write_json(path: Path, payload: Any, pretty: bool = True) -> Path:
    """Persist JSON after creating its parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2 if pretty else None, ensure_ascii=False, default=json_default),
        encoding="utf-8",
    )
    return path


def create_or_update_manifest(
    artifact_dir: Path,
    video_path: Path,
    media_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Create/update the user-readable manifest for one source workspace."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "manifest.json"
    manifest = read_json(manifest_path, {"schema_version": 1})
    stat = video_path.stat()
    manifest.update(
        {
            "schema_version": 1,
            "source": {
                "path": str(video_path.resolve()),
                "name": video_path.name,
                "identity": source_identity(video_path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "media": media_metadata,
            },
            "artifact_directory": str(artifact_dir.resolve()),
            "updated_at": utc_now(),
        }
    )
    manifest.setdefault("remote_file", {})
    manifest.setdefault("artifacts", {})
    manifest.setdefault("status", {"state": "prepared"})
    write_json(manifest_path, manifest)
    return manifest


def update_manifest(artifact_dir: Path, **updates: Any) -> dict[str, Any]:
    """Merge top-level manifest changes and persist them."""
    manifest_path = artifact_dir / "manifest.json"
    manifest = read_json(manifest_path, {"schema_version": 1})
    manifest.update(updates)
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def append_activity(artifact_dir: Path, event: str, details: dict[str, Any] | None = None) -> Path:
    """Append a structured, line-oriented diagnostic event."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "activity.jsonl"
    record = {"timestamp": utc_now(), "event": event, "details": details or {}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=json_default) + "\n")
    return path


def _winget_bin(tool_name: str) -> str | None:
    """Find a user-level Winget FFmpeg install before the terminal PATH refreshes."""
    if os.name != "nt":
        return None
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not package_root.exists():
        return None
    matches = sorted(package_root.glob(f"Gyan.FFmpeg_*/*/bin/{tool_name}.exe"), reverse=True)
    return str(matches[0]) if matches else None


def find_media_tool(tool_name: str) -> str | None:
    """Locate ffmpeg/ffprobe from PATH, FFMPEG_BIN, or current Winget installs."""
    configured_bin = os.environ.get("FFMPEG_BIN")
    candidates = []
    if configured_bin:
        suffix = ".exe" if os.name == "nt" else ""
        candidates.append(Path(configured_bin) / f"{tool_name}{suffix}")
    candidates.append(Path(shutil.which(tool_name)) if shutil.which(tool_name) else None)
    winget = _winget_bin(tool_name)
    candidates.append(Path(winget) if winget else None)
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    return None


def available_media_tools() -> dict[str, str]:
    """Return the installed media tools without making either one mandatory."""
    return {name: tool for name in ("ffmpeg", "ffprobe") if (tool := find_media_tool(name))}


def _missing_media_tools_error(missing: list[str]) -> PrerequisiteError:
    """Describe the permission-first FFmpeg installation path consistently."""
    return PrerequisiteError(
        "Missing required media tool(s): "
        + ", ".join(missing)
        + ". Ask the user before installing FFmpeg. On Windows: winget install --id Gyan.FFmpeg -e. "
        "Then open a new terminal or set FFMPEG_BIN to its bin directory."
    )


def require_ffprobe() -> str:
    """Return ffprobe because deterministic metadata and coverage need it."""
    ffprobe = find_media_tool("ffprobe")
    if not ffprobe:
        raise _missing_media_tools_error(["ffprobe"])
    return ffprobe


def require_ffmpeg() -> str:
    """Return ffmpeg when a local conversion is required."""
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg:
        raise _missing_media_tools_error(["ffmpeg"])
    return ffmpeg


def require_media_tools() -> dict[str, str]:
    """Return both FFmpeg tools or explain a safe installation route."""
    tools = {name: find_media_tool(name) for name in ("ffmpeg", "ffprobe")}
    missing = [name for name, value in tools.items() if not value]
    if missing:
        raise _missing_media_tools_error(missing)
    return {name: value for name, value in tools.items() if value}


def _run_ffprobe(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise VideoAnalysisError(completed.stderr.strip() or "ffprobe could not inspect the video")
    return completed.stdout


def _ratio_to_float(value: Any) -> float | None:
    if value in (None, "", "0/0"):
        return None
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def seconds_to_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS.mmm (or HH:MM:SS.mmm for long videos)."""
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(seconds), 3600)
    minutes, whole_seconds = divmod(remainder, 60)
    milliseconds = round((seconds - math.floor(seconds)) * 1000)
    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0
    if hours:
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _rotation_degrees(stream: dict[str, Any]) -> int:
    """Read common ffprobe rotation fields and normalize them to 0–359 degrees."""
    candidates: list[Any] = []
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        candidates.extend(item.get("rotation") for item in side_data if isinstance(item, dict))
    tags = stream.get("tags")
    if isinstance(tags, dict):
        candidates.append(tags.get("rotate"))
    for value in candidates:
        try:
            return int(float(value)) % 360
        except (TypeError, ValueError):
            continue
    return 0


def probe_video(video_path: Path, runner: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Extract deterministic source facts from ffprobe JSON."""
    if not video_path.exists():
        raise VideoAnalysisError(f"Video file not found: {video_path}")
    if video_path.stat().st_size > MAX_VIDEO_BYTES:
        raise VideoAnalysisError("Video exceeds the conservative 2 GB Gemini Files API limit.")
    ffprobe = find_media_tool("ffprobe")
    if runner is None and not ffprobe:
        ffprobe = require_ffprobe()
    command = [
        ffprobe or "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,bit_rate:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,bit_rate,pix_fmt,sample_rate,channels:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(video_path),
    ]
    raw = (runner or _run_ffprobe)(command)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise VideoAnalysisError(f"ffprobe returned invalid JSON: {error}") from error
    streams = payload.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video_stream:
        raise VideoAnalysisError("ffprobe found no video stream.")
    format_info = payload.get("format") or {}
    duration = _ratio_to_float(format_info.get("duration"))
    if duration is None or duration <= 0:
        raise VideoAnalysisError("ffprobe did not provide a positive video duration.")
    width = video_stream.get("width")
    height = video_stream.get("height")
    rotation_degrees = _rotation_degrees(video_stream)
    display_width, display_height = width, height
    if rotation_degrees in (90, 270):
        display_width, display_height = height, width
    aspect_ratio = None
    if isinstance(display_width, int) and isinstance(display_height, int) and display_width > 0 and display_height > 0:
        divisor = math.gcd(display_width, display_height)
        aspect_ratio = f"{display_width // divisor}:{display_height // divisor}"
    return {
        "duration_seconds": round(duration, 3),
        "duration_timestamp": seconds_to_timestamp(duration),
        "format_name": format_info.get("format_name"),
        "container_bit_rate": _ratio_to_float(format_info.get("bit_rate")),
        "width": display_width,
        "height": display_height,
        "encoded_width": width,
        "encoded_height": height,
        "rotation_degrees": rotation_degrees,
        "aspect_ratio": aspect_ratio,
        "frame_rate": _ratio_to_float(video_stream.get("avg_frame_rate"))
        or _ratio_to_float(video_stream.get("r_frame_rate")),
        "video_codec": video_stream.get("codec_name"),
        "video_bit_rate": _ratio_to_float(video_stream.get("bit_rate")),
        "pixel_format": video_stream.get("pix_fmt"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "audio_sample_rate": _ratio_to_float(audio_stream.get("sample_rate")) if audio_stream else None,
        "audio_channels": audio_stream.get("channels") if audio_stream else None,
    }


def is_transient_error(error: BaseException) -> bool:
    """Return true only for retryable network/service conditions."""
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    try:
        if status is not None and (int(status) == 408 or int(status) == 429 or int(status) >= 500):
            return True
    except (TypeError, ValueError):
        pass
    message = f"{type(error).__name__}: {error}".lower()
    patterns = ("timeout", "timed out", "connection", "temporar", "rate limit", "too many requests", "internal error", "service unavailable")
    return any(pattern in message for pattern in patterns)


def retry_call(
    action: str,
    operation: Callable[[], T],
    logger: Callable[[str, dict[str, Any]], Any],
    sleeper: Callable[[float], Any] = time.sleep,
    jitter: Callable[[], float] = random.random,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> T:
    """Retry only transient failures with bounded exponential backoff and jitter."""
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt >= max_attempts or not is_transient_error(error):
                raise
            delay = min(8.0, float(2 ** (attempt - 1))) + (float(jitter()) * 0.25)
            logger("retry", {"action": action, "attempt": attempt, "delay_seconds": round(delay, 3), "error": str(error)})
            sleeper(delay)
    raise AssertionError("retry loop exited unexpectedly")


def parse_timestamp(value: Any) -> float | None:
    """Parse common Gemini timestamp forms without treating arbitrary numbers as time."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    if match:
        hours = float(match.group(1) or 0)
        minutes = float(match.group(2))
        seconds = float(match.group(3))
        return hours * 3600 + minutes * 60 + seconds
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)", text.lower())
    return float(match.group(1)) if match else None


def iter_timestamps(payload: Any, key: str = "") -> Iterable[float]:
    """Walk only timestamp-like JSON fields to avoid false coverage from scores."""
    if isinstance(payload, dict):
        for child_key, value in payload.items():
            # ffprobe facts and a previous verifier result describe the source or
            # prior decision, not what Gemini itself covered. Recovery records
            # deliberately remain traversable because a locally verified tail
            # adds explicit end evidence only after that tail has passed.
            if child_key in {"_local_metadata", "_verification"}:
                continue
            yield from iter_timestamps(value, str(child_key).lower())
        return
    if isinstance(payload, list):
        for value in payload:
            yield from iter_timestamps(value, key)
        return
    timestamp_key = "duration" not in key and any(
        token in key
        for token in ("timestamp", "timecode", "analyzed_through", "covered_through", "last_observed", "coverage_end")
    )
    if timestamp_key:
        parsed = parse_timestamp(payload)
        if parsed is not None:
            yield parsed


def validate_coverage(payload: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    """Verify that model timestamps reach the deterministic ffprobe duration."""
    timestamps = list(iter_timestamps(payload))
    covered = max(timestamps, default=0.0)
    # Gemini video timestamps are sampled at second granularity. One second is
    # enough to accommodate that resolution while still rejecting a real tail.
    tolerance = max(1.0, duration_seconds * 0.01)
    return {
        "complete": covered >= duration_seconds - tolerance,
        "covered_through_seconds": round(covered, 3),
        "covered_through_timestamp": seconds_to_timestamp(covered),
        "duration_seconds": round(duration_seconds, 3),
        "duration_timestamp": seconds_to_timestamp(duration_seconds),
        "tolerance_seconds": round(tolerance, 3),
        "timestamp_evidence_count": len(timestamps),
    }


def uncovered_tail_start(validation: dict[str, Any]) -> float:
    """Include one second of overlap when recovering an uncovered tail."""
    return max(0.0, float(validation.get("covered_through_seconds") or 0.0) - 1.0)


def normalise_prompt_result(payload: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Normalize a focused request into a predictable evidence-bearing JSON shape."""
    coverage = payload.get("analysis_coverage") or payload.get("coverage") or {}
    findings = payload.get("findings")
    if not isinstance(findings, list):
        findings = []
    return {
        "description": str(payload.get("description") or ""),
        "prompt": prompt,
        "answer": str(payload.get("answer") or ""),
        "findings": findings,
        "not_found": bool(payload.get("not_found", False)),
        "analysis_coverage": coverage if isinstance(coverage, dict) else {},
        "limitations": payload.get("limitations") if isinstance(payload.get("limitations"), list) else [],
    }


def normalization_command(video_path: Path, artifact_dir: Path, ffmpeg: str) -> tuple[list[str], Path]:
    """Return a deterministic H.264/AAC normalization command and artifact path."""
    output = artifact_dir / f"normalized-{safe_name(video_path.stem)}.mp4"
    temporary_output = output.with_name(f"{output.stem}.partial{output.suffix}")
    return (
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ],
        output,
    )


def downscale_dimensions(width: int, height: int, target_height: int) -> tuple[int, int] | None:
    """Return even, proportional dimensions only when a source exceeds a height ceiling."""
    if width <= 0 or height <= 0 or target_height <= 0:
        raise VideoAnalysisError("Video dimensions and target height must be positive.")
    if height <= target_height:
        return None
    scaled_width = max(2, int(round((width * target_height / height) / 2.0)) * 2)
    return scaled_width, target_height


def upload_downscale_command(
    video_path: Path,
    artifact_dir: Path,
    ffmpeg: str,
    width: int,
    height: int,
    target_height: int,
) -> tuple[list[str], Path]:
    """Return a retained, downscale-only H.264/AAC upload preparation command."""
    dimensions = downscale_dimensions(width, height, target_height)
    if dimensions is None:
        raise VideoAnalysisError("The source is already at or below the requested upload height.")
    output = artifact_dir / f"upload-{target_height}p.mp4"
    temporary_output = output.with_name(f"{output.stem}.partial{output.suffix}")
    return (
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            f"scale=-2:{target_height}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ],
        output,
    )


def tail_clip_command(video_path: Path, artifact_dir: Path, ffmpeg: str, start_seconds: float) -> tuple[list[str], Path]:
    """Return a local fallback tail clip command for failed API segment recovery."""
    output = artifact_dir / f"recovery-tail-{int(start_seconds):04d}s.mp4"
    return (
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(video_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ],
        output,
    )


def run_ffmpeg(command: list[str]) -> None:
    """Run an ffmpeg conversion and expose a concise actionable failure."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise VideoAnalysisError(completed.stderr.strip() or "ffmpeg conversion failed")


def is_upload_compatibility_error(error: BaseException) -> bool:
    """Recognize failures for which a local normalization retry is appropriate."""
    message = f"{type(error).__name__}: {error}".lower()
    return any(token in message for token in ("unsupported", "mime", "codec", "format", "media type"))
