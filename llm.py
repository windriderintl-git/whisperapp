"""Local LLM polish via Ollama. Falls back to raw text on any failure."""
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

import logging
import paths

log = logging.getLogger("whisper2.llm")

# Cache by (prompt_name, intensity) so different intensities don't collide.
_PROMPT_CACHE: dict[tuple[str, str], str] = {}

# Final fallback when nothing in the resolution chain hits.
_ULTIMATE_FALLBACK = "cleanup_default_standard.md"


def _load_prompt(name: str, intensity: str = "standard") -> str:
    """Resolve a prompt by (name, intensity) using this lookup order:

    1. USER_PROMPTS_DIR / "{name}_{intensity}.md"   — per-intensity user override
    2. USER_PROMPTS_DIR / "{name}.md"               — intensity-agnostic user override (back-compat)
    3. BUNDLED_PROMPTS_DIR / "{name}_{intensity}.md"
    4. BUNDLED_PROMPTS_DIR / "{name}.md"            — legacy bundled (back-compat)
    5. BUNDLED_PROMPTS_DIR / "cleanup_default_standard.md"  — ultimate fallback

    Caches by (name, intensity). Always returns a real prompt string.
    """
    key = (name, intensity)
    if key in _PROMPT_CACHE:
        return _PROMPT_CACHE[key]

    candidates = [
        paths.USER_PROMPTS_DIR / f"{name}_{intensity}.md",
        paths.USER_PROMPTS_DIR / f"{name}.md",
        paths.BUNDLED_PROMPTS_DIR / f"{name}_{intensity}.md",
        paths.BUNDLED_PROMPTS_DIR / f"{name}.md",
        paths.BUNDLED_PROMPTS_DIR / _ULTIMATE_FALLBACK,
    ]
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                _PROMPT_CACHE[key] = text
                return text
        except OSError:
            continue

    # Last-resort hard-coded fallback so polish() never raises on missing files.
    text = "Clean up this transcript. Remove disfluencies. Output only the cleaned text.\n\nRaw transcript:\n{text}\n\nCleaned text:\n"
    _PROMPT_CACHE[key] = text
    return text


class OllamaPolisher:
    def __init__(self, model: str = "qwen2.5:3b",
                 host: str = "http://localhost:11434",
                 timeout: float = 8.0, enabled: bool = True,
                 polish_intensity: str = "standard",
                 keep_alive: str = "30m"):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.enabled = enabled
        self.polish_intensity = polish_intensity
        # Without keep_alive Ollama unloads the model after ~5 min idle and
        # every dictation after a pause pays a multi-second cold start.
        self.keep_alive = keep_alive
        self._warned_unreachable = False

    def polish(self, text: str, prompt_name: str = "cleanup_default") -> str:
        if not self.enabled or not text.strip():
            return text
        try:
            template = _load_prompt(prompt_name, self.polish_intensity)
            full = template.replace("{text}", text)
            # Cleaned output is never much longer than the input; cap generation
            # accordingly instead of always allowing 768 tokens.
            num_predict = max(64, min(768, int(len(text.split()) * 2.5)))
            result = self._call(full, num_predict)
            return result if result else text
        except Exception as e:
            log.warning(f"[llm] polish failed: {e} — using raw text")
            return text

    def warmup(self):
        """Send a tiny generation to load the model into RAM/VRAM.
        Subsequent polish() calls hit the warm model with much lower latency.
        Runs synchronously; call from a background thread if you don't want
        to block startup.
        """
        if not self.enabled:
            return
        body = json.dumps({
            "model": self.model,
            "prompt": "ok",
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.0, "num_predict": 1},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=body,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                resp.read()
            log.info(f"[llm] warmup ok ({(time.time()-t0)*1000:.0f}ms)")
        except Exception as e:
            log.warning(f"[llm] warmup failed: {e}")

    def _call(self, prompt: str, num_predict: int = 768) -> str | None:
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.0, "num_predict": num_predict},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            elapsed = (time.time() - t0) * 1000
            log.info(f"[llm] polish ok ({elapsed:.0f}ms)")
            return _strip_wrapping(data.get("response", "").strip())
        except (urllib.error.URLError, TimeoutError) as e:
            if not self._warned_unreachable:
                log.warning(f"[llm] Ollama unreachable at {self.host} ({e}). "
                      f"Install from ollama.com and run: ollama pull {self.model}")
                self._warned_unreachable = True
            return None


def _strip_wrapping(text: str) -> str:
    """Models sometimes wrap output in quotes or code fences despite the prompt."""
    t = text.strip()
    for fence in ("```text", "```markdown", "```md", "```"):
        if t.startswith(fence):
            t = t[len(fence):].lstrip("\n")
            if t.endswith("```"):
                t = t[:-3].rstrip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
        t = t[1:-1]
    return t.strip()
