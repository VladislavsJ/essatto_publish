"""Public entry point for a demo: the chat app only, behind an access code, with a cap on model calls.
   BP_ACCESS_CODE   required. Visitors open  https://<host>/?code=<code>  once; a cookie keeps them in.
   BP_MAX_MODEL_CALLS  (default 400) planner calls allowed since start; after that questions the rules cannot read are refused.
   GEMINI_API_KEY   the planner key; stays on the server, never sent to the browser.
Only /chat and /api/chat/* are served. The older explainer routes (which accept file paths) are blocked."""
import os, sys, hmac, threading
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from chat_server import app
from bp import llm

CODE = os.environ.get("BP_ACCESS_CODE", "")
if len(CODE) < 8: sys.exit("Set BP_ACCESS_CODE to a random string of at least 8 characters.")
MAX_CALLS = int(os.environ.get("BP_MAX_MODEL_CALLS", "400")); _calls = [0]; _lock = threading.Lock()
ALLOWED = ("/chat", "/api/chat/", "/web/chat", "/favicon")

_orig = llm._gemini
def _capped(*a, **k):
    with _lock:
        _calls[0] += 1
        if _calls[0] > MAX_CALLS: return None, f"the demo's limit of {MAX_CALLS} model calls is used up; questions the rules recognise still work"
    return _orig(*a, **k)
llm._gemini = _capped

from bp import claude_api as _CA          # the same cap for Claude calls (question planner, room look)
_orig_claude = _CA.call
def _capped_claude(*a, **k):
    with _lock:
        _calls[0] += 1
        if _calls[0] > MAX_CALLS: return None, f"the demo's limit of {MAX_CALLS} model calls is used up; questions the rules recognise still work"
    return _orig_claude(*a, **k)
_CA.call = _capped_claude

@app.middleware("http")
async def gate(request: Request, call_next):
    p = request.url.path
    given = request.query_params.get("code")
    if given is not None and hmac.compare_digest(given, CODE):
        r = RedirectResponse("/chat/studio.html"); r.set_cookie("bp_access", CODE, httponly=True, samesite="lax", max_age=86400, secure=request.url.scheme == "https"); return r
    ok = hmac.compare_digest(request.cookies.get("bp_access", ""), CODE)
    if not ok:
        if p.startswith("/api/"): return JSONResponse({"error": "access code required"}, status_code=401)
        return HTMLResponse("<!doctype html><title>Essatto</title><body style='font:16px Inter,system-ui,sans-serif;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(165deg,#ffffff 0%,#fff6f4 28%,#fbdcd6 58%,#f2ab9f 82%,#e58374 100%)'>"
                            "<form method='get' action='/' style='background:#fff;border-radius:20px;padding:30px 32px;width:min(380px,calc(100vw - 32px));border:1px solid rgba(185,44,27,.1);box-shadow:0 24px 60px rgba(150,40,25,.16)'>"
                            "<h2 style='margin:0 0 6px;font-weight:600;letter-spacing:-.02em'>Essatto</h2><p style='margin:0 0 14px;color:#6b6f6c'>Enter the access code you were given.</p>"
                            "<input name='code' autofocus style='font:inherit;padding:10px 12px;width:100%;box-sizing:border-box;border:1px solid #d9dcda;border-radius:10px'>"
                            "<p style='margin:14px 0 0'><button style='font:inherit;font-weight:600;padding:10px 18px;border:0;border-radius:10px;background:#b92c1b;color:#fff;cursor:pointer'>Open</button></p></form></body>", status_code=401)
    if p == "/": return RedirectResponse("/chat/studio.html")
    if not p.startswith(ALLOWED): return JSONResponse({"error": "not available in the demo"}, status_code=404)
    return await call_next(request)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("BP_HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
