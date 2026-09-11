# Gemini credential onboarding

Read this file only when `GEMINI_API_KEY` is not configured.

## Create a key

1. Open [Google AI Studio API keys](https://aistudio.google.com/apikey).
2. Sign in and create an API key.
3. Configure it as `GEMINI_API_KEY`.

## Handle keys safely

Never repeat, quote, print, log, or include a key in a response, transcript, command argument, source-controlled file, or tool output. Do not confirm a key by showing any part of it.

Prefer asking the user to enter the key locally. If the user explicitly gives a key to the agent, do not echo it; write it only to the installed skill's `scripts/.env`, ensure that file remains untracked, and report only that the credential was stored.

The installed `.env` path is:

- Windows: `%USERPROFILE%\.codex\skills\video-analyze\scripts\.env`
- macOS/Linux: `~/.codex/skills/video-analyze/scripts/.env`

The file may contain:

```dotenv
GEMINI_API_KEY=<enter locally>
```

Environment variables take precedence over `.env` values. An invocation-root `.env` is checked before this skill's `scripts/.env`.

## Give the command for the user's OS

Detect the operating system before offering a command. Never put a literal key in a command because shell history and process inspection can expose it.

For Windows PowerShell, use a hidden prompt for the current session:

```powershell
$secure = Read-Host 'Gemini API key' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:GEMINI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
```

For macOS or Linux, use a silent prompt for the current shell:

```bash
read -rsp 'Gemini API key: ' GEMINI_API_KEY; export GEMINI_API_KEY; printf '\n'
```

For persistent setup, recommend editing the installed `.env` locally instead of embedding a secret in `setx`, shell-profile, or command-history text.

After setup, check only whether `GEMINI_API_KEY` is nonempty. Never display its value. Then continue the original video-analysis task without loading this file again.
