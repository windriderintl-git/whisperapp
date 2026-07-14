# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Windows-only, fully local push-to-talk dictation app. Hold Ctrl+Win → faster-whisper transcribes → local Ollama (`qwen2.5:3b`) polishes → text pasted into the focused window. Degrades to raw Whisper output when Ollama is down.

## Commands

- Run (dev): `run.bat` (installs deps + pulls Ollama model on first run), or `python main.py` directly. Tray/GUI entry: `python tray_app.py`.
- Tests: `pytest tests/` or standalone `python tests/test_commands.py` (each test file has a `__main__` runner that prints "OK"). pytest is NOT in requirements.txt — install it separately.
- Build installer: `build/build.bat` — renders icons, runs PyInstaller (`build/Whisper2.spec`), then Inno Setup (`installer/Whisper2.iss`) → `dist/installer/Whisper2-Setup.exe`. Requires Inno Setup 6 (`ISCC.exe`).
- No linter or formatter is configured.

## Entry points

- `main.py` — app core + CLI (`--config`, `--mode {type,terminal,clipboard}`, `--no-llm`, `--debug`). What run.bat launches in dev.
- `tray_app.py` — the frozen/packaged entry point (per `build/Whisper2.spec`); imports `App` from `main.py`.

## Versioning & releases

- The version lives in exactly ONE place: `#define MyAppVersion` in `installer/Whisper2.iss`. There is no `__version__` anywhere in Python. Bump it there for a release.
- Commit convention: `vX.Y.Z: <description>` for feature/fix commits; branches are `feat/...` off `main`.
- Use `/release` to run the full bump-and-build flow.

## Gotchas

- Frozen vs dev paths (`paths.py`): a frozen install reads/writes config at `%APPDATA%\Whisper2\config.yaml` and prompts at `%APPDATA%\Whisper2\prompts\` (user copies override bundled); dev mode uses the repo's `config.yaml` and `prompts/`. Installer seeds these `onlyifdoesntexist` so user settings survive upgrades — don't assume the repo config is what a user is running.
- CUDA is NOT bundled: `Whisper2.spec` excludes all `nvidia.*` packages; the first-run wizard downloads CUDA DLLs into `{install_dir}/cuda/bin/` at runtime (`cuda_downloader.py`). GPU path requires Python ≤3.12.
- DLL locking: Ollama child processes can lock the app's DLLs; the installer taskkills `Whisper2.exe` before install/uninstall. Be careful changing Ollama process spawning (`ollama_setup.py`) — v2.2.2/v2.2.3 fixed console-window storms and lock issues there.
- Runtime deps: Ollama at `http://localhost:11434`, Whisper `small.en` (~500MB) and `qwen2.5:3b` (~2GB) auto-download on first run.
- `WHISPER2_DEBUG=1` enables debug logging.
- Windows APIs throughout (pywin32, winsound, keyboard global hooks) — code is not portable and doesn't need to be.
