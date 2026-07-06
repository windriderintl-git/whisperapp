"""Whisper 2.0 — on-screen status HUD (the "recorder pill").

A small, always-on-top, click-through pill that mirrors the dictation state
(the same vocabulary the tray icon uses: idle / recording / transcribing /
polishing / editing / paused / degraded:ollama / no_mic).

While recording (and in edit mode) the pill shows a LIVE waveform driven by
real microphone levels plus an elapsed timer — like Wispr Flow's recorder.
While transcribing/polishing it shows a gently pulsing dot and a label.
Attention states (paused, no mic, Ollama down) show a ring + label.

Design constraints (see CRITICAL REQUIREMENTS in the task):

* It must NEVER take keyboard focus. The app types into whatever window the
  user had focused, so a focus-stealing HUD would break pasting. On Windows we
  set WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED
  via ctypes so the window is non-activating and click-through.
* Tk lives on ONE dedicated thread that ``start()`` launches. ``set_state()``,
  ``set_level()`` and ``stop()`` are safe from any thread — set_state/stop only
  push onto a queue that the Tk thread drains via ``root.after(...)``;
  set_level writes a single float that the animation tick samples.
* It degrades to safe no-ops if Tk is unavailable, there's no display, or we're
  not on Windows for the ctypes styling. It must never crash or block dictation.

stdlib only: tkinter, ctypes, queue, threading, sys, math, time, logging.

Contract used by the integrator (tray_app / main)::

    ov = Overlay(config.get("ui"))     # the `ui:` sub-dict of config.yaml
    ov.start()                          # non-blocking, idempotent
    ov.set_state("recording")           # thread-safe, from any thread
    ov.set_level(rms)                   # thread-safe, from the audio thread
    ov.stop()                           # idempotent, from any thread
"""
import logging
import math
import queue
import sys
import threading
import time

import ui_style

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


# States for which the window is shown. "idle" auto-hides the pill.
_HIDDEN_STATES = {"idle"}
# States that show the live waveform + elapsed timer (mic is hot).
_WAVE_STATES = {"recording", "editing"}
# States that pulse the status dot (busy, mic is closed).
_PULSE_STATES = {"transcribing", "polishing"}

# Sentinel pushed onto the queue by stop() to tell the Tk thread to quit.
_STOP = object()

# Layout / look. Small, compact, dark, translucent.
_WIDTH = 232
_HEIGHT = 48
_MARGIN = 60          # gap from the screen edge (above the taskbar for "bottom")
_CHROMA = "#010101"   # transparent-color key so the rounded corners vanish
_BG = "#1c1c1e"       # dark pill fill
_OUTLINE = "#3a3a3c"  # hairline pill border
_FG = "#f2f2f7"       # label text
_MUTED = "#98989d"    # timer text
_BAR = "#ececf0"      # waveform bars
_ALPHA = 0.94
_DOT_R = 5            # base dot radius
_DOT_PULSE = 2.5      # +/- radius during the pulse
_POLL_MS = 40         # how often the Tk thread drains the state queue
_ANIM_MS = 33         # ~30 fps waveform / pulse

# Waveform geometry: thin rounded bars scrolling right-to-left.
_BAR_W = 3            # bar stroke width
_BAR_GAP = 6          # horizontal distance between bar centers
_BAR_MIN = 1.5        # half-height of a silent bar
# Level -> bar normalization. Typical speech RMS at 16 kHz sits around
# 0.02–0.2; the exponent lifts quiet speech so the wave looks alive.
_LEVEL_FLOOR = 0.006
_LEVEL_CEIL = 0.22
_LEVEL_EXP = 0.55

