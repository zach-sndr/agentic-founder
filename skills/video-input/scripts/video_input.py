"""Portable core runtime for the video-input skill.

The OpenRouter route uses only the Python standard library. The NVIDIA hosted
route loads the optional nvidia-riva-client package on demand. Media work is
delegated to standalone ffmpeg and ffprobe programs discovered at runtime.
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
    result = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-.")
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


def _atomic_write(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def save_state(workspace: Path, state: dict[str, Any]) -> Path:
    state_file = Path(workspace) / ".work" / "state.json"
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(state_file, payload)
    return state_file


def build_initial_chunks(duration: float, chunk_seconds: int, overlap: float) -> list[dict[str, Any]]:
    duration_value = _finite_number(duration, "duration")
    overlap_value = _finite_number(overlap, "overlap")
    if duration_value < 0 or overlap_value < 0:
        raise ValueError("duration and overlap cannot be negative")
    if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int) or chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be a positive integer")
    chunks: list[dict[str, Any]] = []
    start = 0.0
    identifier = 0
    while start < duration_value:
        end = min(start + chunk_seconds, duration_value)
        chunks.append({
            "id": identifier,
            "owned_start": start,
            "owned_end": end,
            "extract_start": max(0.0, start - overlap_value),
            "extract_end": min(duration_value, end + overlap_value),
            "status": "pending",
        })
        identifier += 1
        start = end
    return chunks


def normalize_segments(
    payload: dict[str, Any], extraction_start: float, owned_start: float, owned_end: float,
    extraction_end: float | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise TypeError("transcription payload must contain a segments list")
    extract = _finite_number(extraction_start, "extraction_start")
    owned_low = _finite_number(owned_start, "owned_start")
    owned_high = _finite_number(owned_end, "owned_end")
    if owned_low > owned_high:
        raise ValueError("owned range is backwards")
    local_duration: float | None = None
    if extraction_end is not None:
        extract_high = _finite_number(extraction_end, "extraction_end")
        if extract_high < extract:
            raise ValueError("extraction range is backwards")
        local_duration = extract_high - extract
    normalized: list[dict[str, Any]] = []
    previous_end = -math.inf
    for segment in payload["segments"]:
        if not isinstance(segment, dict) or "start" not in segment or "end" not in segment:
            raise ValueError("segment requires start and end")
        start = _finite_number(segment["start"], "segment start")
        end = _finite_number(segment["end"], "segment end")
        if start < 0 or end < 0:
            raise ValueError("segment timestamps cannot be negative")
        if local_duration is not None and end > local_duration + 1e-9:
            raise ValueError("segment timestamp exceeds the extracted audio")
        if start > end or start < previous_end:
            raise ValueError("segments must be monotonically ordered")
        previous_end = end
        global_start = extract + start
        global_end = extract + end
        midpoint = (global_start + global_end) / 2
        if owned_low <= midpoint < owned_high:
            text = segment.get("text", "")
            if not isinstance(text, str):
                raise TypeError("segment text must be a string")
            normalized.append({"start": global_start, "end": global_end, "text": text.strip()})
    return normalized


def _skeleton() -> str:
    return f"# Video Brief\n\n{_INSTRUCTIONS_HEADING}\n\n\n{_TRANSCRIPT_HEADING}\n\n"


def _split_brief(content: str) -> tuple[str, str]:
    if _INSTRUCTIONS_HEADING not in content or _TRANSCRIPT_HEADING not in content:
        content = _skeleton()
    instructions_at = content.index(_INSTRUCTIONS_HEADING)
    transcript_at = content.index(_TRANSCRIPT_HEADING, instructions_at)
    return content[:transcript_at], content[transcript_at:]


def _render_transcript(state: dict[str, Any], chunk_payloads: list[dict[str, Any]]) -> str:
    payload_by_id: dict[object, dict[str, Any]] = {}
    for payload in chunk_payloads:
        if not isinstance(payload, dict) or "chunk_id" not in payload:
            raise ValueError("chunk payload requires a chunk_id")
        payload_by_id[payload["chunk_id"]] = payload
    raw_chunks = state.get("chunks", [])
    if not isinstance(raw_chunks, list):
        raise TypeError("state chunks must be a list")
    ordered_chunks: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, chunk in enumerate(raw_chunks):
        if not isinstance(chunk, dict):
            raise TypeError("state chunk must be an object")
        owned_start = _finite_number(chunk.get("owned_start"), "owned_start")
        owned_end = _finite_number(chunk.get("owned_end"), "owned_end")
        if owned_start > owned_end:
            raise ValueError("chunk ownership range is backwards")
        ordered_chunks.append((owned_start, owned_end, index, chunk))
    ordered_chunks.sort(key=lambda item: (item[0], item[1], item[2]))
    previous_end: float | None = None
    lines: list[str] = []
    for owned_start, owned_end, _, chunk in ordered_chunks:
        if previous_end is not None and not math.isclose(owned_start, previous_end, abs_tol=1e-9):
            raise ValueError("chunk ownership ranges must be contiguous")
        previous_end = owned_end
        if chunk.get("status") != "completed":
            break
        chunk_id = chunk.get("id")
        payload = payload_by_id.get(chunk_id)
        if payload is None or not isinstance(payload.get("segments"), list):
            raise ValueError("completed chunk has no valid transcript payload")
        segments = payload["segments"]
        if not segments:
            lines.append(f"[{format_timestamp(owned_start)} - {format_timestamp(owned_end)}] (No speech detected.)")
            continue
        for segment in segments:
            if not isinstance(segment, dict) or "start" not in segment or "end" not in segment:
                raise ValueError("transcript segment requires start and end")
            start = _finite_number(segment["start"], "segment start")
            end = _finite_number(segment["end"], "segment end")
            if start > end:
                raise ValueError("transcript segment end precedes start")
            text = segment.get("text", "")
            if not isinstance(text, str):
                raise TypeError("transcript text must be a string")
            lines.append(f"[{format_timestamp(start)} - {format_timestamp(end)}] {text}")
    return "\n".join(lines)


def update_transcript(brief_path: Path, state: dict[str, Any], chunk_payloads: list[dict[str, Any]]) -> None:
    brief = Path(brief_path)
    existing = brief.read_text(encoding="utf-8") if brief.exists() else _skeleton()
    prefix, _ = _split_brief(existing)
    transcript = _render_transcript(state, chunk_payloads)
    rendered = prefix + _TRANSCRIPT_HEADING + "\n\n" + transcript + ("\n" if transcript else "")
    _atomic_write(brief, rendered.encode("utf-8"))


def validate_resume_source(state: dict[str, Any]) -> Path:
    try:
        source = Path(state["source_path"])
        expected = state["source_identity"]
    except (KeyError, TypeError) as exc:
        raise ValueError("state has no resumable source identity") from exc
    if not source.is_file():
        raise FileNotFoundError("saved source video no longer exists")
    if source_identity(source) != expected:
        raise ValueError("saved source video has changed")
    return source.resolve()


def find_media_tool(name: str) -> str | None:
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError("media tool must be ffmpeg or ffprobe")
    extension = ".exe" if os.name == "nt" else ""
    binary_name = name + extension

    def usable(candidate: Path) -> bool:
        return candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK))

    explicit = os.environ.get("FFMPEG_BIN", "").strip()
    if explicit:
        candidate = Path(explicit)
        if name == "ffprobe":
            candidate = candidate.with_name("ffprobe" + candidate.suffix)
        if usable(candidate):
            return str(candidate)
    located = shutil.which(name) or shutil.which(binary_name)
    if located:
        return located
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / binary_name
        if usable(candidate):
            return str(candidate)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for package in packages.glob("*FFmpeg*"):
                candidate = package / "bin" / binary_name
                if usable(candidate):
                    return str(candidate)
                for nested in package.glob(f"ffmpeg-*/bin/{binary_name}"):
                    if usable(nested):
                        return str(nested)
    return None


def require_media_tools() -> tuple[str, str]:
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found; set FFMPEG_BIN or add it to PATH")
    ffprobe = find_media_tool("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found; install it alongside ffmpeg")
    return ffmpeg, ffprobe


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def probe_duration(video_path: Path, ffprobe: str | None = None) -> float:
    probe = ffprobe or require_media_tools()[1]
    completed = run_command([probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)])
    value = _finite_number(completed.stdout.strip(), "probed duration")
    if value < 0:
        raise ValueError("probed duration cannot be negative")
    return value


def _dotenv_values(script_dir: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    dotenv = Path(script_dir) / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
            if match:
                value = match.group(2).strip().strip("\"'").strip()
                if value:
                    values[match.group(1)] = value
    return values


def load_api_keys(script_dir: Path) -> dict[str, str]:
    dotenv = _dotenv_values(script_dir)
    keys = {
        "nvidia": os.environ.get("NVIDIA_API_KEY", "").strip() or dotenv.get("NVIDIA_API_KEY", ""),
        "openrouter": os.environ.get("OPENROUTER_API_KEY", "").strip() or dotenv.get("OPENROUTER_API_KEY", ""),
    }
    available = {provider: value for provider, value in keys.items() if value}
    if not available:
        raise RuntimeError(
            "No transcription API key is configured; follow the skill's onboarding.md instructions"
        )
    return available


def load_openrouter_key(script_dir: Path) -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip() or _dotenv_values(script_dir).get(
        "OPENROUTER_API_KEY", ""
    )
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    return key


class TimestampUnavailableError(RuntimeError):
    """Signal that a provider response cannot satisfy the no-invented-timestamps contract."""


class NvidiaProviderError(RuntimeError):
    """A redacted NVIDIA hosted-inference failure eligible for provider fallback."""


def _protobuf_seconds(value: object, label: str) -> float:
    try:
        if hasattr(value, "seconds"):
            seconds = _finite_number(getattr(value, "seconds"), label)
            nanos = _finite_number(getattr(value, "nanos", 0), label)
            result = seconds + nanos / 1_000_000_000
        else:
            result = _finite_number(value, label)
    except (TypeError, ValueError):
        raise TimestampUnavailableError(f"NVIDIA Whisper omitted a valid {label}") from None
    if result < 0:
        raise TimestampUnavailableError(f"NVIDIA Whisper returned a negative {label}")
    return result


class NvidiaWhisperClient:
    SERVER = "grpc.nvcf.nvidia.com:443"
    FUNCTION_ID = "b702f636-f60c-4a3d-a6f4-f3568c13bd7d"

    def __init__(self, api_key: str, client_module: Any | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("an NVIDIA API key is required")
        if any(not 0x21 <= ord(character) <= 0x7E for character in api_key):
            raise ValueError("the NVIDIA API key contains invalid characters")
        self._api_key = api_key
        if client_module is None:
            try:
                client_module = __import__("riva.client", fromlist=["client"])
            except ImportError:
                raise NvidiaProviderError(
                    "NVIDIA Whisper requires the optional nvidia-riva-client package; see onboarding.md"
                ) from None
        self._client = client_module

    def transcribe(self, audio_path: Path, language: str | None = None) -> dict[str, Any]:
        audio = Path(audio_path)
        if not audio.is_file():
            raise FileNotFoundError("audio file does not exist")
        try:
            auth = self._client.Auth(
                uri=self.SERVER,
                use_ssl=True,
                metadata_args=[
                    ["function-id", self.FUNCTION_ID],
                    ["authorization", f"Bearer {self._api_key}"],
                ],
            )
            service = self._client.ASRService(auth)
            config = self._client.RecognitionConfig(
                language_code=language or "multi",
                max_alternatives=1,
                enable_automatic_punctuation=True,
                enable_word_time_offsets=True,
            )
            response = service.offline_recognize(audio.read_bytes(), config)
        except (FileNotFoundError, TimestampUnavailableError):
            raise
        except Exception:
            raise NvidiaProviderError(
                "NVIDIA hosted Whisper transcription failed; the API key and provider details were redacted"
            ) from None

        segments: list[dict[str, Any]] = []
        previous_end = -math.inf
        for result in getattr(response, "results", ()):
            alternatives = getattr(result, "alternatives", ())
            if not alternatives:
                continue
            alternative = alternatives[0]
            words = list(getattr(alternative, "words", ()))
            if not words:
                raise TimestampUnavailableError(
                    "NVIDIA hosted Whisper returned no word timestamps"
                )
            start = _protobuf_seconds(getattr(words[0], "start_time", None), "word start")
            end = _protobuf_seconds(getattr(words[-1], "end_time", None), "word end")
            if end < start or start < previous_end:
                raise TimestampUnavailableError(
                    "NVIDIA hosted Whisper returned non-monotonic word timestamps"
                )
            text = str(getattr(alternative, "transcript", "")).strip()
            if not text:
                text = " ".join(str(getattr(word, "word", "")).strip() for word in words).strip()
            if text:
                segments.append({"start": start, "end": end, "text": text})
            previous_end = end
        if not segments:
            raise TimestampUnavailableError(
                "NVIDIA hosted Whisper returned no timestamped segments"
            )
        return {"segments": segments}


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai") -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("an OpenRouter API key is required")
        if any(not 0x21 <= ord(character) <= 0x7E for character in api_key):
            raise ValueError("the OpenRouter API key contains invalid characters")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def transcribe(self, audio_path: Path, language: str | None = None) -> dict[str, Any]:
        audio = Path(audio_path)
        if not audio.is_file():
            raise FileNotFoundError("audio file does not exist")
        payload: dict[str, Any] = {
            "model": "openai/whisper-large-v3",
            "input_audio": {"format": "flac", "data": base64.b64encode(audio.read_bytes()).decode("ascii")},
            "temperature": 0,
            "response_format": "verbose_json",
            "provider": {"order": ["Together"], "allow_fallbacks": False},
        }
        if language is not None:
            payload["language"] = language
        body = json.dumps(payload).encode("utf-8")
        endpoint = self._base_url + "/api/v1/audio/transcriptions"
        for attempt in range(3):
            try:
                req = urlrequest.Request(endpoint, data=body, method="POST", headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"})
                with urlrequest.urlopen(req, timeout=60) as response:
                    response_body = response.read()
            except ValueError:
                raise RuntimeError("OpenRouter request could not be constructed safely") from None
            except urlerror.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status == 401:
                    raise RuntimeError("OpenRouter request failed with 401: check API key authorization") from None
                if status == 402:
                    raise RuntimeError("OpenRouter request failed with 402: check credits or billing") from None
                if status == 413:
                    raise AudioTooLargeError("OpenRouter rejected the audio chunk as too large (HTTP 413)") from None
                transient = status == 429 or 500 <= status <= 599
                if not transient or attempt == 2:
                    raise RuntimeError(f"OpenRouter transcription failed with HTTP {status}") from None
                retry_text = retry_after.strip() if retry_after else ""
                delay = min(int(retry_text), 30) if retry_text.isdigit() else 1
            except (urlerror.URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise TranscriptionTimeoutError("OpenRouter transcription failed after network retries") from None
                delay = 1
            else:
                return json.loads(response_body.decode("utf-8"))
            time.sleep(delay)
        raise RuntimeError("OpenRouter transcription failed")


class PreferredTranscriptionClient:
    """Use NVIDIA hosted Whisper first and OpenRouter Whisper only when needed."""

    def __init__(self, primary: Any | None, fallback: Any | None) -> None:
        if primary is None and fallback is None:
            raise ValueError("at least one transcription provider is required")
        self._primary = primary
        self._fallback = fallback

    def transcribe(self, audio_path: Path, language: str | None = None) -> dict[str, Any]:
        if self._primary is not None:
            try:
                return self._primary.transcribe(audio_path, language)
            except (NvidiaProviderError, TimestampUnavailableError, TranscriptionTimeoutError):
                if self._fallback is None:
                    raise
        if self._fallback is None:
            raise RuntimeError("no usable transcription provider is configured")
        return self._fallback.transcribe(audio_path, language)


def build_transcription_client(script_dir: Path) -> PreferredTranscriptionClient:
    keys = load_api_keys(script_dir)
    primary: Any | None = None
    fallback: Any | None = None
    nvidia_error: NvidiaProviderError | None = None
    if "nvidia" in keys:
        try:
            primary = NvidiaWhisperClient(keys["nvidia"])
        except NvidiaProviderError as exc:
            nvidia_error = exc
    if "openrouter" in keys:
        fallback = OpenRouterClient(keys["openrouter"])
    if primary is None and fallback is None and nvidia_error is not None:
        raise nvidia_error
    return PreferredTranscriptionClient(primary, fallback)


def transcribe_chunk(audio_path: Path, workspace: Path, api_key: str, language: str | None = None) -> dict[str, Any]:
    """Small 2A seam; chunk extraction and persistence belong to Task 2B."""
    if not Path(audio_path).is_file():
        raise FileNotFoundError("audio chunk does not exist")
    return OpenRouterClient(api_key).transcribe(audio_path, language)


class AudioTooLargeError(RuntimeError):
    """Signal that an audio ownership chunk should be split without retrying it."""


class TranscriptionTimeoutError(RuntimeError):
    """Signal that network retries were exhausted and a chunk may be split."""


def _default_workspace_path(video_path: Path, cwd: Path, output_name: str | None = None) -> Path:
    source = Path(video_path)
    name = safe_name(output_name if output_name is not None else source.stem)
    output_root = Path(cwd).resolve()
    workspace = (output_root / name).resolve()
    if workspace.parent != output_root:
        raise ValueError("workspace escapes invocation root")
    return workspace


def _legacy_workspace_path(video_path: Path, cwd: Path, output_name: str | None = None) -> Path:
    source = Path(video_path)
    name = safe_name(output_name if output_name is not None else source.stem)
    invocation_root = Path(cwd).resolve()
    lexical_root = invocation_root / "video-input"
    output_root = lexical_root.resolve()
    if lexical_root.is_symlink() or output_root != lexical_root:
        raise ValueError("legacy output root cannot be a symbolic link or junction")
    workspace = (output_root / name).resolve()
    if workspace.parent != output_root:
        raise ValueError("legacy workspace escapes its output root")
    return workspace


def _workspace_roots(cwd: Path) -> tuple[Path, ...]:
    direct = Path(cwd).resolve()
    lexical_legacy = direct / "video-input"
    legacy = lexical_legacy.resolve()
    if lexical_legacy.is_symlink() or legacy != lexical_legacy:
        raise ValueError("legacy output root cannot be a symbolic link or junction")
    return (direct,) if legacy == direct else (direct, legacy)


def _named_workspace_for_legacy_compatibility(
    video_path: Path, cwd: Path, output_name: str,
) -> Path:
    direct = _default_workspace_path(video_path, cwd, output_name)
    legacy = _legacy_workspace_path(video_path, cwd, output_name)
    if direct.name.casefold() == "video-input":
        if legacy.exists():
            return legacy
        raise ValueError("video-input is a reserved workspace name; use --output-name")
    return legacy if not direct.exists() and legacy.exists() else direct


def resolve_workspace_for_source(video_path: Path, cwd: Path) -> Path:
    """Read-only discovery of the unique active job for the exact current source."""
    source = Path(video_path).resolve()
    default = _default_workspace_path(source, cwd)
    if not source.is_file():
        return default
    identity = source_identity(source)
    matches: list[Path] = []
    for output_root in _workspace_roots(cwd):
        if not output_root.is_dir():
            continue
        for entry in output_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            workspace = entry.resolve()
            if workspace.parent != output_root:
                continue
            work = workspace / ".work"
            state_file = work / "state.json"
            if work.is_symlink() or state_file.is_symlink() or not state_file.is_file():
                continue
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(state, dict)
                and state.get("source_path") == str(source)
                and state.get("source_identity") == identity
            ):
                matches.append(workspace)
    if len(matches) > 1:
        names = ", ".join(sorted(workspace.name for workspace in matches))
        raise ValueError(f"multiple matching video-input jobs were found: {names}")
    if matches:
        return matches[0]
    legacy_default = _legacy_workspace_path(source, cwd)
    if legacy_default.exists():
        return legacy_default if not default.exists() or default.name.casefold() == "video-input" else default
    if default.name.casefold() == "video-input":
        raise ValueError("video-input is a reserved workspace name; use --output-name")
    return default


def status_video(video_path: Path, cwd: Path, output_name: str | None = None) -> dict[str, str]:
    """Inspect a job without creating directories or changing filesystem metadata."""
    source = Path(video_path)
    if output_name is None:
        workspace = resolve_workspace_for_source(source, cwd)
    else:
        workspace = _named_workspace_for_legacy_compatibility(source, cwd, output_name)
    brief = workspace / "brief.md"
    work = workspace / ".work"
    if brief.is_file() and (workspace / "images").is_dir() and not work.exists() and not work.is_symlink():
        return {"status": "completed"}
    if not workspace.exists():
        return {"status": "absent"}
    state_file = work / "state.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = None
        if isinstance(state, dict):
            try:
                chunks = _validate_transcription_state(state)
                if chunks and all(chunk.get("status") == "completed" for chunk in chunks):
                    _load_saved_responses(workspace, state, chunks)
                    return {"status": "ready-for-review"}
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
    return {"status": "in-progress"}


def _valid_flac(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 4:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"fLaC"
    except OSError:
        return False


def _publish_flac(destination: Path, command_prefix: list[str], description: str) -> None:
    """Render and validate a FLAC beside its destination before atomic publication."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".flac", dir=destination.parent,
    )
    os.close(descriptor)
    staging = Path(temporary_name)
    try:
        try:
            run_command([*command_prefix, str(staging)])
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"FFmpeg {description} failed; recovery files remain in .work") from exc
        if not _valid_flac(staging):
            raise RuntimeError(f"FFmpeg did not create a valid nonempty FLAC during {description}")
        try:
            os.replace(staging, destination)
        except OSError as exc:
            raise RuntimeError(f"could not atomically publish the {description} FLAC") from exc
    finally:
        staging.unlink(missing_ok=True)


