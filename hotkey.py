"""Modifier-combo controller: hold-to-talk + double-tap-to-toggle.

Listens for both modifier keys (default Ctrl + Windows). When both are pressed,
fires `on_capture_start`. On release, fires `on_capture_end(discard=False)` for
holds, or `on_capture_end(discard=True)` plus tracking for short taps. Two short
taps inside `double_tap_window_s` fire `on_double_tap`.

Set debug=True to print press/release timings — useful for tuning thresholds
to your natural finger speed.
"""
import time
import threading
import keyboard

import logging
log = logging.getLogger("whisper2.hotkey")


class ComboController:
    def __init__(self, on_capture_start, on_capture_end, on_double_tap,
                 mod_keys=("ctrl", "windows"),
                 hold_threshold_s: float = 0.35,
                 double_tap_window_s: float = 0.60,
                 debug: bool = False):
        self.on_capture_start = on_capture_start
        self.on_capture_end = on_capture_end
        self.on_double_tap = on_double_tap
        self.mod_keys = tuple(k.lower() for k in mod_keys)
        self.hold_threshold_s = hold_threshold_s
        self.double_tap_window_s = double_tap_window_s
        self.debug = debug
        self._state = {k: False for k in self.mod_keys}
        self._combo_active = False
        self._press_time = 0.0
        self._last_tap_time = 0.0
        self._lock = threading.Lock()

    def install(self):
        keyboard.hook(self._on_event)
        if self.debug:
            log.info(f"[hotkey] watching {'+'.join(self.mod_keys)}  "
                  f"(hold>={self.hold_threshold_s*1000:.0f}ms, "
                  f"double-tap<={self.double_tap_window_s*1000:.0f}ms)")

    def _match_mod(self, name: str | None) -> str | None:
        n = (name or "").lower()
        # `keyboard` reports e.g. 'left ctrl', 'right ctrl', 'left windows'.
        for k in self.mod_keys:
            if k == n or k in n:
                return k
        return None

    def _on_event(self, e):
        if e.event_type not in ("down", "up"):
            return
        mod = self._match_mod(e.name)
        if mod is None:
            return
        new_state = (e.event_type == "down")
        with self._lock:
            if self._state[mod] == new_state:
                return  # repeat / no-op
            self._state[mod] = new_state
            now_active = all(self._state.values())
            now = time.time()

            if now_active and not self._combo_active:
                self._combo_active = True
                self._press_time = now
                if self.debug:
                    log.info("[hotkey] combo PRESSED")
                self._safe(self.on_capture_start)

            elif not now_active and self._combo_active:
                self._combo_active = False
                held_ms = (now - self._press_time) * 1000
                if (now - self._press_time) >= self.hold_threshold_s:
                    if self.debug:
                        log.info(f"[hotkey] HOLD released ({held_ms:.0f}ms)")
                    self._safe(lambda: self.on_capture_end(False))
                    self._last_tap_time = 0.0
                else:
                    self._safe(lambda: self.on_capture_end(True))
                    if self._last_tap_time:
                        gap_ms = (now - self._last_tap_time) * 1000
                        if gap_ms < self.double_tap_window_s * 1000:
                            if self.debug:
                                log.info(f"[hotkey] DOUBLE TAP (tap={held_ms:.0f}ms, "
                                      f"gap={gap_ms:.0f}ms)")
                            self._last_tap_time = 0.0
                            self._safe(self.on_double_tap)
                            return
                        elif self.debug:
                            log.info(f"[hotkey] tap ({held_ms:.0f}ms) — previous tap "
                                  f"too long ago ({gap_ms:.0f}ms), restarting count")
                    elif self.debug:
                        log.info(f"[hotkey] tap ({held_ms:.0f}ms) — waiting for a second")
                    self._last_tap_time = now

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception as ex:
            log.error(f"[hotkey] callback error: {ex}")
