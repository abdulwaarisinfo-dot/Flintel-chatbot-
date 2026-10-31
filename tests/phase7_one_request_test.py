#!/usr/bin/env python3
"""
phase7_one_request_test.py  --  ONE-REQUEST compatibility test for OpenAI Responses API `web_search`
==============================================================================================

PURPOSE
    Find out, with exactly ONE request, whether `gpt-5-mini` + `web_search` (restricted to reddit.com) works with the
    settings below, and record what the API actually returns (status, citations, sources, usage, latency).

WHAT THIS SCRIPT IS NOT
    * It is NOT part of Flintel. It imports no Flintel module, reads no application database, uses no application route
      and no application configuration. Standard library + `httpx` only.
    * It does NOT verify anything. A URL, a title, a citation or a model-written summary is NEVER treated as verified
      original post text. Every URL is written out as "NOT VERIFIED"; verification is a separate, manual step.

SAFETY
    * Default mode is a DRY PREVIEW: the request configuration is VALIDATED first, then displayed (every displayed
      value is read from the configuration itself), and the script stops. Nothing is sent unless `--execute` is given.
    * `--execute` requires an INTERACTIVE terminal (stdin and stdout) and the exact typed answer RUN. No flag or
      environment setting skips this. A non-interactive session is refused BEFORE any attempt marker, HTTP client or
      other network-capable step exists.
    * One request only: no loops, no retries (MAX_RETRIES = 0), redirects not followed (FOLLOW_REDIRECTS = False),
      120 s timeout (plus a hard wall-clock alarm where the platform has one; native Windows does not).
    * All LOCAL preflight checks (request configuration, key, httpx import and defaults, request construction,
      folders, existing marker) run first and create NO marker. A failed preflight does not use up the attempt.
    * The attempt marker (runs/ONE_REQUEST_ATTEMPTED) is created immediately BEFORE the single HTTP request is
      initiated. Once it exists the script refuses to run again (also inside the same process), so a failed HTTP
      attempt can never be retried and a re-run cannot spend a second time.
    * The API key is read from the environment variable OPENAI_API_KEY only. Leading/trailing whitespace is removed
      in memory; a value that is shorter than 20 characters or has characters outside A-Z a-z 0-9 _ - . is rejected.
      The key is never printed, logged or written.
    * Output redaction removes the exact key, every 6-character fragment of it (with the token around it), key-shaped
      tokens (sk-..., including masked forms such as sk-...****abcd), Bearer/Authorization values and masked suffixes.
      The serialised report is then CHECKED; if any key material, masked suffix or key-shaped token remains, or if
      redaction itself fails, nothing is written (fail closed). Request headers are never saved.
    * The script refuses to run (also in preview) when its folder or the current folder is inside a Git working tree on
      any operating system, or inside either Flintel checkout path.
    * The endpoint is a constant in this file; it is NOT taken from the environment.

USAGE
    python3 phase7_one_request_test.py     # validation + preview only; nothing is sent
    A live send needs the --execute flag, an interactive terminal and the typed answer RUN. (No live command is
    recommended here; that decision is made separately.)

THIS REQUEST MAY INCUR CHARGES to whichever OpenAI project owns the key. This script cannot tell which project that
is and does not know current prices.
"""

import argparse
import datetime as dt
import hashlib
import inspect
import json
import os
import re
import signal
import sys
import time

