"""Tk-based modal Settings dialog for Whisper 2.

Public entry point: ``settings_ui.open(parent_app=None) -> bool``.

The dialog edits the user's ``config.yaml`` in place (round-tripped through a
dict so untouched keys are preserved). It also manages the optional
"Run at Windows startup" shortcut in the user's Startup folder.

Used by the tray icon's "Settings..." menu item. Blocks until the user
dismisses the dialog. Returns True if the user saved, False if cancelled.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

import yaml

import paths

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODIFIER_CHOICES = ["Ctrl", "Shift", "Alt", "Win"]
# UI label -> value persisted to config (the `keyboard` library uses "windows").
_MODIFIER_TO_CONFIG = {
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "win": "windows",
}
_MODIFIER_FROM_CONFIG = {
    "ctrl": "Ctrl",
    "shift": "Shift",
    "alt": "Alt",
    "windows": "Win",
    "win": "Win",
}

_WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en"]

# output radio button label -> config value
_OUTPUT_LABELS = [
    ("Type into focused window", "type"),
    ("Copy to clipboard", "clipboard"),
    ("Print to log", "terminal"),
]

_FALLBACK_MODELS = ["qwen2.5:3b", "qwen2.5:7b", "llama3.2:3b", "phi3:mini"]

# Polish intensity: UI label <-> config value (lowercase).
_POLISH_INTENSITY_LABELS = ["Light", "Standard", "Aggressive"]
_POLISH_INTENSITY_TOOLTIPS = {
    "Light": "Preserves every word. Only fixes punctuation.",
    "Standard": "Removes filler (um, uh). Fixes sentence boundaries.",
    "Aggressive": "Combines fragments. Rephrases for flow. Best for posts/essays.",
}

# Keys whose changes require an app restart to take effect.
_RESTART_REQUIRED_KEYS = ("hotkey.modifiers", "whisper.model")

# Built-in defaults used only if config.yaml is missing entirely.
_DEFAULT_CONFIG: dict = {
    "hotkey": {
        "modifiers": ["ctrl", "windows"],
        "hold_threshold_ms": 350,
        "double_tap_window_ms": 1200,
        "debug": False,
    },
    "whisper": {
        "model": "small.en",
        "device": "auto",
        "compute_type": "auto",
        "beam_size": 1,
    },
    "audio": {
        "sample_rate": 16000,
        "silence_threshold": 0.015,
        "silence_duration_s": 1.5,
        "min_chunk_duration_s": 0.5,
    },
    "llm": {
        "enabled": True,
        "backend": "ollama",
        "model": "qwen2.5:3b",
        "host": "http://localhost:11434",
        "timeout_s": 8.0,
        "warmup_on_start": True,
        "skip_below_words": 4,
    },
    "context": {"enabled": True, "override": None},
    "output": {"mode": "type", "trailing_space": True},
    "edit_mode": {
        "enabled": True,
        "modifiers": ["ctrl", "alt"],
        "hold_threshold_ms": 350,
    },
    "commands": {"enabled": True},
    "snippets": {"enabled": True},
    "ui": {"overlay": True, "overlay_position": "bottom"},
    "vocabulary": {},
}

_OVERLAY_POSITIONS = ["bottom", "top", "cursor"]

# Wispr Flow-style palette: warm off-white chrome, white content panel,
# near-black text and primary actions, hairline borders.
_BG = "#f4f2ef"            # app / sidebar background
_BG_CONTENT = "#fdfdfc"    # content panel
_BORDER = "#e6e3de"        # hairline borders / dividers
_TEXT = "#1c1c1e"
_TEXT_MUTED = "#6f6b66"
_NAV_ACTIVE = "#e9e6e0"    # active sidebar item
_NAV_HOVER = "#eeece7"

_FONT_BASE = ("Segoe UI", 10)
_FONT_NAV = ("Segoe UI", 11)
_FONT_BRAND = ("Segoe UI", 12, "bold")
_FONT_PAGE_TITLE = ("Segoe UI", 16, "bold")
_FONT_SECTION = ("Segoe UI", 11, "bold")

# Button palette (matches the HUD's dark-pill look).
_BTN_PRIMARY_BG = "#1c1c1e"
_BTN_PRIMARY_BG_ACTIVE = "#3a3a3c"
_BTN_PRIMARY_FG = "#ffffff"
_BTN_SECONDARY_BG = "#e8e5df"
_BTN_SECONDARY_BG_ACTIVE = "#dcd8d1"
_BTN_SECONDARY_FG = "#1c1c1e"
_BTN_FONT = ("Segoe UI", 10)


def _pill_button(parent: tk.Misc, text: str, command,
                 primary: bool = False, small: bool = False) -> tk.Button:
    """Flat, generously padded button (Wispr Flow-style pill).

    tk.Button rather than ttk.Button because the vista theme draws native
    buttons and ignores background/padding styling.
    """
    padx, pady = (12, 4) if small else (18, 7)
    if primary:
        colors = dict(bg=_BTN_PRIMARY_BG, fg=_BTN_PRIMARY_FG,
                      activebackground=_BTN_PRIMARY_BG_ACTIVE,
                      activeforeground=_BTN_PRIMARY_FG)
    else:
        colors = dict(bg=_BTN_SECONDARY_BG, fg=_BTN_SECONDARY_FG,
                      activebackground=_BTN_SECONDARY_BG_ACTIVE,
                      activeforeground=_BTN_SECONDARY_FG)
    return tk.Button(parent, text=text, command=command, relief="flat",
                     bd=0, cursor="hand2", padx=padx, pady=pady,
                     font=_BTN_FONT, **colors)


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def _read_current_config_dict() -> dict:
    """Load the active config.yaml as a plain dict.

    Falls back to a sane built-in default that mirrors the shape of the
    bundled config.yaml if the file is missing (shouldn't happen after
    first-run, but be defensive).
    """
    try:
        path = paths.resolve_config_path()
    except Exception:  # noqa: BLE001 - never let path resolution crash settings
        log.exception("resolve_config_path failed; using built-in defaults")
        return json.loads(json.dumps(_DEFAULT_CONFIG))

    # NB: Path.open, not the builtin — the module-level `open()` entry point
    # shadows the builtin within this module.
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            log.warning("config.yaml did not parse to a dict; using defaults")
            return json.loads(json.dumps(_DEFAULT_CONFIG))
        return data
    except FileNotFoundError:
        log.info("config.yaml not found at %s; using defaults", path)
        return json.loads(json.dumps(_DEFAULT_CONFIG))
    except Exception:  # noqa: BLE001
        log.exception("Failed to read config.yaml; using defaults")
        return json.loads(json.dumps(_DEFAULT_CONFIG))


def _write_config(cfg: dict) -> None:
    path = paths.resolve_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Ollama discovery
# ---------------------------------------------------------------------------

def _fetch_ollama_models(host: str = "http://localhost:11434",
                         timeout: float = 1.5) -> list[str]:
    """Query Ollama for installed models; fall back to a static list."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        names = sorted({m["name"] for m in data.get("models", []) if "name" in m})
        return names or list(_FALLBACK_MODELS)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return list(_FALLBACK_MODELS)
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error fetching Ollama models")
        return list(_FALLBACK_MODELS)


