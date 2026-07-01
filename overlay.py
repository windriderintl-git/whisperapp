"""Whisper 2.0 — on-screen status HUD (the "recorder pill").

A small, always-on-top, click-through pill that mirrors the dictation state
(the same vocabulary the tray icon uses: idle / recording / transcribing /
polishing / editing / paused / degraded:ollama / no_mic). Think Wispr Flow's
recorder pill: a colored dot, a short label, and a gentle pulse while recording.

Design constraints (see CRITICAL REQUIREMENTS in the task):

* It must NEVER take keyboard focus. The app types into whatever window the
  user had focused, so a focus-stealing HUD would break pasting. On Windows we
  set WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED
  via ctypes so the window is non-activating and click-through.
* Tk lives on ONE dedicated thread that ``start()`` launches. ``set_state()``
  and ``stop()`` are safe from any thread — they only push onto a queue that
  the Tk thread drains via ``root.after(...)``.
* It degrades to safe no-ops if Tk is unavailable, there's no display, or we're
  not on Windows for the ctypes styling. It must never crash or block dictation.

stdlib only: tkinter, ctypes, queue, threading, sys, logging.

Contract used by the integrator (tray_app / main)::

    ov = Overlay(config.get("ui"))     # the `ui:` sub-dict of config.yaml
    ov.start()                          # non-blocking, idempotent
    ov.set_state("recording")           # thread-safe, from any thread
    ov.stop()                           # idempotent, from any thread
"""
import logging
import queue
import sys
import threading

log = logging.getLogger("whisper2.overlay")

# tkinter may be missing (headless build) — import defensively so the module
# always imports and every method can no-op safely.
try:
    import tkinter as tk
    _TK_IMPORT_OK = True
except Exception as e:  # pragma: no cover - platform dependent
    tk = None
    _TK_IMPORT_OK = False
    log.warning("tkinter unavailable; overlay HUD disabled: %s", e)


# --- state -> (rgb, label, filled_dot) --------------------------------------
# Colors and labels mirror tray_app.ICONS / TOOLTIPS so the HUD and the tray
# icon always agree. "filled" False draws the dot as a ring (paused / no_mic),
# matching the tray's outline icons.
_STATE_STYLE = {
    "idle":            ((90, 90, 90),   "Ready",                   True),   # gray (hidden)
    "recording":       ((220, 50, 50),  "Recording…",         True),   # red
    "transcribing":    ((230, 160, 30), "Transcribing…",      True),   # amber
    "polishing":       ((230, 160, 30), "Polishing…",         True),   # amber
    "editing":         ((70, 140, 230), "Editing selection…", True),   # blue-ish
    "paused":          ((140, 140, 140),"Paused",                  False),  # gray ring
    "degraded:ollama": ((230, 200, 30), "Ollama down (raw)",       True),   # yellow
    "no_mic":          ((220, 50, 50),  "Microphone unavailable",  False),  # red ring
}
_DEFAULT_STYLE = ((90, 90, 90), "", True)

# States for which the window is shown. "idle" auto-hides the pill.
_HIDDEN_STATES = {"idle"}
# States that get the gentle pulsing animation.
_ANIMATED_STATES = {"recording"}

# Sentinel pushed onto the queue by stop() to tell the Tk thread to quit.
_STOP = object()

# Layout / look. Small, compact, dark, translucent.
_WIDTH = 180
_HEIGHT = 44
_MARGIN = 60          # gap from the screen edge (above the taskbar for "bottom")
_CHROMA = "#010101"   # transparent-color key so the rounded corners vanish
_BG = "#1e1e1e"       # dark pill fill
_FG = "#f5f5f5"       # label text
_ALPHA = 0.9
_CORNER = 18          # pill corner radius
_DOT_R = 7            # base dot radius
_DOT_PULSE = 3        # +/- radius during the pulse
_POLL_MS = 40         # how often the Tk thread drains the state queue
_ANIM_MS = 80         # ~12 fps pulse

# Windows extended window styles (see CRITICAL REQUIREMENT #1).
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


