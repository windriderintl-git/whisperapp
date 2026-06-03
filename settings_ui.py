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
    "vocabulary": {},
}


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

    try:
        with open(path, "r", encoding="utf-8") as f:
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
    with open(path, "w", encoding="utf-8") as f:
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
        self.dialog.resizable(False, False)
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

        self._build_ui()
        self._size_and_center(460, 820)

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
        pad = {"padx": 10, "pady": 6}
        outer = ttk.Frame(self.dialog, padding=14)
        outer.pack(fill="both", expand=True)

        # Hotkey -----------------------------------------------------------
        hk = ttk.LabelFrame(outer, text="Hotkey")
        hk.pack(fill="x", **pad)

        ttk.Label(hk, text="Modifier 1:").grid(row=0, column=0, sticky="w",
                                               padx=8, pady=6)
        cb1 = ttk.Combobox(hk, textvariable=self.var_mod1, state="readonly",
                           values=_MODIFIER_CHOICES, width=10)
        cb1.grid(row=0, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(hk, text="Modifier 2:").grid(row=0, column=2, sticky="w",
                                               padx=8, pady=6)
        cb2 = ttk.Combobox(hk, textvariable=self.var_mod2, state="readonly",
                           values=_MODIFIER_CHOICES, width=10)
        cb2.grid(row=0, column=3, sticky="w", padx=8, pady=6)

        ttk.Label(hk, text="Hold both to dictate. Double-tap toggles "
                          "continuous mode.",
                  foreground="#666").grid(row=1, column=0, columnspan=4,
                                          sticky="w", padx=8, pady=(0, 6))

        # Whisper ----------------------------------------------------------
        wf = ttk.LabelFrame(outer, text="Transcription")
        wf.pack(fill="x", **pad)
        ttk.Label(wf, text="Whisper model:").grid(row=0, column=0, sticky="w",
                                                  padx=8, pady=6)
        ttk.Combobox(wf, textvariable=self.var_whisper_model, state="readonly",
                     values=self._whisper_choices, width=16
                     ).grid(row=0, column=1, sticky="w", padx=8, pady=6)

        # Output -----------------------------------------------------------
        of = ttk.LabelFrame(outer, text="Output")
        of.pack(fill="x", **pad)
        for i, (label, value) in enumerate(_OUTPUT_LABELS):
            ttk.Radiobutton(of, text=label, value=value,
                            variable=self.var_output_mode
                            ).grid(row=i, column=0, sticky="w", padx=8, pady=2)

        # LLM polish -------------------------------------------------------
        lf = ttk.LabelFrame(outer, text="LLM polish (Ollama)")
        lf.pack(fill="x", **pad)
        ttk.Checkbutton(lf, text="Polish transcripts with a local LLM",
                        variable=self.var_llm_enabled
                        ).grid(row=0, column=0, columnspan=2, sticky="w",
                               padx=8, pady=4)
        ttk.Label(lf, text="Intensity:").grid(row=1, column=0, sticky="w",
                                              padx=8, pady=4)
        intensity_cb = ttk.Combobox(lf, textvariable=self.var_polish_intensity,
                                    state="readonly",
                                    values=_POLISH_INTENSITY_LABELS, width=16)
        intensity_cb.grid(row=1, column=1, sticky="w", padx=8, pady=4)
        intensity_cb.bind("<<ComboboxSelected>>", self._on_polish_intensity_changed)
        ttk.Label(lf, textvariable=self.var_polish_tooltip,
                  foreground="#666", wraplength=400
                  ).grid(row=2, column=0, columnspan=2, sticky="w",
                         padx=8, pady=(0, 6))
        ttk.Label(lf, text="Model:").grid(row=3, column=0, sticky="w",
                                          padx=8, pady=4)
        ttk.Combobox(lf, textvariable=self.var_ollama_model, state="normal",
                     values=self._ollama_models, width=28
                     ).grid(row=3, column=1, sticky="w", padx=8, pady=4)

        # Vocabulary ------------------------------------------------------
        vf = ttk.LabelFrame(outer, text="Vocabulary corrections")
        vf.pack(fill="both", expand=True, **pad)
        ttk.Label(vf, text="Forced spelling fixes applied after polish. "
                          "One canonical term per row; variants are matched "
                          "case-insensitively.",
                  foreground="#666", wraplength=410
                  ).grid(row=0, column=0, columnspan=2, sticky="w",
                         padx=8, pady=(4, 6))

        tree_frame = ttk.Frame(vf)
        tree_frame.grid(row=1, column=0, columnspan=2, sticky="nsew",
                        padx=8, pady=4)
        vf.columnconfigure(0, weight=1)
        vf.rowconfigure(1, weight=1)

        self.vocab_tree = ttk.Treeview(
            tree_frame, columns=("canonical", "variants"),
            show="headings", height=10, selectmode="browse")
        self.vocab_tree.heading("canonical", text="Canonical")
        self.vocab_tree.heading("variants", text="Variants")
        # Column widths: 35% / 65% of ~410px usable width.
        self.vocab_tree.column("canonical", width=143, anchor="w",
                               stretch=False)
        self.vocab_tree.column("variants", width=267, anchor="w",
                               stretch=True)

        vscroll = ttk.Scrollbar(tree_frame, orient="vertical",
                                command=self.vocab_tree.yview)
        self.vocab_tree.configure(yscrollcommand=vscroll.set)
        self.vocab_tree.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        self.vocab_tree.bind("<Double-1>",
                             lambda _e: self._on_vocab_edit())

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

        vbtns = ttk.Frame(vf)
        vbtns.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        ttk.Button(vbtns, text="Add row", command=self._on_vocab_add
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(vbtns, text="Edit selected", command=self._on_vocab_edit
                   ).pack(side="left", padx=4)
        ttk.Button(vbtns, text="Remove selected", command=self._on_vocab_remove
                   ).pack(side="left", padx=4)

        # Autostart --------------------------------------------------------
        af = ttk.LabelFrame(outer, text="Startup")
        af.pack(fill="x", **pad)
        ttk.Checkbutton(af, text="Run Whisper 2 at Windows startup",
                        variable=self.var_autostart
                        ).grid(row=0, column=0, sticky="w", padx=8, pady=6)

        # Buttons ----------------------------------------------------------
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 0))
        # spacer pushes buttons right
        ttk.Frame(btns).pack(side="left", expand=True, fill="x")
        ttk.Button(btns, text="Cancel", command=self._on_cancel
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Save", command=self._on_save
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Save & Restart", command=self._on_save_restart
                   ).pack(side="left", padx=4)

    def _size_and_center(self, w: int, h: int) -> None:
        self.dialog.update_idletasks()
        sw = self.dialog.winfo_screenwidth()
        sh = self.dialog.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.dialog.geometry(f"{w}x{h}+{x}+{y}")
        self.dialog.minsize(w, h)

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
        btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=on_cancel
                   ).pack(side="right", padx=4)
        ttk.Button(btns, text="OK", command=on_ok
                   ).pack(side="right", padx=4)

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

        # Vocabulary section is fully owned by this dialog.
        cfg["vocabulary"] = self._collect_vocabulary()

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
