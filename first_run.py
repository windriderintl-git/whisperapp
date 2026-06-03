"""First-run wizard: detect GPU + Ollama, download what's missing, then hand off."""
import json
import logging
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox
from pathlib import Path
from tkinter import ttk

import paths
import cuda_downloader
import ollama_setup

log = logging.getLogger("whisper2.first_run")

WHISPER_MODEL_DEFAULT = "small.en"
OLLAMA_MODEL_DEFAULT = "qwen2.5:3b"


def run_if_needed() -> bool:
    """Show the wizard if not previously completed. Returns True if OK to launch tray,
    False if the user closed the wizard early."""
    paths.ensure_user_dirs()
    if paths.FIRSTRUN_FLAG.exists():
        return True
    completed = _show_wizard()
    if completed:
        try:
            paths.FIRSTRUN_FLAG.touch()
        except OSError as e:
            log.warning(f"[firstrun] could not write flag: {e}")
    return completed


def _show_wizard() -> bool:
    state = _Progress.load()
    wizard = _Wizard(state)
    wizard.run()
    return wizard.finished


# ---------------------------------------------------------------------------
# Progress state
# ---------------------------------------------------------------------------

class _Progress:
    """Resumable wizard state stored as JSON in paths.FIRSTRUN_PROG."""

    def __init__(self):
        self.gpu_wheels_done = False
        self.ollama_installed = False
        self.ollama_model_pulled = False
        self.whisper_preloaded = False
        self.want_gpu = False
        self.want_ollama = True

    @classmethod
    def load(cls) -> "_Progress":
        p = cls()
        if paths.FIRSTRUN_PROG.exists():
            try:
                data = json.loads(paths.FIRSTRUN_PROG.read_text())
                for k, v in data.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
            except Exception as e:
                log.warning(f"[firstrun] could not load progress: {e}")
        return p

    def save(self) -> None:
        try:
            paths.FIRSTRUN_PROG.write_text(json.dumps(self.__dict__, indent=2))
        except Exception as e:
            log.warning(f"[firstrun] could not save progress: {e}")


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class _Wizard:
    """Tk-based multi-page first-run wizard."""

    def __init__(self, state: _Progress):
        self.state = state
        self.finished = False
        self.gpu_name: str | None = None

        self.root = tk.Tk()
        self.root.title("Whisper 2 — First Run")
        self.root.geometry("560x380")
        self.root.minsize(520, 360)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Container for swappable page frames.
        self.container = ttk.Frame(self.root, padding=16)
        self.container.pack(fill="both", expand=True)
        self.current_frame: ttk.Frame | None = None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        self._show_welcome()
        self.root.mainloop()

    def _on_close(self) -> None:
        # User closed window before finishing — preserve progress, leave flag absent.
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _swap(self, builder) -> None:
        """Destroy current frame, create a fresh one, and call builder(frame)."""
        if self.current_frame is not None:
            try:
                self.current_frame.destroy()
            except tk.TclError:
                pass
        self.current_frame = ttk.Frame(self.container)
        self.current_frame.pack(fill="both", expand=True)
        builder(self.current_frame)

    # -- page 1: welcome ---------------------------------------------------

    def _show_welcome(self) -> None:
        def build(frame: ttk.Frame) -> None:
            ttk.Label(frame, text="Welcome to Whisper 2",
                      font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(8, 12))
            ttk.Label(
                frame,
                text=("This quick setup will detect your hardware, download a few\n"
                      "components, and prepare Whisper 2 for daily use."),
                justify="left",
            ).pack(anchor="w", pady=(0, 16))
            ttk.Label(
                frame,
                text="It should take a few minutes on a typical broadband connection.",
                foreground="#555",
            ).pack(anchor="w")

            btn_row = ttk.Frame(frame)
            btn_row.pack(side="bottom", fill="x", pady=(16, 0))
            ttk.Button(btn_row, text="Next >",
                       command=self._show_gpu).pack(side="right")

        self._swap(build)

    # -- page 2: gpu detection --------------------------------------------

    def _detect_gpu(self) -> str | None:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        name = (result.stdout or "").strip().splitlines()
        return name[0].strip() if name and name[0].strip() else None

    def _show_gpu(self) -> None:
        self.gpu_name = self._detect_gpu()
        want_gpu_var = tk.BooleanVar(value=self.state.want_gpu or bool(self.gpu_name))

        def build(frame: ttk.Frame) -> None:
            ttk.Label(frame, text="GPU detection",
                      font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(8, 12))

            if self.gpu_name:
                ttk.Label(
                    frame,
                    text=f"Detected NVIDIA GPU: {self.gpu_name}.",
                    justify="left",
                ).pack(anchor="w", pady=(0, 8))
                ttk.Label(
                    frame,
                    text=("Enable GPU acceleration?\n"
                          "(+1.5 GB download, ~5–10× faster transcription)"),
                    justify="left",
                ).pack(anchor="w", pady=(0, 8))
                ttk.Checkbutton(
                    frame,
                    text="Enable GPU acceleration",
                    variable=want_gpu_var,
                ).pack(anchor="w", pady=(0, 12))
            else:
                ttk.Label(
                    frame,
                    text="No NVIDIA GPU detected. CPU mode will be used.",
                    justify="left",
                ).pack(anchor="w", pady=(0, 12))
                want_gpu_var.set(False)

            btn_row = ttk.Frame(frame)
            btn_row.pack(side="bottom", fill="x", pady=(16, 0))
            ttk.Button(btn_row, text="< Back",
                       command=self._show_welcome).pack(side="left")

            def go_next() -> None:
                self.state.want_gpu = bool(want_gpu_var.get()) and bool(self.gpu_name)
                self.state.save()
                self._show_ollama()

            ttk.Button(btn_row, text="Next >",
                       command=go_next).pack(side="right")

        self._swap(build)

    # -- page 3: ollama detection -----------------------------------------

    def _show_ollama(self) -> None:
        # If already running, fast-forward.
        if ollama_setup.is_running():
            self.state.ollama_installed = True
            self.state.want_ollama = True
            self.state.save()
            self._show_downloads()
            return

        # Installed but not running — try to start it, then fast-forward if it comes up.
        if ollama_setup.is_installed():
            ollama_setup.start_serve_detached()
            if ollama_setup.wait_until_running(15):
                self.state.ollama_installed = True
                self.state.want_ollama = True
                self.state.save()
                self._show_downloads()
                return

            def build(frame: ttk.Frame) -> None:
                ttk.Label(frame, text="Ollama",
                          font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(8, 12))
                ttk.Label(
                    frame,
                    text=("Ollama is installed but not responding.\n"
                          "The wizard will start it during the next step."),
                    justify="left",
                ).pack(anchor="w", pady=(0, 12))

                btn_row = ttk.Frame(frame)
                btn_row.pack(side="bottom", fill="x", pady=(16, 0))
                ttk.Button(btn_row, text="< Back",
                           command=self._show_gpu).pack(side="left")

                def go_next() -> None:
                    self.state.want_ollama = True
                    self.state.ollama_installed = True
                    self.state.save()
                    self._show_downloads()

                ttk.Button(btn_row, text="Next >",
                           command=go_next).pack(side="right")

            self._swap(build)
            return

        # Not installed — let the user choose.
        def build(frame: ttk.Frame) -> None:
            ttk.Label(frame, text="Ollama",
                      font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(8, 12))
            ttk.Label(
                frame,
                text=("Ollama is needed to clean up transcripts.\n"
                      "(~600 MB install)"),
                justify="left",
            ).pack(anchor="w", pady=(0, 16))

            btn_row = ttk.Frame(frame)
            btn_row.pack(side="bottom", fill="x", pady=(16, 0))
            ttk.Button(btn_row, text="< Back",
                       command=self._show_gpu).pack(side="left")

            def install_choice() -> None:
                self.state.want_ollama = True
                self.state.save()
                self._show_downloads()

            def skip_choice() -> None:
                self.state.want_ollama = False
                self.state.save()
                self._show_downloads()

            ttk.Button(btn_row, text="Install Ollama for me",
                       command=install_choice).pack(side="right", padx=(8, 0))
            ttk.Button(btn_row, text="Skip — raw transcripts only",
                       command=skip_choice).pack(side="right")

        self._swap(build)

    # -- page 4: downloads -------------------------------------------------

    def _show_downloads(self) -> None:
        status_var = tk.StringVar(value="Preparing...")
        progress_var = tk.DoubleVar(value=0.0)

        def build(frame: ttk.Frame) -> None:
            ttk.Label(frame, text="Setting things up",
                      font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(8, 12))
            ttk.Label(frame, textvariable=status_var,
                      wraplength=500, justify="left").pack(anchor="w", pady=(0, 8))
            bar = ttk.Progressbar(frame, mode="determinate", variable=progress_var,
                                  maximum=100.0, length=480)
            bar.pack(anchor="w", pady=(0, 16), fill="x")

            self._dl_status_var = status_var
            self._dl_progress_var = progress_var

            self._dl_btn_row = ttk.Frame(frame)
            self._dl_btn_row.pack(side="bottom", fill="x", pady=(16, 0))
            # Buttons appear only on error/completion.

        self._swap(build)
        # Run the queue on a background thread; UI updates marshalled via after().
        worker = threading.Thread(target=self._run_download_queue, daemon=True)
        worker.start()

    def _ui(self, fn, *args) -> None:
        """Schedule a callable on the Tk thread."""
        try:
            self.root.after(0, lambda: fn(*args))
        except RuntimeError:
            # Root destroyed; ignore.
            pass

    def _set_status(self, text: str) -> None:
        def apply() -> None:
            try:
                self._dl_status_var.set(text)
            except (AttributeError, tk.TclError):
                pass
        self._ui(apply)

    def _set_progress(self, done: int, total: int) -> None:
        def apply() -> None:
            try:
                if total > 0:
                    pct = max(0.0, min(100.0, (done / total) * 100.0))
                    self._dl_progress_var.set(pct)
                else:
                    # Indeterminate-ish: leave bar where it is, just nudge.
                    self._dl_progress_var.set(self._dl_progress_var.get())
            except (AttributeError, tk.TclError):
                pass
        self._ui(apply)

    def _progress_cb(self, label: str, done: int, total: int) -> None:
        suffix = ""
        if total > 0:
            suffix = f"  {done / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MB"
        elif done > 0:
            suffix = f"  {done / (1024 * 1024):.1f} MB"
        self._set_status(f"{label}{suffix}")
        self._set_progress(done, total)

    def _run_download_queue(self) -> None:
        """Background-thread driver for the task queue."""
        tasks = self._build_task_list()
        for idx, (label, fn, marker_attr) in enumerate(tasks):
            self._set_status(f"[{idx + 1}/{len(tasks)}] {label}")
            self._set_progress(0, 0)
            outcome = self._run_one_task(label, fn)
            if outcome == "ok":
                if marker_attr:
                    setattr(self.state, marker_attr, True)
                    self.state.save()
            elif outcome == "skip":
                # Don't mark complete — leave marker alone so future runs can retry.
                continue
            else:  # cancel
                self._ui(self._on_close)
                return
        self._ui(self._show_done)

    def _build_task_list(self) -> list[tuple[str, "callable", str | None]]:
        tasks: list[tuple[str, "callable", str | None]] = []

        if self.state.want_gpu and not self.state.gpu_wheels_done:
            tasks.append((
                "Downloading CUDA runtime (5 wheels)",
                lambda: cuda_downloader.fetch_all(paths.CUDA_BIN_DIR,
                                                  progress=self._progress_cb),
                "gpu_wheels_done",
            ))

        if self.state.want_ollama and not self.state.ollama_installed:
            def install_ollama() -> None:
                if not ollama_setup.is_installed():
                    ollama_setup.install_silent(progress=self._progress_cb)
            tasks.append((
                "Installing Ollama",
                install_ollama,
                "ollama_installed",
            ))

        if self.state.want_ollama:
            def ensure_service() -> None:
                if ollama_setup.is_running():
                    return
                ollama_setup.start_serve_detached()
                if not ollama_setup.wait_until_running(90.0):
                    raise RuntimeError("Ollama service did not start within 90s")
            tasks.append((
                "Starting Ollama service",
                ensure_service,
                None,
            ))

        if self.state.want_ollama and not self.state.ollama_model_pulled:
            def pull() -> None:
                if ollama_setup.model_exists(OLLAMA_MODEL_DEFAULT):
                    return
                ollama_setup.pull_model(OLLAMA_MODEL_DEFAULT,
                                        progress=self._progress_cb)
            tasks.append((
                f"Pulling LLM model ({OLLAMA_MODEL_DEFAULT})",
                pull,
                "ollama_model_pulled",
            ))

        if not self.state.whisper_preloaded:
            def preload() -> None:
                # Force CPU load to avoid CUDA setup races; this just triggers
                # faster_whisper's HF download.
                from transcribe import Transcriber  # local import: keep wizard light
                Transcriber(model_size=WHISPER_MODEL_DEFAULT,
                            device="cpu", compute_type="int8")
            tasks.append((
                f"Preloading Whisper model ({WHISPER_MODEL_DEFAULT})",
                preload,
                "whisper_preloaded",
            ))

        return tasks

    def _run_one_task(self, label: str, fn) -> str:
        """Run a task with retry/skip/cancel on error. Returns 'ok' | 'skip' | 'cancel'."""
        while True:
            try:
                fn()
                return "ok"
            except Exception as e:
                log.exception(f"[firstrun] task failed: {label}")
                choice = self._prompt_error(label, e)
                if choice == "retry":
                    continue
                return choice  # 'skip' or 'cancel'

    def _prompt_error(self, label: str, err: Exception) -> str:
        """Block the worker thread on a Tk-thread dialog. Returns retry/skip/cancel."""
        result: dict[str, str] = {}
        done_evt = threading.Event()

        def ask() -> None:
            msg = (f"Step failed: {label}\n\n"
                   f"{err}\n\n"
                   "Retry, skip this step, or cancel the wizard?")
            # Use a custom dialog with three buttons.
            dlg = tk.Toplevel(self.root)
            dlg.title("Setup error")
            dlg.transient(self.root)
            dlg.grab_set()
            ttk.Label(dlg, text=msg, wraplength=420,
                      justify="left").pack(padx=16, pady=16)
            row = ttk.Frame(dlg)
            row.pack(padx=16, pady=(0, 16), fill="x")

            def choose(value: str) -> None:
                result["v"] = value
                try:
                    dlg.destroy()
                except tk.TclError:
                    pass
                done_evt.set()

            ttk.Button(row, text="Retry",
                       command=lambda: choose("retry")).pack(side="left")
            ttk.Button(row, text="Skip step",
                       command=lambda: choose("skip")).pack(side="left", padx=8)
            ttk.Button(row, text="Cancel",
                       command=lambda: choose("cancel")).pack(side="right")
            dlg.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))

        self._ui(ask)
        done_evt.wait()
        return result.get("v", "cancel")

    # -- page 5: done ------------------------------------------------------

    def _show_done(self) -> None:
        def build(frame: ttk.Frame) -> None:
            ttk.Label(frame, text="You're set",
                      font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(8, 12))
            ttk.Label(
                frame,
                text=("The Whisper 2 tray icon is now active.\n"
                      "Hold Ctrl+Win to dictate."),
                justify="left",
            ).pack(anchor="w", pady=(0, 16))

            btn_row = ttk.Frame(frame)
            btn_row.pack(side="bottom", fill="x", pady=(16, 0))

            def finish() -> None:
                self.finished = True
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass

            ttk.Button(btn_row, text="Finish",
                       command=finish).pack(side="right")

        self._swap(build)