class Overlay:
    """Always-on-top, non-activating, click-through status HUD.

    All public methods are safe no-ops when the HUD can't run (no Tk, no
    display, disabled via config). Nothing here should ever raise into the
    caller or block the dictation pipeline.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # Respect `ui.overlay: false` to disable entirely.
        self._enabled = bool(cfg.get("overlay", True)) and _TK_IMPORT_OK
        pos = str(cfg.get("overlay_position", "bottom")).lower()
        self._position = pos if pos in ("bottom", "top", "cursor") else "bottom"
        # Optional cosmetic overrides (fall back to sensible defaults).
        try:
            self._width = int(cfg.get("overlay_width", _WIDTH))
            self._height = int(cfg.get("overlay_height", _HEIGHT))
        except Exception:
            self._width, self._height = _WIDTH, _HEIGHT

        self._queue: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.Lock()

        # Tk-thread-only state (never touched from other threads).
        self._root = None
        self._canvas = None
        self._dot_id = None
        self._text_id = None
        self._pill_ids = []
        self._current_state = "idle"
        self._anim_job = None
        self._anim_phase = 0.0
        self._visible = False

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        """Spin up the HUD thread. Non-blocking and idempotent."""
        if not self._enabled:
            return
        with self._lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._run, name="whisper2-overlay", daemon=True
            )
            self._thread.start()

    def set_state(self, state: str) -> None:
        """Update the displayed state. Thread-safe; callable from any thread."""
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(state)
        except Exception as e:  # pragma: no cover - queue is unbounded
            log.debug("overlay set_state enqueue failed: %s", e)

    def stop(self) -> None:
        """Tear down the HUD. Idempotent; safe from any thread."""
        if not self._enabled:
            return
        with self._lock:
            if not self._started:
                return
            self._started = False
            t = self._thread
        try:
            self._queue.put_nowait(_STOP)
        except Exception:
            pass
        # Best-effort join, but never block dictation for long.
        if t is not None and t is not threading.current_thread():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

    # -------------------------------------------------------- Tk thread body

    def _run(self) -> None:
        """Entire Tk lifecycle lives here, on the dedicated overlay thread."""
        try:
            self._build_ui()
        except Exception as e:
            # No display, Tk broken, etc. Drain the queue so callers still work,
            # then exit quietly. The HUD is a nice-to-have, never a hard dep.
            log.warning("overlay UI unavailable; running headless no-op: %s", e)
            self._root = None
            self._drain_headless()
            return
        try:
            self._root.after(_POLL_MS, self._poll_queue)
            self._root.mainloop()
        except Exception as e:
            log.warning("overlay mainloop ended abnormally: %s", e)
        finally:
            self._teardown_ui()

    def _drain_headless(self) -> None:
        """If the UI never came up, keep emptying the queue until stopped so
        set_state() never wedges and stop() returns promptly."""
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not self._started:
                    return
                continue
            if item is _STOP:
                return

    def _build_ui(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("Whisper 2 HUD")
        # Borderless, no taskbar entry, always on top.
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", _ALPHA)
        except Exception:
            pass
        # Make the rectangular corners disappear so only the rounded pill shows.
        try:
            root.configure(bg=_CHROMA)
            root.attributes("-transparentcolor", _CHROMA)
            canvas_bg = _CHROMA
        except Exception:
            # transparentcolor unsupported (non-Windows): fall back to opaque.
            root.configure(bg=_BG)
            canvas_bg = _BG

        w, h = self._width, self._height
        canvas = tk.Canvas(
            root, width=w, height=h, bg=canvas_bg,
            highlightthickness=0, bd=0,
        )
        canvas.pack()
        self._canvas = canvas

        self._draw_pill(canvas, w, h)

        # Colored status dot on the left.
        cx, cy = 24, h // 2
        self._dot_id = canvas.create_oval(
            cx - _DOT_R, cy - _DOT_R, cx + _DOT_R, cy + _DOT_R,
            fill=_hex(_DEFAULT_STYLE[0]), outline="",
        )
        # White label text to the right of the dot.
        self._text_id = canvas.create_text(
            44, cy, anchor="w", text="", fill=_FG,
            font=("Segoe UI", 11, "normal"),
        )

        root.update_idletasks()
        self._apply_windows_styles()

        # Start hidden; we're in "idle" until told otherwise.
        self._hide()

    def _draw_pill(self, canvas, w, h) -> None:
        """Draw a rounded-rectangle 'pill' as the dark translucent background."""
        r = min(_CORNER, h // 2)
        pad = 1
        x0, y0, x1, y1 = pad, pad, w - pad, h - pad
        # A rounded rect via a smoothed polygon through the corner points.
        pts = [
            x0 + r, y0,
            x1 - r, y0,
            x1, y0,
            x1, y0 + r,
            x1, y1 - r,
            x1, y1,
            x1 - r, y1,
            x0 + r, y1,
            x0, y1,
            x0, y1 - r,
            x0, y0 + r,
            x0, y0,
        ]
        pill = canvas.create_polygon(
            pts, smooth=True, splinesteps=24, fill=_BG, outline=_BG,
        )
        self._pill_ids = [pill]

    def _apply_windows_styles(self) -> None:
        """Set the extended window styles that make the HUD non-activating and
        click-through. Windows-only; a guarded no-op everywhere else."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = self._root.winfo_id()
            user32 = ctypes.windll.user32
            # Prefer the Ptr variants on 64-bit; fall back to the W variants.
            get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            ex = get_long(hwnd, _GWL_EXSTYLE)
            ex |= (_WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
                   | _WS_EX_TRANSPARENT | _WS_EX_LAYERED)
            set_long(hwnd, _GWL_EXSTYLE, ex)
            # SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        except Exception as e:
            log.warning("could not apply non-activating window styles: %s", e)

    # ----------------------------------------------------- Tk-thread updates

    def _poll_queue(self) -> None:
        """Drain queued state changes on the Tk thread and update the UI."""
        stop = False
        latest = None
        try:
            while True:
                item = self._queue.get_nowait()
                if item is _STOP:
                    stop = True
                    break
                latest = item
        except queue.Empty:
            pass
        except Exception as e:
            log.debug("overlay poll error: %s", e)

        if stop:
            try:
                self._root.quit()
            except Exception:
                pass
            return

        if latest is not None:
            try:
                self._apply_state(latest)
            except Exception as e:
                log.warning("overlay apply_state failed: %s", e)

        try:
            self._root.after(_POLL_MS, self._poll_queue)
        except Exception:
            pass

    def _apply_state(self, state: str) -> None:
        self._current_state = state
        rgb, label, filled = _STATE_STYLE.get(state, _DEFAULT_STYLE)
        color = _hex(rgb)
        canvas = self._canvas
        # Dot: filled disc vs. ring (matches tray's filled/outline icons).
        if filled:
            canvas.itemconfigure(self._dot_id, fill=color, outline="", width=1)
        else:
            canvas.itemconfigure(self._dot_id, fill=_BG, outline=color, width=2)
        canvas.itemconfigure(self._text_id, text=label)

        if state in _HIDDEN_STATES:
            self._hide()
        else:
            self._show()

        # Manage the pulse: run only while in an animated state.
        if state in _ANIMATED_STATES and self._visible:
            self._start_anim()
        else:
            self._stop_anim()
            self._reset_dot_size()

    # ----------------------------------------------------------- animation

    def _start_anim(self) -> None:
        if self._anim_job is not None:
            return
        self._anim_phase = 0.0
        self._tick_anim()

    def _stop_anim(self) -> None:
        if self._anim_job is not None:
            try:
                self._root.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None

    def _tick_anim(self) -> None:
        if self._current_state not in _ANIMATED_STATES or not self._visible:
            self._anim_job = None
            self._reset_dot_size()
            return
        import math
        self._anim_phase += 0.35
        # Gentle radius pulse around the base dot size.
        r = _DOT_R + _DOT_PULSE * (0.5 + 0.5 * math.sin(self._anim_phase))
        self._set_dot_radius(r)
        try:
            self._anim_job = self._root.after(_ANIM_MS, self._tick_anim)
        except Exception:
            self._anim_job = None

    def _dot_center(self):
        cx, cy = 24, self._height // 2
        return cx, cy

    def _set_dot_radius(self, r: float) -> None:
        cx, cy = self._dot_center()
        try:
            self._canvas.coords(self._dot_id, cx - r, cy - r, cx + r, cy + r)
        except Exception:
            pass

    def _reset_dot_size(self) -> None:
        self._set_dot_radius(_DOT_R)

    # ------------------------------------------------------ show/hide/place

    def _show(self) -> None:
        if self._visible:
            return
        try:
            self._place_window()
            self._root.deiconify()
            self._root.attributes("-topmost", True)
            self._visible = True
        except Exception as e:
            log.debug("overlay show failed: %s", e)

    def _hide(self) -> None:
        self._stop_anim()
        try:
            self._root.withdraw()
        except Exception:
            pass
        self._visible = False

    def _place_window(self) -> None:
        root = self._root
        w, h = self._width, self._height
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080

        if self._position == "cursor":
            try:
                px, py = root.winfo_pointerxy()
            except Exception:
                px, py = sw // 2, sh // 2
            x = px - w // 2
            y = py + 24  # a little below the cursor, out from under it
        elif self._position == "top":
            x = (sw - w) // 2
            y = _MARGIN
        else:  # bottom (default)
            x = (sw - w) // 2
            y = sh - h - _MARGIN

        # Clamp on-screen.
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        try:
            root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        except Exception:
            pass

    def _teardown_ui(self) -> None:
        self._stop_anim()
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:
            pass
        self._root = None
        self._canvas = None
        self._visible = False


# --------------------------------------------------------------------- demo
if __name__ == "__main__":
    # Cycles through every state so the HUD can be eyeballed on Windows.
    # (On this Linux build there's no display, so it will just log and no-op.)
    import time

    logging.basicConfig(level=logging.INFO)
    ov = Overlay({"overlay": True, "overlay_position": "bottom"})
    ov.start()
    demo_states = [
        "idle", "recording", "transcribing", "polishing",
        "editing", "degraded:ollama", "no_mic", "paused", "idle",
    ]
    try:
        for _ in range(3):
            for s in demo_states:
                print(f"state -> {s}")
                ov.set_state(s)
                time.sleep(1.5)
    except KeyboardInterrupt:
        pass
    finally:
        ov.stop()
        print("overlay stopped")
