#!/usr/bin/env python3
"""claude.ai web -> OpenAI-compatible API (personal account, single user).

Concept mirrors gemini-web2api / claude2api: reverse-proxy the claude.ai web app
into /v1/chat/completions so any OpenAI-compatible client (e.g. opencode) can use
your Claude subscription. No Anthropic API key -- it rides your browser session.

claude.ai web has no real function-calling, so tool use is emulated at the prompt
level: the tool schemas are injected into the prompt, Claude is told to answer
tool calls inside a <tool_calls> block, and that block is parsed back into OpenAI
`tool_calls`. The prior tool calls + results are replayed each turn so the agent
advances instead of looping on step 1-2.

Run:
    CLAUDE_SESSION_KEY=sk-ant-sid01-... python3 claude_web2api.py
    # then point the client at http://localhost:8080/v1

Env:
    CLAUDE_SESSION_KEY  the `sessionKey` cookie value from claude.ai (required
                        unless CLAUDE_COOKIE is set).
    CLAUDE_COOKIE       full Cookie header copied from a claude.ai request in the
                        browser. Use this if you get 403s -- it carries
                        cf_clearance etc. and bypasses Cloudflare far better.
    COOKIE_FROM         read the claude.ai cookies straight out of your browser
                        profile so you never paste anything: "firefox" (auto-find
                        the active profile) or a path to a profile dir / to a
                        cookies.sqlite file. Grabs the whole cookie jar (sessionKey
                        + cf_clearance + device id) and refreshes every few minutes,
                        so it keeps working across session rotation. Firefox only
                        (its cookies are unencrypted on Linux; Chrome's are not).
    IMPERSONATE         curl_cffi TLS fingerprint. Defaults to "firefox" when
                        COOKIE_FROM is set (to match the jar's cf_clearance),
                        else "chrome". Override with e.g. "firefox147"/"chrome146".
    CLAUDE_ORG_ID       skip the org lookup by hardcoding the org uuid.
    DEFAULT_MODEL       slug used when the request asks for "sonnet"/empty.
    API_KEY             if set, require `Authorization: Bearer <API_KEY>`.
    PROXY               upstream proxy url for the claude.ai requests.
    HOST / PORT         listen address, default 127.0.0.1:8080.
    DELETE_AFTER        "true"(default)/"false" -- delete the claude.ai
                        conversation after each reply so your sidebar stays clean.
    MAX_PROMPT_CHARS    trim old turns past this to dodge claude.ai's input wall.
    TOOL_RESULT_CAP     per tool-result char cap before older ones get trimmed.
    EFFORT              claude.ai thinking effort: low(default)/medium/high/xhigh/max.
    WEB_SEARCH          "true"(default)/"false" -- let Claude use claude.ai's native
                        server-side web search when it needs current info.
"""
import json
import os
import re
import sys
import time
import uuid
import base64
import signal
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from curl_cffi import requests, CurlMime

BASE = "https://claude.ai"
ROOT_MSG = "00000000-0000-4000-8000-000000000000"
NORMAL_STYLE = [{
    "type": "default", "key": "Default", "name": "Normal",
    "nameKey": "normal_style_name", "prompt": "Normal",
    "summary": "Default responses from Claude",
    "summaryKey": "normal_style_summary", "isDefault": True,
}]
WEB_SEARCH_TOOL = {"type": "web_search_v0", "name": "web_search"}

SESSION_KEY = os.environ.get("CLAUDE_SESSION_KEY", "").strip()
COOKIE = os.environ.get("CLAUDE_COOKIE", "").strip()
COOKIE_FROM = os.environ.get("COOKIE_FROM", "").strip()
ORG_ID = os.environ.get("CLAUDE_ORG_ID", "").strip()
API_KEY = os.environ.get("API_KEY", "").strip()
IMPERSONATE = (os.environ.get("IMPERSONATE", "").strip()
               or ("firefox" if COOKIE_FROM else "chrome"))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-5-20250929").strip()
PROXY = os.environ.get("PROXY", "").strip()
HOST = os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("PORT", "8080"))
DELETE_AFTER = os.environ.get("DELETE_AFTER", "true").lower() != "false"
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "120000"))
TOOL_RESULT_CAP = int(os.environ.get("TOOL_RESULT_CAP", "6000"))
EFFORT = os.environ.get("EFFORT", "low").strip()
WEB_SEARCH = os.environ.get("WEB_SEARCH", "true").lower() != "false"
COOKIE_TTL = 300  # re-read the browser jar at most this often (seconds)
SESSION_TTL = int(os.environ.get("SESSION_TTL", "1800"))  # drop idle reused threads
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "40"))

