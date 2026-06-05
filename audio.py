import pyaudio
import numpy as np
import threading
import queue

import logging
log = logging.getLogger("whisper2.audio")


class ContinuousAudioRecorder:
    """Microphone capture with two modes.

    - single_shot=True (push-to-talk): accumulate everything, flush only on stop.
    - single_shot=False (continuous): chunk on silence boundaries so transcription
      can run while the user is still speaking.

    Safe under rapid start/stop sequences (e.g. double-tap detection): each
    stop_recording() joins the previous capture thread before returning, and
    each capture thread owns its own stream snapshot so a stale thread can
    never close a freshly opened stream.
    """

    def __init__(self, chunk=1024, format=pyaudio.paInt16, channels=1, rate=16000,
                 silence_threshold=0.015, silence_duration=1.5,
                 min_chunk_duration_s=0.5):
        self.chunk = chunk
        self.format = format
        self.channels = channels
        self.rate = rate
        self.p = pyaudio.PyAudio()
        self.recording = False
        self.stream = None
        self.audio_queue = queue.Queue()
        self.silence_threshold = silence_threshold
        self.silence_duration = silence_duration
        self.min_chunk_duration_s = min_chunk_duration_s
        self._discard_on_stop = False
        self._single_shot = False
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()

    def start_recording(self, single_shot: bool = False):
        with self._state_lock:
            if self.recording:
                return
            prev = self._thread
        # Join any lingering previous thread OUTSIDE the lock to avoid deadlock.
        if prev is not None and prev.is_alive():
            prev.join(timeout=1.0)
        with self._state_lock:
            if self.recording:
                return
            self.recording = True
            self._discard_on_stop = False
            self._single_shot = single_shot
            try:
                self.stream = self.p.open(
                    format=self.format, channels=self.channels, rate=self.rate,
                    input=True, frames_per_buffer=self.chunk,
                )
            except Exception as e:
                log.error(f"[audio] failed to open stream: {e}")
                self.recording = False
                # PyAudio caches the device list at PyAudio() construction.
                # If the mic was plugged in / turned on AFTER we constructed
                # this instance, p.open() will keep failing until we reinit.
                # Rebuild so the next start_recording() sees current devices.
                try:
                    self.p.terminate()
                except Exception:
                    pass
                try:
                    self.p = pyaudio.PyAudio()
                    log.info("[audio] reinitialized PyAudio after open failure")
                except Exception as ee:
                    log.error(f"[audio] PyAudio reinit failed: {ee}")
                return
            self._thread = threading.Thread(target=self._record_loop,
                                            args=(self.stream,), daemon=True)
            self._thread.start()

    def stop_recording(self, discard: bool = False):
        with self._state_lock:
            if not self.recording:
                return
            self._discard_on_stop = discard
            self.recording = False
            t = self._thread
        if t is not None:
            t.join(timeout=1.0)

    def _record_loop(self, stream):
        frames: list[bytes] = []
        silence_frames = 0
        frames_per_sec = self.rate / self.chunk
        max_silence_frames = int(self.silence_duration * frames_per_sec)
        min_frames = int(self.min_chunk_duration_s * frames_per_sec)

        while self.recording:
            try:
                data = stream.read(self.chunk, exception_on_overflow=False)
            except OSError as e:
                log.warning(f"[audio] read error: {e}")
                break

            if self._single_shot:
                frames.append(data)
                continue

            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(np.square(arr))))
            if rms > self.silence_threshold:
                frames.append(data)
                silence_frames = 0
            elif frames:
                frames.append(data)
                silence_frames += 1
                if silence_frames >= max_silence_frames:
                    if len(frames) > min_frames:
                        self.audio_queue.put(self._to_float32(frames))
                    frames = []
                    silence_frames = 0

        # Close OUR stream snapshot. Don't touch self.stream if start_recording
        # already replaced it with a newer stream.
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        with self._state_lock:
            if self.stream is stream:
                self.stream = None

        if frames and not self._discard_on_stop and len(frames) > min_frames:
            self.audio_queue.put(self._to_float32(frames))

    @staticmethod
    def _to_float32(frames: list[bytes]) -> np.ndarray:
        raw = b"".join(frames)
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def __del__(self):
        try:
            self.p.terminate()
        except Exception:
            pass