# ---------------------------------------------------------------------------
# Autostart (Windows Startup folder .lnk)
# ---------------------------------------------------------------------------

def _startup_lnk_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "Whisper 2.lnk")


def _autostart_target_and_args() -> tuple[Path, str]:
    """Return (exe_path, args_string) used for the Startup shortcut."""
    exe = Path(sys.executable)
    if paths.is_frozen():
        return exe, ""
    # Dev: launch python.exe with tray_app.py in this repo.
    tray_script = Path(__file__).resolve().parent / "tray_app.py"
    return exe, str(tray_script)


def _make_shortcut(target_lnk: Path, exe: Path, args: str = "") -> None:
    """Create a .lnk file using pywin32. Imports are local so non-Windows
    dev environments can still import this module."""
    import pythoncom  # type: ignore
    from win32com.client import Dispatch  # type: ignore

    target_lnk.parent.mkdir(parents=True, exist_ok=True)
    pythoncom.CoInitialize()
    try:
        shell = Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(target_lnk))
        sc.TargetPath = str(exe)
        sc.Arguments = args
        sc.WorkingDirectory = str(exe.parent)
        sc.Save()
    finally:
        pythoncom.CoUninitialize()


def _apply_autostart(enabled: bool) -> None:
    """Create or remove the Startup shortcut to match the desired state."""
    target_lnk = _startup_lnk_path()
    if enabled:
        exe, args = _autostart_target_and_args()
        try:
            _make_shortcut(target_lnk, exe, args)
        except Exception:  # noqa: BLE001
            log.exception("Failed to create autostart shortcut at %s", target_lnk)
            messagebox.showerror(
                "Whisper 2 - Settings",
                f"Couldn't create the startup shortcut:\n{target_lnk}\n\n"
                "Run-at-startup was NOT enabled.",
            )
    else:
        try:
            target_lnk.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            log.exception("Failed to remove autostart shortcut at %s", target_lnk)


