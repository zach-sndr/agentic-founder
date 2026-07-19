# Transcription credential onboarding

Read this file only when neither `NVIDIA_API_KEY` nor `OPENROUTER_API_KEY` is configured.

## Provider choices

Recommend NVIDIA-hosted Whisper first for developer and non-production use:

1. Open the [NVIDIA Whisper Large v3 API page](https://build.nvidia.com/openai/whisper-large-v3/api).
2. Sign in and select **Get API Key**.
3. Install the hosted endpoint's lightweight client:

   ```text
   python -m pip install --upgrade nvidia-riva-client
   ```

OpenRouter Whisper is the fallback. Create a key at [OpenRouter Keys](https://openrouter.ai/keys).

NVIDIA-hosted Whisper is the primary provider when both keys exist. OpenRouter is used when NVIDIA is unavailable or its response does not contain the timestamp data this skill requires.

## Handle keys safely

Never repeat, quote, print, log, or include a key in a response, transcript, command argument, source-controlled file, or tool output. Do not confirm a key by showing any part of it.

Prefer asking the user to enter the key locally. If the user explicitly gives a key to the agent, do not echo it; write it only to the installed skill's `scripts/.env`, ensure that file remains untracked, and report only that the credential was stored.

The installed `.env` path is:

- Windows: `%USERPROFILE%\.codex\skills\video-input\scripts\.env`
- macOS/Linux: `~/.codex/skills/video-input/scripts/.env`

The file may contain either or both entries:

```dotenv
NVIDIA_API_KEY=<enter locally>
OPENROUTER_API_KEY=<enter locally>
```

Environment variables take precedence over `.env` values.

## Give the command for the user's OS

Detect the operating system before offering a command. Never put a literal key in a command because shell history and process inspection can expose it.

For Windows PowerShell, use a hidden prompt for the current session. Change only the variable name when configuring OpenRouter:

```powershell
$secure = Read-Host 'NVIDIA API key' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:NVIDIA_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
```

For macOS or Linux, use a silent prompt for the current shell. Change only the variable name when configuring OpenRouter:

```bash
read -rsp 'NVIDIA API key: ' NVIDIA_API_KEY; export NVIDIA_API_KEY; printf '\n'
```

For persistent setup, recommend editing the installed `.env` locally instead of embedding a secret in `setx`, shell-profile, or command-history text.

After setup, check only whether one of the two variables is nonempty. Never display its value. Then continue the original video task without loading this file again.
