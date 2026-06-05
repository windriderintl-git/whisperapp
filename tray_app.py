"""Whisper 2.0 system-tray entry point."""
import os
import sys
import threading
import tkinter as tk
import tkinter.messagebox
import yaml
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

import logging_config
import paths

# logging_config.setup() must run before anything else.
logging_config.setup()
import logging
log = logging.getLogger("whisper2.tray")

from main import App
import first_run
import settings_ui


def _make_icon(color: tuple[int, int, int], filled: bool = True) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if filled:
        d.ellipse((8, 8, 56, 56), fill=color + (255,))
    else:
        d.ellipse((8, 8, 56, 56), outline=color + (255,), width=4)
    # small mic-ish dot in the middle (just visual interest)
    d.ellipse((28, 28, 36, 36), fill=(255, 255, 255, 255) if filled else color + (255,))
    return img


ICONS = {
    "idle":            _make_icon((90, 90, 90)),                 # gray
    "recording":       _make_icon((220, 50, 50)),                # red
    "transcribing":    _make_icon((230, 160, 30)),               # amber
    "polishing":       _make_icon((230, 160, 30)),               # amber
    "paused":          _make_icon((140, 140, 140), filled=False),# gray ring
    "degraded:ollama": _make_icon((230, 200, 30)),               # yellow
    "no_mic":          _make_icon((220, 50, 50), filled=False),  # red ring
}

TOOLTIPS = {
    "idle":            "Whisper 2 — Ready. Hold Ctrl+Win to dictate.",
    "recording":       "Whisper 2 — Recording…",
    "transcribing":    "Whisper 2 — Transcribing…",
    "polishing":       "Whisper 2 — Polishing…",
    "paused":          "Whisper 2 — Paused",
    "degraded:ollama": "Whisper 2 — Ollama not running (raw transcripts)",
    "no_mic":          "Whisper 2 — Microphone unavailable",
}


