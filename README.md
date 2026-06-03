# Whisper 2.0 — Realtime Dictation

Local push-to-talk dictation with LLM polish and context-aware tone, all offline. No subscriptions, no cloud calls.

## What it does

1. You hold (or double-tap) a hotkey, speak, release.
2. `faster-whisper` transcribes locally.
3. A local Ollama model (`qwen2.5:3b`) cleans the transcript — strips filler, fixes punctuation, breaks into paragraphs.
4. The active window decides the cleanup tone (formal for Outlook/Gmail, casual for Slack/Discord/Teams, concise/technical for VS Code/Cursor/JetBrains).
5. Text is pasted into the focused field.

If Ollama is unreachable, you get raw Whisper output — the app degrades gracefully.

## Hotkey

| Action | What it does |
|---|---|
| **Hold** `Ctrl + Win` | Push-to-talk. Record while held, transcribe on release. |
| **Double-tap** `Ctrl + Win` (within 400ms) | Toggle continuous mode. Double-tap again to stop. |

A short single tap by itself does nothing. Thresholds live in `config.yaml`.

## Quick start

1. Install Python from <https://www.python.org/downloads/> (check "Add Python to PATH").
2. Install Ollama from <https://ollama.com/>.
3. Double-click `run.bat`. First run installs Python deps, pulls `qwen2.5:3b` (~2 GB), and downloads the Whisper `small.en` model (~500 MB).

After that, `run.bat` starts instantly.

## Configuration

Everything is in `config.yaml`. Common changes:

- **Output mode** — `type` (default, pastes into focused window), `clipboard` (just copies), `terminal` (prints to console).
- **Whisper model** — `small.en` (default, balanced) → `medium.en` for higher accuracy at the cost of latency.
- **GPU** — leave `device: auto`; it picks CUDA if available.
- **Disable LLM polish** — `llm.enabled: false`, or run `python main.py --no-llm`.
- **Vocabulary** — add canonical spellings under `vocabulary:`. Variants are regex-escaped and replaced case-insensitively after polish.

## Context-aware prompts

`prompts/` ships with four cleanup prompts:

| Prompt | Triggered by window title containing… |
|---|---|
| `cleanup_email` | Outlook, Gmail, Thunderbird, Mailbird, Spark, Proton/Fastmail, Apple Mail |
| `cleanup_chat` | Slack, Discord, Microsoft Teams, Telegram, WhatsApp, Signal, Messenger |
| `cleanup_code` | VS Code, Cursor, PyCharm, IntelliJ, WebStorm, GoLand, Rider, Sublime, Neovim, Vim, Zed, Xcode |
| `cleanup_default` | Anything else |

Edit the rules in `context.py` to add apps. Edit the `.md` files in `prompts/` to change voice. Force a single prompt via `context.override` in the config.

## Project layout

```
Whisper2.0/
  config.yaml          all tunables
  main.py              app entry, audio consumer, output routing
  hotkey.py            Ctrl+Win combo controller (hold + double-tap)
  audio.py             mic capture (single-shot PTT + silence-chunked continuous)
  transcribe.py        faster-whisper wrapper, auto CPU/CUDA
  llm.py               Ollama HTTP client + prompt loader
  context.py           active-window -> prompt rule table
  prompts/             cleanup_*.md prompt files
  requirements.txt     pinned-loose Python deps
  run.bat / run-terminal.bat / setup.bat
```

## Transferring to another PC

It's still portable. Copy the folder, install Python + Ollama on the target, run `run.bat`. Dependencies and models auto-install on first run.

## Troubleshooting

- **Nothing happens when I press the hotkey** — the `keyboard` library needs to see global key events; on locked-down systems, run `run.bat` as administrator.
- **`[llm] Ollama unreachable`** — make sure `ollama serve` is running (the installer usually adds a tray app that starts it). The app still works, you just lose polish.
- **Slow LLM polish** — try `llama3.2:3b` (`config.yaml` → `llm.model`). On CPU, expect 300–800 ms; on a recent GPU, sub-200 ms.
- **Wrong tone** — edit `prompts/cleanup_*.md`, or `context.override: cleanup_default` to disable context routing.
