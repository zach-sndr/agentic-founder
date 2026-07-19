---
name: video-input
description: Use when a local video or screen recording contains spoken instructions, a UI walkthrough, design feedback, or update requests that need a complete timestamped brief with only the necessary screenshots.
---

# Video Input

Convert a local instruction video into one illustrated Markdown brief. Keep working media in the invocation root, use the transcript to decide where imagery is actually needed, and leave only `brief.md` plus selected WebP files after success.

## Run the workflow

Stay in the user's invocation root. Never change into this skill directory to process media.

1. Start or resume transcription:

   ```powershell
   python <skill-directory>/scripts/video_input.py transcribe <video> --workers 2 --chunk-seconds 600
   ```

   Add `--language CODE` only when the user supplied a language. Add `--output-name SLUG` only to avoid a known collision. The command immediately creates `<invocation-root>/<video-slug>/brief.md`, then progressively fills `## Complete Transcript`. Request timestamped `whisper-large-v3` transcription from Groq first and use OpenRouter Whisper as fallback. Never invent timestamps when a provider omits them. Resume matching interrupted work by rerunning the exact original command, including its original `--output-name`; changing the name starts a different workspace. Choose a new name only before a fresh run when the default destination is already completed. `video-input` is a reserved name because that directory identifies older job layouts; pass a different `--output-name` if the video sanitizes to it. Jobs made by older versions under `<invocation-root>/video-input/<video-slug>/` remain discoverable and resumable when that legacy container is a real local directory, not a link or junction.

2. Read the complete transcript in bounded chunk order. Preserve every instruction, decision, caveat, dependency, and UI change; remove only filler and repetition. Do not invent content or timing.

3. Create `<invocation-root>/<video-slug>/.work/brief-plan.json` with schema version `1`:

   ```json
   {
     "schema_version": 1,
     "title": "Brief title",
     "summary": "What the video asks for.",
     "source_identity": "copy from .work/state.json",
     "reviewed_chunk_ids": [0, 1],
     "instructions": [{
       "heading": "Do the specific task",
       "source_ranges": [{"start": 12.4, "end": 28.1}],
       "body_markdown": "Complete details, constraints, and caveats.",
       "visuals": [{
         "timestamp": 18.2,
         "role": "reference",
         "alt": "Descriptive UI state",
         "caption": "The required final appearance state."
       }]
     }]
   }
   ```

   Use seconds for JSON timestamps. Keep instructions and ranges chronological. Include every completed chunk ID exactly once. Each instruction may have zero to three chronological visuals. Roles are `reference`, `before`, `after`, or `step`. Write captions without a timestamp; the finalizer appends the selected frame time.

4. Request temporary contact sheets only where visual evidence changes how an instruction should be understood:

   ```powershell
   python <skill-directory>/scripts/video_input.py contact-sheet <video> --id 3 --start 72.5 --duration 5
   ```

   Use a five-second window centered on a stable screen state. For a transition, use ten seconds beginning two seconds before the spoken action. If the relevant state falls just outside the first sheet, inspect one adjacent window. Select one reference frame, a before/after pair, or at most three chronological frames. Avoid decorative or redundant screenshots.

5. Finalize only after the plan is complete:

   ```powershell
   python <skill-directory>/scripts/video_input.py finalize <video> --plan <job>/.work/brief-plan.json
   ```

   The finalizer validates transcript review, ranges, images, links, and containment. Success leaves exactly:

   ```text
   <invocation-root>/<video-slug>/
   ├── brief.md
   └── images/
   ```

   `brief.md` interleaves actionable instructions, source ranges, screenshots, captions, and the complete transcript. A failed run retains `.work` for diagnosis and resume. Never delete recovery data or overwrite completed output without the user's explicit direction.

## Credentials

Check whether `GROQ_API_KEY` or `OPENROUTER_API_KEY` is available. If neither key is available, read and follow [onboarding.md](onboarding.md). Do not load `onboarding.md` when either key is already configured.

Never print, log, or repeat credentials. On timestamp validation failure, stop without guessing timestamps. Use `status <video>` to inspect resumable progress.