class TrayController:
    def __init__(self, app: App):
        self.app = app
        self.state = "idle"
        self._degraded_notified = False
        self._no_mic_notified = False
        self.icon = pystray.Icon(
            "Whisper2",
            icon=ICONS["idle"],
            title=TOOLTIPS["idle"],
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _: f"Status: {self._status_label()}",
                             None, enabled=False),
            pystray.MenuItem(
                lambda _: "Resume" if self.app._paused else "Pause",
                self._on_toggle_pause,
            ),
            pystray.MenuItem(
                lambda _: ("Stop continuous mode"
                           if self.app.continuous_mode else "Start continuous mode"),
                self._on_toggle_continuous,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", self._on_settings),
            pystray.MenuItem("Open Log Folder", self._on_open_logs),
            pystray.MenuItem("Open Prompts Folder", self._on_open_prompts),
            pystray.MenuItem("About", self._on_about),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def _status_label(self) -> str:
        # human-readable
        return {
            "idle":            "Ready",
            "recording":       "Recording",
            "transcribing":    "Transcribing",
            "polishing":       "Polishing",
            "paused":          "Paused",
            "degraded:ollama": "Ollama down (raw)",
            "no_mic":          "Microphone unavailable",
        }.get(self.state, self.state)

    def on_status(self, state: str) -> None:
        """Called from any thread by App.status_callback."""
        self.state = state
        try:
            self.icon.icon = ICONS.get(state, ICONS["idle"])
            self.icon.title = TOOLTIPS.get(state, "Whisper 2")
            self.icon.update_menu()
        except Exception as e:
            log.warning(f"icon update failed: {e}")
        if state == "degraded:ollama" and not self._degraded_notified:
            self._degraded_notified = True
            try:
                self.icon.notify(
                    "Ollama isn't running. Transcripts will be raw (no LLM polish).",
                    "Whisper 2",
                )
            except Exception:
                pass
        if state == "no_mic" and not self._no_mic_notified:
            self._no_mic_notified = True
            try:
                self.icon.notify(
                    "Couldn't open the microphone. Check your input device "
                    "(Windows Settings → Sound) and try again.",
                    "Whisper 2",
                )
            except Exception:
                pass
        if state == "recording":
            # Mic is clearly working again — re-arm the no-mic balloon.
            self._no_mic_notified = False

    def _on_toggle_pause(self, icon, item):
        if self.app._paused:
            self.app.resume()
        else:
            self.app.pause()

    def _on_toggle_continuous(self, icon, item):
        try:
            self.app.on_double_tap()
        except Exception as e:
            log.exception(f"toggle continuous failed: {e}")

    def _on_settings(self, icon, item):
        # Tk has to run on its own thread when invoked from a pystray callback;
        # the settings dialog is modal and blocks until closed.
        def show():
            try:
                settings_ui.open(self.app)
            except Exception as e:
                log.exception(f"settings dialog crashed: {e}")
        threading.Thread(target=show, daemon=True).start()

    def _on_open_logs(self, icon, item):
        try:
            os.startfile(str(paths.LOG_DIR))
        except Exception as e:
            log.warning(f"open log folder failed: {e}")

    def _on_open_prompts(self, icon, item):
        try:
            paths.ensure_user_prompts()
            os.startfile(str(paths.USER_PROMPTS_DIR))
        except Exception as e:
            log.warning(f"open prompts folder failed: {e}")

    def _on_about(self, icon, item):
        def show():
            root = tk.Tk()
            root.withdraw()
            tk.messagebox.showinfo(
                "About Whisper 2",
                "Whisper 2.0\nLocal push-to-talk dictation with LLM polish.\n"
                f"Logs: {paths.LOG_DIR}",
            )
            root.destroy()
        threading.Thread(target=show, daemon=True).start()

    def _on_quit(self, icon, item):
        try:
            self.app.stop()
        finally:
            icon.stop()

    def run(self) -> None:
        self.icon.run()


def _load_config() -> dict:
    cfg_path = paths.resolve_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fatal_dialog(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        tk.messagebox.showerror("Whisper 2", message)
        root.destroy()
    except Exception:
        pass


def _info_dialog(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        tk.messagebox.showinfo("Whisper 2", message)
        root.destroy()
    except Exception:
        pass


def _acquire_single_instance_lock():
    """Claim a per-user named mutex. Returns the handle on success, or None
    if another Whisper 2 instance already owns it. Windows auto-releases the
    mutex when the process exits, so the returned handle just needs to stay
    referenced for the lifetime of the process."""
    try:
        import win32event
        import win32api
        import winerror
    except ImportError:
        return True  # pywin32 missing (dev shell?); skip the check
    handle = win32event.CreateMutex(None, False, "Whisper2-SingleInstance-v1")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def main():
    log.info("[tray] starting")

    # Single-instance guard. Held for the lifetime of the process; Windows
    # auto-releases on exit. Without this, a second launch fights the first
    # for the keyboard hook and each dictation gets typed twice.
    _instance_lock = _acquire_single_instance_lock()
    if _instance_lock is None:
        log.info("[tray] another instance is already running; exiting")
        _info_dialog(
            "Whisper 2 is already running.\n\n"
            "Look for the microphone icon in your system tray "
            "(bottom-right of the screen, may be hidden under the ^ arrow)."
        )
        return

    # First-run wizard gates everything else: it downloads CUDA wheels,
    # installs Ollama, and pulls the model. If the user cancels, exit cleanly.
    try:
        if not first_run.run_if_needed():
            log.info("[tray] first-run wizard cancelled; exiting")
            return
    except Exception as e:
        log.exception("[tray] first-run wizard crashed")
        _fatal_dialog(f"First-run setup failed:\n{e}")
        return

    try:
        cfg = _load_config()
    except Exception as e:
        log.error(f"[tray] failed to load config: {e}")
        _fatal_dialog(f"Failed to load config:\n{e}")
        return

    try:
        app = App(cfg)
    except Exception as e:
        log.exception("[tray] App construction failed")
        _fatal_dialog(f"Whisper 2 failed to start:\n{e}")
        return

    tray = TrayController(app)
    app.status_callback = tray.on_status
    app.start()
    log.info("[tray] App started, entering tray mainloop")
    try:
        tray.run()
    finally:
        app.stop()
        log.info("[tray] exited cleanly")


if __name__ == "__main__":
    main()
