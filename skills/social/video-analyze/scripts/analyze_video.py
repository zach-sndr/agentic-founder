#!/usr/bin/env python3
"""Analyze a video with Gemini, deterministic media preflight, and recovery.

The public entrypoint is intentionally small. `video_analysis_core` owns all
deterministic local behavior so this command can be tested without uploading a
video. The source video stays untouched; upload derivatives are retained only
when a requested downscale or API format/codec fallback needs one.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import video_analysis_core as core


FLASH_LITE_MODEL = "gemini-3.1-flash-lite"
FLASH_MODEL = "gemini-3-flash-preview"
HIGH_MODEL = "gemini-3.5-flash"
# Retained as a compatibility alias for callers that mean the video-only default.
DEFAULT_MODEL = FLASH_LITE_MODEL
UPLOAD_QUALITY_TARGET_HEIGHTS: dict[str, int | None] = {
    "low": 480,
    "medium": 720,
    "high": None,
}
PROCESSING_TIMEOUT_SECONDS = 600
PROCESSING_POLL_SECONDS = 5


def info(message: str) -> None:
    """Write operational messages to stderr so JSON stdout remains pipeable."""
    print(f"[video-analyze] {message}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the supported, explicit analysis interface."""
    parser = argparse.ArgumentParser(
        description="Analyze a video with Gemini and verify coverage against ffprobe metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video_file", help="Local source video to inspect")
    parser.add_argument(
        "--prompt",
        help="Focused analysis task, for example: 'Find every big green frog and give timestamps.'",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="With --prompt, also generate the comprehensive full JSON report.",
    )
    model_override = parser.add_mutually_exclusive_group()
    model_override.add_argument(
        "--fast",
        action="store_true",
        help=f"Force {FLASH_LITE_MODEL} for any analysis mode.",
    )
    model_override.add_argument(
        "--high",
        action="store_true",
        help=f"Force {HIGH_MODEL} for any analysis mode.",
    )
    upload_quality = parser.add_mutually_exclusive_group()
    upload_quality.add_argument(
        "-lq",
        "--lq",
        dest="upload_quality",
        action="store_const",
        const="low",
        help="Downscale the upload to at most 480p; never upscale.",
    )
    upload_quality.add_argument(
        "-mq",
        "--mq",
        dest="upload_quality",
        action="store_const",
        const="medium",
        help="Downscale the upload to at most 720p; this is the default.",
    )
    upload_quality.add_argument(
        "-hq",
        "--hq",
        dest="upload_quality",
        action="store_const",
        const="high",
        help="Upload the original resolution without a quality downscale.",
    )
    parser.set_defaults(upload_quality="medium")
    parser.add_argument(
        "--output",
        choices=("console", "json", "file"),
        default="console",
        help="Where to render the primary result; artifacts are always saved.",
    )
    parser.add_argument(
        "--format",
        choices=("pretty", "raw"),
        default="pretty",
        help="Formatting for saved and stdout JSON.",
    )
    parser.add_argument(
        "--save",
        nargs="?",
        const="AUTO",
        metavar="FILENAME",
        help="Optionally save an additional named export inside the video workspace.",
    )
    args = parser.parse_args(argv)
    if args.full and not args.prompt:
        parser.error("--full requires --prompt; default analysis is already comprehensive.")
    return args


def select_model(prompt: str | None, fast: bool = False, high: bool = False) -> str:
    """Route video-only and prompted work, honoring explicit quality overrides."""
    if high:
        return HIGH_MODEL
    if fast:
        return FLASH_LITE_MODEL
    return FLASH_MODEL if prompt and prompt.strip() else FLASH_LITE_MODEL


def persist_upload_preparation(artifact_dir: Path, preparation: dict[str, Any]) -> None:
    """Keep the current prepared-upload facts and retained derivative path readable."""
    manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    artifacts.pop("prepared_upload", None)
    artifacts["upload_input"] = str(preparation["prepared_path"])
    if preparation.get("downscaled"):
        artifacts["upload_derivative"] = str(preparation["prepared_path"])
    else:
        artifacts.pop("upload_derivative", None)
    core.update_manifest(artifact_dir, upload_preparation=preparation, artifacts=artifacts)


