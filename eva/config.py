
"""Central configuration for EVA."""

import os
from pathlib import Path
from typing import Any


def load_dotenv_if_present(env_path: Path | str | None = None) -> None:
    """Load key-value pairs from a .env file into os.environ if present.

    Uses python-dotenv when installed; otherwise falls back to a clean,
    zero-dependency parser supporting comments, exports, and quoted values.
    Existing os.environ entries are preserved (never overwritten).
    """
    target = None
    if env_path is not None:
        target = Path(env_path)
    else:
        candidates = [
            Path(__file__).resolve().parent.parent / ".env",
            Path.cwd() / ".env",
        ]
        for candidate in candidates:
            if candidate.is_file():
                target = candidate
                break

    if not target or not target.is_file():
        return

    try:
        import dotenv
        dotenv.load_dotenv(dotenv_path=target, override=False)
        return
    except ImportError:
        pass

    try:
        content = target.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
                val = val[1:-1]
            os.environ.setdefault(key, val)
    except Exception:
        pass


load_dotenv_if_present()

# Local AI model served through Ollama. Set EVA_OLLAMA_URL when Ollama listens
# on a non-default host/port (for example, http://127.0.0.1:11434).
OLLAMA_URL = os.environ.get("EVA_OLLAMA_URL", "http://localhost:11434").strip().rstrip("/")

# Override with the EVA_MODEL environment variable or edit this line.
OLLAMA_MODEL = os.environ.get("EVA_MODEL", "qwen2.5-coder:3b")

def _parse_int_env(key: str, default: int) -> int:
    val = os.environ.get(key)
    if not val:
        return default
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return default


OLLAMA_TIMEOUT = max(1, min(300, _parse_int_env("EVA_OLLAMA_TIMEOUT", 120)))

NUM_PREDICT = _parse_int_env("EVA_NUM_PREDICT", 256)
NUM_CTX = _parse_int_env("EVA_NUM_CTX", 4096)
PLANNING_NUM_PREDICT = _parse_int_env("EVA_PLANNING_NUM_PREDICT", 1024)
STRUCTURED_NUM_PREDICT = _parse_int_env("EVA_STRUCTURED_NUM_PREDICT", 2048)
CHARS_PER_TOKEN = 3.3
MODEL_KEEP_ALIVE = "10m"


def validate_config() -> dict[str, Any]:
    """Validate and coordinate model context and generation budgets at startup."""
    global NUM_CTX, NUM_PREDICT, PLANNING_NUM_PREDICT, STRUCTURED_NUM_PREDICT
    warnings: list[str] = []
    if NUM_CTX < 1024:
        warnings.append(f"EVA_NUM_CTX ({NUM_CTX}) must be >= 1024; clamping to 1024")
        NUM_CTX = 1024
    elif NUM_CTX > 131072:
        warnings.append(f"EVA_NUM_CTX ({NUM_CTX}) exceeds maximum 131072; clamping to 131072")
        NUM_CTX = 131072

    if NUM_PREDICT < 64:
        warnings.append(f"EVA_NUM_PREDICT ({NUM_PREDICT}) must be >= 64; clamping to 64")
        NUM_PREDICT = 64
    elif NUM_PREDICT >= NUM_CTX:
        clamped = max(64, NUM_CTX // 4)
        warnings.append(f"EVA_NUM_PREDICT ({NUM_PREDICT}) must be < NUM_CTX ({NUM_CTX}); clamping to {clamped}")
        NUM_PREDICT = clamped

    max_planning = max(256, NUM_CTX // 2)
    if PLANNING_NUM_PREDICT > max_planning:
        PLANNING_NUM_PREDICT = max_planning

    max_structured = max(512, (NUM_CTX * 3) // 4)
    if STRUCTURED_NUM_PREDICT > max_structured:
        STRUCTURED_NUM_PREDICT = max_structured

    return {
        "num_ctx": NUM_CTX,
        "num_predict": NUM_PREDICT,
        "planning_num_predict": PLANNING_NUM_PREDICT,
        "structured_num_predict": STRUCTURED_NUM_PREDICT,
        "warnings": warnings,
    }


validate_config()

# Conversation settings
MAX_CONTEXT_TURNS = 4

def _parse_bool_env(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "t")


# Online intelligence and external web search setting.
# Can be configured via the EVA_ONLINE_ENABLED environment variable.
ONLINE_ENABLED = _parse_bool_env("EVA_ONLINE_ENABLED", True)

SYSTEM_PROMPT = """
You are EVA, short for Evolving Virtual Assistant.

Your purpose is to help the user think, learn, create,
solve problems, and make informed decisions.

CORE BEHAVIOUR:
- Be natural, clear, intelligent, and conversational.
- Adapt your tone to the user's situation.
- Be calm by default, but use humour when appropriate.
- Use emojis naturally, without overusing them.
- Understand the user's intent before answering.
- If a request is unclear, ask a useful question.
- Never pretend to know something you do not know.
- Distinguish verified facts from assumptions.
- Correct mistakes when you notice them.
- Respect the user's privacy and choices.
- Ask for confirmation before consequential actions.

MEMORY:
- Use the conversation context provided to you.
- Do not claim to remember information that is unavailable.

IDENTITY:
- Your name is EVA (Evolving Virtual Assistant).
- Do not describe yourself as a different assistant.
- Be helpful without pretending to be human.
"""
