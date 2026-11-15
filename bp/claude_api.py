"""Claude through the Anthropic API (official SDK). Used when a model name starts with 'claude-', for example
'claude-haiku-5-5'. The key is read from the environment (ANTHROPIC_API_KEY); it is never written to a file here.

Thinking depth is set by effort (models.json "effort", or BP_MODEL_EFFORT): current Claude models think adaptively and
effort is the control.
Every call's tokens are appended to cache/llm_cost.jsonl, the same log the other model routes use, so the cost shown on
the page covers it."""
import os, json, time, base64, threading

COST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "llm_cost.jsonl")
_LOCK = threading.Lock(); _client = None


def is_claude_api(model) -> bool:
    return isinstance(model, str) and model.startswith("claude-")


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _get():
    global _client
    with _LOCK:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        return _client


def call(model, prompt, images=None, question="", use="plan", max_tokens=8000):
    """-> (text | None, error | None). images: list of PNG bytes, placed before the text."""
    import anthropic
    t0 = time.time()
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(b).decode("utf-8")}} for b in (images or [])]
    content.append({"type": "text", "text": prompt})
    try:
        r = _get().messages.create(model=model, max_tokens=max_tokens, output_config={"effort": os.environ.get("BP_CLAUDE_EFFORT") or __import__("bp.models", fromlist=["effort"]).effort()},
                                   messages=[{"role": "user", "content": content}])
    except anthropic.AuthenticationError: return None, "Claude API: the key was rejected (check ANTHROPIC_API_KEY)"
    except anthropic.PermissionDeniedError as e: return None, f"Claude API: the key has no access to {model}: {str(getattr(e, 'message', e))[:140]}"
    except anthropic.NotFoundError: return None, f"Claude API: model '{model}' was not found"
    except anthropic.RateLimitError: return None, "Claude API: rate limit reached, try again in a moment"
    except anthropic.BadRequestError as e: return None, f"Claude API rejected the request: {str(getattr(e, 'message', e))[:200]}"
    except anthropic.APIStatusError as e: return None, f"Claude API error {e.status_code}: {str(getattr(e, 'message', e))[:160]}"
    except anthropic.APIConnectionError as e: return None, f"Claude API could not be reached: {type(e).__name__}"
    u = r.usage
    try:
        with _LOCK: open(COST, "a").write(json.dumps({"model": model, "usd": None, "in": u.input_tokens, "out": u.output_tokens, "cache_read": getattr(u, "cache_read_input_tokens", None),
                                                      "cache_write": getattr(u, "cache_creation_input_tokens", None), "ms": round((time.time() - t0) * 1000), "q": (question or "")[:80], "use": use}) + "\n")
    except Exception: pass
    if r.stop_reason == "refusal": return None, "Claude declined to answer this request"
    text = "".join(b.text for b in r.content if b.type == "text")
    if not text.strip(): return None, f"Claude returned no text (stop reason: {r.stop_reason})"
    return text, None