TOOL_OPEN, TOOL_CLOSE = "<tool_calls>", "</tool_calls>"
_TOOL_RE = re.compile(re.escape(TOOL_OPEN) + r"\s*(.*?)\s*" + re.escape(TOOL_CLOSE), re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class ClaudeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status or 502
        self.message = message


# ---------------------------------------------------------------------------
# credentials: read straight from the browser cookie jar (no manual paste)
# ---------------------------------------------------------------------------
_cookie_cache = {"val": "", "ts": 0.0}


def _firefox_cookie_db(spec):
    """Locate a Firefox cookies.sqlite from a spec ("firefox"/path/dir)."""
    import glob
    if spec and spec.lower() not in ("firefox", "ff", "auto", "1", "true"):
        p = os.path.expanduser(spec)
        if os.path.isdir(p):
            p = os.path.join(p, "cookies.sqlite")
        return p if os.path.isfile(p) else None
    roots = ["~/.mozilla/firefox",
             "~/.config/mozilla/firefox",
             "~/snap/firefox/common/.mozilla/firefox",
             "~/.var/app/org.mozilla.firefox/.mozilla/firefox"]
    dbs = []
    for r in roots:
        dbs += glob.glob(os.path.join(os.path.expanduser(r), "*", "cookies.sqlite"))
    dbs = [d for d in dbs if os.path.isfile(d)]
    # shortcut: pick the most recently written jar (the active profile), rather
    # than parsing profiles.ini. Set COOKIE_FROM to a path if you have several.
    return max(dbs, key=os.path.getmtime) if dbs else None


def read_firefox_cookies(spec, host_like="%claude.ai%"):
    """Return a Cookie header for claude.ai read from the Firefox jar on disk."""
    import shutil
    import sqlite3
    import tempfile
    db = _firefox_cookie_db(spec)
    if not db:
        raise ClaudeError(500, f"could not find a Firefox cookies.sqlite (COOKIE_FROM={spec!r})")
    tmp = tempfile.mkdtemp(prefix="c2a_")
    try:
        dst = os.path.join(tmp, "cookies.sqlite")
        shutil.copy2(db, dst)  # copy so a running Firefox lock/WAL doesn't block us
        for ext in ("-wal", "-shm"):
            if os.path.exists(db + ext):
                try:
                    shutil.copy2(db + ext, dst + ext)
                except Exception:
                    pass
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ?", (host_like,)
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    pairs = [f"{n}={v}" for n, v in rows if n]
    if not pairs:
        raise ClaudeError(401, "no claude.ai cookies in Firefox - log in at claude.ai in Firefox first")
    return "; ".join(pairs)


def resolve_cookie_header(force=False):
    """The Cookie header to send, or "" to fall back to the sessionKey cookie."""
    if COOKIE:
        return COOKIE
    if not COOKIE_FROM:
        return ""
    now = time.time()
    if force or not _cookie_cache["val"] or now - _cookie_cache["ts"] > COOKIE_TTL:
        try:
            _cookie_cache["val"] = read_firefox_cookies(COOKIE_FROM)
            _cookie_cache["ts"] = now
        except ClaudeError:
            if not _cookie_cache["val"]:
                raise
    return _cookie_cache["val"]


# ---------------------------------------------------------------------------
# claude.ai web client
# ---------------------------------------------------------------------------
class ClaudeClient:
    def __init__(self):
        self.s = requests.Session(impersonate=IMPERSONATE)
        self.cookie = resolve_cookie_header()
        self._org = ORG_ID
        self._models = None
        self.last_uuid = None

    def _headers(self, referer="/new", accept="*/*"):
        h = {
            "anthropic-client-platform": "web_claude_ai",
            "origin": BASE,
            "referer": BASE + referer,
            "accept": accept,
            "content-type": "application/json",
        }
        if self.cookie:
            h["cookie"] = self.cookie
        return h

    def _req(self, method, path, referer="/new", accept="*/*", _retry=True, **kw):
        if PROXY:
            kw.setdefault("proxies", {"http": PROXY, "https": PROXY})
        cookies = None if self.cookie else {"sessionKey": SESSION_KEY}
        try:
            resp = self.s.request(
                method, BASE + path,
                headers=self._headers(referer, accept),
                cookies=cookies, timeout=300, **kw,
            )
        except Exception as e:
            raise ClaudeError(502, f"upstream request failed: {e}")
        # session rotated? refresh the jar from the browser once and retry.
        if resp.status_code in (401, 403) and _retry and COOKIE_FROM and not COOKIE:
            try:
                self.cookie = resolve_cookie_header(force=True)
            except ClaudeError:
                return resp
            return self._req(method, path, referer, accept, _retry=False, **kw)
        return resp

    def org(self):
        if self._org:
            return self._org
        r = self._req("GET", "/api/organizations")
        if r.status_code != 200:
            raise ClaudeError(r.status_code, f"organizations: {r.status_code} {r.text[:200]}")
        orgs = r.json() or []
        if not orgs:
            raise ClaudeError(500, "no organizations found for this session")
        pick = orgs[0]
        if len(orgs) > 1:
            for o in orgs:
                if str(o.get("rate_limit_tier", "")).startswith("default_"):
                    pick = o
                    break
        self._org = pick["uuid"]
        return self._org

    def models(self):
        if self._models is not None:
            return self._models
        org = self.org()
        ids = []
        path = (f"/edge-api/bootstrap/{org}/app_start?statsig_hashing_algorithm=djb2"
                "&growthbook_format=sdk&cache_bust=1&include_system_prompts=false")
        try:
            r = self._req("GET", path)
            if r.status_code == 200:
                models = (r.json().get("claude_ai_available_models") or {}).get("models") or []
                for m in models:
                    mid = (m.get("model_id") or "").strip()
                    if mid and mid not in ids:
                        ids.append(mid)
        except Exception:
            pass
        self._models = ids or [DEFAULT_MODEL]
        return self._models

    def resolve(self, requested):
        requested = (requested or "").strip()
        if requested.startswith("claude-"):
            return requested
        alias = requested.lower()
        needle = "sonnet" if alias in ("", "default", "sonnet", "sonnet-latest", "claude") else alias
        try:
            avail = self.models()
        except ClaudeError:
            avail = [DEFAULT_MODEL]
        matches = [m for m in avail if needle in m.lower()]
        if matches:
            return sorted(matches)[-1]  # date-suffixed slugs sort newest-last
        return requested or DEFAULT_MODEL

    def create_conversation(self):
        org = self.org()
        cid = str(uuid.uuid4())
        r = self._req("POST", f"/api/organizations/{org}/chat_conversations",
                      json={"uuid": cid, "name": ""})
        if r.status_code not in (200, 201):
            raise ClaudeError(r.status_code, f"create conversation: {r.status_code} {r.text[:200]}")
        try:
            return r.json().get("uuid") or cid
        except Exception:
            return cid

    def completion(self, conv, prompt, model, parent=ROOT_MSG, files=None):
        """Yield text pieces from the claude.ai SSE stream; capture the new
        assistant message uuid in self.last_uuid to thread the next turn."""
        org = self.org()
        payload = {
            "prompt": prompt,
            "parent_message_uuid": parent,
            "timezone": "America/Los_Angeles",
            "model": model,
            "attachments": [],
            "files": files or [],
            "sync_sources": [],
            "rendering_mode": "messages",
            "personalized_styles": NORMAL_STYLE,
            # only web_search is enabled (runs server-side, returns plain text);
            # artifacts/repl stay off so the reply is parseable for tool emulation.
            "tools": [WEB_SEARCH_TOOL] if WEB_SEARCH else [],
        }
        if EFFORT:
            payload["effort"] = EFFORT  # low|medium|high|xhigh|max
        r = self._req("POST",
                      f"/api/organizations/{org}/chat_conversations/{conv}/completion",
                      referer=f"/chat/{conv}", accept="text/event-stream",
                      json=payload, stream=True)
        if r.status_code != 200:
            try:  # a streamed response won't populate .text; drain it instead
                body = b"".join(r.iter_content()).decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            r.close()
            raise ClaudeError(r.status_code, f"completion: {r.status_code} {body}".rstrip())
        try:
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
                kind = parse_sse_line(line)
                if kind is None:
                    continue
                what, value = kind
                if what == "text" and value:
                    yield value
                elif what == "error":
                    raise ClaudeError(502, value)
                elif what == "stop":
                    break
                elif what == "limit":
                    _record_usage(value)
                elif what == "start":
                    self.last_uuid = value
        finally:
            r.close()

    def upload_file(self, data, mime, name):
        """Upload bytes to claude.ai; return the file_uuid to put in `files`."""
        org = self.org()
        mp = CurlMime()
        mp.addpart(name="file", filename=name, content_type=mime, data=data)
        h = {k: v for k, v in self._headers(referer="/new").items() if k.lower() != "content-type"}
        kw = {"proxies": {"http": PROXY, "https": PROXY}} if PROXY else {}
        try:
            r = self.s.post(BASE + f"/api/{org}/upload", headers=h,
                            cookies=None if self.cookie else {"sessionKey": SESSION_KEY},
                            multipart=mp, timeout=120, **kw)
        except Exception as e:
            raise ClaudeError(502, f"upload failed: {e}")
        if r.status_code != 200:
            raise ClaudeError(r.status_code, f"upload: {r.status_code} {r.text[:150]}")
        try:
            return (r.json() or {}).get("file_uuid")
        except Exception:
            return None

    def images_in(self, messages):
        """Upload every image found in these messages and return their file_uuids.
        Failures are skipped so the turn still goes through as text."""
        uuids = []
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url") or ""
                elif part.get("type") in ("image", "input_image"):
                    url = part.get("image") or part.get("url") or ""
                else:
                    continue
                got = _data_url(url)
                if not got and url.startswith(("http://", "https://")):
                    try:
                        resp = self.s.get(url, timeout=30)
                        if resp.status_code == 200:
                            mime = (resp.headers.get("content-type") or "image/png").split(";")[0].strip()
                            got = (resp.content, mime or "image/png", "image." + (mime.split("/")[-1] or "png"))
                    except Exception:
                        got = None
                if not got:
                    continue
                try:
                    fid = self.upload_file(*got)
                    if fid:
                        uuids.append(fid)
                except ClaudeError:
                    pass
        return uuids

    def delete_conversation(self, conv):
        try:
            org = self.org()
            self._req("DELETE", f"/api/organizations/{org}/chat_conversations/{conv}",
                      referer=f"/chat/{conv}", json={"uuid": conv})
        except Exception:
            pass


def parse_sse_line(line):
    """Pure parser for one claude.ai SSE line.

    Returns ('text'|'thinking'|'error'|'start'|'limit', val) | ('stop',None) | None.
    """
    if not line or not line.startswith("data:"):
        return None
    data = line[5:].lstrip()
    if data == "[DONE]":
        return ("stop", None)
    try:
        evt = json.loads(data)
    except Exception:
        return None
    t = evt.get("type")
    if t == "error":
        return ("error", (evt.get("error") or {}).get("message") or "upstream error")
    delta = evt.get("delta") or {}
    if t == "content_block_delta" and delta.get("type") == "text_delta":
        return ("text", delta.get("text") or "")
    if t == "content_block_delta" and delta.get("type") == "thinking_delta":
        return ("thinking", delta.get("thinking") or "")
    if t == "message_limit":
        return ("limit", evt.get("message_limit") or {})
    if t == "message_start":
        return ("start", (evt.get("message") or {}).get("uuid") or "")
    if t == "message_stop":
        return ("stop", None)
    return None


# ---------------------------------------------------------------------------
# claude.ai subscription usage (piggybacks on the message_limit SSE event)
# ---------------------------------------------------------------------------
_usage = {"windows": None, "ts": 0.0}


def _record_usage(ml):
    w = (ml or {}).get("windows")
    if w:
        _usage["windows"] = w
        _usage["ts"] = time.time()


def usage_report():
    """Percent of the 5h / 7d limits used, from the last reply claude.ai sent."""
    w = _usage["windows"]
    if not w:
        return {"status": "no data yet - send one message through the proxy first"}
    now = time.time()
    out = {}
    for key in ("5h", "7d"):
        win = w.get(key) or {}
        util = win.get("utilization")
        item = {"used_percent": round(util * 100, 1) if isinstance(util, (int, float)) else None}
        ra = win.get("resets_at")
        if ra:
            secs = int(ra - now)
            item["resets_in"] = f"{secs // 3600}h{(secs % 3600) // 60}m" if secs > 0 else "now"
        out[key] = item
    out["as_of"] = f"{int(now - _usage['ts'])}s ago"
    return out


# ---------------------------------------------------------------------------
# OpenAI <-> prompt translation
# ---------------------------------------------------------------------------
def _data_url(url):
    """Decode a base64 data: URL into (bytes, mime, filename), else None."""
    if not url.startswith("data:") or "," not in url:
        return None
    head, b64 = url[5:].split(",", 1)
    if "base64" not in head:
        return None
    mime = (head.split(";")[0] or "image/png").strip() or "image/png"
    try:
        data = base64.b64decode(b64)
    except Exception:
        return None
    ext = (mime.split("/")[-1] or "png").split("+")[0] or "png"
    return data, mime, "image." + ext


def _text_of(content):
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and p.get("text"))
    return content or ""