# Windows extended window styles (see CRITICAL REQUIREMENT #1).
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000


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

        # Live mic level. Written from the audio thread (a single float store
        # is atomic under the GIL), sampled by the Tk animation tick.
        self._level_raw = 0.0

        # Tk-thread-only state (never touched from other threads).
        self._root = None
        self._canvas = None
        self._dot_id = None
        self._text_id = None
        self._timer_id = None
        self._bar_ids = []
        self._bar_levels = []
        self._level_smooth = 0.0
        self._wave_x0 = 0
        self._wave_x1 = 0
        self._rec_started = 0.0
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

    def set_level(self, rms: float) -> None:
        """Feed the latest microphone RMS (0..1). Thread-safe and cheap —
        called from the audio capture loop for every frame; drives the live
        waveform while recording."""
        try:
            self._level_raw = float(rms)
        except Exception:
            pass

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

        cy = h // 2
        # Colored status dot on the left.
        cx = 20
        self._dot_id = canvas.create_oval(
            cx - _DOT_R, cy - _DOT_R, cx + _DOT_R, cy + _DOT_R,
            fill=ui_style.to_hex(ui_style.DEFAULT.rgb), outline="",
        )
        # Label text to the right of the dot (hidden in waveform states).
        self._text_id = canvas.create_text(
            36, cy, anchor="w", text="", fill=_FG,
            font=("Segoe UI", 10, "normal"),
        )
        # Elapsed-time readout, right-aligned (waveform states only).
        self._timer_id = canvas.create_text(
            w - 16, cy, anchor="e", text="", fill=_MUTED,
            font=("Segoe UI", 9, "normal"), state="hidden",
        )

        # Waveform bars: rounded-cap vertical strokes between dot and timer,
        # scrolling right-to-left as new levels arrive.
        self._wave_x0 = 36
        self._wave_x1 = w - 56
        n = max(4, (self._wave_x1 - self._wave_x0) // _BAR_GAP + 1)
        self._bar_ids = []
        self._bar_levels = [0.0] * n
        for i in range(n):
            x = self._wave_x0 + i * _BAR_GAP
            bar = canvas.create_line(
                x, cy - _BAR_MIN, x, cy + _BAR_MIN,
                fill=_BAR, width=_BAR_W, capstyle="round", state="hidden",
            )
            self._bar_ids.append(bar)

        root.update_idletasks()
        self._apply_windows_styles()

        # Start hidden; we're in "idle" until told otherwise.
        self._hide()

    def _draw_pill(self, canvas, w, h) -> None:
        """Draw a rounded-rectangle 'pill' as the dark translucent background."""
        r = h // 2
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
        canvas.create_polygon(
            pts, smooth=True, splinesteps=36, fill=_BG,
            outline=_OUTLINE, width=1,
        )

    def _apply_windows_styles(self) -> None:
        """Set the extended window styles that make the HUD non-activating and
        click-through. Windows-only; a guarded no-op everywhere else."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # winfo_id() is Tk's CLIENT child window; the real top-level is its
            # parent wrapper. Styling the child is worse than useless: setting
            # WS_EX_LAYERED on a window that never gets layered attributes
            # makes Windows skip rendering it entirely — an invisible pill.
            child = self._root.winfo_id()
            hwnd = user32.GetParent(child) or child
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
        entering_wave = (state in _WAVE_STATES
                         and self._current_state not in _WAVE_STATES)
        self._current_state = state
        style = ui_style.get(state)
        color = ui_style.to_hex(style.rgb)
        canvas = self._canvas
        # Dot: filled disc vs. ring (matches tray's filled/outline icons).
        if style.filled:
            canvas.itemconfigure(self._dot_id, fill=color, outline="", width=1)
        else:
            canvas.itemconfigure(self._dot_id, fill=_BG, outline=color, width=2)

        if state in _WAVE_STATES:
            # Waveform mode: the wave + timer speak for themselves; no label.
            if entering_wave:
                self._rec_started = time.monotonic()
                self._bar_levels = [0.0] * len(self._bar_ids)
                self._level_smooth = 0.0
            canvas.itemconfigure(self._text_id, text="")
            canvas.itemconfigure(self._timer_id, state="normal", text="0:00")
            for bar in self._bar_ids:
                canvas.itemconfigure(bar, state="normal")
        else:
            canvas.itemconfigure(self._text_id, text=style.label)
            canvas.itemconfigure(self._timer_id, state="hidden")
            for bar in self._bar_ids:
                canvas.itemconfigure(bar, state="hidden")

        if state in _HIDDEN_STATES:
            self._hide()
        else:
            self._show()

        # Animate while recording (waveform) or busy (dot pulse).
        if state in (_WAVE_STATES | _PULSE_STATES) and self._visible:
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
        state = self._current_state
        if state not in (_WAVE_STATES | _PULSE_STATES) or not self._visible:
            self._anim_job = None
            self._reset_dot_size()
            return
        try:
            if state in _WAVE_STATES:
                self._tick_wave()
            else:
                self._tick_pulse()
        except Exception as e:
            log.debug("overlay anim tick failed: %s", e)
        try:
            self._anim_job = self._root.after(_ANIM_MS, self._tick_anim)
        except Exception:
            self._anim_job = None

    def _tick_wave(self) -> None:
        """Scroll the waveform left and append the newest mic level."""
        lvl = self._normalized_level()
        # Fast attack, slow release — bars jump with speech, settle gently.
        self._level_smooth = max(lvl, self._level_smooth * 0.82)
        self._bar_levels.pop(0)
        self._bar_levels.append(self._level_smooth)

        canvas = self._canvas
        cy = self._height / 2
        max_half = (self._height - 20) / 2
        for bar, level in zip(self._bar_ids, self._bar_levels):
            half = _BAR_MIN + level * (max_half - _BAR_MIN)
            x = canvas.coords(bar)[0]
            canvas.coords(bar, x, cy - half, x, cy + half)

        # Elapsed timer, mm:ss.
        secs = int(time.monotonic() - self._rec_started)
        canvas.itemconfigure(self._timer_id,
                             text=f"{secs // 60}:{secs % 60:02d}")

    def _tick_pulse(self) -> None:
        """Gentle radius pulse on the status dot (transcribing/polishing)."""
        self._anim_phase += 0.18
        r = _DOT_R + _DOT_PULSE * (0.5 + 0.5 * math.sin(self._anim_phase))
        self._set_dot_radius(r)

    def _normalized_level(self) -> float:
        """Map raw mic RMS to a 0..1 bar height with a perceptual curve."""
        raw = self._level_raw
        if raw <= _LEVEL_FLOOR:
            return 0.0
        norm = (raw - _LEVEL_FLOOR) / (_LEVEL_CEIL - _LEVEL_FLOOR)
        return min(1.0, max(0.0, norm)) ** _LEVEL_EXP

    def _dot_center(self):
        return 20, self._height // 2

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
    # Cycles through every state, feeding a synthetic speech-like level so the
    # waveform can be eyeballed without a hot microphone.
    import random

    logging.basicConfig(level=logging.INFO)
    ov = Overlay({"overlay": True, "overlay_position": "bottom"})
    ov.start()
    demo_states = [
        ("recording", 5.0), ("transcribing", 2.0), ("polishing", 2.0),
        ("editing", 3.0), ("degraded:ollama", 2.0), ("no_mic", 2.0),
        ("paused", 2.0), ("idle", 1.0),
    ]
    try:
        for s, dur in demo_states:
            print(f"state -> {s}")
            ov.set_state(s)
            t_end = time.time() + dur
            while time.time() < t_end:
                # Bursty fake speech: loud syllables with quiet gaps.
                if random.random() < 0.75:
                    ov.set_level(random.uniform(0.02, 0.18))
                else:
                    ov.set_level(random.uniform(0.0, 0.01))
                time.sleep(0.06)
    except KeyboardInterrupt:
        pass
    finally:
        ov.stop()
        print("overlay stopped")
