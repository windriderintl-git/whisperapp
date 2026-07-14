---
name: release
description: Bump the Whisper2 version and build the Windows installer. Use when the user wants to cut a release or rebuild the installer.
disable-model-invocation: true
---

Cut a Whisper2 release. `$ARGUMENTS` may contain the new version (e.g. `2.2.4`) — if absent, read the current version and propose the next patch version, confirming with the user before proceeding.

1. Read the current version from `installer/Whisper2.iss` (`#define MyAppVersion "X.Y.Z"`, near the top). This is the ONLY version location in the repo — do not hunt for others.
2. Confirm the working tree is clean and tests pass (`pytest tests/`, or `python tests/test_commands.py` and `python tests/test_snippets.py` if pytest isn't installed).
3. Edit `#define MyAppVersion` in `installer/Whisper2.iss` to the new version.
4. Run `build/build.bat` from the repo root. It renders icons, runs PyInstaller (`build/Whisper2.spec`), then Inno Setup. Requires Inno Setup 6 (`ISCC.exe`) on PATH or in its default install location; if ISCC is missing, stop and tell the user rather than improvising.
5. Verify `dist/installer/Whisper2-Setup.exe` exists and is newly modified. Report its size and timestamp.
6. Commit in the repo's convention: a `vX.Y.Z: <summary of what changed>` commit if uncommitted feature work is being released, and the version bump itself as `Bump installer version to X.Y.Z`. Do not push unless asked.

If the build fails, show the failing tool's output (PyInstaller vs ISCC) and stop — do not retry blindly; DLLs may be locked by a running Whisper2.exe or Ollama child process (taskkill `Whisper2.exe` and `ollama*` processes is the known fix, but confirm with the user before killing processes).