def _loads(x):
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return {}
    return {}


def _render_message(m):
    role = m.get("role", "user")
    text = _text_of(m.get("content"))
    if role == "system":
        return "System: " + text
    if role == "assistant":
        calls = m.get("tool_calls") or []
        if calls:
            rendered = [{"name": (tc.get("function") or {}).get("name"),
                         "arguments": _loads((tc.get("function") or {}).get("arguments"))}
                        for tc in calls]
            block = TOOL_OPEN + "\n" + json.dumps(rendered, ensure_ascii=False) + "\n" + TOOL_CLOSE
            text = (text + "\n" + block).strip() if text else block
        return "Assistant: " + text
    if role == "tool":
        name = m.get("name") or m.get("tool_call_id") or "tool"
        if len(text) > TOOL_RESULT_CAP:
            text = text[:TOOL_RESULT_CAP] + f"\n...[truncated {len(text) - TOOL_RESULT_CAP} chars]"
        return f"Human: [tool result: {name}]\n{text}"
    return "Human: " + text


def tool_instructions(tools, tool_choice):
    defs = []
    for t in tools:
        fn = t.get("function") or {}
        defs.append({"name": fn.get("name"), "description": fn.get("description", ""),
                     "parameters": fn.get("parameters", {})})
    lines = [
        "# Tools",
        "You can call tools. Available tools (JSON Schema):",
        json.dumps(defs, ensure_ascii=False),
        "",
        f"To call tools, output ONLY this block and nothing else:",
        TOOL_OPEN,
        '[{"name": "<tool>", "arguments": {<args matching the schema>}}]',
        TOOL_CLOSE,
        "Rules:",
        "- When calling tools, emit ONLY the block above - no prose, no markdown fences.",
        "- `arguments` is a JSON object. List several entries in the array to run tools in parallel.",
        "- Tool calls already shown above with their results are DONE. Never repeat an identical "
        "call; use the result and move to the next step.",
        "- When the task is complete, reply with the final answer as normal prose and NO "
        f"{TOOL_OPEN} block.",
    ]
    if isinstance(tool_choice, dict):
        fname = (tool_choice.get("function") or {}).get("name")
        if fname:
            lines.append(f"- You MUST call the `{fname}` tool now.")
    elif tool_choice == "required":
        lines.append("- You MUST call at least one tool now; do not answer in prose yet.")
    return "\n".join(lines)