# ---------------------------------------------------------------------------------------------------------------------
# Fixed configuration (the exact experiment approved in Phase 7). Change nothing here without re-approval.
# ---------------------------------------------------------------------------------------------------------------------
SCRIPT_VERSION = "1.3"
ENDPOINT = "https://api.openai.com/v1/responses"          # constant on purpose; not read from the environment
MODEL = "gpt-5-mini"
PROMPT = "Find recent Reddit discussions from people looking for a habit-tracking app."
INSTRUCTIONS = (
    "Search, then report only what the sources show. Quote the author's words verbatim only when they are visible. "
    "Write 'unknown' for anything that is not visible. Never guess a date, an author or a URL."
)
TOOL = {
    "type": "web_search",
    "filters": {"allowed_domains": ["reddit.com"]},
    "search_context_size": "low",
    "user_location": {"type": "approximate"},
}
REQUEST_BODY = {
    "model": MODEL,
    "input": PROMPT,
    "instructions": INSTRUCTIONS,
    "tools": [TOOL],
    "tool_choice": "required",          # official ToolChoiceOptions value; the only configured tool is web_search
    "include": ["web_search_call.action.sources"],
    "max_tool_calls": 1,
    "max_output_tokens": 2000,
    "reasoning": {"effort": "low"},
    "store": False,
}
TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 0                   # no retries anywhere; preflight verifies httpx's default transport agrees
FOLLOW_REDIRECTS = False          # redirects are never followed
MAX_REQUESTS = 1                  # exactly one request (enforced by the attempt marker)
KEY_MIN_LENGTH = 20
FRAGMENT_LEN = 6                  # every FRAGMENT_LEN-character window of the key is redacted
HARD_ALARM_SECONDS = 130          # wall-clock backstop; httpx timeouts apply per phase, not to the whole call
MARKER_NAME = "ONE_REQUEST_ATTEMPTED"
_ATTEMPT_STARTED = False          # in-process guard: a second send_once() in the same process is refused
FORBIDDEN_PATH_PREFIXES = (
    "/home/user/flintel-chatbot-",
    "/home/user/abdulwaarisinfo-dot/flintel-chatbot-",
)

_REDDIT_POST = re.compile(
    r"^https?://(?:(?:www|old|np|new)\.)?reddit\.com/r/([^/\s]+)/comments/([a-z0-9]+)(?:[/?#].*)?$", re.IGNORECASE)
_REDDIT_SHORT = re.compile(r"^https?://redd\.it/([a-z0-9]+)(?:[/?#].*)?$", re.IGNORECASE)
_SHAPE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-.*\u2022\u2026\u00b7]+"),                          # sk-..., incl. masked / ellipsis forms
    re.compile(r"\bbearer\s+[^\s\"',;)\]}]+", re.IGNORECASE),                          # Bearer <anything>
    re.compile(r"\bauthorization\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;)\]}]+", re.IGNORECASE),  # Authorization: <value>
    re.compile(r"\bending\s+(?:in|with)\s+[A-Za-z0-9_\-]{3,12}", re.IGNORECASE),        # "ending in abcd"
    re.compile(r"[*\u2022\u2026\u00b7]{3,}[A-Za-z0-9_\-]{1,12}"),                       # ****abcd
    re.compile(r"[A-Za-z0-9_\-]{1,12}[*\u2022\u2026\u00b7]{3,}"),                       # abcd****
)
_FRAGMENT_CACHE = {}
_SUFFIX_CACHE = {}
_MASK_RUN = r"(?:[*\u2022\u2026\u00b7]|\.{2,})"           # *, bullet, ellipsis, middle dot, or two or more dots


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------
def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def inside_forbidden_path(path):
    real = os.path.realpath(path)
    return any(real == p or real.startswith(p + os.sep) for p in FORBIDDEN_PATH_PREFIXES)


def _fragment_regex(key):
    """Matches the whole token around ANY FRAGMENT_LEN-character window of the key (prefix, suffix or middle)."""
    if key not in _FRAGMENT_CACHE:
        grams = {key[i:i + FRAGMENT_LEN] for i in range(len(key) - FRAGMENT_LEN + 1)}
        alternatives = "|".join(re.escape(g) for g in sorted(grams))
        _FRAGMENT_CACHE[key] = re.compile(r"[A-Za-z0-9_\-]*(?:" + alternatives + r")[A-Za-z0-9_\-]*")
    return _FRAGMENT_CACHE[key]


def _suffix_regex(key):
    """Matches the token before a mask run plus the mask run plus any 4..8 character suffix of the key."""
    if key not in _SUFFIX_CACHE:
        suffixes = "|".join(re.escape(key[-k:]) for k in range(8, 3, -1))
        _SUFFIX_CACHE[key] = re.compile(r"[A-Za-z0-9_\-]*" + _MASK_RUN + r"+\s*(?:" + suffixes + r")")
    return _SUFFIX_CACHE[key]


