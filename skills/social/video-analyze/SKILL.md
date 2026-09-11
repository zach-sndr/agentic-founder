---
name: video-analyze
description: Use when analyzing a local video with Gemini, including timestamp evidence, FFmpeg/ffprobe preflight, downscale-only uploads, or incomplete-video recovery.
---

# Video Analyze

Analyze a local video with Gemini and retain one readable workspace per source revision. The source is never modified. The analyzer saves results and only reports a verified completion when Gemini timestamp evidence reaches the ffprobe duration.

## Required workflow

1. Confirm the local video path and choose upload quality before running the tool. Default to medium (`-mq`).
2. Check `ffprobe`, FFmpeg when the requested quality needs a conversion, `google-genai`, `python-dotenv`, and `GEMINI_API_KEY`.
3. Missing dependency or media tool? Show the exact setup command and ask permission before installing. Do not silently install anything.
4. If the source is above the selected 480p/720p ceiling and FFmpeg is missing, ask once to install it. **If the user declines**, rerun the requested analysis with `-hq`; it uploads the original file without a quality conversion. A source already at or below its ceiling needs no conversion.
5. Run from the project/invocation root so artifacts are stored in that root's `video-analyze/` folder.
6. Inspect the saved result's `_verification.complete` and `manifest.json` → `status.coverage_complete`. Claim completion only when both are true and the command exits `0`.
7. If coverage is incomplete, inspect `activity.jsonl` and `recovery-tail-*.json`. The analyzer has already tried Gemini's uncovered tail and, when FFmpeg is available, a local tail clip. Do not claim success when status is `incomplete` or `unverified`.

## Setup

Install Python dependencies only with user approval:

```bash
python -m pip install -r "$SKILL_PATH/requirements.txt"
```

FFmpeg is required for actual 480p/720p conversion, source compatibility fallback, and local tail recovery. ffprobe is required for deterministic duration metadata and verified coverage. With user approval:

```powershell
# Windows
winget install --id Gyan.FFmpeg -e
```

```bash
# macOS
brew install ffmpeg

# Debian/Ubuntu
sudo apt-get update && sudo apt-get install -y ffmpeg
```

Open a new terminal after a Winget install, or set `FFMPEG_BIN` to FFmpeg's `bin` directory.

`-hq` can upload directly without FFmpeg. If ffprobe is also unavailable, it still saves an analysis, but marks it `unverified` and exits `4`; duration coverage cannot be claimed.

### Gemini key

Check whether `GEMINI_API_KEY` is available. If it is not, read and follow [onboarding.md](onboarding.md). Do not load `onboarding.md` when the key is already configured.

The tool reads `<invocation-root>/.env` first, then this skill's `scripts/.env`, then `GEMINI_API_KEY` from the process environment. Never echo or log a supplied key.

## Commands

Stay in the user's invocation root. Never change into this skill directory to process media.

```bash
python "$SKILL_PATH/scripts/analyze_video.py" "video.mp4" --output file
```

```powershell
$script = Join-Path $env:SKILL_PATH "scripts\\analyze_video.py"
python $script "video.mp4" --output file
```

If `SKILL_PATH` is unset, use this skill directory:

```powershell
python <skill-directory>/scripts/analyze_video.py "video.mp4" --output file
```

### Upload quality

| Flag | Upload behavior |
|---|---|
| `-lq` / `--lq` | Downscale to at most 480p. |
| `-mq` / `--mq` | Downscale to at most 720p; default. |
| `-hq` / `--hq` | Upload the original resolution. |

The analyzer never upscales. If the source is already at or below the selected 480p/720p height, it uploads the original without re-encoding. Downscaled derivatives are proportional H.264/AAC MP4 files retained in the artifact folder.

```bash
# Faster 480p upload
python "$SKILL_PATH/scripts/analyze_video.py" "video.mp4" -lq --output file

# Original-resolution upload, including after a declined FFmpeg install
python "$SKILL_PATH/scripts/analyze_video.py" "video.mp4" -hq --output file
```

`-lq`, `-mq`, and `-hq` control upload resolution. They are distinct from `--fast` and `--high`, which control the Gemini model, and may be combined with either model override. If a user says only "low quality," clarify whether they mean the upload (`-lq`) or model speed (`--fast`).

### Analysis modes and models

| Input | Automatic model |
|---|---|
| Video only | `gemini-3.1-flash-lite` |
| Video + `--prompt` | `gemini-3-flash-preview` |

`--fast` forces Flash Lite and `--high` forces `gemini-3.5-flash`; they cannot be combined.

```bash
# Focused evidence task; regular Flash by default
python "$SKILL_PATH/scripts/analyze_video.py" "video.mp4" \\
  --prompt "Find every big green frog and give timestamps." --output file

# Focused task plus comprehensive report
python "$SKILL_PATH/scripts/analyze_video.py" "video.mp4" \\
  --prompt "List all ingredients used." --full --high -hq --output file
```

`--full` requires `--prompt`. Video-only output contains a short `description` plus the detailed structured report. Prompted output contains the direct answer, findings, evidence timestamps, and coverage data.

### Output controls

- `--output console` — concise summary (default).
- `--output json` — primary JSON on stdout; diagnostics remain on stderr.
- `--output file` — save artifacts without printing the result.
- `--format pretty|raw` — JSON formatting.
- `--save [filename]` — optional extra JSON export inside the video workspace.

## Artifact workspace and recovery

```text
video-analyze/
  recipe-demo--a1b2c3d4/
    manifest.json
    activity.jsonl
    upload-480p.mp4 | upload-720p.mp4  # only when downscaled
    analysis.json | prompt-result.json
    full-analysis.json                  # --prompt + --full
    normalized-*.mp4                    # compatibility fallback only
    recovery-tail-*.json/.mp4           # recovery only
```

There are no `run-*` folders. `manifest.json` records source metadata, `upload_preparation`, `artifacts.upload_input`, the selected model, the uploaded-file identity, remote-file reuse state, result paths, and completion state. Remote reuse is allowed only for the same prepared upload; two flags may share it only when both resolve to the unchanged original. Delete this one source folder when its artifacts are no longer useful.

On incomplete coverage, the analyzer asks Gemini for the uncovered tail first. If that fails and FFmpeg exists, it creates one retained local tail clip. Without FFmpeg it records why that local fallback is unavailable and leaves the analysis incomplete.
