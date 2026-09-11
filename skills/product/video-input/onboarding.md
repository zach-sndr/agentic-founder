# Transcription credential onboarding

Read this file only when neither `GROQ_API_KEY` nor `OPENROUTER_API_KEY` is configured.

## Provider choices

Recommend Groq Whisper as the primary free-access option:

1. Open [Groq API Keys](https://console.groq.com/keys).
2. Sign in and create an API key.
3. Configure it as `GROQ_API_KEY`.

Groq uses an OpenAI-compatible HTTPS endpoint and requires no additional Python package. OpenRouter Whisper is the paid fallback; create a key at [OpenRouter Keys](https://openrouter.ai/keys) and configure `OPENROUTER_API_KEY`.

When both keys exist, use Groq first and OpenRouter only when Groq is unavailable.

## Handle keys safely

Never repeat, quote, print, log, or include a key in a response, transcript, command argument, source-controlled file, or tool output. Do not confirm a key by showing any part of it.

Prefer asking the user to enter the key locally. If the user explicitly gives a key to the agent, do not echo it; write it only to the installed skill's `scripts/.env`, ensure that file remains untracked, and report only that the credential was stored.

The installed `.env` path is:

- Windows: `%USERPROFILE%\.codex\skills\video-input\scripts\.env`
- macOS/Linux: `~/.codex/skills/video-input/scripts/.env`

The file may contain either or both entries:

```dotenv
GROQ_API_KEY=<enter locally>
OPENROUTER_API_KEY=<enter locally>
```

Environment variables take precedence over `.env` values.

## Give the command for the user's OS

Detect the operating system before offering a command. Never put a literal key in a command because shell history and process inspection can expose it.

For Windows PowerShell, use a hidden prompt for the current session. Change only the variable name when configuring OpenRouter:

```powershell
$secure = Read-Host 'Groq API key' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:GROQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
```

For macOS or Linux, use a silent prompt for the current shell. Change only the variable name when configuring OpenRouter:

```bash
read -rsp 'Groq API key: ' GROQ_API_KEY; export GROQ_API_KEY; printf '\n'
```

For persistent setup, recommend editing the installed `.env` locally instead of embedding a secret in `setx`, shell-profile, or command-history text.

After setup, check only whether one of the two variables is nonempty. Never display its value. Then continue the original video task without loading this file again.