def _validate_transcription_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise ValueError("saved transcription schema_version must be 1")
    duration = _finite_number(state.get("duration"), "saved duration")
    if duration < 0:
        raise ValueError("saved duration cannot be negative")
    overlap = _finite_number(state.get("overlap_seconds"), "saved overlap")
    if not math.isclose(overlap, 2.0, abs_tol=1e-9):
        raise ValueError("saved transcription overlap must be 2 seconds")
    saved_chunk_seconds = state.get("chunk_seconds")
    if isinstance(saved_chunk_seconds, bool) or not isinstance(saved_chunk_seconds, int) or saved_chunk_seconds <= 0:
        raise ValueError("saved chunk_seconds must be a positive integer")
    chunks = state.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("saved transcription chunks must be a list")
    if duration > 0 and not chunks:
        raise ValueError("positive-duration transcription state requires chunks")
    if duration == 0 and chunks:
        raise ValueError("zero-duration transcription state cannot contain chunks")
    identifiers: set[int] = set()
    previous_end = 0.0
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise ValueError("saved transcription chunk must be an object")
        identifier = chunk.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0 or identifier in identifiers:
            raise ValueError("saved transcription chunk IDs must be unique nonnegative integers")
        identifiers.add(identifier)
        if chunk.get("status") not in {"pending", "completed"}:
            raise ValueError("transcription chunk has an invalid status")
        owned_start = _finite_number(chunk.get("owned_start"), "owned_start")
        owned_end = _finite_number(chunk.get("owned_end"), "owned_end")
        extract_start = _finite_number(chunk.get("extract_start"), "extract_start")
        extract_end = _finite_number(chunk.get("extract_end"), "extract_end")
        if owned_start < 0 or owned_end <= owned_start or owned_end > duration:
            raise ValueError("transcription chunk ownership lies outside the video")
        if not math.isclose(owned_start, previous_end, abs_tol=1e-9):
            raise ValueError("transcription chunk ownership must be chronological and contiguous")
        expected_extract_start = max(0.0, owned_start - overlap)
        expected_extract_end = min(duration, owned_end + overlap)
        if (
            not math.isclose(extract_start, expected_extract_start, abs_tol=1e-9)
            or not math.isclose(extract_end, expected_extract_end, abs_tol=1e-9)
        ):
            raise ValueError("transcription chunk extraction geometry is invalid")
        previous_end = owned_end
    if not math.isclose(previous_end, duration, abs_tol=1e-9):
        raise ValueError("transcription chunks do not cover the complete duration")
    return chunks