def prepare_upload(
    video_path: Path,
    media_metadata: dict[str, Any],
    artifact_dir: Path,
    upload_quality: str,
    media_tools: dict[str, str],
    logger: Callable[[str, dict[str, Any]], Any],
) -> tuple[Path, dict[str, Any]]:
    """Choose the original or one retained, proportional, downscale-only upload file."""
    target_height = UPLOAD_QUALITY_TARGET_HEIGHTS.get(upload_quality)
    if upload_quality not in UPLOAD_QUALITY_TARGET_HEIGHTS:
        raise core.VideoAnalysisError(f"Unsupported upload quality: {upload_quality}")

    width = media_metadata.get("width")
    height = media_metadata.get("height")
    has_dimensions = isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0
    if target_height is not None and not has_dimensions:
        raise core.VideoAnalysisError("ffprobe did not provide valid source dimensions for upload preparation.")

    source_identity = core.source_identity(video_path)
    prepared_path = video_path
    dimensions: tuple[int, int] | None = None
    downscaled = False
    reason = "high_quality_requested" if target_height is None else "source_at_or_below_target"

    if target_height is not None:
        dimensions = core.downscale_dimensions(width, height, target_height)
        if dimensions is not None:
            ffmpeg = media_tools.get("ffmpeg")
            if not ffmpeg:
                raise core.PrerequisiteError(
                    f"FFmpeg is required to prepare the {target_height}p upload. Ask the user before installing it, "
                    "or rerun with -hq to upload the original resolution."
                )
            command, prepared_path = core.upload_downscale_command(
                video_path,
                artifact_dir,
                ffmpeg,
                width=width,
                height=height,
                target_height=target_height,
            )
            previous_manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
            previous_preparation = (
                previous_manifest.get("upload_preparation")
                if isinstance(previous_manifest.get("upload_preparation"), dict)
                else {}
            )
            prepared_path_text = str(prepared_path.resolve())
            reusable_derivative = (
                prepared_path.exists()
                and prepared_path.is_file()
                and prepared_path.stat().st_size > 0
                and previous_preparation.get("source_identity") == source_identity
                and previous_preparation.get("prepared_path") == prepared_path_text
                and previous_preparation.get("prepared_identity") == core.source_identity(prepared_path)
            )
            if reusable_derivative:
                logger(
                    "upload_preparation_reused",
                    {"quality": upload_quality, "output": str(prepared_path), "dimensions": list(dimensions)},
                )
                reason = "retained_derivative_reused"
            else:
                temporary_output = Path(command[-1])
                for stale_path in (prepared_path, temporary_output):
                    try:
                        stale_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                logger(
                    "upload_preparation_started",
                    {"quality": upload_quality, "target_height": target_height, "output": str(prepared_path)},
                )
                try:
                    core.run_ffmpeg(command)
                    if not temporary_output.is_file() or temporary_output.stat().st_size <= 0:
                        raise core.VideoAnalysisError("FFmpeg did not produce a usable upload derivative.")
                    temporary_output.replace(prepared_path)
                except Exception:
                    for stale_path in (prepared_path, temporary_output):
                        try:
                            stale_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    logger("upload_preparation_failed", {"quality": upload_quality, "output": str(prepared_path)})
                    raise
                logger(
                    "upload_preparation_completed",
                    {"quality": upload_quality, "output": str(prepared_path), "dimensions": list(dimensions)},
                )
                reason = "downscaled"
            downscaled = True

    output_width, output_height = dimensions or ((width, height) if has_dimensions else (None, None))
    preparation = {
        "requested_quality": upload_quality,
        "target_height": target_height,
        "downscaled": downscaled,
        "reason": reason,
        "source_identity": source_identity,
        "prepared_path": str(prepared_path.resolve()),
        "prepared_identity": core.source_identity(prepared_path),
        "width": output_width,
        "height": output_height,
        "size_bytes": prepared_path.stat().st_size,
        "prepared_at": core.utc_now(),
    }
    persist_upload_preparation(artifact_dir, preparation)
    if not downscaled:
        logger(
            "upload_preparation_skipped",
            {"quality": upload_quality, "reason": reason, "source": str(video_path)},
        )
    return prepared_path, preparation


def load_api_key(cwd: Path) -> str:
    """Load the key from cwd `.env` first, then the process environment."""
    try:
        from dotenv import load_dotenv
    except ImportError as error:
        raise core.PrerequisiteError(
            "Missing Python dependency 'python-dotenv'. Ask the user before installing it with: "
            "python -m pip install python-dotenv"
        ) from error

    dotenv_path = cwd / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)
    skill_dotenv = Path(__file__).resolve().parent / ".env"
    if skill_dotenv.exists():
        load_dotenv(dotenv_path=skill_dotenv, override=False)
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        return api_key
    raise core.PrerequisiteError(
        "GEMINI_API_KEY is not set. Do not paste a key into chat logs. Set it for this PowerShell session with: "
        '$env:GEMINI_API_KEY = "PASTE_KEY_HERE". To persist it for future terminals, run: '
        '[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "PASTE_KEY_HERE", "User"). '
        "Or create a .env file in the invocation root or this skill scripts directory containing GEMINI_API_KEY=PASTE_KEY_HERE."
    )


