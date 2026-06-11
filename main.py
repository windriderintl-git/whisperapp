"""Whisper 2.0 — push-to-talk dictation with LLM polish + context awareness.

Hotkey:
    Hold Ctrl+Win                 -> push-to-talk (record while held, transcribe on release)
    Double-tap Ctrl+Win quickly   -> toggle continuous mode (records until you double-tap again)
"""
import argparse
import collections
import logging
import queue
import re
import threading
import time
import winsound
from pathlib import Path
from typing import Callable, Optional

import keyboard
import pyautogui
import pyperclip
import yaml

import logging_config
from audio import ContinuousAudioRecorder
from context import get_active_window_info, select_prompt_for
from hotkey import ComboController
from llm import OllamaPolisher
from transcribe import Transcriber

log = logging.getLogger("whisper2.main")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def apply_vocabulary(text: str, vocab: dict) -> str:
    if not vocab:
        return text
    for canonical, variants in vocab.items():
        for v in (variants or []):
            text = re.compile(re.escape(v), re.I).sub(canonical, text)
    return text


class App:
    def __init__(self, config: dict):
        self.config = config
        a = config["audio"]
        self.recorder = ContinuousAudioRecorder(
            rate=a.get("sample_rate", 16000),
            silence_threshold=a["silence_threshold"],
            silence_duration=a["silence_duration_s"],
            min_chunk_duration_s=a["min_chunk_duration_s"],
            ctx_provider=self._window_ctx,
        )
        w = config["whisper"]
        self.transcriber = Transcriber(
            model_size=w["model"],
            device=w.get("device", "auto"),
            compute_type=w.get("compute_type", "auto"),
            beam_size=w.get("beam_size", 1),
            vad_filter=w.get("vad_filter", True),
            vad_min_silence_ms=w.get("vad_min_silence_ms", 500),
            condition_on_previous_text=w.get("condition_on_previous_text", False),
        )
        l = config["llm"]
        self.polisher = OllamaPolisher(
            model=l["model"], host=l["host"],
            timeout=l["timeout_s"], enabled=l.get("enabled", True),
            polish_intensity=l.get("polish_intensity", "standard"),
            keep_alive=l.get("keep_alive", "30m"),
        )
        self.skip_polish_below = int(l.get("skip_below_words", 0))
        self.output_mode = config["output"]["mode"]
        self.trailing_space = config["output"].get("trailing_space", True)
        self.restore_clipboard = config["output"].get("restore_clipboard", True)
        self.restore_delay_ms = int(config["output"].get("restore_delay_ms", 300))
        self.vocab = config.get("vocabulary", {}) or {}
        self.context_enabled = config["context"].get("enabled", True)
        self.context_override = config["context"].get("override")

        if l.get("warmup_on_start", False) and self.polisher.enabled:
            threading.Thread(target=self.polisher.warmup, daemon=True).start()

        self.last_transcript = ""
        self.history: collections.deque[str] = collections.deque(maxlen=10)
        self.continuous_mode = False
        self.in_ptt = False
        self._lock = threading.Lock()
        # Window snapshot taken at hotkey press (PTT) so context reflects the
        # app the user was dictating into, not whatever has focus later.
        self._ctx_snapshot: Optional[tuple[str, str]] = None

        # Lifecycle / status plumbing for tray + CLI shared use.
        self._controller: Optional[ComboController] = None
        self._stop_consumer = threading.Event()
        self._consumer_thread: Optional[threading.Thread] = None
        self.status_callback: Optional[Callable[[str], None]] = None
        self._paused = False

        self._consumer_thread = threading.Thread(target=self._consume_audio, daemon=True)
        self._consumer_thread.start()
        # Single emit worker: pasting stays FIFO across continuous-mode chunks
        # while transcription of the next chunk no longer waits on the
        # clipboard + paste + restore sequence of the previous one.
        self._emit_queue: queue.Queue = queue.Queue()
        self._emit_thread = threading.Thread(target=self._emit_worker, daemon=True)
        self._emit_thread.start()

    # ----- status -----

    def _notify(self, state: str) -> None:
        cb = self.status_callback
        if cb:
            try:
                cb(state)
            except Exception as e:
                log.warning(f"status_callback raised: {e}")

    # ----- audio + hotkey callbacks -----

    def _beep(self, freq: int, duration: int = 120):
        # Non-blocking: winsound.Beep is synchronous and would stall the
        # hotkey thread, distorting tap/hold timing measurements.
        def _do():
            try:
                winsound.Beep(freq, duration)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def _window_ctx(self) -> tuple[str, str]:
        """Context for the chunk being queued. PTT uses the snapshot taken at
        hotkey press; continuous mode queries live since the user may have
        moved between windows mid-session."""
        snap = self._ctx_snapshot
        return snap if snap is not None else get_active_window_info()

    def on_capture_start(self):
        with self._lock:
            if self.continuous_mode or self.in_ptt:
                return
            self.in_ptt = True
        self._ctx_snapshot = get_active_window_info()
        self._beep(700, 100)
        self.recorder.start_recording(single_shot=True)
        if not self.recorder.recording:
            with self._lock:
                self.in_ptt = False                # roll back
            self._notify("no_mic")
            log.warning("[audio] mic unavailable; aborted push-to-talk")
            return
        self._notify("recording")

    def on_capture_end(self, discard: bool):
        with self._lock:
            if self.continuous_mode:
                return
            if not self.in_ptt:
                return
            self.in_ptt = False
        self.recorder.stop_recording(discard=discard)
        if not discard:
            self._beep(450, 90)

    def on_double_tap(self):
        if self._paused:
            return
        with self._lock:
            entering = not self.continuous_mode
            self.continuous_mode = entering
        if entering:
            self._ctx_snapshot = None    # continuous mode: query window live per chunk
            self._beep(800, 90)
            self._beep(950, 90)
            self.recorder.start_recording(single_shot=False)
            if not self.recorder.recording:
                with self._lock:
                    self.continuous_mode = False       # roll back
                self._notify("no_mic")
                log.warning("[mode] mic unavailable; could not enter continuous mode")
                return
            self._notify("recording")
            log.info("[mode] continuous ON")
        else:
            self.recorder.stop_recording(discard=False)
            self._beep(500, 90)
            self._beep(350, 120)
            log.info("[mode] continuous OFF")
            self._notify("idle")

    # ----- audio consumer -----

    def _consume_audio(self):
        while not self._stop_consumer.is_set():
            try:
                audio, ctx = self.recorder.audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_audio(audio, ctx)
            except Exception as e:
                log.error(f"[error] processing chunk: {e}")
            finally:
                self.recorder.audio_queue.task_done()

    def _process_audio(self, audio, ctx=None):
        self._notify("transcribing")
        prompt_ctx = self.last_transcript[-200:] if self.last_transcript else None
        t0 = time.time()
        raw = self.transcriber.transcribe(audio, initial_prompt=prompt_ctx)
        if not raw:
            self._notify("idle")
            return
        word_count = len(raw.split())
        # Transcript content is sensitive (passwords, PII); it only reaches the
        # log file at DEBUG level (--debug), never at INFO.
        log.info(f"[asr] {(time.time()-t0)*1000:.0f}ms, {word_count} words")
        log.debug(f"[asr] text: {raw!r}")

        if self.polisher.enabled and word_count >= self.skip_polish_below:
            override = self.context_override if self.context_enabled else "cleanup_default"
            proc, title = ctx if ctx is not None else get_active_window_info()
            prompt_name = select_prompt_for(proc, title, override)
            log.info(f"[ctx] {prompt_name}  <- proc={proc or '?'}")
            log.debug(f"[ctx] window title: {title[:60]!r}")
            self._notify("polishing")
            was_unreachable = self.polisher._warned_unreachable
            polished = self.polisher.polish(raw, prompt_name=prompt_name)
            if (not was_unreachable) and self.polisher._warned_unreachable:
                self._notify("degraded:ollama")
        else:
            if self.polisher.enabled:
                log.info(f"[skip] {word_count} words < {self.skip_polish_below}, no polish")
            polished = raw

        polished = apply_vocabulary(polished, self.vocab)
        self.last_transcript = polished
        self.history.append(polished)
        self._emit_queue.put(polished)
        self._notify("idle")

    def _emit_worker(self):
        while not self._stop_consumer.is_set():
            try:
                text = self._emit_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._emit(text)
            except Exception as e:
                log.error(f"[error] emitting text: {e}")
            finally:
                self._emit_queue.task_done()

    def _emit(self, text: str):
        if self.output_mode == "terminal":
            # print, not log: transcript text must not reach the log file.
            print(f"\n{'='*60}\n{text}\n{'='*60}\n")
            return
        payload = text + (" " if self.trailing_space else "")
        if self.output_mode == "clipboard":
            pyperclip.copy(text)
            log.info("[out] copied")
        else:
            old = None
            if self.restore_clipboard:
                try:
                    old = pyperclip.paste()
                except Exception:
                    old = None    # non-text clipboard; skip restore
            pyperclip.copy(payload)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
            log.info("[out] typed")
            if self.restore_clipboard and old is not None and old != payload:
                time.sleep(self.restore_delay_ms / 1000)
                try:
                    pyperclip.copy(old)
                except Exception:
                    pass

    # ----- lifecycle -----

    def start(self) -> None:
        """Install hotkey hook. Non-blocking. Returns immediately."""
        if self._paused:
            return
        if self._controller is None:
            hk = self.config["hotkey"]
            self._controller = ComboController(
                on_capture_start=self.on_capture_start,
                on_capture_end=self.on_capture_end,
                on_double_tap=self.on_double_tap,
                mod_keys=tuple(hk["modifiers"]),
                hold_threshold_s=hk["hold_threshold_ms"] / 1000,
                double_tap_window_s=hk["double_tap_window_ms"] / 1000,
                debug=hk.get("debug", False),
            )
        self._controller.install()
        self._notify("idle")

    def stop(self) -> None:
        """Uninstall hotkey hook, stop recorder, stop audio consumer thread.
        Idempotent. Safe to call from any thread."""
        try:
            if self._controller is not None:
                try:
                    keyboard.unhook_all()
                except Exception as e:
                    log.warning(f"unhook_all raised: {e}")
        finally:
            try:
                if self.recorder.recording:
                    self.recorder.stop_recording(discard=True)
            except Exception as e:
                log.warning(f"recorder.stop_recording raised: {e}")
            self._stop_consumer.set()

    def pause(self) -> None:
        """Remove keyboard hook; subsequent Ctrl+Win does nothing.
        Stops in-progress recording. Sets self._paused = True."""
        try:
            keyboard.unhook_all()
        except Exception as e:
            log.warning(f"unhook_all raised during pause: {e}")
        try:
            if self.recorder.recording:
                self.recorder.stop_recording(discard=True)
        except Exception as e:
            log.warning(f"recorder.stop_recording raised during pause: {e}")
        with self._lock:
            self.in_ptt = False
            self.continuous_mode = False
        self._paused = True
        self._notify("paused")

    def resume(self) -> None:
        """Re-install keyboard hook. Sets self._paused = False."""
        self._paused = False
        if self._controller is not None:
            self._controller.install()
        else:
            # Never started; start() will install the hook fresh.
            self.start()
            return
        self._notify("idle")


def main():
    ap = argparse.ArgumentParser(description="Whisper 2.0 dictation app")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--mode", choices=["type", "terminal", "clipboard"], default=None,
                    help="Override output mode from config")
    ap.add_argument("--no-llm", action="store_true", help="Disable LLM polish for this run")
    ap.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    logging_config.setup(debug=args.debug)
    config = load_config(args.config)
    if args.mode:
        config["output"]["mode"] = args.mode
    if args.no_llm:
        config["llm"]["enabled"] = False

    app = App(config)
    app.start()
    combo = "+".join(config["hotkey"]["modifiers"])
    print(f"Ready. Hold {combo} to dictate. Double-tap {combo} for continuous mode.")
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        app.stop()


if __name__ == "__main__":
    main()
