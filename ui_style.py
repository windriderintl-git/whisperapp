"""Whisper 2.0 — shared UI style vocabulary.

Single source of truth for the state -> color/label mapping used by BOTH the
on-screen HUD (overlay.py) and the system tray (tray_app.py). Before this
module existed the same table lived in three places and had to be kept in
sync by hand.

States: idle / recording / transcribing / polishing / editing / paused /
degraded:ollama / no_mic.
"""
from dataclasses import dataclass

# Brand purple — matches tools/render_icons.py (the app .ico background).
BRAND_RGB = (109, 76, 165)


@dataclass(frozen=True)
class StateStyle:
    rgb: tuple          # accent color for this state
    label: str          # short human label (HUD text, tray menu)
    filled: bool        # filled dot vs. ring (ring = attention/inactive)
    tooltip: str        # tray icon tooltip


STATES: dict[str, StateStyle] = {
    "idle": StateStyle(
        (142, 142, 147), "Ready", True,
        "Whisper 2 — Ready. Hold Ctrl+Win to dictate."),
    "recording": StateStyle(
        (235, 68, 60), "Recording…", True,
        "Whisper 2 — Recording…"),
    "transcribing": StateStyle(
        (240, 165, 30), "Transcribing…", True,
        "Whisper 2 — Transcribing…"),
    "polishing": StateStyle(
        (240, 165, 30), "Polishing…", True,
        "Whisper 2 — Polishing…"),
    "editing": StateStyle(
        (70, 145, 235), "Editing selection…", True,
        "Whisper 2 — Editing selection…"),
    "paused": StateStyle(
        (142, 142, 147), "Paused", False,
        "Whisper 2 — Paused"),
    "degraded:ollama": StateStyle(
        (235, 200, 40), "Ollama down (raw)", True,
        "Whisper 2 — Ollama not running (raw transcripts)"),
    "no_mic": StateStyle(
        (235, 68, 60), "Microphone unavailable", False,
        "Whisper 2 — Microphone unavailable"),
}

DEFAULT = StateStyle((142, 142, 147), "", True, "Whisper 2")


def get(state: str) -> StateStyle:
    return STATES.get(state, DEFAULT)


def to_hex(rgb) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))