def redact(text, api_key):
    """Remove the key, any fragment of it, and anything shaped like a key, a bearer token or a masked key.

    Order matters: whole key-shaped tokens first (so a partly shown key is removed as one piece), then masked suffixes
    of THIS key, then every 6-character fragment of it."""
    if not isinstance(text, str):
        return text
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    for pattern in _SHAPE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if api_key:
        text = _suffix_regex(api_key).sub("[REDACTED]", text)
        if len(api_key) >= FRAGMENT_LEN:
            text = _fragment_regex(api_key).sub("[REDACTED]", text)
    return text


def redact_obj(obj, api_key):
    """Redacts every string VALUE and every string dictionary KEY."""
    if isinstance(obj, dict):
        return {(redact(k, api_key) if isinstance(k, str) else k): redact_obj(v, api_key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, api_key) for v in obj]
    if isinstance(obj, str):
        return redact(obj, api_key)
    return obj


def leak_check(text, api_key):
    """Returns a reason string if key material may remain in `text`, else None. Used to FAIL CLOSED before writing."""
    if not api_key:
        return "no key available to check against"
    if api_key in text:
        return "the exact key is present"
    for i in range(len(api_key) - 7):
        if api_key[i:i + 8] in text:
            return "a fragment of 8 or more key characters is present"
    for k in range(4, 8):
        suffix = re.escape(api_key[-k:])
        if re.search(_MASK_RUN + r"\s*" + suffix, text) or re.search(r"(?i)ending\s+(?:in|with)\s+" + suffix, text):
            return "a masked key suffix is present"
    if re.search(r"\bsk-", text) or re.search(r"(?i)\bbearer\s+(?!\[REDACTED\])", text):
        return "a key-shaped or bearer token is present"
    return None


