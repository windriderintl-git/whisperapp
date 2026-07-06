"""Whisper 2.0 — push-to-talk dictation with LLM polish + context awareness.

Hotkey:
    Hold Ctrl+Win                 -> push-to-talk (record while held, transcribe on release)
    Double-tap Ctrl+Win quickly   -> toggle continuous mode (records until you double-tap again)
    Hold Ctrl+Alt                 -> edit mode (rewrite the highlighted selection by voice)
"""
import argparse
import collections
import logging
import queue
import re
import threading
import time
import winsound
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import keyboard
import pyautogui
import pyperclip
import yaml

import commands
import logging_config
import snippets
from audio import ContinuousAudioRecorder
from context import get_active_window_info, select_prompt_for
from hotkey import ComboController
from llm import OllamaPolisher
from overlay import Overlay
from transcribe import Transcriber

log = logging.getLogger("whisper2.main")

# Sentinel: "no clipboard-restore override" vs. an explicit value (which may be
# None). Edit mode restores the clipboard it saved BEFORE its own Ctrl+C, so it
# can't rely on _emit sampling whatever is on the clipboard at paste time.
_NO_CLIP_OVERRIDE = object()


@dataclass
class _EmitItem:
    """One unit of output handed to the emit worker.

    trailing_keys: keys pressed (in order) AFTER a successful paste — voice
                   commands like "press enter". Type mode only.
    force_paste:   edit mode always pastes over the live selection regardless
                   of the configured output mode.
    add_space:     dictation appends a trailing space; edits replace in place.
    restore_clip:  edit mode restores the clipboard captured BEFORE its Ctrl+C;
                   dictation uses the sentinel and samples at paste time.
    """
    text: str
    trailing_keys: list[str] = field(default_factory=list)
    force_paste: bool = False
    add_space: bool = True
    restore_clip: object = _NO_CLIP_OVERRIDE


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
            level_callback=self._on_audio_level,
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

        # New building blocks: voice commands, snippets, edit mode, HUD overlay.
        self.commands_cfg = config.get("commands", {}) or {}
        # `snippets` is a mixed dict: an `enabled` flag plus trigger->expansion
        # string pairs. Keep the whole thing; _snippet_map() filters out the flag.
        self.snippets_cfg = config.get("snippets", {}) or {}
        self.snippets_enabled = bool(self.snippets_cfg.get("enabled", True))
        self.edit_cfg = config.get("edit_mode", {}) or {}
        ui_cfg = config.get("ui", {}) or {}
        self._overlay = Overlay(ui_cfg)
        self._overlay.start()

        self._warmup_on_start = l.get("warmup_on_start", False)
        if self.polisher.enabled:
            threading.Thread(target=self._ensure_llm_ready, daemon=True).start()

        self.last_transcript = ""
        self.history: collections.deque[str] = collections.deque(maxlen=10)
        self.continuous_mode = False
        self.in_ptt = False
        self._lock = threading.Lock()
        # Window snapshot taken at hotkey press (PTT) so context reflects the
        # app the user was dictating into, not whatever has focus later.
        self._ctx_snapshot: Optional[tuple[str, str]] = None

        # Edit mode: a SECOND hotkey (Ctrl+Alt) captures a spoken instruction
        # applied to the highlighted selection. Mutually exclusive with PTT /
        # continuous dictation. `_capture_mode` tags the queued chunk ("dictate"
        # vs "edit") without touching audio.py.
        self._capture_mode = "dictate"
        self._edit_active = False
        self._edit_selection = ""
        self._edit_saved_clip: Optional[str] = None
        self._edit_controller: Optional[ComboController] = None

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

    def _ensure_llm_ready(self):
        """Background: auto-start Ollama if installed but not running, then
        warm the model. Without this, a reboot leaves Ollama down and every
        dictation silently falls back to raw, unpolished text."""
        try:
            import ollama_setup
            if not ollama_setup.is_running():
                ollama_setup.start_serve_detached()
                if not ollama_setup.wait_until_running(20.0):
                    log.warning("[llm] Ollama not reachable after autostart; "
                                "polish will fall back to raw text")
                    return
                log.info("[llm] Ollama autostarted")
        except Exception as e:
            log.warning(f"[llm] Ollama autostart failed: {e}")
            return
        if self._warmup_on_start:
            self.polisher.warmup()

    # ----- status -----

    def _notify(self, state: str) -> None:
        cb = self.status_callback
        if cb:
            try:
                cb(state)
            except Exception as e:
                log.warning(f"status_callback raised: {e}")
        # Fan the same state out to the on-screen HUD. Wrapped separately so a
        # broken overlay can never take down dictation.
        try:
            self._overlay.set_state(state)
        except Exception as e:
            log.warning(f"overlay set_state raised: {e}")

    def _on_audio_level(self, rms: float) -> None:
        """Per-frame mic RMS from the recorder thread -> HUD waveform.
        The recorder is built before the overlay, so look it up lazily."""
        ov = getattr(self, "_overlay", None)
        if ov is not None:
            ov.set_level(rms)

    def _snippet_map(self) -> dict[str, str]:
        """Trigger->expansion pairs from the `snippets:` config, minus the
        `enabled` flag and any non-string entries a hand-edited config might hold."""
        return {
            k: v for k, v in self.snippets_cfg.items()
            if k != "enabled" and isinstance(k, str) and isinstance(v, str)
        }

    def _restore_clip(self, value: Optional[str]) -> None:
        """Best-effort clipboard restore; None means we never captured one."""
        if value is None:
            return
        try:
            pyperclip.copy(value)
        except Exception:
            pass

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

    def _window_ctx(self) -> tuple[str, str, str]:
        """Context for the chunk being queued: (proc, title, capture_mode).

        PTT/edit use the snapshot taken at hotkey press; continuous mode queries
        live since the user may have moved between windows mid-session. The mode
        rides along so _process_audio can route the chunk to the dictate-vs-edit
        path without audio.py knowing anything about it."""
        snap = self._ctx_snapshot
        proc, title = snap if snap is not None else get_active_window_info()
        return (proc, title, self._capture_mode)

    def on_capture_start(self):
        with self._lock:
            if self.continuous_mode or self.in_ptt or self._edit_active:
                return
            self.in_ptt = True
        self._capture_mode = "dictate"
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
            if self._edit_active:
                return
            entering = not self.continuous_mode
            self.continuous_mode = entering
        if entering:
            self._capture_mode = "dictate"
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

    # ----- edit-mode (second hotkey) callbacks -----

    def on_edit_start(self):
        """Hold Ctrl+Alt: record a spoken instruction to apply to the currently
        highlighted selection. Mutually exclusive with dictation.

        The selection itself is copied on RELEASE (see on_edit_end), NOT here:
        injecting Ctrl+C while the combo is physically held feeds a synthetic
        Ctrl-up back into the keyboard hook, which the edit ComboController would
        read as the combo releasing — prematurely ending the capture."""
        if not self.edit_cfg.get("enabled", True):
            return
        with self._lock:
            if self.continuous_mode or self.in_ptt or self._edit_active:
                return
            self._edit_active = True
        self._capture_mode = "edit"
        self._ctx_snapshot = get_active_window_info()
        self._beep(700, 100)
        self.recorder.start_recording(single_shot=True)
        if not self.recorder.recording:
            self._capture_mode = "dictate"
            with self._lock:
                self._edit_active = False           # roll back
            self._notify("no_mic")
            log.warning("[edit] mic unavailable; aborted edit")
            return
        self._notify("editing")

    def on_edit_end(self, discard: bool):
        with self._lock:
            if not self._edit_active:
                return
            self._edit_active = False
        if discard:
            # A tap (too short to be a hold), a stray Ctrl+Alt chord, or explicit
            # cancel: drop the audio. We never touched the clipboard, so there's
            # nothing to restore.
            self.recorder.stop_recording(discard=True)
            self._capture_mode = "dictate"
            return
        self._beep(450, 90)
        # The combo is released now, so injecting Ctrl+C is safe. Grab the
        # selection BEFORE stopping the recorder so it's stored ahead of the
        # chunk being queued and picked up by the consumer thread.
        self._edit_selection = self._capture_selection()
        self.recorder.stop_recording(discard=False)
        # The chunk is now queued (stop_recording joined the record thread); the
        # mode tag it carries is already "edit", so reset for the next capture.
        self._capture_mode = "dictate"

    def _capture_selection(self) -> str:
        """Copy the highlighted selection via Ctrl+C. Saves the pre-copy
        clipboard into self._edit_saved_clip so _emit can restore it after the
        replacement paste. Returns the selected text ('' if nothing was
        highlighted or the copy failed)."""
        try:
            self._edit_saved_clip = pyperclip.paste()
        except Exception:
            self._edit_saved_clip = None    # non-text clipboard; nothing to restore
        try:
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.12)                 # let the target app service the copy
            return pyperclip.paste() or ""
        except Exception as e:
            log.warning(f"[edit] copy selection failed: {e}")
            return ""

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
        # ctx rides with the chunk from audio.py: a 3-tuple (proc, title, mode)
        # from the new _window_ctx, or a legacy 2-tuple / None. Default to the
        # dictate path so nothing regresses.
        mode = "dictate"
        if ctx is None:
            proc, title = get_active_window_info()
        elif len(ctx) >= 3:
            proc, title, mode = ctx[0], ctx[1], ctx[2]
        else:
            proc, title = ctx[0], ctx[1]

        if mode == "edit":
            self._process_edit(audio)
            return

        self._notify("transcribing")
        prompt_ctx = self.last_transcript[-200:] if self.last_transcript else None
        t0 = time.time()
        raw = self.transcriber.transcribe(audio, initial_prompt=prompt_ctx)
        if not raw:
            self._notify("idle")
            return
        # Transcript content is sensitive (passwords, PII); it only reaches the
        # log file at DEBUG level (--debug), never at INFO.
        log.info(f"[asr] {(time.time()-t0)*1000:.0f}ms, {len(raw.split())} words")
        log.debug(f"[asr] text: {raw!r}")

        # Voice commands run on the RAW transcript, before polish would rewrite
        # literal phrases like "press enter" into prose.
        parsed = commands.parse(raw, self.commands_cfg)
        if parsed.drop:
            log.info("[commands] cancel utterance — dropping")
            self._notify("idle")
            return
        text = parsed.text
        word_count = len(text.split())

        if self.polisher.enabled and word_count >= self.skip_polish_below:
            override = self.context_override if self.context_enabled else "cleanup_default"
            prompt_name = select_prompt_for(proc, title, override)
            log.info(f"[ctx] {prompt_name}  <- proc={proc or '?'}")
            log.debug(f"[ctx] window title: {title[:60]!r}")
            self._notify("polishing")
            was_unreachable = self.polisher._warned_unreachable
            polished = self.polisher.polish(text, prompt_name=prompt_name)
            if (not was_unreachable) and self.polisher._warned_unreachable:
                self._notify("degraded:ollama")
        else:
            if self.polisher.enabled:
                log.info(f"[skip] {word_count} words < {self.skip_polish_below}, no polish")
            polished = text

        polished = apply_vocabulary(polished, self.vocab)
        if self.snippets_enabled:
            polished = snippets.expand(polished, self._snippet_map())
        self.last_transcript = polished
        if polished.strip():
            self.history.append(polished)
        self._emit_queue.put(_EmitItem(text=polished,
                                       trailing_keys=parsed.trailing_keys))
        self._notify("idle")

    def _process_edit(self, audio):
        """The captured audio is a spoken INSTRUCTION; apply it to the selection
        copied on release, then paste the result over the still-highlighted
        text. On any failure the selection is left untouched."""
        selection = self._edit_selection
        if not selection.strip():
            # Nothing was highlighted when the user released the combo.
            log.info("[edit] no selection; nothing to edit")
            self._restore_clip(self._edit_saved_clip)
            self._notify("idle")
            return
        self._notify("transcribing")
        instruction = self.transcriber.transcribe(audio, initial_prompt=None)
        if not instruction or not instruction.strip():
            log.info("[edit] empty instruction; leaving selection unchanged")
            self._restore_clip(self._edit_saved_clip)
            self._notify("idle")
            return
        log.debug(f"[edit] instruction: {instruction!r}")
        self._notify("editing")
        # edit() returns the original selection on any LLM failure, so a paste
        # never destroys the user's text.
        edited = self.polisher.edit(instruction, selection)
        edited = apply_vocabulary(edited, self.vocab)
        self.last_transcript = edited
        if edited.strip():
            self.history.append(edited)
        self._emit_queue.put(_EmitItem(
            text=edited,
            force_paste=True,     # replace the selection regardless of output mode
            add_space=False,      # in-place replacement, no trailing space
            restore_clip=self._edit_saved_clip,
        ))
        self._notify("idle")

    def _emit_worker(self):
        while not self._stop_consumer.is_set():
            try:
                item = self._emit_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._emit(item)
            except Exception as e:
                log.error(f"[error] emitting text: {e}")
            finally:
                self._emit_queue.task_done()

    def _emit(self, item: _EmitItem):
        text = item.text
        # terminal / clipboard modes still apply to normal dictation; edit mode
        # (force_paste) always pastes so it can replace the live selection.
        if self.output_mode == "terminal" and not item.force_paste:
            # print, not log: transcript text must not reach the log file.
            print(f"\n{'='*60}\n{text}\n{'='*60}\n")
            return
        if self.output_mode == "clipboard" and not item.force_paste:
            pyperclip.copy(text)
            log.info("[out] copied")
            return

        payload = text + (" " if (item.add_space and self.trailing_space) else "")

        # Only paste when there's real text; a command-only utterance ("press
        # enter") still needs its trailing keys pressed below.
        if payload and (text.strip() or item.force_paste):
            if item.restore_clip is _NO_CLIP_OVERRIDE:
                old = None
                if self.restore_clipboard:
                    try:
                        old = pyperclip.paste()
                    except Exception:
                        old = None    # non-text clipboard; skip restore
                restore = self.restore_clipboard
            else:
                # Edit mode: restore the clipboard saved before its Ctrl+C, not
                # whatever (the selection) is on the clipboard right now.
                old = item.restore_clip
                restore = True
            pyperclip.copy(payload)
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
            log.info("[out] typed")
            if restore and old is not None and old != payload:
                time.sleep(self.restore_delay_ms / 1000)
                try:
                    pyperclip.copy(old)
                except Exception:
                    pass

        # Voice-command trailing keys fire after the paste, in spoken order.
        if item.trailing_keys and self.output_mode == "type":
            for k in item.trailing_keys:
                try:
                    pyautogui.press(k)
                except Exception as e:
                    log.warning(f"[out] trailing key {k!r} failed: {e}")

    # ----- lifecycle -----

    def _install_controllers(self) -> None:
        """(Re)install BOTH hotkey controllers together. keyboard.unhook_all()
        tears down every hook at once, so the dictation and edit controllers
        must always be armed as a pair on start/resume."""
        if self._controller is not None:
            self._controller.install()
        if self._edit_controller is not None:
            self._edit_controller.install()

    def start(self) -> None:
        """Install hotkey hooks. Non-blocking. Returns immediately."""
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
        if self._edit_controller is None and self.edit_cfg.get("enabled", True):
            hk = self.config["hotkey"]
            self._edit_controller = ComboController(
                on_capture_start=self.on_edit_start,
                on_capture_end=self.on_edit_end,
                on_double_tap=lambda: None,     # edit mode has no continuous variant
                mod_keys=tuple(self.edit_cfg.get("modifiers", ["ctrl", "alt"])),
                hold_threshold_s=self.edit_cfg.get("hold_threshold_ms", 350) / 1000,
                double_tap_window_s=hk["double_tap_window_ms"] / 1000,
                debug=hk.get("debug", False),
            )
        self._install_controllers()
        self._notify("idle")

    def stop(self) -> None:
        """Uninstall hotkey hooks, stop recorder, stop audio consumer thread,
        tear down the overlay. Idempotent. Safe to call from any thread."""
        try:
            if self._controller is not None or self._edit_controller is not None:
                try:
                    keyboard.unhook_all()    # removes BOTH controllers' hooks
                except Exception as e:
                    log.warning(f"unhook_all raised: {e}")
        finally:
            try:
                if self.recorder.recording:
                    self.recorder.stop_recording(discard=True)
            except Exception as e:
                log.warning(f"recorder.stop_recording raised: {e}")
            try:
                self._overlay.stop()
            except Exception as e:
                log.warning(f"overlay.stop raised: {e}")
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
            self._edit_active = False
        self._capture_mode = "dictate"
        self._paused = True
        self._notify("paused")

    def resume(self) -> None:
        """Re-install keyboard hooks (both controllers). Sets self._paused = False."""
        self._paused = False
        if self._controller is not None or self._edit_controller is not None:
            self._install_controllers()
        else:
            # Never started; start() will install the hooks fresh.
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
