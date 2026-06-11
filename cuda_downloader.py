"""Fetch NVIDIA CUDA runtime DLLs from PyPI wheels (no pip required)."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
import zipfile
from pathlib import Path

import paths

log = logging.getLogger("whisper2.cuda_downloader")

# nvidia-* wheels are platform-tagged py3-none-win_amd64, version-agnostic.
WHEELS = [
    "nvidia-cuda-runtime-cu12",
    "nvidia-cuda-nvrtc-cu12",
    "nvidia-nvjitlink-cu12",
    "nvidia-cublas-cu12",
    "nvidia-cudnn-cu12",          # cuDNN 9.x
]

ProgressFn = "Callable[[str, int, int], None]"  # (label, bytes_done, bytes_total)


def fetch_all(target_dir: Path, progress: ProgressFn | None = None) -> None:
    """Download every wheel in WHEELS and extract DLLs into target_dir."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in WHEELS:
        log.info(f"[cuda] downloading {name}")
        url, size, sha256 = _resolve_wheel_url(name)
        local_zip = _download(url, expected_size=size, expected_sha256=sha256,
                              label=f"{name} ({_fmt_mb(size)})",
                              progress=progress)
        try:
            _extract_dlls(local_zip, target_dir)
        finally:
            try:
                local_zip.unlink()
            except OSError:
                pass


def _resolve_wheel_url(pkg_name: str) -> tuple[str, int, str]:
    """Return (url, size_bytes, sha256) for the latest win_amd64 wheel of pkg_name."""
    meta_url = f"https://pypi.org/pypi/{pkg_name}/json"
    with urllib.request.urlopen(meta_url, timeout=20.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    latest_ver = data["info"]["version"]
    files = data["releases"].get(latest_ver, [])
    # Prefer py3-none-win_amd64; fall back to anything matching win_amd64.
    for entry in files:
        fn = entry["filename"]
        if fn.endswith("-py3-none-win_amd64.whl"):
            return entry["url"], int(entry.get("size") or 0), _entry_sha256(entry)
    for entry in files:
        fn = entry["filename"]
        if "win_amd64" in fn and fn.endswith(".whl"):
            return entry["url"], int(entry.get("size") or 0), _entry_sha256(entry)
    raise RuntimeError(f"No win_amd64 wheel for {pkg_name} {latest_ver}")


def _entry_sha256(entry: dict) -> str:
    return (entry.get("digests") or {}).get("sha256", "")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, expected_size: int, expected_sha256: str, label: str,
              progress: ProgressFn | None) -> Path:
    """Stream to the per-user download cache; verify SHA256 against the PyPI
    digest both for fresh downloads and cache hits (a same-size file is not
    proof of integrity)."""
    paths.DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = paths.DOWNLOAD_CACHE_DIR / Path(url).name
    if tmp.exists():
        if expected_sha256 and _file_sha256(tmp) == expected_sha256:
            log.info(f"[cuda] cached {tmp.name} (sha256 ok)")
            if progress:
                progress(label, expected_size, expected_size)
            return tmp
        log.warning(f"[cuda] cached {tmp.name} failed verification; re-downloading")
        tmp.unlink(missing_ok=True)
    with urllib.request.urlopen(url, timeout=60.0) as resp:
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        got = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(label, got, total)
    if expected_sha256:
        actual = _file_sha256(tmp)
        if actual != expected_sha256:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA256 mismatch for {tmp.name}: expected {expected_sha256}, "
                f"got {actual}. Download corrupted or tampered with."
            )
    else:
        log.warning(f"[cuda] no PyPI digest available for {tmp.name}; skipping verification")
    return tmp


def _extract_dlls(wheel_path: Path, target_dir: Path) -> None:
    """Pull every .dll from nvidia/*/bin/ inside the wheel into target_dir (flat)."""
    with zipfile.ZipFile(wheel_path) as zf:
        for member in zf.namelist():
            # Wheels have layout nvidia/<pkg>/bin/<dll>
            parts = member.split("/")
            if len(parts) >= 4 and parts[0] == "nvidia" and parts[2] == "bin" \
                    and member.lower().endswith(".dll"):
                target = target_dir / parts[-1]
                with zf.open(member) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)
                log.info(f"[cuda]   extracted {parts[-1]}")


def _fmt_mb(n: int) -> str:
    if not n:
        return "?"
    return f"{n / (1024 * 1024):.0f} MB"