def is_interactive():
    """True only when BOTH stdin and stdout are terminals."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def find_git_working_tree(path):
    """The nearest enclosing folder that contains a .git entry (folder, or file for worktrees/submodules), else None."""
    current = os.path.realpath(path)
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def print_confirmation(run_dir, key_state):
    """Every displayed setting is read from REQUEST_BODY / the constants (never from fixed text)."""
    body = REQUEST_BODY
    tool = body["tools"][0]
    alarm = f"{HARD_ALARM_SECONDS} s hard wall-clock alarm" if hasattr(signal, "SIGALRM") else "NO hard wall-clock alarm on this platform"
    line = "=" * 78
    print(line)
    print("PRE-EXECUTION CONFIRMATION  (nothing has been sent yet)")
    print(line)
    print(f"Endpoint        : {ENDPOINT}")
    print(f"Model           : {body['model']}   (compatibility with web_search is UNVERIFIED)")
    print(f"Prompt (exact)  : {body['input']}")
    print(f"Tool            : {tool['type']} | allowed_domains={tool['filters']['allowed_domains']} | "
          f"search_context_size={tool['search_context_size']} | user_location={tool['user_location']}")
    print(f"tool_choice     : {json.dumps(body['tool_choice'])}   (tools configured: {len(body['tools'])})")
    print(f"include         : {body['include']}")
    print(f"max_tool_calls  : {body['max_tool_calls']}")
    print(f"max_output_tokens: {body['max_output_tokens']}   reasoning.effort: {body['reasoning']['effort']}")
    print(f"store           : {body['store']}")
    print(f"Timeout         : {TIMEOUT_SECONDS:.0f} s per network phase; {alarm}")
    print(f"Retries         : {MAX_RETRIES}   Redirects followed: {FOLLOW_REDIRECTS}   Requests allowed: {MAX_REQUESTS}")
    print(f"OPENAI_API_KEY  : {key_state} (value never shown)")
    print(f"Output folder   : {run_dir}")
    print()
    print("THIS REQUEST MAY INCUR CHARGES to the OpenAI project that owns the key.")
    print("This script cannot identify that project and does not know current prices. A rejected or failed request")
    print("is NOT assumed to be free.")
    print("Nothing returned will be counted as verified original post evidence; URLs, titles, citations and model")
    print("summaries are recorded as NOT VERIFIED.")
    print(line)


def classify_url(url):
    """Pure text check of the URL shape. This proves nothing about the page."""
    m = _REDDIT_POST.match(url or "")
    if m:
        return {"reddit_post_url_pattern": True, "subreddit": m.group(1), "post_id": m.group(2).lower()}
    m = _REDDIT_SHORT.match(url or "")
    if m:
        return {"reddit_post_url_pattern": True, "subreddit": None, "post_id": m.group(1).lower()}
    return {"reddit_post_url_pattern": False, "subreddit": None, "post_id": None}


def analyse_response(data):
    """Extract structure only. Nothing here verifies content."""
    calls, annotations, texts = [], [], []
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            sources = action.get("sources") if isinstance(action.get("sources"), list) else None
            calls.append({
                "id": item.get("id"),
                "status": item.get("status"),
                "action_type": action.get("type"),
                "query": action.get("query"),
                "queries": action.get("queries"),
                "opened_url": action.get("url"),
                "sources_field_present": sources is not None,
                "source_urls": [s.get("url") for s in (sources or []) if isinstance(s, dict)],
            })
        elif kind == "message":
            for part in (item.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(part.get("text") or "")
                    for ann in (part.get("annotations") or []):
                        if isinstance(ann, dict) and ann.get("type") == "url_citation":
                            annotations.append({"url": ann.get("url"), "title": ann.get("title"),
                                                "start_index": ann.get("start_index"), "end_index": ann.get("end_index")})
    source_urls = [u for c in calls for u in c["source_urls"] if u]
    cited_urls = [a["url"] for a in annotations if a.get("url")]
    titles = {}
    for a in annotations:
        if a.get("url") and a["url"] not in titles:
            titles[a["url"]] = a.get("title")
    rows, seen_ids = [], {}
    for url in dict.fromkeys(source_urls + cited_urls):          # unique, order kept
        info = classify_url(url)
        pid = info["post_id"]
        if pid:
            seen_ids[pid] = seen_ids.get(pid, 0) + 1
        rows.append({
            "url": url,
            "in_sources_list": url in source_urls,
            "in_citations": url in cited_urls,
            "cited_title": titles.get(url),
            **info,
            "duplicate_of_same_post_id": False,                 # filled in below
            # --- everything below is deliberately NOT established by this script ---
            "url_resolves_to_cited_post": "NOT_VERIFIED",
            "relevant_to_original_prompt": "NOT_ASSESSED",
            "original_text_obtained": False,
            "publication_time_verified": False,
            "verified_age_under_one_hour": "UNKNOWN",
            "evidence_class": "citation_metadata_only" if url in cited_urls else "url_only",
        })
    for row in rows:
        if row["post_id"] and seen_ids.get(row["post_id"], 0) > 1:
            row["duplicate_of_same_post_id"] = True
    return {
        "web_search_calls": calls,
        "url_citation_annotations": annotations,
        "model_text_is_a_generated_summary_not_source_text": "\n".join(texts),
        "url_rows": rows,
    }


class PreflightError(Exception):
    """A LOCAL problem found before any request. Raised only when no attempt marker has been created."""


def normalize_api_key(raw):
    """Strip leading/trailing whitespace IN MEMORY and validate shape. Never echoes the value."""
    key = (raw or "").strip()
    if not key:
        raise PreflightError("OPENAI_API_KEY is missing or blank.")
    if len(key) < KEY_MIN_LENGTH or not re.fullmatch(r"[A-Za-z0-9_\-.]+", key):
        raise PreflightError("OPENAI_API_KEY is too short or contains characters outside A-Z a-z 0-9 _ - .")
    return key


def describe_key_state(env):
    try:
        normalize_api_key(env.get("OPENAI_API_KEY"))
        return "set and well-formed in the environment"
    except PreflightError:
        return "NOT usable (missing, blank or malformed)"


def validate_request_config():
    """Local consistency checks of the fixed request. These do not claim the API will accept it."""
    problems = []
    body = REQUEST_BODY
    if not str(ENDPOINT).startswith("https://api.openai.com/"):
        problems.append("endpoint is not https://api.openai.com/")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        problems.append("model is empty")
    if not isinstance(body.get("input"), str) or not body["input"].strip() or body["input"] != PROMPT:
        problems.append("input is empty or differs from the approved prompt")
    if body.get("store") is not False:
        problems.append("store must be false")
    if body.get("max_tool_calls") != 1:
        problems.append("max_tool_calls must be 1")
    mot = body.get("max_output_tokens")
    if not isinstance(mot, int) or isinstance(mot, bool) or not 1 <= mot <= 4000:
        problems.append("max_output_tokens must be an integer from 1 to 4000")
    tools = body.get("tools")
    if (not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict)
            or tools[0].get("type") != "web_search"
            or (tools[0].get("filters") or {}).get("allowed_domains") != ["reddit.com"]):
        problems.append("tools must be exactly one web_search restricted to reddit.com")
    if tools and isinstance(tools, list) and isinstance(tools[0], dict):
        t0 = tools[0]
        if not isinstance(t0.get("search_context_size"), str) or not isinstance(t0.get("user_location"), dict):
            problems.append("web_search tool must define search_context_size and user_location")
    if not isinstance(body.get("reasoning"), dict) or not isinstance(body["reasoning"].get("effort"), str):
        problems.append("reasoning.effort must be defined")
    if MAX_RETRIES != 0:
        problems.append("MAX_RETRIES must be 0")
    if FOLLOW_REDIRECTS is not False:
        problems.append("FOLLOW_REDIRECTS must be False")
    if MAX_REQUESTS != 1:
        problems.append("MAX_REQUESTS must be 1")
    if body.get("tool_choice") != "required":
        problems.append('tool_choice must be the string "required" (the only configured tool is web_search)')
    if "web_search_call.action.sources" not in (body.get("include") or []):
        problems.append("include must request web_search_call.action.sources")
    if TIMEOUT_SECONDS != 120.0:
        problems.append("timeout must be 120 seconds")
    try:
        json.dumps(body)
    except (TypeError, ValueError):
        problems.append("request body is not JSON-serialisable")
    if problems:
        raise PreflightError("Invalid request configuration: " + "; ".join(problems))


def check_httpx_defaults(httpx_module):
    """The default transport must not retry. Local introspection only; no I/O."""
    try:
        default = inspect.signature(httpx_module.HTTPTransport.__init__).parameters["retries"].default
    except Exception:
        raise PreflightError("could not confirm that the httpx transport does not retry.") from None
    if default != MAX_RETRIES:
        raise PreflightError("the installed httpx transport would retry by default; refusing to continue.")


def preflight(env, runs_dir, run_dir):
    """Every LOCAL check. Creates NO attempt marker and sends NOTHING. Returns the context for send_once()."""
    validate_request_config()
    api_key = normalize_api_key(env.get("OPENAI_API_KEY"))
    marker = os.path.join(runs_dir, MARKER_NAME)
    if os.path.exists(marker):
        raise PreflightError(f"{marker} already exists: this script allows ONE attempt.")
    try:
        import httpx
    except Exception:
        raise PreflightError("the httpx package is not available.") from None
    check_httpx_defaults(httpx)
    client = None
    try:
        client = httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=FOLLOW_REDIRECTS)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        request = client.build_request("POST", ENDPOINT, headers=headers, json=REQUEST_BODY)   # local only, no I/O
    except Exception as exc:
        if client is not None:
            client.close()
        raise PreflightError(f"the request could not be built locally ({type(exc).__name__}).") from None
    try:
        os.makedirs(runs_dir, exist_ok=True)
        os.makedirs(run_dir, exist_ok=False)
    except OSError as exc:
        client.close()
        raise PreflightError(f"the output folder could not be created ({type(exc).__name__}).") from None
    return {"client": client, "request": request, "api_key": api_key, "marker": marker,
            "runs_dir": runs_dir, "run_dir": run_dir}


def default_sender(ctx):
    """The one real HTTP send. Called only from send_once(), after the attempt marker exists."""
    response = ctx["client"].send(ctx["request"])
    return response.status_code, response.text


def send_once(ctx, sender=None):
    """Create the attempt marker, THEN initiate the single HTTP request. Never retries.

    `sender` exists only so the safety logic can be tested without a network; the real run uses default_sender."""
    global _ATTEMPT_STARTED
    if ctx.get("confirmed") is not True:
        raise PreflightError("the interactive RUN confirmation is missing; nothing was sent.")
    if _ATTEMPT_STARTED:
        raise PreflightError("an attempt was already started in this process; retries are not allowed.")

    def _alarm(_signum, _frame):
        raise TimeoutError("hard wall-clock limit reached")
    have_alarm = hasattr(signal, "SIGALRM")
    if have_alarm:                                # local setup that can fail: do it BEFORE the marker exists
        try:
            signal.signal(signal.SIGALRM, _alarm)
        except (ValueError, OSError):
            raise PreflightError("the wall-clock alarm handler could not be installed.") from None
    try:
        fd = os.open(ctx["marker"], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PreflightError("the attempt marker already exists; this script allows ONE attempt.") from None
    except OSError as exc:
        raise PreflightError(f"the attempt marker could not be created ({type(exc).__name__}).") from None
    _ATTEMPT_STARTED = True                       # from here on the attempt is consumed, whatever happens next
    started_at = utc_now_iso()
    try:                                          # best effort: nothing between marker and send may abort the send
        with os.fdopen(fd, "w") as fh:
            fh.write(f"attempted_at={started_at}\nscript_version={SCRIPT_VERSION}\n")
    except OSError:
        pass
    http_status, raw_text, error_text = None, None, None
    t0 = time.monotonic()
    if have_alarm:
        signal.alarm(HARD_ALARM_SECONDS)
    try:
        http_status, raw_text = (sender or default_sender)(ctx)                       # the ONE request; no retry
    except Exception as exc:                                                         # recorded, never retried
        error_text = f"{type(exc).__name__}: {exc}"
    finally:
        if have_alarm:
            signal.alarm(0)
        try:
            ctx["client"].close()
        except Exception:
            pass
    return {"started_at_utc": started_at, "finished_at_utc": utc_now_iso(), "latency": time.monotonic() - t0,
            "http_status": http_status, "raw_text": raw_text, "error_text": error_text}


def main():
    parser = argparse.ArgumentParser(description="One-request web_search compatibility test (preview unless --execute).")
    parser.add_argument("--execute", action="store_true",
                        help="send the single request: needs an interactive terminal and the typed answer RUN (may incur charges)")
    args = parser.parse_args()

    script_path = os.path.realpath(__file__)
    base_dir = os.path.dirname(script_path)
    for label, location in (("script", script_path), ("script folder", base_dir), ("current folder", os.getcwd())):
        if inside_forbidden_path(location):
            print(f"REFUSED: the {label} is inside a Flintel checkout. Use a folder outside both checkouts.")
            return 2
        git_root = find_git_working_tree(location)
        if git_root:
            print(f"REFUSED: the {label} is inside a Git working tree ({git_root}). Use a folder outside any repository.")
            return 2

    try:
        validate_request_config()
    except PreflightError as exc:
        print(f"INVALID REQUEST CONFIGURATION: {exc}\nNothing is shown or sent.")
        return 2

    runs_dir = os.path.join(base_dir, "runs")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(runs_dir, stamp)

    print_confirmation(run_dir, describe_key_state(os.environ))
    print("\nExact request body (no headers, no key):")
    print(json.dumps(REQUEST_BODY, indent=2))

    if not args.execute:
        print("\nPREVIEW ONLY. Nothing was sent.")
        return 0

    if not is_interactive():                       # BEFORE preflight, marker, httpx import or any network-capable step
        print("\nREFUSED: --execute needs an interactive terminal (stdin and stdout). Nothing was prepared or sent.")
        return 2

    try:
        ctx = preflight(os.environ, runs_dir, run_dir)
    except PreflightError as exc:
        print(f"\nSTOPPED before sending anything. The one-attempt marker was NOT created.\nReason: {exc}")
        return 2

    try:
        answer = input("\nType RUN (exactly, in capitals) to send the single request; anything else aborts: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "RUN":
        ctx["client"].close()
        print("Aborted. Nothing was sent. The one-attempt marker was NOT created.")
        return 0
    ctx["confirmed"] = True

    try:
        result = send_once(ctx)
    except PreflightError as exc:
        ctx["client"].close()
        print(f"\nSTOPPED. Nothing was sent.\nReason: {exc}")
        return 2
    api_key = ctx["api_key"]
    http_status, raw_text, error_text = result["http_status"], result["raw_text"], result["error_text"]

    record = {
        "script_version": SCRIPT_VERSION,
        "script_sha256": hashlib.sha256(open(script_path, "rb").read()).hexdigest(),
        "endpoint": ENDPOINT,
        "request_body": REQUEST_BODY,
        "request_started_at_utc": result["started_at_utc"],
        "notice": ("Nothing in this file is verified original post evidence. URLs, titles, citations and the model "
                   "text are unverified API output. Retrieval time is NOT publication time."),
    }
    record["retrieved_at_utc"] = result["finished_at_utc"]
    record["latency_seconds"] = round(result["latency"], 3)
    record["http_status"] = http_status
    record["client_error"] = error_text if error_text else None

    data = None
    if raw_text is not None:
        try:
            data = json.loads(raw_text)
        except ValueError:
            record["unparseable_body_excerpt"] = raw_text[:4000]
    if isinstance(data, dict):
        record["response"] = {
            "id": data.get("id"), "status": data.get("status"), "model": data.get("model"),
            "error": data.get("error"), "incomplete_details": data.get("incomplete_details"),
            "usage": data.get("usage"),
        }
        record["analysis"] = analyse_response(data)
        raw_to_save = data
    else:
        raw_to_save = None

    # Redact EVERYTHING (values, keys, final text), then CHECK. Any doubt, or any failure, means nothing is written.
    problem, serialized, safe_raw, safe_record = None, "", "", {}
    try:
        safe_record = redact_obj(record, api_key)
        serialized = redact(json.dumps(safe_record, indent=2, ensure_ascii=False), api_key)
        if raw_to_save is not None:
            safe_raw = redact(json.dumps(redact_obj(raw_to_save, api_key), indent=2, ensure_ascii=False), api_key)
        problem = leak_check(serialized, api_key) or (leak_check(safe_raw, api_key) if safe_raw else None)
    except Exception:
        problem = "redaction itself failed"
    if problem:
        print("\nSTOPPED WRITING (fail closed): safe redaction could not be established "
              f"({problem}). Nothing was saved. The one attempt is consumed.")
        print(f"  HTTP status: {http_status}   Latency: {round(result['latency'], 3)} s")
        return 3
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as fh:
        fh.write(serialized)
    if safe_raw:
        with open(os.path.join(run_dir, "raw_response.json"), "w", encoding="utf-8") as fh:
            fh.write(safe_raw)

    resp = safe_record.get("response") or {}
    rows = (safe_record.get("analysis") or {}).get("url_rows") or []
    print("\nRequest finished (no key or headers were saved).")
    print(f"  HTTP status      : {http_status}")
    print(f"  Response status  : {resp.get('status')}")
    print(f"  Latency          : {record['latency_seconds']} s")
    print(f"  Usage (as given) : {resp.get('usage')}")
    print(f"  URLs returned    : {len(rows)} (all NOT VERIFIED; {sum(1 for r in rows if r['reddit_post_url_pattern'])} match a Reddit post URL pattern)")
    print(f"  Saved to         : {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
