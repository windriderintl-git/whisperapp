import importlib.util
import os
import sys

import logging
log = logging.getLogger("whisper2.transcribe")


def _find_nvidia_bin_dirs() -> list[str]:
    """Locate site-packages/nvidia/{cublas,cudnn,...}/bin/ for installed wheels.

    Robust to namespace packages: many `nvidia.*` subpackages have no
    __file__, so we use find_spec().submodule_search_locations.
    """
    if sys.platform != "win32":
        return []
    candidates: list[str] = []

    # Direct lookup of each subpackage.
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime",
                     "nvidia.cuda_nvrtc", "nvidia.nvjitlink"):
        try:
            spec = importlib.util.find_spec(pkg_name)
        except (ImportError, ValueError):
            spec = None
        if spec and spec.submodule_search_locations:
            candidates.extend(spec.submodule_search_locations)

    # Fallback: walk the `nvidia` namespace package and look for known subdirs.
    if not candidates:
        try:
            spec = importlib.util.find_spec("nvidia")
            if spec and spec.submodule_search_locations:
                for base in spec.submodule_search_locations:
                    for sub in ("cublas", "cudnn", "cuda_runtime",
                                "cuda_nvrtc", "nvjitlink"):
                        candidates.append(os.path.join(base, sub))
        except (ImportError, ValueError):
            pass

    bin_dirs: list[str] = []
    for loc in candidates:
        bin_dir = os.path.join(loc, "bin")
        if os.path.isdir(bin_dir) and bin_dir not in bin_dirs:
            bin_dirs.append(bin_dir)

    # Frozen builds have no site-packages — the first-run wizard drops CUDA
    # DLLs into {install_dir}\cuda\bin\ instead. Check there too.
    try:
        from paths import CUDA_BIN_DIR
        cuda_extra = str(CUDA_BIN_DIR)
        if os.path.isdir(cuda_extra) and cuda_extra not in bin_dirs:
            bin_dirs.append(cuda_extra)
    except ImportError:
        pass

    return bin_dirs


def _setup_cuda_dlls() -> tuple[list[str], list[str], list[str]]:
    """Make CUDA DLLs loadable by ctranslate2's native code on Windows.

    Three things, because Python 3.8+ neutered the default DLL search path
    and `os.add_dll_directory()` only helps callers that pass
    LOAD_LIBRARY_SEARCH_USER_DIRS — which native C++ libraries often don't:

    1. os.add_dll_directory(bin_dir)   -- helps Python's loader
    2. prepend bin_dir to PATH         -- legacy fallback some libs use
    3. ctypes.WinDLL(full_path)        -- preload the DLL into the process,
       so any later LoadLibrary("cublas64_12.dll") just returns the handle
       of the already-mapped module without searching at all.

    Returns (registered_dirs, preloaded, failed).
    """
    if sys.platform != "win32":
        return [], [], []
    bin_dirs = _find_nvidia_bin_dirs()
    registered: list[str] = []
    preloaded: list[str] = []
    failed: list[str] = []

    for bin_dir in bin_dirs:
        try:
            os.add_dll_directory(bin_dir)
            registered.append(bin_dir)
        except (OSError, AttributeError) as e:
            log.warning(f"[gpu] add_dll_directory failed for {bin_dir}: {e}")
        # Prepend to PATH so plain LoadLibrary by name also finds them.
        if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    # Preload critical DLLs by full path. Order matters: dependencies first.
    # cublas depends on cudart and nvjitlink; cudnn depends on cublas + cudart.
    preload_order = [
        "cudart64_12.dll",
        "nvJitLink_120_0.dll",
        "nvrtc64_120_0.dll",
        "cublasLt64_12.dll",
        "cublas64_12.dll",
        "cudnn64_9.dll",
        "cudnn_graph64_9.dll",
        "cudnn_ops64_9.dll",
        "cudnn_engines_precompiled64_9.dll",
        "cudnn_engines_runtime_compiled64_9.dll",
        "cudnn_heuristic64_9.dll",
        "cudnn_adv64_9.dll",
        "cudnn_cnn64_9.dll",
    ]
    import ctypes
    for dll_name in preload_order:
        for bin_dir in bin_dirs:
            full = os.path.join(bin_dir, dll_name)
            if os.path.isfile(full):
                try:
                    ctypes.WinDLL(full)
                    preloaded.append(dll_name)
                except OSError as e:
                    failed.append(f"{dll_name}: {e}")
                break  # don't try other bin dirs for the same DLL
    return registered, preloaded, failed


try:
    _reg, _pre, _fail = _setup_cuda_dlls()
    if _reg:
        log.info(f"[gpu] registered {len(_reg)} NVIDIA DLL dir(s)")
        for d in _reg:
            log.info(f"       {d}")
    elif sys.platform == "win32":
        log.info("[gpu] no nvidia-* CUDA wheels found in site-packages")
    if _pre:
        log.info(f"[gpu] preloaded {len(_pre)} DLL(s): {', '.join(_pre)}")
    if _fail:
        log.warning(f"[gpu] {len(_fail)} DLL(s) failed to preload:")
        for f in _fail:
            log.warning(f"       {f}")
except Exception as e:
    log.warning(f"[gpu] DLL discovery raised {type(e).__name__}: {e}")

from faster_whisper import WhisperModel  # noqa: E402  (must come after DLL setup)


class Transcriber:
    def __init__(self, model_size: str = "small.en",
                 device: str = "auto", compute_type: str = "auto",
                 beam_size: int = 1):
        self.beam_size = beam_size
        self.model = self._load(model_size, device, compute_type)

    @staticmethod
    def _resolve_compute(device: str, compute_type: str) -> str:
        if compute_type != "auto":
            return compute_type
        return "float16" if device == "cuda" else "int8"

    def _load(self, model_size, device, compute_type):
        if device == "auto":
            try:
                ct = self._resolve_compute("cuda", compute_type)
                log.info(f"[whisper] trying CUDA + {ct}...")
                m = WhisperModel(model_size, device="cuda", compute_type=ct)
                # Force a tiny inference to surface DLL-load problems early.
                import numpy as np
                _ = list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
                log.info("[whisper] loaded on GPU.")
                return m
            except Exception as e:
                log.warning(f"[whisper] CUDA unavailable ({e}); falling back to CPU.")
                ct = self._resolve_compute("cpu", compute_type)
                m = WhisperModel(model_size, device="cpu", compute_type=ct)
                log.info("[whisper] loaded on CPU.")
                return m
        ct = self._resolve_compute(device, compute_type)
        log.info(f"[whisper] loading {model_size} on {device} ({ct})...")
        m = WhisperModel(model_size, device=device, compute_type=ct)
        log.info("[whisper] loaded.")
        return m

    def transcribe(self, audio_data, initial_prompt: str | None = None) -> str:
        segments, _info = self.model.transcribe(
            audio_data,
            beam_size=self.beam_size,
            initial_prompt=initial_prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()
