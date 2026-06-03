"""Filesystem locations for Whisper 2.

Single source of truth: dev (running from the repo) vs frozen (PyInstaller
build inside Program Files / LocalAppData) handled in one place.
"""
import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Whisper2"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _install_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


INSTALL_DIR = _install_dir()
# User-writable area for config, logs, first-run progress.
USER_DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
LOG_DIR = USER_DATA_DIR / "logs"
CONFIG_PATH = USER_DATA_DIR / "config.yaml"
FIRSTRUN_FLAG = USER_DATA_DIR / ".firstrun_done"
FIRSTRUN_PROG = USER_DATA_DIR / ".firstrun_progress.json"

# First-run wizard drops downloaded CUDA DLLs here; transcribe.py picks them up.
CUDA_BIN_DIR = INSTALL_DIR / "cuda" / "bin"

# Bundled default config (ships in the install dir / lives next to repo files in dev).
DEFAULT_CONFIG = INSTALL_DIR / "config.yaml"


def ensure_user_dirs() -> None:
    """Create %APPDATA%\\Whisper2\\... and seed config.yaml from the bundled
    template if the user doesn't yet have one."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and DEFAULT_CONFIG.exists() and DEFAULT_CONFIG != CONFIG_PATH:
        try:
            shutil.copyfile(DEFAULT_CONFIG, CONFIG_PATH)
        except OSError:
            pass


def resolve_config_path() -> Path:
    """Pick the config path appropriate for this launch.
    - Frozen install: %APPDATA%\\Whisper2\\config.yaml (user-editable).
    - Dev: the repo's config.yaml (existing behavior).
    """
    if is_frozen():
        ensure_user_dirs()
        return CONFIG_PATH
    return DEFAULT_CONFIG