def get_gemini_sdk() -> tuple[Any, Any]:
    """Import the optional Gemini dependency with actionable setup guidance."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as error:
        raise core.PrerequisiteError(
            "Missing Python dependency 'google-genai'. Ask the user before installing it with: "
            "python -m pip install google-genai"
        ) from error
    return genai, types


def create_client(api_key: str) -> Any:
    genai, _ = get_gemini_sdk()
    return genai.Client(api_key=api_key)


def build_file_part(remote_file: Any, start_seconds: float | None = None, end_seconds: float | None = None) -> Any:
    """Create a normal File part or a Gemini video-offset part for tail recovery."""
    _, types = get_gemini_sdk()
    mime_type = getattr(remote_file, "mime_type", None) or "video/mp4"
    if start_seconds is None or end_seconds is None:
        return types.Part.from_uri(file_uri=remote_file.uri, mime_type=mime_type)
    return types.Part(
        file_data=types.FileData(file_uri=remote_file.uri, mime_type=mime_type),
        video_metadata=types.VideoMetadata(
            start_offset=f"{start_seconds:.3f}s",
            end_offset=f"{end_seconds:.3f}s",
        ),
    )


def read_prompt(filename: str) -> str:
    """Read a prompt located next to this executable module."""
    path = Path(__file__).resolve().parent / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise core.VideoAnalysisError(f"Required prompt file is unavailable: {path}") from error


def parse_json_response(text: str | None) -> dict[str, Any]:
    """Parse JSON even when a model defensively wraps it in a markdown fence."""
    if not text or not text.strip():
        raise core.VideoAnalysisError("Gemini returned an empty response.")
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else ""
        if candidate.endswith("```"):
            candidate = candidate[:-3].rstrip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise core.VideoAnalysisError(f"Gemini did not return valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise core.VideoAnalysisError("Gemini returned JSON that is not an object.")
    return payload


def generate_json(
    client: Any,
    model: str,
    remote_file: Any,
    system_prompt: str,
    logger: Callable[[str, dict[str, Any]], Any],
    request_text: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    """Request JSON from Gemini and retry only transient transport/service failures."""
    _, types = get_gemini_sdk()
    contents: list[Any] = [build_file_part(remote_file, start_seconds, end_seconds), request_text]

    def operation() -> dict[str, Any]:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        return parse_json_response(getattr(response, "text", None))

    return core.retry_call("generation", operation, logger)


def state_name(remote_file: Any) -> str:
    """Normalize SDK File state values across SDK versions."""
    state = getattr(remote_file, "state", None)
    name = getattr(state, "name", state)
    return str(name or "").upper().split(".")[-1]


def serialise_remote_file(remote_file: Any) -> dict[str, Any]:
    """Store reusable remote-file details without storing credentials."""
    fields = (
        "name",
        "uri",
        "mime_type",
        "size_bytes",
        "sha256_hash",
        "create_time",
        "expiration_time",
        "update_time",
    )
    result: dict[str, Any] = {field: getattr(remote_file, field, None) for field in fields}
    result["state"] = state_name(remote_file)
    return json.loads(json.dumps(result, default=core.json_default))


def update_status(artifact_dir: Path, **values: Any) -> dict[str, Any]:
    """Merge a status change into the manifest without discarding prior detail."""
    manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
    status = manifest.get("status") if isinstance(manifest.get("status"), dict) else {}
    status.update(values)
    return core.update_manifest(artifact_dir, status=status)


def update_remote_file(artifact_dir: Path, remote_file: Any, upload_identity: str | None = None) -> dict[str, Any]:
    """Persist the primary retained Gemini file and the prepared-upload identity it represents."""
    remote_info = serialise_remote_file(remote_file)
    if upload_identity:
        remote_info["upload_identity"] = upload_identity
    return core.update_manifest(artifact_dir, remote_file=remote_info)


def append_recovery_remote_file(artifact_dir: Path, remote_file: Any) -> None:
    """Record a fallback-tail remote file without overwriting the primary one."""
    manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
    files = manifest.get("recovery_remote_files") if isinstance(manifest.get("recovery_remote_files"), list) else []
    files.append(serialise_remote_file(remote_file))
    core.update_manifest(artifact_dir, recovery_remote_files=files)


def remote_manifest_is_expired(remote_info: dict[str, Any]) -> bool:
    """Avoid trying a known-expired remote file before upload recovery."""
    expiry = remote_info.get("expiration_time")
    if not isinstance(expiry, str) or not expiry:
        return False
    try:
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= datetime.now(UTC)


def wait_until_active(client: Any, remote_file: Any, logger: Callable[[str, dict[str, Any]], Any]) -> Any:
    """Poll a remote video until it is usable, failing decisively on timeout/error."""
    start = time.monotonic()
    current = remote_file
    while True:
        state = state_name(current)
        if state == "ACTIVE":
            return current
        if state == "FAILED":
            details = getattr(current, "error", None)
            raise core.VideoAnalysisError(f"Gemini video processing failed: {details or 'unknown remote error'}")
        if time.monotonic() - start > PROCESSING_TIMEOUT_SECONDS:
            raise core.VideoAnalysisError("Gemini video processing timed out after 10 minutes.")
        logger("processing_wait", {"remote_file": getattr(current, "name", None), "state": state or "UNKNOWN"})
        time.sleep(PROCESSING_POLL_SECONDS)
        current = core.retry_call(
            "processing_poll",
            lambda: client.files.get(name=current.name),
            logger,
        )


def upload_new_remote_file(
    client: Any,
    source_path: Path,
    logger: Callable[[str, dict[str, Any]], Any],
) -> Any:
    """Upload and process a local video; source remains untouched."""
    logger("upload_started", {"source": str(source_path), "size_bytes": source_path.stat().st_size})
    remote = core.retry_call("upload", lambda: client.files.upload(file=str(source_path)), logger)
    return wait_until_active(client, remote, logger)


def attempt_remote_reuse(
    client: Any,
    artifact_dir: Path,
    upload_identity: str,
    logger: Callable[[str, dict[str, Any]], Any],
) -> Any | None:
    """Reuse a retained remote file only when it represents this prepared upload."""
    manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
    remote_info = manifest.get("remote_file") if isinstance(manifest.get("remote_file"), dict) else {}
    remote_name = remote_info.get("name")
    if not remote_name or remote_manifest_is_expired(remote_info):
        return None
    if remote_info.get("upload_identity") != upload_identity:
        logger(
            "remote_reuse_skipped",
            {"reason": "prepared_upload_changed", "remote_file": remote_name},
        )
        return None
    try:
        logger("remote_reuse_attempt", {"remote_file": remote_name})
        remote = core.retry_call("remote_file_lookup", lambda: client.files.get(name=remote_name), logger)
        remote = wait_until_active(client, remote, logger)
    except Exception as error:
        logger("remote_reuse_unavailable", {"remote_file": remote_name, "error": str(error)})
        return None
    update_remote_file(artifact_dir, remote, upload_identity=upload_identity)
    logger("remote_reused", {"remote_file": getattr(remote, "name", None)})
    return remote


def acquire_remote_file(
    client: Any,
    video_path: Path,
    artifact_dir: Path,
    media_tools: dict[str, str],
    logger: Callable[[str, dict[str, Any]], Any],
    upload_identity: str,
) -> Any:
    """Reuse, upload, or transparently normalize only after a compatibility failure."""
    reused = attempt_remote_reuse(client, artifact_dir, upload_identity, logger)
    if reused is not None:
        return reused
    try:
        remote = upload_new_remote_file(client, video_path, logger)
    except Exception as error:
        if not core.is_upload_compatibility_error(error):
            raise
        ffmpeg = media_tools.get("ffmpeg")
        if not ffmpeg:
            raise core.PrerequisiteError(
                "Gemini rejected this source format, and FFmpeg is required for the compatibility fallback. "
                "Ask the user before installing FFmpeg, then retry the analysis."
            ) from error
        command, normalized = core.normalization_command(video_path, artifact_dir, ffmpeg)
        temporary_normalized = Path(command[-1])
        for stale_path in (normalized, temporary_normalized):
            try:
                stale_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger("normalization_started", {"source": str(video_path), "output": str(normalized), "reason": str(error)})
        try:
            core.run_ffmpeg(command)
            if not temporary_normalized.is_file() or temporary_normalized.stat().st_size <= 0:
                raise core.VideoAnalysisError("FFmpeg did not produce a usable compatibility-normalized video.")
            temporary_normalized.replace(normalized)
        except Exception:
            for stale_path in (normalized, temporary_normalized):
                try:
                    stale_path.unlink(missing_ok=True)
                except OSError:
                    pass
            logger("normalization_failed", {"output": str(normalized)})
            raise
        manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        artifacts["normalized_video"] = str(normalized)
        core.update_manifest(artifact_dir, artifacts=artifacts)
        remote = upload_new_remote_file(client, normalized, logger)
        logger("normalization_succeeded", {"output": str(normalized)})
    update_remote_file(artifact_dir, remote, upload_identity=upload_identity)
    logger("remote_uploaded", {"remote_file": getattr(remote, "name", None)})
    return remote


def enrich_result(payload: dict[str, Any], video_path: Path, media_metadata: dict[str, Any], model: str) -> dict[str, Any]:
    """Attach local facts without altering the model's detailed report fields."""
    payload.setdefault("description", "")
    if not isinstance(payload["description"], str) or not payload["description"].strip():
        assessment = payload.get("overall_assessment") if isinstance(payload.get("overall_assessment"), dict) else {}
        payload["description"] = str(assessment.get("summary") or "No standalone description was returned by Gemini.")
    payload["_local_metadata"] = {
        "source_video": str(video_path.resolve()),
        "source_name": video_path.name,
        "media": media_metadata,
        "model": model,
        "generated_at": core.utc_now(),
    }
    return payload


