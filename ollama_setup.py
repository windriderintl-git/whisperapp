"""Bootstrapping for Ollama: detection, silent install, model pull."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("whisper2.ollama_setup")

OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
API_HOST = "http://localhost:11434"

ProgressFn = "Callable[[str, int, int], None]"


def is_installed() -> bool:
    return shutil.which("ollama") is not None


def is_running(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{API_HOST}/api/version", timeout=timeout):
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False
    except Exception:
        return False


def start_serve_detached() -> None:
    """If installed but not running, kick off `ollama serve` as a detached child."""
    if not is_installed():
        return
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as e:
        log.warning(f"[ollama] start_serve_detached failed: {e}")


def wait_until_running(timeout_s: float = 90.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(2.0)
    return False


def download_installer(progress: ProgressFn | None = None) -> Path:
    # Fresh private directory, not the shared %TEMP% root: a predictable path
    # there could be pre-planted or swapped by another local process.
    dest = Path(tempfile.mkdtemp(prefix="whisper2-ollama-")) / "OllamaSetup.exe"
    log.info(f"[ollama] downloading installer to {dest}")
    with urllib.request.urlopen(OLLAMA_INSTALLER_URL, timeout=60.0) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress("Ollama installer", got, total)
    return dest


def _verify_authenticode(path: Path) -> None:
    """Refuse to run the installer unless its Authenticode signature is valid
    and the signer is Ollama. Get-AuthenticodeSignature is a cmdlet, so it
    works regardless of PowerShell execution policy."""
    ps = (
        f"$sig = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "$sig.Status.ToString(); "
        "if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        status = lines[0] if lines else ""
        subject = lines[1] if len(lines) > 1 else ""
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not verify Ollama installer signature: {e}")
    if result.returncode != 0 or status != "Valid" or "ollama" not in subject.lower():
        raise RuntimeError(
            "Ollama installer failed signature verification "
            f"(status={status or 'unknown'}, signer={subject or 'unknown'}). "
            "Refusing to run it. Install Ollama manually from https://ollama.com."
        )
    log.info(f"[ollama] installer signature valid ({subject})")


def install_silent(installer_path: Path | None = None,
                   progress: ProgressFn | None = None) -> None:
    """Download (if needed), verify the signature, and run OllamaSetup.exe
    /SILENT. Blocks until done."""
    p = installer_path or download_installer(progress=progress)
    _verify_authenticode(p)
    log.info(f"[ollama] running {p.name} /SILENT")
    subprocess.run([str(p), "/SILENT"], check=True)
    if not wait_until_running(90.0):
        raise RuntimeError("Ollama install completed but service did not start within 90s")


def model_exists(name: str) -> bool:
    try:
        with urllib.request.urlopen(f"{API_HOST}/api/tags", timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return any(m.get("name") == name for m in data.get("models", []))
    except Exception:
        return False


def pull_model(name: str, progress: ProgressFn | None = None) -> None:
    """Stream POST /api/pull and surface progress."""
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_HOST}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=None) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = evt.get("status", "")
            total = int(evt.get("total") or 0)
            done = int(evt.get("completed") or 0)
            if progress:
                progress(f"{name}: {status}", done, total)
            if evt.get("error"):
                raise RuntimeError(f"Ollama pull error: {evt['error']}")
            if status == "success":
                return
    log.info(f"[ollama] pulled {name}")
