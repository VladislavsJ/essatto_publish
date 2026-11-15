"""Which model does what: ONE place to change it.

Two roles, set in ../models.json or by environment:
  basic   BP_MODEL_BASIC   reads questions the rules cannot parse, looks at room crops, reviews doubtful wall spots
  smart   BP_MODEL_SMART   asked only when the basic model failed twice or said it is unsure; 'none' = no second level
  effort  BP_MODEL_EFFORT  low | medium | high: how much the model thinks (Claude effort, Gemini thinking level)

The provider follows from the name, so a new model needs no code change:
  claude-...   Anthropic API   (ANTHROPIC_API_KEY)
  gemini-...   Gemini API      (GEMINI_API_KEY)
  gpt-...      codex command line
  other        claude command line (for example 'haiku', 'opus')

The older per-task settings still win when set (BP_PLANNER_MODEL, BP_PLANNER_STRONG, BP_LOOK_L1, BP_LOOK_L2, BP_REVIEW_L1,
BP_REVIEW_L2), so one task can be pinned to a different model."""
import os, json

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models.json")
_cfg = None


def config() -> dict:
    global _cfg
    if _cfg is None:
        try: _cfg = json.load(open(FILE, encoding="utf-8"))
        except Exception: _cfg = {}
    return _cfg


def basic() -> str: return os.environ.get("BP_MODEL_BASIC") or config().get("basic") or "claude-haiku-5-5"
def smart() -> str: return os.environ.get("BP_MODEL_SMART") or config().get("smart") or "none"
def effort() -> str: return os.environ.get("BP_MODEL_EFFORT") or config().get("effort") or "low"

def for_task(task: str) -> tuple:
    """(level 1, level 2) for 'plan' | 'look' | 'review'"""
    one, two = {"plan": ("BP_PLANNER_MODEL", "BP_PLANNER_STRONG"), "look": ("BP_LOOK_L1", "BP_LOOK_L2"), "review": ("BP_REVIEW_L1", "BP_REVIEW_L2")}[task]
    return os.environ.get(one) or basic(), os.environ.get(two) or smart()

def provider(model: str) -> str:
    m = (model or "").lower()
    return "none" if m in ("", "none") else "anthropic" if m.startswith("claude-") else "gemini" if m.startswith("gemini") else "codex" if m.startswith("gpt") else "claude-cli"

def role(model: str) -> str:
    """'basic' | 'smart' | 'other': how the page describes the model that answered"""
    return "smart" if model and model == smart() and model != basic() else "basic" if model and model == basic() else "other"

def price(model: str):
    """-> (in, out, cache_read, cache_write) US dollars per million tokens, or None when no price is set for this model"""
    p = (config().get("prices_usd_per_million") or {}).get(model or "")
    return (float(p.get("in") or 0), float(p.get("out") or 0), float(p.get("cache_read") or 0), float(p.get("cache_write") or 0)) if p else None

def missing_key(model: str):
    """-> a sentence for the user when the key this model needs is not configured, else None"""
    pv = provider(model)
    if pv == "anthropic" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")): return f"the model '{model}' needs ANTHROPIC_API_KEY, which is not set on this server"
    if pv == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.path.exists(os.path.expanduser("~/.config/bp_gemini_key"))): return f"the model '{model}' needs GEMINI_API_KEY, which is not set on this server"
    return None

def describe() -> dict:
    b, s = basic(), smart()
    return {"basic": b, "smart": s, "effort": effort(), "providers": {"basic": provider(b), "smart": provider(s)},
            "tasks": {t: list(for_task(t)) for t in ("plan", "look", "review")}, "key_problem": missing_key(b) or (missing_key(s) if s != "none" else None),
            "priced": {m: price(m) is not None for m in {b, s} if m and m != "none"}}