def validate_tail_payload(payload: dict[str, Any], start_seconds: float, end_seconds: float) -> dict[str, Any]:
    """Accept absolute source timestamps or explicitly recorded segment-relative ones."""
    absolute = core.validate_coverage(payload, end_seconds)
    if absolute["complete"]:
        return {"complete": True, "reference": "source", "validation": absolute}
    segment_duration = max(0.001, end_seconds - start_seconds)
    relative = core.validate_coverage(payload, segment_duration)
    if relative["complete"]:
        return {"complete": True, "reference": "segment-relative", "validation": relative}
    return {"complete": False, "reference": "source", "validation": absolute, "relative_validation": relative}


def recovery_request(mode: str, original_prompt: str | None, start_seconds: float, end_seconds: float, local_clip: bool = False) -> str:
    """Build a precise coverage-recovery request without changing primary intent."""
    source = "a local clip from" if local_clip else "the requested segment of"
    focused = f" The original focused task is: {original_prompt!r}." if original_prompt else ""
    return (
        f"Coverage recovery: inspect {source} the original source video spanning {start_seconds:.3f}s through "
        f"{end_seconds:.3f}s.{focused} Return valid JSON following the system contract. Analyze this entire tail, "
        "not only the first frame. Use original source timestamps whenever possible; if timestamps are relative to "
        "this segment, state that in analysis_coverage.timestamp_reference. Set analysis_coverage.analyzed_through "
        f"to the end actually analyzed, and include concrete tail evidence."
    )