def _autostart_currently_enabled() -> bool:
    return _startup_lnk_path().exists()


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class _SettingsDialog:
    """Internal helper: builds the modal Tk dialog and tracks state."""

    def __init__(self, parent: Optional[tk.Misc], parent_app=None) -> None:
        self.parent_app = parent_app
        self.saved = False
        self.restart_after_save = False

        # Owns its own root only if no parent was provided.
        self._owns_root = parent is None
        if parent is None:
            self._root = tk.Tk()
            self._root.withdraw()
            self.dialog = tk.Toplevel(self._root)
        else:
            self._root = None
            self.dialog = tk.Toplevel(parent)

        self.dialog.title("Whisper 2 - Settings")
        self.dialog.resizable(True, True)
        # Prevent close-via-X from being treated as "save".
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # ttk theme - prefer "vista" on Windows for a nicer look.
        style = ttk.Style(self.dialog)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except tk.TclError:
            pass

        # Load current config and snapshot the keys we may need to detect changes.
        self._cfg = _read_current_config_dict()
        self._snapshot = self._capture_restart_snapshot(self._cfg)

        # Tk variables ---------------------------------------------------
        mods = self._cfg.get("hotkey", {}).get("modifiers", ["ctrl", "windows"])
        mod1 = _MODIFIER_FROM_CONFIG.get(
            str(mods[0]).lower() if len(mods) > 0 else "ctrl", "Ctrl")
        mod2 = _MODIFIER_FROM_CONFIG.get(
            str(mods[1]).lower() if len(mods) > 1 else "win", "Win")
        self.var_mod1 = tk.StringVar(value=mod1)
        self.var_mod2 = tk.StringVar(value=mod2)

        whisper_model = self._cfg.get("whisper", {}).get("model", "small.en")
        if whisper_model not in _WHISPER_MODELS:
            # Keep unknown values visible so users don't silently lose them.
            self._whisper_choices = _WHISPER_MODELS + [whisper_model]
        else:
            self._whisper_choices = list(_WHISPER_MODELS)
        self.var_whisper_model = tk.StringVar(value=whisper_model)

        output_mode = self._cfg.get("output", {}).get("mode", "type")
        if output_mode not in {v for _, v in _OUTPUT_LABELS}:
            output_mode = "type"
        self.var_output_mode = tk.StringVar(value=output_mode)

        self.var_llm_enabled = tk.BooleanVar(
            value=bool(self._cfg.get("llm", {}).get("enabled", True)))

        polish_raw = str(self._cfg.get("llm", {}).get("polish_intensity",
                                                     "standard")).lower()
        polish_label = {
            "light": "Light",
            "standard": "Standard",
            "aggressive": "Aggressive",
        }.get(polish_raw, "Standard")
        self.var_polish_intensity = tk.StringVar(value=polish_label)
        self.var_polish_tooltip = tk.StringVar(
            value=_POLISH_INTENSITY_TOOLTIPS[polish_label])

        ollama_host = self._cfg.get("llm", {}).get("host", "http://localhost:11434")
        self._ollama_models = _fetch_ollama_models(ollama_host)
        current_model = str(self._cfg.get("llm", {}).get("model", "qwen2.5:3b"))
        if current_model and current_model not in self._ollama_models:
            self._ollama_models = [current_model] + self._ollama_models
        self.var_ollama_model = tk.StringVar(value=current_model)

        self.var_autostart = tk.BooleanVar(value=_autostart_currently_enabled())

        # On-screen HUD (ui:) ------------------------------------------------
        ui_cfg = self._cfg.get("ui", {}) or {}
        self.var_overlay = tk.BooleanVar(value=bool(ui_cfg.get("overlay", True)))
        overlay_pos = str(ui_cfg.get("overlay_position", "bottom")).lower()
        if overlay_pos not in _OVERLAY_POSITIONS:
            overlay_pos = "bottom"
        self.var_overlay_position = tk.StringVar(value=overlay_pos)

        # Voice commands (commands:) ----------------------------------------
        cmd_cfg = self._cfg.get("commands", {}) or {}
        self.var_commands_enabled = tk.BooleanVar(
            value=bool(cmd_cfg.get("enabled", True)))

        # Edit mode (edit_mode:) --------------------------------------------
        edit_cfg = self._cfg.get("edit_mode", {}) or {}
        self.var_edit_enabled = tk.BooleanVar(
            value=bool(edit_cfg.get("enabled", True)))

        self._build_ui()
        self._size_and_center()

        # Modal grab
        if parent is not None:
            try:
                self.dialog.transient(parent)
            except tk.TclError:
                pass
        self.dialog.grab_set()
        self.dialog.focus_set()

    # -- snapshotting -----------------------------------------------------

    @staticmethod
    def _capture_restart_snapshot(cfg: dict) -> dict:
        hotkey = cfg.get("hotkey", {}) or {}
        whisper = cfg.get("whisper", {}) or {}
        mods = list(hotkey.get("modifiers", []) or [])
        return {
            "hotkey.modifiers": [str(m).lower() for m in mods],
            "whisper.model": whisper.get("model"),
        }

    # -- layout ----------------------------------------------------------

    def _build_ui(self) -> None:
        d = self.dialog
        d.configure(bg=_BG)

        # Restyle the themed widgets that sit on the white content panel.
        style = ttk.Style(d)
        style.configure("TFrame", background=_BG_CONTENT)
        style.configure("TLabel", background=_BG_CONTENT, foreground=_TEXT,
                        font=_FONT_BASE)
        style.configure("TCheckbutton", background=_BG_CONTENT,
                        foreground=_TEXT, font=_FONT_BASE)
        style.configure("TRadiobutton", background=_BG_CONTENT,
                        foreground=_TEXT, font=_FONT_BASE)
        style.configure("Treeview", background="#ffffff",
                        fieldbackground="#ffffff", foreground=_TEXT,
                        rowheight=30, font=_FONT_BASE, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                        foreground=_TEXT_MUTED, relief="flat")

        # Sidebar ------------------------------------------------------------
        sidebar = tk.Frame(d, bg=_BG, width=180)
        sidebar.pack(side="left", fill="y", padx=(14, 0), pady=16)
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="Whisper 2", bg=_BG, fg=_TEXT,
                 font=_FONT_BRAND, anchor="w", padx=14
                 ).pack(fill="x", pady=(4, 18))

        self._nav_labels: dict[str, tk.Label] = {}
        self._pages: dict[str, tk.Frame] = {}
        self._active_page: Optional[str] = None
        for key, label in (("general", "General"),
                           ("dictation", "Dictation"),
                           ("vocabulary", "Vocabulary"),
                           ("snippets", "Snippets")):
            self._add_nav_item(sidebar, key, label)

        # Right column ---------------------------------------------------------
        right = tk.Frame(d, bg=_BG)
        right.pack(side="left", fill="both", expand=True, padx=16, pady=16)

        # Bottom action bar is packed FIRST and anchored to the bottom edge:
        # when the window is shorter than the content wants (e.g. display
        # scaling > 100%), Tk clips the last-packed widget — that must be the
        # page body, never the Save/Cancel row.
        btns = tk.Frame(right, bg=_BG)
        btns.pack(side="bottom", fill="x", pady=(12, 0))
        tk.Frame(btns, bg=_BG).pack(side="left", expand=True, fill="x")
        _pill_button(btns, "Cancel", self._on_cancel
                     ).pack(side="left", padx=4)
        _pill_button(btns, "Save & Restart", self._on_save_restart
                     ).pack(side="left", padx=4)
        _pill_button(btns, "Save", self._on_save, primary=True
                     ).pack(side="left", padx=(4, 0))

        # White content panel the pages live on.
        self._page_container = tk.Frame(right, bg=_BG_CONTENT,
                                        highlightbackground=_BORDER,
                                        highlightthickness=1)
        self._page_container.pack(fill="both", expand=True)

        self._build_general_page()
        self._build_dictation_page()
        self._build_vocab_page()
        self._build_snippets_page()
        self._show_page("general")

    # -- sidebar nav -------------------------------------------------------

    def _add_nav_item(self, sidebar: tk.Frame, key: str, label: str) -> None:
        lbl = tk.Label(sidebar, text=label, bg=_BG, fg=_TEXT, font=_FONT_NAV,
                       anchor="w", padx=14, pady=7, cursor="hand2")
        lbl.pack(fill="x", pady=1)
        lbl.bind("<Button-1>", lambda _e: self._show_page(key))
        lbl.bind("<Enter>", lambda _e: self._nav_hover(key, True))
        lbl.bind("<Leave>", lambda _e: self._nav_hover(key, False))
        self._nav_labels[key] = lbl

    def _nav_hover(self, key: str, entering: bool) -> None:
        if key == self._active_page:
            return
        self._nav_labels[key].configure(bg=_NAV_HOVER if entering else _BG)

    def _show_page(self, key: str) -> None:
        if key == self._active_page:
            return
        if self._active_page is not None:
            self._pages[self._active_page].pack_forget()
            self._nav_labels[self._active_page].configure(bg=_BG)
        self._pages[key].pack(fill="both", expand=True, padx=26, pady=(20, 22))
        self._nav_labels[key].configure(bg=_NAV_ACTIVE)
        self._active_page = key

    # -- page scaffolding ----------------------------------------------------

    def _new_page(self, key: str, title: str, action_text: str = "",
                  action_cmd=None) -> tk.Frame:
        """Page with a Flow-style header: bold title left, optional dark
        primary action pill right."""
        page = tk.Frame(self._page_container, bg=_BG_CONTENT)
        self._pages[key] = page
        header = tk.Frame(page, bg=_BG_CONTENT)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(header, text=title, bg=_BG_CONTENT, fg=_TEXT,
                 font=_FONT_PAGE_TITLE).pack(side="left")
        if action_text:
            _pill_button(header, action_text, action_cmd, primary=True,
                         small=True).pack(side="right")
        return page

    def _section(self, page: tk.Frame, heading: str, desc: str = "",
                 first: bool = False) -> tk.Frame:
        """Flat section: hairline divider, bold heading, optional muted
        description; returns the body frame for controls."""
        if not first:
            tk.Frame(page, bg=_BORDER, height=1).pack(fill="x", pady=(14, 0))
        tk.Label(page, text=heading, bg=_BG_CONTENT, fg=_TEXT,
                 font=_FONT_SECTION, anchor="w").pack(fill="x", pady=(12, 2))
        if desc:
            tk.Label(page, text=desc, bg=_BG_CONTENT, fg=_TEXT_MUTED,
                     font=_FONT_BASE, anchor="w", justify="left",
                     wraplength=440).pack(fill="x", pady=(0, 2))
        body = tk.Frame(page, bg=_BG_CONTENT)
        body.pack(fill="x", pady=(4, 0))
        return body

    @staticmethod
    def _lbl(parent: tk.Misc, text: str, muted: bool = False) -> tk.Label:
        return tk.Label(parent, text=text, bg=_BG_CONTENT,
                        fg=_TEXT_MUTED if muted else _TEXT, font=_FONT_BASE)

    def _tree_card(self, page: tk.Frame, columns: tuple[str, str],
                   headings: tuple[str, str]) -> ttk.Treeview:
        """White hairline-bordered list card holding a two-column Treeview."""
        card = tk.Frame(page, bg="#ffffff", highlightbackground=_BORDER,
                        highlightthickness=1)
        card.pack(fill="both", expand=True, pady=(8, 0))
        tree = ttk.Treeview(card, columns=columns, show="headings",
                            height=10, selectmode="browse")
        tree.heading(columns[0], text=headings[0], anchor="w")
        tree.heading(columns[1], text=headings[1], anchor="w")
        tree.column(columns[0], width=160, anchor="w", stretch=False)
        tree.column(columns[1], width=320, anchor="w", stretch=True)
        vscroll = ttk.Scrollbar(card, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vscroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        vscroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
        return tree

    # -- pages ---------------------------------------------------------------

    def _build_general_page(self) -> None:
        page = self._new_page("general", "General")

        hk = self._section(page, "Hotkey",
                           "Hold both to dictate. Double-tap toggles "
                           "continuous mode.", first=True)
        self._lbl(hk, "Modifier 1").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(hk, textvariable=self.var_mod1, state="readonly",
                     values=_MODIFIER_CHOICES, width=10
                     ).grid(row=0, column=1, sticky="w", padx=(10, 28), pady=4)
        self._lbl(hk, "Modifier 2").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Combobox(hk, textvariable=self.var_mod2, state="readonly",
                     values=_MODIFIER_CHOICES, width=10
                     ).grid(row=0, column=3, sticky="w", padx=(10, 0), pady=4)

        of = self._section(page, "Output")
        for i, (label, value) in enumerate(_OUTPUT_LABELS):
            ttk.Radiobutton(of, text=label, value=value,
                            variable=self.var_output_mode
                            ).grid(row=i, column=0, sticky="w", pady=2)

        hud = self._section(page, "On-screen HUD")
        ttk.Checkbutton(hud, text="Show status pill while dictating",
                        variable=self.var_overlay
                        ).grid(row=0, column=0, columnspan=2, sticky="w",
                               pady=2)
        self._lbl(hud, "Position").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(hud, textvariable=self.var_overlay_position,
                     state="readonly", values=_OVERLAY_POSITIONS, width=12
                     ).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

        st = self._section(page, "Startup")
        ttk.Checkbutton(st, text="Run Whisper 2 at Windows startup",
                        variable=self.var_autostart
                        ).grid(row=0, column=0, sticky="w", pady=2)

    def _build_dictation_page(self) -> None:
        page = self._new_page("dictation", "Dictation")

        tr = self._section(page, "Transcription", first=True)
        self._lbl(tr, "Whisper model").grid(row=0, column=0, sticky="w",
                                            pady=4)
        ttk.Combobox(tr, textvariable=self.var_whisper_model,
                     state="readonly", values=self._whisper_choices, width=16
                     ).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)

        lf = self._section(page, "LLM polish (Ollama)")
        ttk.Checkbutton(lf, text="Polish transcripts with a local LLM",
                        variable=self.var_llm_enabled
                        ).grid(row=0, column=0, columnspan=2, sticky="w",
                               pady=2)
        self._lbl(lf, "Intensity").grid(row=1, column=0, sticky="w", pady=4)
        intensity_cb = ttk.Combobox(lf, textvariable=self.var_polish_intensity,
                                    state="readonly",
                                    values=_POLISH_INTENSITY_LABELS, width=16)
        intensity_cb.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)
        intensity_cb.bind("<<ComboboxSelected>>",
                          self._on_polish_intensity_changed)
        tk.Label(lf, textvariable=self.var_polish_tooltip, bg=_BG_CONTENT,
                 fg=_TEXT_MUTED, font=_FONT_BASE, anchor="w", justify="left",
                 wraplength=440).grid(row=2, column=0, columnspan=2,
                                      sticky="w", pady=(0, 4))
        self._lbl(lf, "Model").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(lf, textvariable=self.var_ollama_model, state="normal",
                     values=self._ollama_models, width=28
                     ).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=4)

        cf = self._section(page, "Voice commands")
        ttk.Checkbutton(cf, text='Enable spoken commands ("press enter", '
                                 '"new line", "scratch that")',
                        variable=self.var_commands_enabled
                        ).grid(row=0, column=0, sticky="w", pady=2)

        ef = self._section(page, "Edit mode",
                           "Hold Ctrl+Alt, highlight some text, then speak an "
                           "instruction (e.g. \"make this formal\").")
        ttk.Checkbutton(ef, text="Edit highlighted text by voice",
                        variable=self.var_edit_enabled
                        ).grid(row=0, column=0, sticky="w", pady=2)

    def _build_vocab_page(self) -> None:
        page = self._new_page("vocabulary", "Vocabulary", "Add new",
                              self._on_vocab_add)
        tk.Label(page, text="Forced spelling fixes applied after polish. One "
                            "canonical term per row; variants are matched "
                            "case-insensitively.",
                 bg=_BG_CONTENT, fg=_TEXT_MUTED, font=_FONT_BASE, anchor="w",
                 justify="left", wraplength=460).pack(fill="x")

        self.vocab_tree = self._tree_card(page, ("canonical", "variants"),
                                          ("Canonical", "Variants"))
        self.vocab_tree.bind("<Double-1>", lambda _e: self._on_vocab_edit())

        # Populate from cfg.
        vocab_cfg = self._cfg.get("vocabulary") or {}
        if isinstance(vocab_cfg, dict):
            for canon, variants in vocab_cfg.items():
                canon_str = str(canon).strip()
                if not canon_str:
                    continue
                if isinstance(variants, (list, tuple)):
                    variants_str = ", ".join(str(v) for v in variants)
                else:
                    variants_str = str(variants) if variants else ""
                self.vocab_tree.insert("", "end",
                                       values=(canon_str, variants_str))

        vbtns = tk.Frame(page, bg=_BG_CONTENT)
        vbtns.pack(fill="x", pady=(10, 0))
        _pill_button(vbtns, "Edit selected", self._on_vocab_edit, small=True
                     ).pack(side="left", padx=(0, 6))
        _pill_button(vbtns, "Remove selected", self._on_vocab_remove,
                     small=True).pack(side="left", padx=6)

    def _build_snippets_page(self) -> None:
        page = self._new_page("snippets", "Snippets", "Add new",
                              self._on_snippet_add)
        tk.Label(page, text="Spoken trigger phrases that expand to canned "
                            "text (applied after polish). e.g. \"my email\" "
                            "-> your address.",
                 bg=_BG_CONTENT, fg=_TEXT_MUTED, font=_FONT_BASE, anchor="w",
                 justify="left", wraplength=460).pack(fill="x")

        self.snippet_tree = self._tree_card(page, ("trigger", "expansion"),
                                            ("Trigger", "Expansion"))
        self.snippet_tree.bind("<Double-1>", lambda _e: self._on_snippet_edit())

        # Populate from cfg (skip the `enabled` flag; keep only string pairs).
        snippets_cfg = self._cfg.get("snippets") or {}
        if isinstance(snippets_cfg, dict):
            for trig, expansion in snippets_cfg.items():
                if trig == "enabled":
                    continue
                trig_str = str(trig).strip()
                if not trig_str:
                    continue
                self.snippet_tree.insert("", "end",
                                         values=(trig_str, str(expansion)))

        sbtns = tk.Frame(page, bg=_BG_CONTENT)
        sbtns.pack(fill="x", pady=(10, 0))
        _pill_button(sbtns, "Edit selected", self._on_snippet_edit, small=True
                     ).pack(side="left", padx=(0, 6))
        _pill_button(sbtns, "Remove selected", self._on_snippet_remove,
                     small=True).pack(side="left", padx=6)

    def _size_and_center(self, min_w: int = 760, min_h: int = 560) -> None:
        """Size the window from what the built content actually requests, so
        larger fonts at high display scaling can't clip anything.

        Only the active page is mapped, but unmapped pages still report their
        requested size — pad the dialog's request by the difference to the
        largest page so switching pages never clips.
        """
        self.dialog.update_idletasks()
        active = self._pages[self._active_page]
        extra_w = max(p.winfo_reqwidth() for p in self._pages.values()) \
            - active.winfo_reqwidth()
        extra_h = max(p.winfo_reqheight() for p in self._pages.values()) \
            - active.winfo_reqheight()
        req_w = self.dialog.winfo_reqwidth() + max(0, extra_w)
        req_h = self.dialog.winfo_reqheight() + max(0, extra_h)
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        w = min(max(min_w, req_w), sw - 80)
        h = min(max(min_h, req_h), sh - 120)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")
        self.dialog.minsize(680, 520)

    # -- collectors ------------------------------------------------------

    def _collect_modifiers(self) -> list[str]:
        raw = [self.var_mod1.get(), self.var_mod2.get()]
        out: list[str] = []
        for label in raw:
            v = _MODIFIER_TO_CONFIG.get(str(label).lower())
            if v and v not in out:
                out.append(v)
        if not out:
            out = ["ctrl", "windows"]
        return out

    def _collect_vocabulary(self) -> dict:
        """Rebuild the vocabulary dict from the Treeview rows."""
        result: dict[str, list[str]] = {}
        for iid in self.vocab_tree.get_children(""):
            vals = self.vocab_tree.item(iid, "values")
            if not vals:
                continue
            canon = str(vals[0]).strip()
            if not canon:
                continue
            raw_variants = str(vals[1]) if len(vals) > 1 else ""
            variants = [v.strip() for v in raw_variants.split(",")]
            variants = [v for v in variants if v]
            result[canon] = variants
        return result

    def _collect_snippets(self) -> dict:
        """Rebuild the snippets map (trigger->expansion) from the Treeview,
        re-attaching the `enabled` flag from the checkbox."""
        result: dict[str, object] = {"enabled": self._snippets_enabled_persisted()}
        for iid in self.snippet_tree.get_children(""):
            vals = self.snippet_tree.item(iid, "values")
            if not vals:
                continue
            trigger = str(vals[0]).strip()
            if not trigger or trigger == "enabled":
                continue
            expansion = str(vals[1]) if len(vals) > 1 else ""
            result[trigger] = expansion
        return result

    def _snippets_enabled_persisted(self) -> bool:
        """Snippets have no dedicated enable checkbox in the UI; preserve the
        value already on disk (default True) so round-trips don't flip it."""
        snips = self._cfg.get("snippets", {}) or {}
        return bool(snips.get("enabled", True))

    # -- polish intensity -------------------------------------------------

    def _on_polish_intensity_changed(self, _event=None) -> None:
        label = self.var_polish_intensity.get()
        tip = _POLISH_INTENSITY_TOOLTIPS.get(label, "")
        self.var_polish_tooltip.set(tip)

    # -- vocabulary actions -----------------------------------------------

    def _on_vocab_add(self) -> None:
        result = self._open_vocab_editor("Add vocabulary entry", "", "")
        if result is None:
            return
        canon, variants = result
        if not canon.strip():
            return
        self.vocab_tree.insert("", "end", values=(canon.strip(), variants))

    def _on_vocab_edit(self) -> None:
        sel = self.vocab_tree.selection()
        if not sel:
            return
        iid = sel[0]
        vals = self.vocab_tree.item(iid, "values")
        canon_cur = str(vals[0]) if vals else ""
        variants_cur = str(vals[1]) if vals and len(vals) > 1 else ""
        result = self._open_vocab_editor("Edit vocabulary entry",
                                         canon_cur, variants_cur)
        if result is None:
            return
        canon, variants = result
        if not canon.strip():
            return
        self.vocab_tree.item(iid, values=(canon.strip(), variants))

    def _on_vocab_remove(self) -> None:
        sel = self.vocab_tree.selection()
        if not sel:
            return
        for iid in sel:
            self.vocab_tree.delete(iid)

    def _open_vocab_editor(self, title: str, canon: str,
                           variants: str) -> Optional[tuple[str, str]]:
        """Modal sub-dialog with two entry fields. Returns (canon, variants)
        on OK, None on Cancel."""
        top = tk.Toplevel(self.dialog)
        top.title(title)
        top.configure(bg=_BG_CONTENT)
        top.resizable(False, False)
        top.transient(self.dialog)
        try:
            top.grab_set()
        except tk.TclError:
            pass

        frm = ttk.Frame(top, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Canonical spelling:").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        var_canon = tk.StringVar(value=canon)
        ent_canon = ttk.Entry(frm, textvariable=var_canon, width=40)
        ent_canon.grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(frm, text="Variants (comma-separated):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        var_variants = tk.StringVar(value=variants)
        ent_variants = ttk.Entry(frm, textvariable=var_variants, width=40)
        ent_variants.grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        result: dict[str, Optional[tuple[str, str]]] = {"value": None}

        def on_ok() -> None:
            result["value"] = (var_canon.get(), var_variants.get())
            top.destroy()

        def on_cancel() -> None:
            result["value"] = None
            top.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        _pill_button(btns, "OK", on_ok, primary=True, small=True
                     ).pack(side="right", padx=(6, 0))
        _pill_button(btns, "Cancel", on_cancel, small=True
                     ).pack(side="right", padx=6)

        top.protocol("WM_DELETE_WINDOW", on_cancel)
        top.bind("<Return>", lambda _e: on_ok())
        top.bind("<Escape>", lambda _e: on_cancel())

        # Center over parent dialog.
        top.update_idletasks()
        try:
            px = self.dialog.winfo_rootx()
            py = self.dialog.winfo_rooty()
            pw = self.dialog.winfo_width()
            ph = self.dialog.winfo_height()
            tw = top.winfo_width()
            th = top.winfo_height()
            x = px + max(0, (pw - tw) // 2)
            y = py + max(0, (ph - th) // 3)
            top.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

        ent_canon.focus_set()
        top.wait_window()
        return result["value"]

    # -- snippet actions --------------------------------------------------

    def _on_snippet_add(self) -> None:
        result = self._open_snippet_editor("Add snippet", "", "")
        if result is None:
            return
        trigger, expansion = result
        if not trigger.strip():
            return
        self.snippet_tree.insert("", "end", values=(trigger.strip(), expansion))

    def _on_snippet_edit(self) -> None:
        sel = self.snippet_tree.selection()
        if not sel:
            return
        iid = sel[0]
        vals = self.snippet_tree.item(iid, "values")
        trig_cur = str(vals[0]) if vals else ""
        exp_cur = str(vals[1]) if vals and len(vals) > 1 else ""
        result = self._open_snippet_editor("Edit snippet", trig_cur, exp_cur)
        if result is None:
            return
        trigger, expansion = result
        if not trigger.strip():
            return
        self.snippet_tree.item(iid, values=(trigger.strip(), expansion))

    def _on_snippet_remove(self) -> None:
        for iid in self.snippet_tree.selection():
            self.snippet_tree.delete(iid)

    def _open_snippet_editor(self, title: str, trigger: str,
                             expansion: str) -> Optional[tuple[str, str]]:
        """Modal sub-dialog with trigger + expansion fields. Mirrors the vocab
        editor. Returns (trigger, expansion) on OK, None on Cancel."""
        top = tk.Toplevel(self.dialog)
        top.title(title)
        top.configure(bg=_BG_CONTENT)
        top.resizable(False, False)
        top.transient(self.dialog)
        try:
            top.grab_set()
        except tk.TclError:
            pass

        frm = ttk.Frame(top, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Spoken trigger:").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        var_trigger = tk.StringVar(value=trigger)
        ent_trigger = ttk.Entry(frm, textvariable=var_trigger, width=40)
        ent_trigger.grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        ttk.Label(frm, text="Expands to:").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        var_expansion = tk.StringVar(value=expansion)
        ent_expansion = ttk.Entry(frm, textvariable=var_expansion, width=40)
        ent_expansion.grid(row=1, column=1, sticky="ew", padx=4, pady=4)

        result: dict[str, Optional[tuple[str, str]]] = {"value": None}

        def on_ok() -> None:
            result["value"] = (var_trigger.get(), var_expansion.get())
            top.destroy()

        def on_cancel() -> None:
            result["value"] = None
            top.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        _pill_button(btns, "OK", on_ok, primary=True, small=True
                     ).pack(side="right", padx=(6, 0))
        _pill_button(btns, "Cancel", on_cancel, small=True
                     ).pack(side="right", padx=6)

        top.protocol("WM_DELETE_WINDOW", on_cancel)
        top.bind("<Return>", lambda _e: on_ok())
        top.bind("<Escape>", lambda _e: on_cancel())

        # Center over parent dialog.
        top.update_idletasks()
        try:
            px = self.dialog.winfo_rootx()
            py = self.dialog.winfo_rooty()
            pw = self.dialog.winfo_width()
            ph = self.dialog.winfo_height()
            tw = top.winfo_width()
            th = top.winfo_height()
            x = px + max(0, (pw - tw) // 2)
            y = py + max(0, (ph - th) // 3)
            top.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

        ent_trigger.focus_set()
        top.wait_window()
        return result["value"]

    # -- actions ---------------------------------------------------------

    def _on_cancel(self) -> None:
        self.saved = False
        self._teardown()

    def _on_save(self) -> None:
        if not self._write_settings():
            return
        if self._restart_required() and self._confirm_restart_prompt():
            self.restart_after_save = True
        self.saved = True
        self._teardown()

    def _on_save_restart(self) -> None:
        if not self._write_settings():
            return
        self.saved = True
        self.restart_after_save = True
        self._teardown()

    def _write_settings(self) -> bool:
        # Refresh from disk so we don't clobber edits made elsewhere.
        try:
            cfg = _read_current_config_dict()
        except Exception:  # noqa: BLE001
            log.exception("Failed reloading config prior to save")
            cfg = self._cfg

        cfg.setdefault("hotkey", {})["modifiers"] = self._collect_modifiers()
        cfg.setdefault("whisper", {})["model"] = self.var_whisper_model.get()
        cfg.setdefault("output", {})["mode"] = self.var_output_mode.get()
        llm = cfg.setdefault("llm", {})
        llm["enabled"] = bool(self.var_llm_enabled.get())
        model_name = self.var_ollama_model.get().strip()
        if model_name:
            llm["model"] = model_name
        intensity_label = self.var_polish_intensity.get().strip() or "Standard"
        llm["polish_intensity"] = intensity_label.lower()

        # On-screen HUD.
        ui = cfg.setdefault("ui", {})
        ui["overlay"] = bool(self.var_overlay.get())
        ui["overlay_position"] = self.var_overlay_position.get()

        # Voice commands.
        cfg.setdefault("commands", {})["enabled"] = bool(
            self.var_commands_enabled.get())

        # Edit mode: only the enable flag is exposed; preserve modifiers /
        # threshold already on disk (read-modify-write) so power-user edits stick.
        edit = cfg.setdefault("edit_mode", {})
        edit["enabled"] = bool(self.var_edit_enabled.get())
        edit.setdefault("modifiers", ["ctrl", "alt"])
        edit.setdefault("hold_threshold_ms", 350)

        # Vocabulary + snippets sections are fully owned by this dialog.
        cfg["vocabulary"] = self._collect_vocabulary()
        cfg["snippets"] = self._collect_snippets()

        try:
            _write_config(cfg)
        except Exception:  # noqa: BLE001
            log.exception("Failed writing config")
            messagebox.showerror("Whisper 2 - Settings",
                                 "Couldn't write config.yaml. Check the log for details.")
            return False

        # Persist the new snapshot so an in-session re-open compares against
        # what's actually on disk now.
        self._cfg = cfg
        new_snapshot = self._capture_restart_snapshot(cfg)
        self._restart_diff = (new_snapshot != self._snapshot)
        self._snapshot = new_snapshot

        _apply_autostart(bool(self.var_autostart.get()))
        return True

    def _restart_required(self) -> bool:
        return bool(getattr(self, "_restart_diff", False))

    def _confirm_restart_prompt(self) -> bool:
        return messagebox.askyesno(
            "Whisper 2 - Settings",
            "Some changes (hotkey or Whisper model) only take effect after a "
            "restart.\n\nRestart Whisper 2 now?",
        )

    def _teardown(self) -> None:
        try:
            self.dialog.grab_release()
        except tk.TclError:
            pass
        try:
            self.dialog.destroy()
        except tk.TclError:
            pass
        if self._owns_root and self._root is not None:
            try:
                self._root.destroy()
            except tk.TclError:
                pass

    # -- driver ----------------------------------------------------------

    def run(self) -> bool:
        self.dialog.wait_window()
        if self.saved and self.restart_after_save:
            _restart_app()
        return self.saved


def _restart_app() -> None:
    """Re-exec the current process and exit. Best-effort."""
    try:
        log.info("Restarting Whisper 2 after settings save")
        subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
    except Exception:  # noqa: BLE001
        log.exception("Failed to spawn restart process")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def open(parent_app=None) -> bool:  # noqa: A001 - matches required public API
    """Show the modal Settings dialog.

    Returns True if the user saved changes, False if cancelled. ``parent_app``
    is the running App instance; safe to ignore in v1. We try to use its Tk
    root as the parent if it exposes one (attributes ``root`` or ``tk``), so
    the dialog parents properly under the tray's hidden window.
    """
    parent: Optional[tk.Misc] = None
    if parent_app is not None:
        for attr in ("root", "tk_root", "tk", "_root"):
            val = getattr(parent_app, attr, None)
            if isinstance(val, tk.Misc):
                parent = val
                break

    try:
        dlg = _SettingsDialog(parent, parent_app=parent_app)
    except Exception:  # noqa: BLE001
        log.exception("Failed to build settings dialog")
        return False
    return dlg.run()
