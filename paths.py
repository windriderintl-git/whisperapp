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
# Download cache for first-run artifacts (CUDA wheels). Lives under the user
# profile, not the shared %TEMP%, so other local users can't pre-plant files.
DOWNLOAD_CACHE_DIR = USER_DATA_DIR / "cache"
CONFIG_PATH = USER_DATA_DIR / "config.yaml"
FIRSTRUN_FLAG = USER_DATA_DIR / ".firstrun_done"
FIRSTRUN_PROG = USER_DATA_DIR / ".firstrun_progress.json"

# First-run wizard drops downloaded CUDA DLLs here; transcribe.py picks them up.
CUDA_BIN_DIR = INSTALL_DIR / "cuda" / "bin"

# Bundled default config (ships in the install dir / lives next to repo files in dev).
DEFAULT_CONFIG = INSTALL_DIR / "config.yaml"

# Prompt directories.
#   BUNDLED_PROMPTS_DIR — ships with the install (read-only from the user's POV).
#   USER_PROMPTS_DIR    — user-editable copy under %APPDATA%; overrides bundled.
USER_PROMPTS_DIR = USER_DATA_DIR / "prompts"
BUNDLED_PROMPTS_DIR = INSTALL_DIR / "prompts"


def ensure_user_dirs() -> None:
    """Create %APPDATA%\\Whisper2\\... and seed config.yaml from the bundled
    template if the user doesn't yet have one. Also seeds the user prompts dir."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and DEFAULT_CONFIG.exists() and DEFAULT_CONFIG != CONFIG_PATH:
        try:
            shutil.copyfile(DEFAULT_CONFIG, CONFIG_PATH)
        except OSError:
            pass
    ensure_user_prompts()


def ensure_user_prompts() -> None:
    """Seed %APPDATA%\\Whisper2\\prompts\\ from the bundled prompts, and keep
    UNEDITED copies in sync with the bundled versions across upgrades.

    A manifest records the hash each file had when we last seeded it. On a
    later run, a user file whose hash still matches its seeded hash was never
    edited by the user, so it's safe to refresh with the new bundled version.
    User-edited files are never touched. Cheap and self-healing — safe to
    call on every startup."""
    import hashlib
    import json

    USER_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    if not BUNDLED_PROMPTS_DIR.is_dir():
        return

    manifest_path = USER_PROMPTS_DIR / ".seeded_hashes.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            manifest = {}
    except (OSError, ValueError):
        manifest = {}

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    changed = False
    for src in BUNDLED_PROMPTS_DIR.glob("*.md"):
        dst = USER_PROMPTS_DIR / src.name
        try:
            src_hash = _sha(src)
            if not dst.exists():
                shutil.copyfile(src, dst)
                manifest[src.name] = src_hash
                changed = True
                continue
            dst_hash = _sha(dst)
            if dst_hash == src_hash:
                # Already current; make sure the manifest knows it.
                if manifest.get(src.name) != src_hash:
                    manifest[src.name] = src_hash
                    changed = True
            elif manifest.get(src.name) == dst_hash:
                # Unedited stale seed — refresh to the new bundled version.
                shutil.copyfile(src, dst)
                manifest[src.name] = src_hash
                changed = True
            # else: user-edited (or pre-manifest legacy) — leave it alone.
        except OSError:
            pass

    if changed:
        try:
            manifest_path.write_text(
                json.dumps(manifest, indent=0, sort_keys=True),
                encoding="utf-8")
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