def recover_coverage(
    payload: dict[str, Any],
    mode: str,
    original_prompt: str | None,
    client: Any,
    model: str,
    remote_file: Any,
    system_prompt: str,
    video_path: Path,
    artifact_dir: Path,
    media_tools: dict[str, str],
    duration_seconds: float,
    logger: Callable[[str, dict[str, Any]], Any],
    recovery_label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify complete coverage and recover an uncovered tail with bounded fallback."""
    verification = core.validate_coverage(payload, duration_seconds)
    if verification["complete"]:
        payload["_verification"] = verification
        return payload, verification

    recoveries = payload.get("_coverage_recovery") if isinstance(payload.get("_coverage_recovery"), list) else []
    start_seconds = core.uncovered_tail_start(verification)
    tail_attempts: list[tuple[str, Any, float | None, float | None, float, bool]] = [
        ("remote_segment", remote_file, start_seconds, duration_seconds, start_seconds, False)
    ]

    for attempt_number, (strategy, tail_remote, part_start, part_end, segment_start, local_clip) in enumerate(tail_attempts, start=1):
        logger(
            "coverage_recovery_started",
            {"strategy": strategy, "start_seconds": segment_start, "end_seconds": duration_seconds, "attempt": attempt_number},
        )
        try:
            tail_payload = generate_json(
                client,
                model,
                tail_remote,
                system_prompt,
                logger,
                recovery_request(mode, original_prompt, segment_start, duration_seconds, local_clip=local_clip),
                start_seconds=part_start,
                end_seconds=part_end,
            )
            if mode == "prompt":
                tail_payload = core.normalise_prompt_result(tail_payload, original_prompt or "")
            tail_validation = validate_tail_payload(tail_payload, segment_start, duration_seconds)
        except Exception as error:
            tail_payload = {"error": str(error)}
            tail_validation = {"complete": False, "error": str(error)}

        recovery_record = {
            "strategy": strategy,
            "segment_start_seconds": segment_start,
            "segment_end_seconds": duration_seconds,
            "tail_result": tail_payload,
            "tail_verification": tail_validation,
        }
        if tail_validation.get("complete"):
            recovery_record["verified_end_timestamp"] = core.seconds_to_timestamp(duration_seconds)
        recoveries.append(recovery_record)
        payload["_coverage_recovery"] = recoveries
        core.write_json(artifact_dir / f"recovery-tail-{recovery_label}-{attempt_number}.json", recovery_record)
        verification = core.validate_coverage(payload, duration_seconds)
        if verification["complete"]:
            verification["recovered"] = True
            verification["recovery_strategy"] = strategy
            payload["_verification"] = verification
            logger("coverage_recovery_succeeded", {"strategy": strategy})
            return payload, verification

    # API video offsets did not produce enough evidence. Create one retained, local fallback clip.
    ffmpeg = media_tools.get("ffmpeg")
    if not ffmpeg:
        reason = "FFmpeg is unavailable, so a local tail clip cannot be created for coverage recovery."
        fallback_record = {
            "strategy": "local_tail_clip",
            "segment_start_seconds": start_seconds,
            "segment_end_seconds": duration_seconds,
            "available": False,
            "reason": reason,
        }
        recoveries.append(fallback_record)
        payload["_coverage_recovery"] = recoveries
        core.write_json(artifact_dir / f"recovery-tail-{recovery_label}-local.json", fallback_record)
        verification = core.validate_coverage(payload, duration_seconds)
        verification["recovered"] = False
        verification["local_recovery_available"] = False
        payload["_verification"] = verification
        logger("tail_clip_unavailable", {"reason": reason, "start_seconds": start_seconds})
        return payload, verification

    clip_command, clip_path = core.tail_clip_command(video_path, artifact_dir, ffmpeg, start_seconds)
    try:
        logger("tail_clip_started", {"start_seconds": start_seconds, "output": str(clip_path)})
        core.run_ffmpeg(clip_command)
        tail_remote = upload_new_remote_file(client, clip_path, logger)
        append_recovery_remote_file(artifact_dir, tail_remote)
        tail_payload = generate_json(
            client,
            model,
            tail_remote,
            system_prompt,
            logger,
            recovery_request(mode, original_prompt, start_seconds, duration_seconds, local_clip=True),
        )
        if mode == "prompt":
            tail_payload = core.normalise_prompt_result(tail_payload, original_prompt or "")
        tail_validation = validate_tail_payload(tail_payload, start_seconds, duration_seconds)
    except Exception as error:
        tail_payload = {"error": str(error)}
        tail_validation = {"complete": False, "error": str(error)}

    fallback_record = {
        "strategy": "local_tail_clip",
        "segment_start_seconds": start_seconds,
        "segment_end_seconds": duration_seconds,
        "clip": str(clip_path),
        "tail_result": tail_payload,
        "tail_verification": tail_validation,
    }
    if tail_validation.get("complete"):
        fallback_record["verified_end_timestamp"] = core.seconds_to_timestamp(duration_seconds)
    recoveries.append(fallback_record)
    payload["_coverage_recovery"] = recoveries
    core.write_json(artifact_dir / f"recovery-tail-{recovery_label}-local.json", fallback_record)
    verification = core.validate_coverage(payload, duration_seconds)
    verification["recovered"] = bool(verification["complete"])
    if verification["complete"]:
        verification["recovery_strategy"] = "local_tail_clip"
        logger("coverage_recovery_succeeded", {"strategy": "local_tail_clip"})
    else:
        logger("coverage_recovery_incomplete", {"covered_through_seconds": verification["covered_through_seconds"]})
    payload["_verification"] = verification
    return payload, verification


def verify_or_mark_unverified(
    payload: dict[str, Any],
    mode: str,
    original_prompt: str | None,
    client: Any,
    model: str,
    remote_file: Any,
    system_prompt: str,
    video_path: Path,
    artifact_dir: Path,
    media_tools: dict[str, str],
    duration_seconds: float | None,
    logger: Callable[[str, dict[str, Any]], Any],
    recovery_label: str,
    unavailable_reason: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover coverage when ffprobe succeeded, otherwise retain an explicit unverified result."""
    if duration_seconds is None:
        verification = {
            "complete": False,
            "verification_available": False,
            "reason": unavailable_reason or "ffprobe did not provide a usable duration.",
        }
        payload["_verification"] = verification
        logger("coverage_verification_unavailable", {"reason": verification["reason"]})
        return payload, verification
    return recover_coverage(
        payload,
        mode,
        original_prompt,
        client,
        model,
        remote_file,
        system_prompt,
        video_path,
        artifact_dir,
        media_tools,
        duration_seconds,
        logger,
        recovery_label,
    )


def persist_results(
    artifact_dir: Path,
    mode: str,
    primary_result: dict[str, Any],
    full_result: dict[str, Any] | None,
    pretty: bool,
) -> dict[str, Path]:
    """Persist current authoritative results in the single source workspace."""
    primary_name = "prompt-result.json" if mode == "prompt" else "analysis.json"
    paths = {"primary": core.write_json(artifact_dir / primary_name, primary_result, pretty=pretty)}
    if full_result is not None:
        paths["full"] = core.write_json(artifact_dir / "full-analysis.json", full_result, pretty=pretty)
    return paths


def save_optional_export(
    artifact_dir: Path,
    requested_name: str | None,
    primary_result: dict[str, Any],
    pretty: bool,
) -> Path | None:
    """Keep legacy --save behavior safely inside the source artifact workspace."""
    if not requested_name:
        return None
    filename = "export.json" if requested_name == "AUTO" else Path(requested_name).name
    if not filename.lower().endswith(".json"):
        filename += ".json"
    return core.write_json(artifact_dir / filename, primary_result, pretty=pretty)


def build_console_message(outcome: dict[str, Any]) -> str:
    """Render a concise human-facing summary while detailed JSON stays on disk."""
    result = outcome["primary_result"]
    lines = [str(result.get("description") or "Video analysis complete.")]
    if outcome["mode"] == "prompt" and result.get("answer"):
        lines.extend(["", str(result["answer"])])
    lines.extend(["", f"Saved: {outcome['paths']['primary']}"])
    if outcome["paths"].get("full"):
        lines.append(f"Full report: {outcome['paths']['full']}")
    if not outcome["complete"]:
        verification = outcome.get("primary_verification")
        if isinstance(verification, dict) and not verification.get("verification_available", True):
            lines.append("WARNING: Coverage is unverified because ffprobe metadata was unavailable; do not rely on this as a complete analysis.")
        else:
            lines.append("WARNING: Coverage is incomplete; inspect _verification and recovery artifacts before relying on this result.")
    return "\n".join(lines)


def run_analysis(
    args: argparse.Namespace,
    cwd: Path | None = None,
    client: Any | None = None,
    media_tools: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one default or prompted analysis and persist all local evidence."""
    working_dir = (cwd or Path.cwd()).resolve()
    video_path = Path(args.video_file).expanduser().resolve()
    if not video_path.exists() or not video_path.is_file():
        raise core.VideoAnalysisError(f"Video file not found: {video_path}")

    tools = dict(media_tools) if media_tools is not None else core.available_media_tools()
    artifact_dir = core.build_artifact_dir(video_path, working_dir)
    preflight_error: str | None = None
    try:
        media_metadata = core.probe_video(video_path)
    except core.PrerequisiteError as error:
        if args.upload_quality != "high":
            raise
        preflight_error = str(error)
        media_metadata = {"preflight": {"available": False, "error": preflight_error}}
    core.create_or_update_manifest(artifact_dir, video_path, media_metadata)
    logger = lambda event, details: core.append_activity(artifact_dir, event, details)
    if preflight_error:
        logger("preflight_unavailable", {"reason": preflight_error, "upload_quality": args.upload_quality})
    try:
        prepared_upload, upload_preparation = prepare_upload(
            video_path,
            media_metadata,
            artifact_dir,
            args.upload_quality,
            tools,
            logger,
        )
    except Exception as error:
        update_status(artifact_dir, state="failed", error=str(error), coverage_complete=False)
        logger("analysis_failed", {"error": str(error)})
        raise
    model = select_model(args.prompt, fast=args.fast, high=args.high)
    mode = "prompt" if args.prompt else "global"
    update_status(artifact_dir, state="prepared")
    core.update_manifest(
        artifact_dir,
        last_request={
            "mode": mode,
            "prompt": args.prompt,
            "full": bool(args.full),
            "model": model,
            "upload_quality": args.upload_quality,
            "coverage_verification_available": preflight_error is None,
            "requested_at": core.utc_now(),
        },
    )
    logger(
        "analysis_started",
        {
            "mode": mode,
            "model": model,
            "full": bool(args.full),
            "upload_quality": args.upload_quality,
            "prepared_upload": str(prepared_upload),
        },
    )

    try:
        active_client = client or create_client(load_api_key(working_dir))
        remote_file = acquire_remote_file(
            active_client,
            prepared_upload,
            artifact_dir,
            tools,
            logger,
            upload_preparation["prepared_identity"],
        )
        global_prompt = read_prompt("global_analysis_prompt.md")
        prompt_prompt = read_prompt("prompted_analysis_prompt.md")
        raw_duration = media_metadata.get("duration_seconds")
        try:
            duration_seconds = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        if duration_seconds is not None and duration_seconds <= 0:
            duration_seconds = None

        if args.prompt:
            primary_result = core.normalise_prompt_result(
                generate_json(
                    active_client,
                    model,
                    remote_file,
                    prompt_prompt,
                    logger,
                    f"Perform this focused video-analysis task exactly: {args.prompt}",
                ),
                args.prompt,
            )
            primary_result = enrich_result(primary_result, video_path, media_metadata, model)
            primary_result, primary_verification = verify_or_mark_unverified(
                primary_result,
                "prompt",
                args.prompt,
                active_client,
                model,
                remote_file,
                prompt_prompt,
                prepared_upload,
                artifact_dir,
                tools,
                duration_seconds,
                logger,
                "prompt",
                preflight_error,
            )
            full_result = None
            full_verification = None
            if args.full:
                full_result = enrich_result(
                    generate_json(
                        active_client,
                        model,
                        remote_file,
                        global_prompt,
                        logger,
                        "Analyze the supplied video completely and return the comprehensive JSON report.",
                    ),
                    video_path,
                    media_metadata,
                    model,
                )
                full_result, full_verification = verify_or_mark_unverified(
                    full_result,
                    "global",
                    None,
                    active_client,
                    model,
                    remote_file,
                    global_prompt,
                    prepared_upload,
                    artifact_dir,
                    tools,
                    duration_seconds,
                    logger,
                    "full",
                    preflight_error,
                )
        else:
            primary_result = enrich_result(
                generate_json(
                    active_client,
                    model,
                    remote_file,
                    global_prompt,
                    logger,
                    "Analyze the supplied video completely and return the comprehensive JSON report.",
                ),
                video_path,
                media_metadata,
                model,
            )
            primary_result, primary_verification = verify_or_mark_unverified(
                primary_result,
                "global",
                None,
                active_client,
                model,
                remote_file,
                global_prompt,
                prepared_upload,
                artifact_dir,
                tools,
                duration_seconds,
                logger,
                "global",
                preflight_error,
            )
            full_result = None
            full_verification = None

        paths = persist_results(
            artifact_dir,
            mode,
            primary_result,
            full_result,
            pretty=args.format == "pretty",
        )
        export = save_optional_export(artifact_dir, args.save, primary_result, pretty=args.format == "pretty")
        if export:
            paths["export"] = export
        all_complete = bool(primary_verification["complete"]) and (
            full_verification is None or bool(full_verification["complete"])
        )
        verification_available = bool(primary_verification.get("verification_available", True)) and (
            full_verification is None or bool(full_verification.get("verification_available", True))
        )
        manifest = core.read_json(artifact_dir / "manifest.json", {"schema_version": 1})
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        artifacts.update({name: str(path) for name, path in paths.items()})
        core.update_manifest(
            artifact_dir,
            artifacts=artifacts,
            status={
                "state": "completed" if all_complete else ("incomplete" if verification_available else "unverified"),
                "coverage_complete": all_complete,
                "coverage_verification_available": verification_available,
                "primary_verification": primary_verification,
                "full_verification": full_verification,
            },
        )
        logger("analysis_finished", {"complete": all_complete, "primary": str(paths["primary"])})
        return {
            "mode": mode,
            "artifact_dir": artifact_dir,
            "prepared_upload": prepared_upload,
            "upload_preparation": upload_preparation,
            "paths": paths,
            "primary_result": primary_result,
            "full_result": full_result,
            "complete": all_complete,
            "primary_verification": primary_verification,
            "full_verification": full_verification,
        }
    except Exception as error:
        update_status(artifact_dir, state="failed", error=str(error), coverage_complete=False)
        logger("analysis_failed", {"error": str(error)})
        raise


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint with purposeful exit codes and no secret disclosure."""
    args = parse_args(argv)
    try:
        outcome = run_analysis(args)
    except core.PrerequisiteError as error:
        print(f"Prerequisite error: {error}", file=sys.stderr)
        return 2
    except core.VideoAnalysisError as error:
        print(f"Analysis error: {error}", file=sys.stderr)
        return 3
    except Exception as error:  # Keep an SDK/network implementation failure actionable.
        print(f"Unexpected analysis error: {error}", file=sys.stderr)
        return 3

    if args.output == "json":
        print(json.dumps(outcome["primary_result"], indent=2 if args.format == "pretty" else None, ensure_ascii=False))
    elif args.output == "console":
        print(build_console_message(outcome))
    if not outcome["complete"]:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
