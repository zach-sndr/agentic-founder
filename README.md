# Agentic Founder

Open agent skills for founders and builders. Skills in this repository follow the portable `SKILL.md` format and can be installed with the [`skills` CLI](https://www.npmjs.com/package/skills).

## Install

List the available skills:

```bash
npx skills add zach-sndr/agentic-founder --list
```

Install `video-input` interactively:

```bash
npx skills add zach-sndr/agentic-founder --skill video-input
```

Install it globally for Codex without prompts:

```bash
npx skills add zach-sndr/agentic-founder --skill video-input --agent codex --global --yes
```

## Available skills

### `video-input`

Convert long instruction videos, screen recordings, UI walkthroughs, and design feedback into one timestamped Markdown brief with selected screenshots.

Requirements:

- Python 3.10 or newer
- FFmpeg and ffprobe on `PATH`, or `FFMPEG_BIN` pointing to FFmpeg
- A Groq API key in `GROQ_API_KEY` (primary free-access route) or an OpenRouter key in `OPENROUTER_API_KEY` (fallback)

The skill extracts audio locally and sends timestamped audio chunks to Groq Whisper Large v3 first, with OpenRouter Whisper as fallback. Video frames and screenshots remain local.

## Repository layout

```text
skills/
└── video-input/
    ├── SKILL.md
    ├── onboarding.md
    ├── agents/openai.yaml
    └── scripts/video_input.py
```

Each folder under `skills/` is independently installable. Future public skills will be added alongside `video-input`.

## License

MIT