def build_prompt(messages, tools=None, tool_choice="auto"):
    """Flatten OpenAI messages into one claude.ai prompt, trimming old turns to
    stay under the input wall and appending tool instructions last (high attention).
    """
    items = [_render_message(m) for m in messages]
    instr = tool_instructions(tools, tool_choice) if tools and tool_choice != "none" else ""
    budget = MAX_PROMPT_CHARS - len(instr) - 100

    if sum(len(i) + 2 for i in items) > budget:
        head = [i for i in items if i.startswith("System: ")]
        rest = [i for i in items if not i.startswith("System: ")]
        total = sum(len(h) + 2 for h in head)
        kept = []
        for it in reversed(rest):
            if kept and total + len(it) + 2 > budget:
                break
            kept.append(it)
            total += len(it) + 2
        kept.reverse()
        omitted = ["[... earlier turns omitted ...]"] if len(kept) < len(rest) else []
        items = head + omitted + kept

    prompt = "\n\n".join(items)
    if instr:
        prompt += "\n\n" + instr
    return prompt.strip()


def parse_tool_calls(text):
    """Return (clean_text, [{name, arguments(dict)}]). Robust to fences/stray prose."""
    m = _TOOL_RE.search(text)
    if m:
        raw = m.group(1).strip()
        clean = (text[:m.start()] + text[m.end():]).strip()
    else:
        stripped = text.strip()
        fence = _FENCE_RE.match(stripped)
        cand = fence.group(1).strip() if fence else stripped
        if cand[:1] in ("[", "{") and '"name"' in cand and '"arguments"' in cand:
            raw, clean = cand, ""
        else:
            return text, []
    fence = _FENCE_RE.match(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return text, []
    items = data if isinstance(data, list) else [data]
    calls = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        args = it.get("arguments", it.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        calls.append({"name": it["name"], "arguments": args if isinstance(args, dict) else {}})
    return (clean, calls) if calls else (text, [])


def to_openai_calls(calls):
    return [{"id": "call_" + uuid.uuid4().hex[:24], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
            for c in calls]


def _id():
    return "chatcmpl-" + uuid.uuid4().hex


def chunk_obj(cid, model, delta=None, finish=None):
    return {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def full_obj(cid, model, message, finish="stop"):
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def err(msg):
    return {"error": {"message": msg, "type": "proxy_error"}}


# ---------------------------------------------------------------------------
# conversation reuse: keep one claude.ai thread per opencode session and send
# only the new turn each step instead of replaying the whole history (which
# burns subscription usage fast). The OpenAI API is stateless, so we match a
# request to its thread by the fingerprint-prefix of its message list.
# ---------------------------------------------------------------------------
_sessions = {}  # conv_uuid -> {"sig": [fp, ...], "parent": uuid, "ts": float}
_sess_lock = threading.Lock()


def _fingerprint(messages):
    out = []
    for m in messages:
        h = hashlib.sha1()
        h.update((m.get("role") or "").encode())
        h.update(b"\x00")
        h.update(_text_of(m.get("content")).encode("utf-8", "replace"))
        if isinstance(m.get("content"), list):
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    h.update(b"\x00img\x00")
                    h.update(((part.get("image_url") or {}).get("url") or "").encode("utf-8", "replace"))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            h.update(b"\x00tc\x00")
            h.update((fn.get("name") or "").encode())
            h.update((args if isinstance(args, str) else json.dumps(args, sort_keys=True)).encode("utf-8", "replace"))
        if m.get("tool_call_id"):
            h.update(b"\x00id\x00" + str(m["tool_call_id"]).encode())
        out.append(h.hexdigest())
    return out


def _match_session(fps):
    """(conv, parent, consumed) for the longest tracked thread whose fingerprints
    are a prefix of fps, else None."""
    best = None
    for conv, s in _sessions.items():
        n = len(s["sig"])
        if n and n <= len(fps) and fps[:n] == s["sig"]:
            if best is None or n > best[2]:
                best = (conv, s["parent"], n)
    return best


def _store_session(conv, fps, parent):
    _sessions[conv] = {"sig": fps, "parent": parent, "ts": time.time()}


def _drop_session(conv):
    _sessions.pop(conv, None)


def _evict_old():
    """Drop idle / overflow threads; return their conv ids for upstream deletion.
    Call under _sess_lock."""
    now = time.time()
    dead = [c for c, s in _sessions.items() if now - s["ts"] > SESSION_TTL]
    keep = [(s["ts"], c) for c, s in _sessions.items() if c not in dead]
    if len(keep) > MAX_SESSIONS:
        dead += [c for _, c in sorted(keep)[:len(keep) - MAX_SESSIONS]]
    for c in dead:
        _sessions.pop(c, None)
    return dead


def build_turn(delta, tools=None, tool_choice="auto"):
    """Render only the new (non-assistant) messages as the next human turn; the
    thread already holds everything before them."""
    body = "\n\n".join(p for p in (_render_message(m) for m in delta
                                   if m.get("role") != "assistant") if p).strip()
    if not body:
        return ""
    if tools and tool_choice != "none":
        body += ("\n\n(Continue. To call tools, output ONLY the " + TOOL_OPEN +
                 " block described earlier - no prose. Do not repeat tool calls already "
                 "completed above; when the task is done, reply in plain prose.)")
    return body


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authed(self):
        if not API_KEY:
            return True
        return self.headers.get("Authorization", "").strip() == f"Bearer {API_KEY}"

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_stream(self):
        self.close_connection = True  # body delimited by EOF (no Content-Length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_sse(self, obj):
        if obj == "[DONE]":
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/health"):
            return self._json(200, {"status": "ok"})
        if path == "/v1/models":
            if not self._authed():
                return self._json(401, err("missing or invalid API key"))
            try:
                ids = ClaudeClient().models()
            except ClaudeError as e:
                return self._json(e.status, err(e.message))
            return self._json(200, {"object": "list",
                                    "data": [{"id": i, "object": "model", "owned_by": "anthropic"} for i in ids]})
        if path == "/usage":
            if not self._authed():
                return self._json(401, err("missing or invalid API key"))
            return self._json(200, usage_report())
        return self._json(404, err("not found"))

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/v1/chat/completions":
            return self._json(404, err("not found"))
        if not self._authed():
            return self._json(401, err("missing or invalid API key"))
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, err("invalid JSON body"))
        messages = req.get("messages") or []
        if not messages:
            return self._json(400, err("no messages provided"))

        stream = bool(req.get("stream"))
        tools = req.get("tools") or []
        tool_choice = req.get("tool_choice", "auto")
        agent = bool(tools) and tool_choice != "none"
        cid = _id()

        try:
            c = ClaudeClient()
            model = c.resolve(req.get("model"))
        except ClaudeError as e:
            return self._json(e.status, err(e.message))

        if agent:
            self._agent(c, messages, tools, tool_choice, model, cid, stream)
        elif stream:
            self._stream(c, messages, tools, tool_choice, model, cid)
        else:
            self._full(c, messages, tools, tool_choice, model, cid)

    def _prepare(self, c, messages, tools, tool_choice):
        """Pick a reused thread + incremental turn when the request continues a
        known session; else create a fresh thread and send the full history."""
        agent = bool(tools) and tool_choice != "none"
        fps = _fingerprint(messages)
        with _sess_lock:
            dead = _evict_old()
            hit = _match_session(fps)
        for conv in dead:  # delete evicted threads off-lock
            if DELETE_AFTER:
                threading.Thread(target=c.delete_conversation, args=(conv,), daemon=True).start()
        if hit:
            conv, parent, n = hit
            turn = build_turn(messages[n:], tools if agent else None, tool_choice)
            if turn:
                return conv, turn, parent, fps, True, messages[n:]
        conv = c.create_conversation()
        prompt = build_prompt(messages, tools if agent else None, tool_choice)
        return conv, prompt, ROOT_MSG, fps, False, messages

    def _complete(self, c, messages, tools, tool_choice, model, emit):
        """Run one turn (reused or fresh), feeding text pieces to emit(), then
        record the thread so the next turn continues it. Falls back to a fresh
        full-history turn if a reused turn fails before any output."""
        agent = bool(tools) and tool_choice != "none"
        conv, prompt, parent, fps, reuse, turn_msgs = self._prepare(c, messages, tools, tool_choice)
        files = c.images_in(turn_msgs)
        self.log_message("turn=%s conv=%s files=%d", "reuse" if reuse else "fresh", conv[:8], len(files))
        sent = {"any": False}

        def _emit(p):
            sent["any"] = True
            emit(p)

        try:
            for piece in c.completion(conv, prompt, model, parent, files):
                _emit(piece)
        except ClaudeError:
            if not reuse or sent["any"]:
                raise
            with _sess_lock:
                _drop_session(conv)
            conv = c.create_conversation()
            c.last_uuid = None
            prompt = build_prompt(messages, tools if agent else None, tool_choice)
            files = c.images_in(messages)
            for piece in c.completion(conv, prompt, model, ROOT_MSG, files):
                _emit(piece)
        with _sess_lock:
            _store_session(conv, fps, c.last_uuid or parent)

    # plain chat, true token streaming
    def _stream(self, c, messages, tools, tool_choice, model, cid):
        self._begin_stream()
        try:
            self._send_sse(chunk_obj(cid, model, delta={"role": "assistant"}))
            self._complete(c, messages, tools, tool_choice, model,
                           lambda p: self._send_sse(chunk_obj(cid, model, delta={"content": p})))
            self._send_sse(chunk_obj(cid, model, delta={}, finish="stop"))
            self._send_sse("[DONE]")
        except ClaudeError as e:
            try:
                self._send_sse({"error": {"message": e.message, "code": e.status}})
                self._send_sse("[DONE]")
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _full(self, c, messages, tools, tool_choice, model, cid):
        buf = []
        try:
            self._complete(c, messages, tools, tool_choice, model, buf.append)
        except ClaudeError as e:
            return self._json(e.status, err(e.message))
        self._json(200, full_obj(cid, model, {"role": "assistant", "content": "".join(buf)}))

    # agentic: buffer the reply, parse tool calls, emit OpenAI tool_calls
    def _agent(self, c, messages, tools, tool_choice, model, cid, stream):
        buf = []
        try:
            self._complete(c, messages, tools, tool_choice, model, buf.append)
        except ClaudeError as e:
            return self._json(e.status, err(e.message))
        clean, calls = parse_tool_calls("".join(buf))
        oai_calls = to_openai_calls(calls) if calls else []
        finish = "tool_calls" if calls else "stop"

        if not stream:
            message = {"role": "assistant", "content": clean or None}
            if oai_calls:
                message["tool_calls"] = oai_calls
            return self._json(200, full_obj(cid, model, message, finish))

        self._begin_stream()
        try:
            self._send_sse(chunk_obj(cid, model, delta={"role": "assistant"}))
            if oai_calls:
                deltas = [{"index": i, "id": tc["id"], "type": "function", "function": tc["function"]}
                          for i, tc in enumerate(oai_calls)]
                if clean:
                    self._send_sse(chunk_obj(cid, model, delta={"content": clean}))
                self._send_sse(chunk_obj(cid, model, delta={"tool_calls": deltas}))
            elif clean:
                self._send_sse(chunk_obj(cid, model, delta={"content": clean}))
            self._send_sse(chunk_obj(cid, model, delta={}, finish=finish))
            self._send_sse("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass


def selftest():
    assert build_prompt([{"role": "user", "content": "hi"}]) == "Human: hi"
    assert build_prompt([{"role": "system", "content": "s"},
                          {"role": "user", "content": "u"}]) == "System: s\n\nHuman: u"
    assert build_prompt([{"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}},
        {"type": "text", "text": "b"}]}]) == "Human: a\nb"

    # image data URL decoding (images get uploaded as files, not inlined as text)
    _d, _m, _n = _data_url("data:image/png;base64," + base64.b64encode(b"PNGDATA").decode())
    assert _d == b"PNGDATA" and _m == "image/png" and _n == "image.png"
    assert _data_url("data:text/plain,hi") is None and _data_url("https://x/y.png") is None

    # tool instructions appended, tag present
    p = build_prompt([{"role": "user", "content": "x"}],
                     tools=[{"function": {"name": "read", "description": "d", "parameters": {}}}])
    assert TOOL_OPEN in p and '"read"' in p and p.startswith("Human: x")

    # assistant tool_calls + tool result replay
    rendered = build_prompt([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "FILE BODY"},
    ])
    assert "Assistant: <tool_calls>" in rendered and '"read"' in rendered
    assert "Human: [tool result: read]\nFILE BODY" in rendered

    # parse: tagged
    clean, calls = parse_tool_calls('ok\n<tool_calls>\n[{"name":"read","arguments":{"path":"a"}}]\n</tool_calls>')
    assert clean == "ok" and calls == [{"name": "read", "arguments": {"path": "a"}}]
    # parse: fenced inside tags
    _, calls = parse_tool_calls('<tool_calls>\n```json\n[{"name":"ls","arguments":{}}]\n```\n</tool_calls>')
    assert calls == [{"name": "ls", "arguments": {}}]
    # parse: string arguments normalized
    _, calls = parse_tool_calls('<tool_calls>[{"name":"ls","arguments":"{\\"a\\":1}"}]</tool_calls>')
    assert calls == [{"name": "ls", "arguments": {"a": 1}}]
    # parse: plain prose -> no calls
    assert parse_tool_calls("just a normal answer") == ("just a normal answer", [])
    # parse: bare json fallback
    _, calls = parse_tool_calls('[{"name":"ls","arguments":{}}]')
    assert calls == [{"name": "ls", "arguments": {}}]

    oc = to_openai_calls([{"name": "read", "arguments": {"path": "a"}}])
    assert oc[0]["type"] == "function" and oc[0]["function"]["name"] == "read"
    assert json.loads(oc[0]["function"]["arguments"]) == {"path": "a"}

    assert parse_sse_line(
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}') == ("text", "Hi")
    assert parse_sse_line('data: {"type":"message_stop"}') == ("stop", None)
    assert parse_sse_line('data: [DONE]') == ("stop", None)
    assert parse_sse_line('event: completion') is None
    assert parse_sse_line('data: {"type":"error","error":{"message":"boom"}}') == ("error", "boom")
    assert parse_sse_line(
        'data: {"type":"message_limit","message_limit":{"windows":{"5h":{"utilization":0.5}}}}'
    ) == ("limit", {"windows": {"5h": {"utilization": 0.5}}})
    _record_usage({"windows": {"5h": {"utilization": 0.29, "resets_at": int(time.time()) + 3600},
                               "7d": {"utilization": 0.01}}})
    _ur = usage_report()
    assert _ur["5h"]["used_percent"] == 29.0 and _ur["7d"]["used_percent"] == 1.0
    assert _ur["5h"]["resets_in"].endswith("m")

    # conversation reuse: prefix-stable fingerprints, match + incremental turn
    m1 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    m2 = m1 + [{"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "name": "ls", "content": "OUT"}]
    f1, f2 = _fingerprint(m1), _fingerprint(m2)
    assert f2[:len(f1)] == f1
    _sessions.clear()
    _store_session("conv-x", f1, "uuid-1")
    assert _match_session(f2) == ("conv-x", "uuid-1", 2)
    turn = build_turn(m2[2:], tools=[{"function": {"name": "ls", "parameters": {}}}])
    assert "[tool result: ls]\nOUT" in turn and "Assistant:" not in turn
    assert build_turn([{"role": "assistant", "content": "x"}]) == ""
    _sessions.clear()

    # trimming keeps system + newest, drops middle
    big = "x" * 80000
    msgs = [{"role": "system", "content": "SYS"}] + \
           [{"role": "user", "content": big}, {"role": "assistant", "content": big},
            {"role": "user", "content": "LATEST"}]
    tp = build_prompt(msgs)
    assert tp.startswith("System: SYS") and tp.endswith("Human: LATEST") and "earlier turns omitted" in tp

    # firefox cookie reader against a synthetic jar (host filter + path spec)
    import sqlite3
    import tempfile
    import shutil
    d = tempfile.mkdtemp()
    try:
        dbp = os.path.join(d, "cookies.sqlite")
        con = sqlite3.connect(dbp)
        con.execute("CREATE TABLE moz_cookies(name TEXT, value TEXT, host TEXT)")
        con.executemany("INSERT INTO moz_cookies VALUES(?,?,?)", [
            ("sessionKey", "sk-ant-sid01-TEST", ".claude.ai"),
            ("cf_clearance", "cf-TEST", "claude.ai"),
            ("other", "x", "example.com")])
        con.commit()
        con.close()
        ck = read_firefox_cookies(dbp)
        assert "sessionKey=sk-ant-sid01-TEST" in ck and "cf_clearance=cf-TEST" in ck
        assert "other=x" not in ck
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("selftest OK")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if not (SESSION_KEY or COOKIE or COOKIE_FROM):
        sys.exit("ERROR: set COOKIE_FROM=firefox (recommended), or CLAUDE_SESSION_KEY, "
                 "or CLAUDE_COOKIE. See header docstring.")
    creds = "manual"
    if COOKIE_FROM:
        try:
            jar = resolve_cookie_header(force=True)
            creds = f"browser:{COOKIE_FROM} ({jar.count('=')} cookies)"
        except ClaudeError as e:
            sys.exit(f"ERROR reading browser cookies: {e.message}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))  # run cleanup on the wrapper's kill
    print(f"claude-web2api -> http://{HOST}:{PORT}/v1  "
          f"(creds={creds}, impersonate={IMPERSONATE}, delete_after={DELETE_AFTER}, "
          f"reuse_ttl={SESSION_TTL}s, auth={'on' if API_KEY else 'off'})")
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if DELETE_AFTER and _sessions:  # best effort: delete reused threads on exit
            cc = ClaudeClient()
            ths = [threading.Thread(target=cc.delete_conversation, args=(conv,), daemon=True)
                   for conv in list(_sessions)]
            for t in ths:
                t.start()
            for t in ths:
                t.join(timeout=2)


if __name__ == "__main__":
    main()