def _validate_saved_response(
    payload: object, chunk: dict[str, Any], duration: float,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("chunk_id") != chunk.get("id"):
        raise ValueError("saved transcription response has the wrong chunk ID")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("saved transcription response must contain a segments list")
    owned_start = _finite_number(chunk.get("owned_start"), "owned_start")
    owned_end = _finite_number(chunk.get("owned_end"), "owned_end")
    extract_start = _finite_number(chunk.get("extract_start"), "extract_start")
    extract_end = _finite_number(chunk.get("extract_end"), "extract_end")
    previous_end = -math.inf
    for segment in segments:
        if not isinstance(segment, dict) or "start" not in segment or "end" not in segment:
            raise ValueError("saved transcript segment requires start and end")
        start = _finite_number(segment["start"], "saved segment start")
        end = _finite_number(segment["end"], "saved segment end")
        if (
            start < 0 or end < start or end > duration or start < previous_end
            or start < extract_start or end > extract_end
        ):
            raise ValueError("saved transcript segment timing is invalid")
        midpoint = (start + end) / 2.0
        if not owned_start <= midpoint < owned_end:
            raise ValueError("saved transcript segment does not belong to its ownership chunk")
        if not isinstance(segment.get("text"), str):
            raise TypeError("saved transcript segment text must be a string")
        previous_end = end
    return payload


def _load_saved_responses(
    workspace: Path, state: dict[str, Any], chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    duration = _finite_number(state.get("duration"), "saved duration")
    payloads: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.get("status") != "completed":
            continue
        identifier = chunk.get("id")
        response_file = _contained_path(
            workspace, ".work", "responses", f"chunk-{identifier:06d}.json",
        )
        if not response_file.is_file():
            raise ValueError("completed transcription chunk has no saved response")
        try:
            payload = json.loads(response_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("saved transcription response is unreadable") from exc
        payloads.append(_validate_saved_response(payload, chunk, duration))
    return payloads


def transcribe_video(
    video_path: Path,
    cwd: Path,
    language: str | None = None,
    chunk_seconds: int = 600,
    workers: int = 2,
    output_name: str | None = None,
) -> Path:
    """Create or resume a concurrent, progressively durable transcription job."""
    source = Path(video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError("source video does not exist")
    if isinstance(chunk_seconds, bool) or not isinstance(chunk_seconds, int) or chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be a positive integer")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("workers must be a positive integer")
    if language is not None and (not isinstance(language, str) or not language.strip()):
        raise ValueError("language must be a nonempty string when provided")

    if output_name is None:
        selected = resolve_workspace_for_source(source, cwd)
    else:
        selected = _named_workspace_for_legacy_compatibility(source, cwd, output_name)
    workspace = selected if selected.exists() else build_workspace(source, cwd, output_name)
    if (
        (workspace / "brief.md").is_file()
        and (workspace / "images").is_dir()
        and not (workspace / ".work").exists()
        and not (workspace / ".work").is_symlink()
    ):
        raise FileExistsError("completed output already exists")
    work = _contained_path(workspace, ".work")
    brief = _contained_path(workspace, "brief.md")
    work.mkdir(parents=True, exist_ok=True)
    if not brief.exists():
        _atomic_write(brief, _skeleton().encode("utf-8"))
    state_file = _contained_path(workspace, ".work", "state.json")
    media_tools: tuple[str, str] | None = None

    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("saved transcription state is unreadable") from exc
        if not isinstance(state, dict):
            raise ValueError("saved transcription state must be an object")
        saved_source = validate_resume_source(state)
        if saved_source != source:
            raise ValueError("saved transcription source path does not match this video")
        if state.get("language") != language:
            raise ValueError("saved transcription language does not match this run")
        if state.get("chunk_seconds") != chunk_seconds:
            raise ValueError("saved transcription chunk settings do not match this run")
    else:
        media_tools = require_media_tools()
        duration = probe_duration(source, media_tools[1])
        state = {
            "schema_version": 1,
            "source_path": str(source),
            "source_identity": source_identity(source),
            "duration": duration,
            "language": language,
            "chunk_seconds": chunk_seconds,
            "overlap_seconds": 2.0,
            "chunks": build_initial_chunks(duration, chunk_seconds, 2.0),
        }
        save_state(workspace, state)

    chunks = _validate_transcription_state(state)

    def artifact_path(kind: str, chunk: dict[str, Any]) -> Path:
        identifier = chunk.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
            raise ValueError("chunk IDs must be nonnegative integers")
        directory = "chunks" if kind == "audio" else "responses"
        suffix = ".flac" if kind == "audio" else ".json"
        return _contained_path(workspace, ".work", directory, f"chunk-{identifier:06d}{suffix}")

    payloads = _load_saved_responses(workspace, state, chunks)

    orphaned: list[tuple[dict[str, Any], dict[str, Any]]] = []
    saved_duration = _finite_number(state.get("duration"), "saved duration")
    for chunk in chunks:
        if chunk.get("status") != "pending":
            continue
        identifier = chunk.get("id")
        logical_response = work / "responses" / f"chunk-{identifier:06d}.json"
        if logical_response.is_symlink():
            raise ValueError("orphan transcription response cannot be a symbolic link")
        response_file = artifact_path("response", chunk)
        if not response_file.exists():
            continue
        if not response_file.is_file():
            raise ValueError("orphan transcription response is not a regular file")
        try:
            orphan_payload = json.loads(response_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("orphan transcription response is unreadable") from exc
        orphaned.append((chunk, _validate_saved_response(orphan_payload, chunk, saved_duration)))

    if orphaned:
        for chunk, _payload in orphaned:
            chunk["status"] = "completed"
        save_state(workspace, state)
        payloads.extend(payload for _chunk, payload in orphaned)
        update_transcript(brief, state, payloads)

    if chunks and all(chunk.get("status") == "completed" for chunk in chunks):
        update_transcript(brief, state, payloads)
        return workspace

    def ffmpeg_binary() -> str:
        nonlocal media_tools
        if media_tools is None:
            media_tools = require_media_tools()
        return media_tools[0]

    full_audio = _contained_path(workspace, ".work", "full-audio.flac")
    if not _valid_flac(full_audio):
        _publish_flac(full_audio, [
            ffmpeg_binary(), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "flac",
        ], "audio extraction")

    def ensure_chunk_audio(chunk: dict[str, Any]) -> Path:
        owned_start = _finite_number(chunk.get("owned_start"), "owned_start")
        owned_end = _finite_number(chunk.get("owned_end"), "owned_end")
        extract_start = _finite_number(chunk.get("extract_start"), "extract_start")
        extract_end = _finite_number(chunk.get("extract_end"), "extract_end")
        if owned_start > owned_end or extract_start < 0 or extract_start > owned_start or extract_end < owned_end:
            raise ValueError("transcription chunk has invalid ownership or extraction bounds")
        chunk_audio = artifact_path("audio", chunk)
        if not _valid_flac(chunk_audio):
            _publish_flac(chunk_audio, [
                ffmpeg_binary(), "-y", "-ss", f"{extract_start:.3f}", "-i", str(full_audio),
                "-t", f"{extract_end - extract_start:.3f}", "-c:a", "flac",
            ], "chunk extraction")
        return chunk_audio

    pending: list[dict[str, Any]] = []
    for chunk in chunks:
        if chunk.get("status") == "completed":
            continue
        if chunk.get("status") != "pending":
            raise ValueError("transcription chunk has an invalid status")
        ensure_chunk_audio(chunk)
        pending.append(chunk)

    if not pending:
        update_transcript(brief, state, payloads)
        return workspace

    client = build_transcription_client(Path(__file__).resolve().parent)
    first_error: Exception | None = None

    def split_chunk(parent: dict[str, Any]) -> list[dict[str, Any]]:
        owned_start = _finite_number(parent.get("owned_start"), "owned_start")
        owned_end = _finite_number(parent.get("owned_end"), "owned_end")
        if owned_end - owned_start < 120.0 - 1e-9:
            raise RuntimeError(
                "transcription chunk cannot be split into the minimum 60-second child ranges; recovery files remain in .work"
            )
        midpoint = (owned_start + owned_end) / 2.0
        if midpoint - owned_start < 60.0 - 1e-9 or owned_end - midpoint < 60.0 - 1e-9:
            raise RuntimeError(
                "transcription chunk cannot be split into the minimum 60-second child ranges; recovery files remain in .work"
            )
        identifiers = [chunk["id"] for chunk in chunks]
        first_id = max(identifiers, default=-1) + 1
        duration = _finite_number(state.get("duration"), "saved duration")
        children = [
            {
                "id": first_id,
                "owned_start": owned_start,
                "owned_end": midpoint,
                "extract_start": max(0.0, owned_start - 2.0),
                "extract_end": min(duration, midpoint + 2.0),
                "status": "pending",
            },
            {
                "id": first_id + 1,
                "owned_start": midpoint,
                "owned_end": owned_end,
                "extract_start": max(0.0, midpoint - 2.0),
                "extract_end": min(duration, owned_end + 2.0),
                "status": "pending",
            },
        ]
        try:
            parent_index = next(index for index, item in enumerate(chunks) if item is parent)
        except StopIteration as exc:
            raise RuntimeError("split parent is no longer present in transcription state") from exc
        chunks[parent_index:parent_index + 1] = children
        _validate_transcription_state(state)
        save_state(workspace, state)
        for child in children:
            ensure_chunk_audio(child)
        return children

    with ThreadPoolExecutor(max_workers=workers) as executor:
        queue = deque(pending)
        futures: dict[Any, dict[str, Any]] = {}

        def fill_workers() -> None:
            while queue and len(futures) < workers:
                next_chunk = queue.popleft()
                future = executor.submit(client.transcribe, artifact_path("audio", next_chunk), language)
                futures[future] = next_chunk

        fill_workers()
        while (futures or queue) and first_error is None:
            fill_workers()
            if not futures:
                break
            future = next(as_completed(tuple(futures)))
            chunk = futures.pop(future)
            try:
                raw_payload = future.result()
                try:
                    normalized = normalize_segments(
                        raw_payload,
                        _finite_number(chunk.get("extract_start"), "extract_start"),
                        _finite_number(chunk.get("owned_start"), "owned_start"),
                        _finite_number(chunk.get("owned_end"), "owned_end"),
                        extraction_end=_finite_number(chunk.get("extract_end"), "extract_end"),
                    )
                except (TypeError, ValueError):
                    raise ValueError(
                        "Timestamped segments are unavailable; "
                        "the coordinator refuses to invent timing data"
                    ) from None
                saved_payload = {"chunk_id": chunk["id"], "segments": normalized}
                _validate_saved_response(
                    saved_payload, chunk, _finite_number(state.get("duration"), "saved duration"),
                )
                _atomic_write(
                    artifact_path("response", chunk),
                    (json.dumps(saved_payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
                payloads = [item for item in payloads if item.get("chunk_id") != chunk["id"]]
                payloads.append(saved_payload)
                chunk["status"] = "completed"
                save_state(workspace, state)
                update_transcript(brief, state, payloads)
            except (AudioTooLargeError, TranscriptionTimeoutError, TimeoutError):
                try:
                    children = split_chunk(chunk)
                except Exception as exc:
                    first_error = exc
                    queue.clear()
                    for queued in futures:
                        queued.cancel()
                else:
                    for child in reversed(children):
                        queue.appendleft(child)
            except Exception as exc:
                first_error = exc
                queue.clear()
                for queued in futures:
                    queued.cancel()
    if first_error is not None:
        raise first_error
    return workspace


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _markdown_destinations(markdown: str) -> list[str]:
    destinations: list[str] = []
    for match in re.finditer(r"!?(?:\[[^\]]*\])\(([^)]+)\)", markdown):
        destination = match.group(1).strip()
        if destination.startswith("<") and ">" in destination:
            destination = destination[1:destination.index(">")].strip()
        else:
            destination = destination.split(maxsplit=1)[0]
        destinations.append(destination)
    for match in re.finditer(r"(?m)^[ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*(?:<([^>\r\n]+)>|(\S+))", markdown):
        destinations.append(match.group(1) or match.group(2))
    for match in re.finditer(r"<([A-Za-z][A-Za-z0-9+.-]*:[^ <>\r\n]*)>", markdown):
        destinations.append(match.group(1))
    email_autolink = re.compile(
        r"<([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)>"
    )
    for match in email_autolink.finditer(markdown):
        destinations.append("mailto:" + match.group(1))
    html_attribute = re.compile(
        r"(?is)\b(?:href|src)\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))"
    )
    for match in html_attribute.finditer(markdown):
        destinations.append(match.group(2) if match.group(1) else match.group(3))
    srcset_attribute = re.compile(
        r"(?is)\bsrcset\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))"
    )
    for match in srcset_attribute.finditer(markdown):
        srcset = match.group(2) if match.group(1) else match.group(3)
        for candidate in srcset.split(","):
            fields = candidate.strip().split()
            if fields:
                destinations.append(fields[0])
    return destinations


def _validate_markdown_destinations(markdown: str) -> None:
    """Reject link destinations that can address data outside the output job."""
    for destination in _markdown_destinations(markdown):
        normalized = destination.replace("\\", "/")
        decoded = urlparse.unquote(html_lib.unescape(normalized)).replace("\\", "/")
        if not normalized:
            raise ValueError("Markdown link destination cannot be empty")
        if decoded.startswith(("/", "//")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", decoded):
            raise ValueError("Markdown links must be relative")
        if ".." in decoded.split("/"):
            raise ValueError("Markdown links cannot escape the output directory")
        if decoded.startswith("#"):
            continue
        if not decoded.startswith("images/") or decoded == "images/":
            raise ValueError("durable Markdown file links must be contained under images/")


def validate_plan(plan: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Validate a version-1 finalization plan without mutating it."""
    if not isinstance(plan, dict) or not isinstance(state, dict):
        raise TypeError("plan and state must be objects")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("plan schema_version must be 1")
    title = _required_text(plan.get("title"), "plan title")
    summary = _required_text(plan.get("summary"), "plan summary")
    _validate_markdown_destinations(title)
    _validate_markdown_destinations(summary)
    if "source_identity" not in plan or plan["source_identity"] != state.get("source_identity"):
        raise ValueError("plan source_identity does not match the transcription state")

    chunks = state.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError("state chunks must be a list")
    completed_ids: list[object] = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or "id" not in chunk:
            raise ValueError("every state chunk must have an id")
        if chunk.get("status") != "completed":
            raise ValueError("all transcription chunks must be completed before finalization")
        completed_ids.append(chunk["id"])
    try:
        if len(set(completed_ids)) != len(completed_ids):
            raise ValueError("completed chunk IDs must be unique")
    except TypeError as exc:
        raise ValueError("chunk IDs must be hashable") from exc

    reviewed = plan.get("reviewed_chunk_ids")
    if not isinstance(reviewed, list):
        raise ValueError("reviewed_chunk_ids must be a list")
    try:
        reviewed_set = set(reviewed)
    except TypeError as exc:
        raise ValueError("reviewed chunk IDs must be hashable") from exc
    if len(reviewed_set) != len(reviewed) or reviewed_set != set(completed_ids):
        raise ValueError("reviewed_chunk_ids must uniquely cover every completed chunk")

    duration = _finite_number(state.get("duration"), "video duration")
    if duration < 0:
        raise ValueError("video duration cannot be negative")
    instructions = plan.get("instructions")
    if not isinstance(instructions, list):
        raise ValueError("instructions must be a list")

    previous_range_end = -math.inf
    roles = {"reference", "before", "after", "step"}
    for instruction_index, instruction in enumerate(instructions):
        if not isinstance(instruction, dict):
            raise ValueError("each instruction must be an object")
        heading = _required_text(instruction.get("heading"), f"instruction {instruction_index} heading")
        _validate_markdown_destinations(heading)
        body = _required_text(instruction.get("body_markdown"), f"instruction {instruction_index} body_markdown")
        _validate_markdown_destinations(body)

        ranges = instruction.get("source_ranges")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("each instruction requires at least one source range")
        for source_range in ranges:
            if not isinstance(source_range, dict) or "start" not in source_range or "end" not in source_range:
                raise ValueError("source ranges require start and end")
            start = _finite_number(source_range["start"], "source range start")
            end = _finite_number(source_range["end"], "source range end")
            if start < 0 or end < start or end > duration:
                raise ValueError("source range lies outside the video or runs backwards")
            if start < previous_range_end:
                raise ValueError("source ranges must be globally chronological and non-overlapping")
            previous_range_end = end

        visuals = instruction.get("visuals")
        if not isinstance(visuals, list) or len(visuals) > 3:
            raise ValueError("instruction visuals must be a list of at most three items")
        previous_timestamp = -math.inf
        for visual in visuals:
            if not isinstance(visual, dict):
                raise ValueError("each visual must be an object")
            if "timestamp" not in visual:
                raise ValueError("visual timestamp is required")
            timestamp = _finite_number(visual["timestamp"], "visual timestamp")
            if timestamp < 0 or timestamp > duration:
                raise ValueError("visual timestamp lies outside the video")
            if timestamp < previous_timestamp:
                raise ValueError("visuals must be chronological within an instruction")
            previous_timestamp = timestamp
            if visual.get("role") not in roles:
                raise ValueError("visual role is invalid")
            alt = _required_text(visual.get("alt"), "visual alt")
            caption = _required_text(visual.get("caption"), "visual caption")
            _validate_markdown_destinations(alt)
            _validate_markdown_destinations(caption)
    return plan


def _contained_path(root: Path, *parts: str) -> Path:
    resolved_root = Path(root).resolve()
    result = resolved_root.joinpath(*parts).resolve()
    if not result.is_relative_to(resolved_root):
        raise ValueError("generated path escapes the job workspace")
    return result


def _escape_drawtext_path(path: Path) -> str:
    text = path.resolve().as_posix()
    escaped: list[str] = []
    for character in text:
        if character in "\\':,;[]":
            escaped.append("\\")
        escaped.append(character)
    return "".join(escaped)


def _find_contact_sheet_font() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("VIDEO_INPUT_FONT", "").strip()
    if configured:
        candidates.append(Path(configured))
    windows_root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windows_root:
        candidates.extend([
            Path(windows_root) / "Fonts" / "arial.ttf",
            Path(windows_root) / "Fonts" / "segoeui.ttf",
        ])
    candidates.extend([
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    font_match = shutil.which("fc-match")
    if font_match:
        completed = subprocess.run(
            [font_match, "-f", "%{file}", "sans-serif"],
            check=False, text=True, capture_output=True,
        )
        matched = Path(completed.stdout.strip())
        if completed.returncode == 0 and matched.is_file():
            return matched.resolve()
    raise RuntimeError("a system TrueType font is required to label contact sheets")


def _contact_sheet_font() -> str:
    return _escape_drawtext_path(_find_contact_sheet_font())


def _prepared_contact_sheet_font() -> tuple[str, Path | None]:
    source = _find_contact_sheet_font()
    if not any(character in source.as_posix() for character in "',;[]"):
        return _escape_drawtext_path(source), None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="video-input-font-", suffix=source.suffix or ".ttf", delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(source, temporary)
        return _escape_drawtext_path(temporary), temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def contact_sheet(video: Path, workspace: Path, chunk_id: int, start: float, duration: int) -> Path:
    """Render a temporary, labeled 1-fps contact sheet for a bounded window."""
    if isinstance(chunk_id, bool) or not isinstance(chunk_id, int) or chunk_id < 0:
        raise ValueError("contact-sheet id must be a nonnegative integer")
    start_value = _finite_number(start, "contact-sheet start")
    if start_value < 0:
        raise ValueError("contact-sheet start cannot be negative")
    if isinstance(duration, bool) or duration not in {5, 10}:
        raise ValueError("contact-sheet duration must be 5 or 10 seconds")
    source = Path(video).resolve()
    if not source.is_file():
        raise FileNotFoundError("source video does not exist")
    root = Path(workspace).resolve()
    sheet_dir = _contained_path(root, ".work", "contact-sheets")
    sheet_dir.mkdir(parents=True, exist_ok=True)
    start_label = format_timestamp(start_value).replace(":", "-").replace(".", "-")
    output = _contained_path(sheet_dir, f"{chunk_id:06d}-{start_label}-{duration}s.png")
    ffmpeg, ffprobe = require_media_tools()
    video_duration = probe_duration(source, ffprobe)
    if video_duration <= 0 or start_value >= video_duration:
        raise ValueError("contact-sheet start must be before the end of the video")
    window_duration = min(float(duration), video_duration - start_value)
    columns = 5
    rows = 1 if duration == 5 else 2
    timestamp_label = f"%{{pts\\:hms\\:{start_value:.3f}}}"
    font_value, temporary_font = _prepared_contact_sheet_font()
    label_filter = f"drawtext=fontfile='{font_value}':text='{timestamp_label}':x=8:y=8:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.65"
    video_filter = f"fps=1,{label_filter},scale=384:-2:force_original_aspect_ratio=decrease,tile={columns}x{rows}"
    output.unlink(missing_ok=True)
    try:
        run_command([
            ffmpeg, "-y", "-ss", f"{start_value:.3f}", "-i", str(source), "-t", f"{window_duration:.3f}",
            "-vf", video_filter, "-frames:v", "1", "-update", "1", str(output),
        ])
    finally:
        if temporary_font is not None:
            temporary_font.unlink(missing_ok=True)
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("FFmpeg did not create a nonempty contact sheet")
    return output


def _valid_webp(path: Path) -> bool:
    if not path.is_file():
        return False
    data = path.read_bytes()
    if len(data) < 22 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size + 8 != len(data):
        return False
    if data[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}:
        return False
    chunk_size = int.from_bytes(data[16:20], "little")
    if chunk_size <= 0:
        return False
    padded_end = 20 + chunk_size + (chunk_size % 2)
    return padded_end <= len(data)


def _visual_filename(role: str, counts: dict[str, int]) -> str:
    count = counts.get(role, 0) + 1
    counts[role] = count
    return f"{role}.webp" if count == 1 else f"{role}-{count}.webp"


def _markdown_transcript(brief: Path) -> str:
    if not brief.is_file():
        return _TRANSCRIPT_HEADING + "\n"
    content = brief.read_text(encoding="utf-8")
    if _TRANSCRIPT_HEADING not in content:
        raise ValueError("progressive brief has no complete transcript region")
    return content[content.index(_TRANSCRIPT_HEADING):].rstrip() + "\n"


_PUBLISH_ARTIFACTS = (
    ("brief.md", "file"),
    ("images", "directory"),
    ("state.json", "file"),
    ("plan.json", "file"),
)
_PUBLISH_INTENT_NAME = ".publish-intent.json"
_PUBLISH_INTENT_TEMP_NAME = ".publish-intent.json.tmp"


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _matches_artifact_kind(path: Path, kind: str) -> bool:
    if path.is_symlink():
        return False
    return path.is_file() if kind == "file" else path.is_dir()


def _publish_journal_file(backup: Path) -> Path:
    return _contained_path(backup, "journal.json")


def _write_publish_journal(backup: Path, journal: dict[str, Any]) -> None:
    payload = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(_publish_journal_file(backup), payload)


def _write_initial_publish_intent(work: Path, journal: dict[str, Any]) -> None:
    """Durably stage the first journal outside its not-yet-created backup directory."""
    destination = _contained_path(work, _PUBLISH_INTENT_NAME)
    temporary = _contained_path(work, _PUBLISH_INTENT_TEMP_NAME)
    if _path_exists(destination) or _path_exists(temporary):
        raise RuntimeError("publish initialization intent already exists")
    payload = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _create_publish_backup(backup: Path) -> None:
    backup.mkdir(parents=True)


def _unique_quarantine_path(root: Path, label: str) -> Path:
    for attempt in range(100):
        candidate = root.parent / f".{root.name}-{label}-{os.getpid()}-{time.time_ns()}-{attempt}"
        if not _path_exists(candidate):
            return candidate
    raise RuntimeError("could not allocate a same-volume cleanup quarantine")


def _best_effort_remove_quarantine(path: Path) -> None:
    for attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            if not _path_exists(path):
                return
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
        except OSError:
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    print(f"warning: cleanup quarantine remains at {path}", file=sys.stderr)


def _load_publish_journal(root: Path, backup: Path) -> dict[str, Any]:
    if backup.is_symlink() or not backup.is_dir():
        raise RuntimeError("interrupted publish backup is not a safe directory")
    journal_file = _publish_journal_file(backup)
    if journal_file.is_symlink() or not journal_file.is_file():
        raise RuntimeError("interrupted publish backup has no valid journal")
    try:
        journal = json.loads(journal_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("interrupted publish journal is unreadable") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        raise RuntimeError("interrupted publish journal has an unsupported schema")
    if journal.get("phase") not in {"backing_up", "publishing", "recovering"}:
        raise RuntimeError("interrupted publish journal has an invalid phase")
    entries = journal.get("entries")
    if not isinstance(entries, list) or len(entries) != len(_PUBLISH_ARTIFACTS):
        raise RuntimeError("interrupted publish journal has an invalid artifact manifest")
    expected = {name: kind for name, kind in _PUBLISH_ARTIFACTS}
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "kind", "existed", "status"}:
            raise RuntimeError("interrupted publish journal contains a malformed artifact entry")
        name = entry.get("name")
        kind = entry.get("kind")
        existed = entry.get("existed")
        status = entry.get("status")
        if name not in expected or name in seen or kind != expected[name] or type(existed) is not bool:
            raise RuntimeError("interrupted publish journal artifact metadata is inconsistent")
        if existed and status not in {"pending", "moving", "backed_up"}:
            raise RuntimeError("interrupted publish journal has an invalid backup status")
        if not existed and status != "absent":
            raise RuntimeError("interrupted publish journal has an invalid absent status")
        seen.add(name)
    generated = journal.get("generated_names", [])
    if not isinstance(generated, list) or len(generated) != len(set(generated)):
        raise RuntimeError("interrupted publish journal has invalid generated artifacts")
    if any(name not in {"brief.md", "images"} for name in generated):
        raise RuntimeError("interrupted publish journal names an unsafe generated artifact")
    allowed_backup = {"journal.json", "generated", *(entry["name"] for entry in entries if entry["existed"])}
    unexpected_backup = {path.name for path in backup.iterdir()} - allowed_backup
    if unexpected_backup:
        raise RuntimeError("interrupted publish backup contains unjournaled artifacts")
    return journal


def _recover_interrupted_publish(root: Path, work: Path, backup: Path) -> None:
    """Restore prior root artifacts from an atomically journaled interrupted publish."""
    journal = _load_publish_journal(root, backup)
    entries = journal["entries"]
    phase = journal["phase"]

    if phase != "recovering":
        for entry in entries:
            name, kind = entry["name"], entry["kind"]
            prior = _contained_path(root, name)
            saved = _contained_path(backup, name)
            prior_exists = _path_exists(prior)
            saved_exists = _path_exists(saved)
            if not entry["existed"]:
                if saved_exists or (phase == "backing_up" and prior_exists):
                    raise RuntimeError("interrupted publish backup is inconsistent with its journal")
                if prior_exists and (name not in {"brief.md", "images"} or not _matches_artifact_kind(prior, kind)):
                    raise RuntimeError("interrupted publish created an unsafe root artifact")
                continue
            status = entry["status"]
            if phase == "publishing":
                if status != "backed_up" or not saved_exists or not _matches_artifact_kind(saved, kind):
                    raise RuntimeError("interrupted publish is missing a journaled prior artifact")
                if prior_exists and (name not in {"brief.md", "images"} or not _matches_artifact_kind(prior, kind)):
                    raise RuntimeError("interrupted publish created an unsafe root artifact")
            elif status == "pending":
                if saved_exists or not prior_exists or not _matches_artifact_kind(prior, kind):
                    raise RuntimeError("interrupted publish pending artifact is inconsistent")
            elif status == "moving":
                if prior_exists == saved_exists:
                    raise RuntimeError("interrupted publish moving artifact is inconsistent")
                present = prior if prior_exists else saved
                if not _matches_artifact_kind(present, kind):
                    raise RuntimeError("interrupted publish moving artifact has the wrong type")
            elif status == "backed_up":
                if prior_exists or not saved_exists or not _matches_artifact_kind(saved, kind):
                    raise RuntimeError("interrupted publish backup artifact is inconsistent")

        generated_names = [
            name for name in ("brief.md", "images")
            if phase == "publishing" and _path_exists(_contained_path(root, name))
        ]
        journal["phase"] = "recovering"
        journal["generated_names"] = generated_names
        _write_publish_journal(backup, journal)

    generated_names = journal.get("generated_names", [])
    generated_dir = _contained_path(backup, "generated")
    if generated_dir.is_symlink():
        raise RuntimeError("interrupted publish generated quarantine is unsafe")
    if generated_names:
        generated_dir.mkdir(exist_ok=True)
    elif _path_exists(generated_dir):
        raise RuntimeError("interrupted publish has an unjournaled generated quarantine")

    for name in generated_names:
        root_artifact = _contained_path(root, name)
        quarantined = _contained_path(generated_dir, name)
        root_exists = _path_exists(root_artifact)
        quarantined_exists = _path_exists(quarantined)
        if not quarantined_exists:
            if not root_exists:
                raise RuntimeError("interrupted generated artifact is missing during recovery")
            os.replace(root_artifact, quarantined)
        elif root_exists:
            saved = _contained_path(backup, name)
            if _path_exists(saved):
                raise RuntimeError("interrupted recovery has ambiguous generated and prior artifacts")

    for entry in entries:
        name, kind = entry["name"], entry["kind"]
        prior = _contained_path(root, name)
        saved = _contained_path(backup, name)
        prior_exists = _path_exists(prior)
        saved_exists = _path_exists(saved)
        if entry["existed"]:
            if saved_exists:
                if prior_exists:
                    raise RuntimeError("interrupted recovery would overwrite an existing root artifact")
                if not _matches_artifact_kind(saved, kind):
                    raise RuntimeError("journaled prior artifact has the wrong type")
                os.replace(saved, prior)
            elif not prior_exists or not _matches_artifact_kind(prior, kind):
                raise RuntimeError("journaled prior artifact cannot be restored")
        elif prior_exists:
            raise RuntimeError("interrupted recovery left an artifact that did not previously exist")

    for entry in entries:
        prior = _contained_path(root, entry["name"])
        if entry["existed"] != _path_exists(prior):
            raise RuntimeError("interrupted publish recovery did not restore the original inventory")
        if entry["existed"] and not _matches_artifact_kind(prior, entry["kind"]):
            raise RuntimeError("interrupted publish recovery restored the wrong artifact type")

    detached = _unique_quarantine_path(root, "publish-rollback")
    os.replace(backup, detached)
    _best_effort_remove_quarantine(detached)


def _prejournal_state_intact(root: Path, candidate: Path) -> bool:
    if candidate.is_symlink() or not candidate.is_dir():
        return False
    root_brief = root / "brief.md"
    if root_brief.is_symlink() or not root_brief.is_file():
        return False
    try:
        _markdown_transcript(root_brief)
    except (OSError, ValueError):
        return False
    candidate_brief = candidate / "brief.md"
    candidate_images = candidate / "images"
    if (
        {entry.name for entry in candidate.iterdir()} != {"brief.md", "images"}
        or candidate_brief.is_symlink() or not candidate_brief.is_file()
        or candidate_images.is_symlink() or not candidate_images.is_dir()
    ):
        return False
    if {entry.name for entry in root.iterdir()} - {"brief.md", "images", ".work", "state.json", "plan.json"}:
        return False
    for name, kind in _PUBLISH_ARTIFACTS:
        artifact = root / name
        if _path_exists(artifact) and not _matches_artifact_kind(artifact, kind):
            return False
    return True


def _clear_empty_prejournal_backup(root: Path, candidate: Path, backup: Path) -> bool:
    """Clear only the exact no-move crash window between backup mkdir and journal commit."""
    if backup.is_symlink() or not backup.is_dir():
        return False
    try:
        if any(backup.iterdir()):
            return False
    except OSError:
        return False
    if not _prejournal_state_intact(root, candidate):
        return False
    try:
        backup.rmdir()
    except OSError:
        return False
    return True


def _load_initial_publish_intent(intent: Path, root: Path) -> dict[str, Any]:
    if intent.is_symlink() or not intent.is_file():
        raise RuntimeError("publish initialization intent is not a safe file")
    try:
        journal = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("publish initialization intent is unreadable") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("phase") != "backing_up"
        or journal.get("generated_names") != []
    ):
        raise RuntimeError("publish initialization intent is invalid")
    entries = journal.get("entries")
    expected = {name: kind for name, kind in _PUBLISH_ARTIFACTS}
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise RuntimeError("publish initialization intent has an invalid artifact manifest")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "kind", "existed", "status"}:
            raise RuntimeError("publish initialization intent contains a malformed artifact entry")
        name = entry.get("name")
        existed = entry.get("existed")
        if (
            name not in expected or name in seen or entry.get("kind") != expected[name]
            or type(existed) is not bool
            or entry.get("status") != ("pending" if existed else "absent")
        ):
            raise RuntimeError("publish initialization intent artifact metadata is inconsistent")
        artifact = root / name
        if existed != _path_exists(artifact):
            raise RuntimeError("publish initialization intent does not match the root inventory")
        if existed and not _matches_artifact_kind(artifact, expected[name]):
            raise RuntimeError("publish initialization root artifact has the wrong type")
        seen.add(name)
    return journal


def _recover_publish_initialization(root: Path, work: Path, candidate: Path, backup: Path) -> None:
    intent = _contained_path(work, _PUBLISH_INTENT_NAME)
    temporary = _contained_path(work, _PUBLISH_INTENT_TEMP_NAME)
    intent_exists = _path_exists(intent)
    temporary_exists = _path_exists(temporary)
    backup_exists = _path_exists(backup)

    if temporary_exists:
        if (
            temporary.is_symlink() or not temporary.is_file() or intent_exists or backup_exists
            or not _prejournal_state_intact(root, candidate)
        ):
            raise RuntimeError("publish initialization temp is inconsistent; recovery was retained")
        temporary.unlink()
        return

    if intent_exists:
        if not _prejournal_state_intact(root, candidate):
            raise RuntimeError("publish initialization intent does not have an intact pre-move state")
        _load_initial_publish_intent(intent, root)
        if backup_exists:
            if not _clear_empty_prejournal_backup(root, candidate, backup):
                raise RuntimeError("publish initialization backup contains unknown content")
        intent.unlink()
        return

    if backup_exists:
        _clear_empty_prejournal_backup(root, candidate, backup)


def finalize_plan(workspace: Path, plan: dict[str, Any], state: dict[str, Any]) -> Path:
    """Stage and verify the complete candidate before reversible publication and cleanup."""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise FileNotFoundError("job workspace does not exist")
    work = _contained_path(root, ".work")
    work.mkdir(parents=True, exist_ok=True)
    brief = _contained_path(root, "brief.md")
    images = _contained_path(root, "images")

    candidate_path = work / "final-candidate"
    backup_path = work / "publish-backup"
    if candidate_path.is_symlink() or backup_path.is_symlink():
        raise ValueError("finalization staging paths cannot be symbolic links")
    candidate = _contained_path(work, "final-candidate")
    backup = _contained_path(work, "publish-backup")
    _recover_publish_initialization(root, work, candidate, backup)
    if _path_exists(backup):
        _recover_interrupted_publish(root, work, backup)

    validate_plan(plan, state)
    source = validate_resume_source(state)
    transcript = _markdown_transcript(brief)
    if candidate.exists():
        shutil.rmtree(candidate)
    if _path_exists(backup):
        raise RuntimeError("interrupted publish backup was not safely recovered")
    if _path_exists(work / _PUBLISH_INTENT_NAME) or _path_exists(work / _PUBLISH_INTENT_TEMP_NAME):
        raise RuntimeError("publish initialization intent was not safely recovered")
    candidate_images = _contained_path(candidate, "images")
    candidate_images.mkdir(parents=True)
    candidate_brief = _contained_path(candidate, "brief.md")

    has_visuals = any(instruction["visuals"] for instruction in plan["instructions"])
    ffmpeg = require_media_tools()[0] if has_visuals else "ffmpeg"
    counts: dict[str, int] = {}
    rendered_instructions: list[str] = []
    referenced_images: list[str] = []
    for instruction in plan["instructions"]:
        range_text = ", ".join(
            f"{format_timestamp(source_range['start'])}–{format_timestamp(source_range['end'])}"
            for source_range in instruction["source_ranges"]
        )
        parts = [f"### {instruction['heading']}", "", f"Source: `{range_text}`", "", instruction["body_markdown"].strip()]
        for visual in instruction["visuals"]:
            filename = _visual_filename(visual["role"], counts)
            staged = _contained_path(candidate_images, filename)
            timestamp = _finite_number(visual["timestamp"], "visual timestamp")
            run_command([
                ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(source), "-frames:v", "1",
                "-vf", "scale=w='min(1920,iw)':h=-2", "-c:v", "libwebp", "-quality", "82", str(staged),
            ])
            if not _valid_webp(staged):
                raise RuntimeError(f"FFmpeg did not create a valid WebP frame: {filename}")
            relative = f"images/{filename}"
            referenced_images.append(relative)
            parts.extend([
                "", f"![{visual['alt']}]({relative})", "",
                f"*{visual['caption']} — {format_timestamp(timestamp)}*",
            ])
        rendered_instructions.append("\n".join(parts).rstrip())

    actionable = "\n\n".join(rendered_instructions) if rendered_instructions else "No actionable instructions were identified."
    rendered = (
        f"# {plan['title'].strip()}\n\n{plan['summary'].strip()}\n\n"
        f"{_INSTRUCTIONS_HEADING}\n\n{actionable}\n\n{transcript}"
    )
    _atomic_write(candidate_brief, rendered.encode("utf-8"))

    _validate_markdown_destinations(rendered)
    for destination in _markdown_destinations(rendered):
        normalized = urlparse.unquote(html_lib.unescape(destination.replace("\\", "/"))).replace("\\", "/")
        if normalized.startswith("#"):
            continue
        link_path = _contained_path(candidate, *normalized.split("/"))
        if not link_path.is_file():
            raise RuntimeError(f"final Markdown link does not resolve in the staged candidate: {destination}")
    staged_files = sorted(path for path in candidate_images.rglob("*") if path.is_file())
    if len(staged_files) != len(referenced_images) or not all(_valid_webp(path) for path in staged_files):
        raise RuntimeError("staged final image inventory does not match the plan")
    if sum(len(item["visuals"]) for item in plan["instructions"]) != len(referenced_images):
        raise RuntimeError("final image count does not match the plan")
    if {entry.name for entry in candidate.iterdir()} != {"brief.md", "images"}:
        raise RuntimeError("staged final output must contain only brief.md and images")
    validate_plan(plan, state)
    validate_resume_source(state)

    allowed_top_level = {"brief.md", "images", ".work", "state.json", "plan.json"}
    unexpected = {entry.name for entry in root.iterdir()} - allowed_top_level
    if unexpected:
        raise RuntimeError("unexpected durable output entries: " + ", ".join(sorted(unexpected)))
    legacy_paths = [root / "state.json", root / "plan.json"]
    for legacy in legacy_paths:
        if legacy.exists() and not legacy.is_file():
            raise RuntimeError(f"legacy state path is not a file: {legacy.name}")

    entries: list[dict[str, Any]] = []
    for name, kind in _PUBLISH_ARTIFACTS:
        prior = _contained_path(root, name)
        existed = _path_exists(prior)
        if existed and not _matches_artifact_kind(prior, kind):
            raise RuntimeError(f"prior output artifact has the wrong type: {name}")
        entries.append({
            "name": name,
            "kind": kind,
            "existed": existed,
            "status": "pending" if existed else "absent",
        })
    journal: dict[str, Any] = {
        "schema_version": 1,
        "phase": "backing_up",
        "generated_names": [],
        "entries": entries,
    }
    _write_initial_publish_intent(work, journal)
    _create_publish_backup(backup)
    os.replace(_contained_path(work, _PUBLISH_INTENT_NAME), _publish_journal_file(backup))

    try:
        for entry in entries:
            if not entry["existed"]:
                continue
            entry["status"] = "moving"
            _write_publish_journal(backup, journal)
            os.replace(_contained_path(root, entry["name"]), _contained_path(backup, entry["name"]))
            entry["status"] = "backed_up"
            _write_publish_journal(backup, journal)
        journal["phase"] = "publishing"
        _write_publish_journal(backup, journal)
        os.replace(candidate_images, images)
        os.replace(candidate_brief, brief)

        if brief.read_text(encoding="utf-8") != rendered:
            raise RuntimeError("published brief does not match the staged candidate")
        for relative in referenced_images:
            destination = _contained_path(root, *relative.split("/"))
            if not _valid_webp(destination) or f"]({relative})" not in rendered:
                raise RuntimeError(f"published image is missing or invalid: {relative}")
        for destination in _markdown_destinations(rendered):
            normalized = urlparse.unquote(html_lib.unescape(destination.replace("\\", "/"))).replace("\\", "/")
            if normalized.startswith("#"):
                continue
            if not _contained_path(root, *normalized.split("/")).is_file():
                raise RuntimeError(f"published Markdown link does not resolve: {destination}")
        if {entry.name for entry in root.iterdir()} != {"brief.md", "images", ".work"}:
            raise RuntimeError("published output has an unexpected pre-cleanup inventory")
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            _recover_interrupted_publish(root, work, backup)
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        detail = "; rollback encountered errors" if rollback_errors else ""
        raise RuntimeError("failed to publish final output; prior artifacts were restored" + detail) from exc

    try:
        cleanup_quarantine = _unique_quarantine_path(root, "cleanup")
        os.replace(work, cleanup_quarantine)
    except OSError as exc:
        raise RuntimeError("final output is verified, but .work could not be atomically detached") from exc
    if {entry.name for entry in root.iterdir()} != {"brief.md", "images"}:
        try:
            os.replace(cleanup_quarantine, work)
        except OSError as restore_exc:
            raise RuntimeError("post-commit inventory failed and detached recovery could not be restored") from restore_exc
        raise RuntimeError("post-commit output inventory is not exact")
    _best_effort_remove_quarantine(cleanup_quarantine)
    return root


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _nonnegative_timestamp(value: str) -> float:
    try:
        return parse_timestamp(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _resolve_plan_workspace(plan_argument: Path, cwd: Path) -> tuple[Path, Path | None]:
    raw = Path(plan_argument)
    lexical = (raw if raw.is_absolute() else Path(cwd) / raw).absolute()
    resolved_plan = lexical.resolve()
    work_ancestor = next((parent for parent in lexical.parents if parent.name == ".work"), None)
    if work_ancestor is None:
        return resolved_plan, None
    resolved_work = work_ancestor.resolve()
    workspace = resolved_work.parent
    if (
        workspace.parent not in _workspace_roots(cwd)
        or resolved_work != workspace / ".work"
        or not resolved_plan.is_relative_to(resolved_work)
    ):
        raise ValueError("plan .work location is outside a contained video-input job")
    return resolved_plan, workspace


def _load_matching_job_state(workspace: Path, video_path: Path, cwd: Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    if root.parent not in _workspace_roots(cwd):
        raise ValueError("selected workspace is outside the invocation root")
    work_path = root / ".work"
    if work_path.is_symlink():
        raise ValueError("job .work directory cannot be a symbolic link")
    state_file = _contained_path(root, ".work", "state.json")
    if state_file.is_symlink() or not state_file.is_file():
        raise FileNotFoundError("transcription state is missing")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("transcription state is unreadable") from exc
    if not isinstance(state, dict):
        raise ValueError("transcription state must be an object")
    source = Path(video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError("source video does not exist")
    if (
        state.get("source_path") != str(source)
        or state.get("source_identity") != source_identity(source)
    ):
        raise ValueError("selected video does not match the transcription job source")
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video_input.py", description="Convert instruction videos into illustrated Markdown briefs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    transcribe = subparsers.add_parser("transcribe")
    transcribe.add_argument("video", type=Path)
    transcribe.add_argument("--language")
    transcribe.add_argument("--chunk-seconds", type=_positive_integer, default=600)
    transcribe.add_argument("--workers", type=_positive_integer, default=2)
    transcribe.add_argument("--output-name", type=safe_name)

    status = subparsers.add_parser("status")
    status.add_argument("video", type=Path)

    sheet = subparsers.add_parser("contact-sheet")
    sheet.add_argument("video", type=Path)
    sheet.add_argument("--id", dest="chunk_id", type=_nonnegative_integer, required=True)
    sheet.add_argument("--start", type=_nonnegative_timestamp, required=True)
    sheet.add_argument("--duration", type=int, choices=(5, 10), required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("video", type=Path)
    finalize.add_argument("--plan", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = Path.cwd()
    if args.command == "transcribe":
        transcribe_video(
            args.video, cwd, language=args.language,
            chunk_seconds=args.chunk_seconds, workers=args.workers,
            output_name=args.output_name,
        )
        return 0
    if args.command == "status":
        print(json.dumps(status_video(args.video, cwd), sort_keys=True))
        return 0
    if args.command == "contact-sheet":
        workspace = resolve_workspace_for_source(args.video, cwd)
        if workspace == _default_workspace_path(args.video, cwd):
            workspace = build_workspace(args.video, cwd)
        contact_sheet(args.video, workspace, chunk_id=args.chunk_id, start=args.start, duration=args.duration)
        return 0
    if args.command == "finalize":
        plan_path, workspace = _resolve_plan_workspace(args.plan, cwd)
        if workspace is None:
            workspace = resolve_workspace_for_source(args.video, cwd)
        state = _load_matching_job_state(workspace, args.video, cwd)
        if not plan_path.is_file():
            raise FileNotFoundError("finalization plan is missing")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        finalize_plan(workspace, plan, state)
        return 0
    raise RuntimeError("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
