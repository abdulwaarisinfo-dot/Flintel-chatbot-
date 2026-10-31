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
    * Default mode is a DRY PREVIEW: it prints the confirmation block and the exact request body, then stops.
      Nothing is sent unless `--execute` is given.
    * One request only: no loops, no retries, 120 s timeout (plus a hard wall-clock alarm).
    * All LOCAL preflight checks (request configuration, key, httpx import, request construction, folders, existing
      marker) run first and create NO marker. A failed preflight therefore does not use up the attempt.
    * The attempt marker (runs/ONE_REQUEST_ATTEMPTED) is created immediately BEFORE the single HTTP request is
      initiated. Once it exists, the script refuses to run again (also inside the same process), so a failed HTTP
      attempt can never be retried and a re-run cannot spend a second time.
    * The API key is read from the environment variable OPENAI_API_KEY only. Leading/trailing whitespace is removed in
      memory; a blank, whitespace-containing or non-ASCII value is rejected. The key is never printed, logged or written.
      Output files are scanned and redacted for the key and for anything shaped like a key or a Bearer token.
      Request headers are never saved.
    * The script refuses to run (or write) inside either Flintel checkout.
    * The endpoint is a constant in this file; it is NOT taken from the environment.

USAGE
    python3 phase7_one_request_test.py                 # preview only, nothing is sent
    python3 phase7_one_request_test.py --execute       # sends ONE request (asks you to type RUN)
    python3 phase7_one_request_test.py --execute --yes # sends ONE request without the typed prompt

THIS REQUEST MAY INCUR CHARGES to whichever OpenAI project owns the key. This script cannot tell which project that
is and does not know current prices.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import sys
import time

# ---------------------------------------------------------------------------------------------------------------------
# Fixed configuration (the exact experiment approved in Phase 7). Change nothing here without re-approval.
# ---------------------------------------------------------------------------------------------------------------------
SCRIPT_VERSION = "1.2"
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
_KEY_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------
def utc_now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def inside_forbidden_path(path):
    real = os.path.realpath(path)
    return any(real == p or real.startswith(p + os.sep) for p in FORBIDDEN_PATH_PREFIXES)


def redact(text, api_key):
    """Remove the key itself and anything shaped like a key or a bearer token from a string."""
    if not isinstance(text, str):
        return text
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    for pattern in _KEY_SHAPES:
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_obj(obj, api_key):
    if isinstance(obj, dict):
        return {k: redact_obj(v, api_key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, api_key) for v in obj]
    if isinstance(obj, str):
        return redact(obj, api_key)
    return obj


def print_confirmation(run_dir, key_state):
    line = "=" * 78
    print(line)
    print("PRE-EXECUTION CONFIRMATION  (nothing has been sent yet)")
    print(line)
    print(f"Endpoint        : {ENDPOINT}")
    print(f"Model           : {MODEL}   (compatibility with web_search is UNVERIFIED)")
    print(f"Prompt (exact)  : {PROMPT}")
    print(f"Tool            : web_search | allowed_domains={TOOL['filters']['allowed_domains']} | "
          f"search_context_size={TOOL['search_context_size']} | user_location={TOOL['user_location']}")
    print("tool_choice     : \"required\" (the model must call a tool; web_search is the only tool configured)")
    print(f"include         : {REQUEST_BODY['include']}")
    print(f"max_tool_calls  : {REQUEST_BODY['max_tool_calls']}")
    print(f"max_output_tokens: {REQUEST_BODY['max_output_tokens']}   reasoning.effort: {REQUEST_BODY['reasoning']['effort']}")
    print(f"store           : {REQUEST_BODY['store']}")
    print(f"Timeout / retries: {TIMEOUT_SECONDS:.0f} s / 0 retries; exactly one request")
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
    if any(ch.isspace() for ch in key) or not key.isascii() or not key.isprintable():
        raise PreflightError("OPENAI_API_KEY contains whitespace, control or non-ASCII characters.")
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
    client = None
    try:
        client = httpx.Client(timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False)
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
    parser.add_argument("--execute", action="store_true", help="actually send the single request (may incur charges)")
    parser.add_argument("--yes", action="store_true", help="skip the typed RUN confirmation (use with --execute)")
    args = parser.parse_args()

    script_path = os.path.realpath(__file__)
    base_dir = os.path.dirname(script_path)
    if inside_forbidden_path(script_path) or inside_forbidden_path(os.getcwd()) or inside_forbidden_path(base_dir):
        print("REFUSED: this script and its working directory must be outside both Flintel checkouts.")
        return 2

    runs_dir = os.path.join(base_dir, "runs")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(runs_dir, stamp)

    print_confirmation(run_dir, describe_key_state(os.environ))
    print("\nExact request body (no headers, no key):")
    print(json.dumps(REQUEST_BODY, indent=2))

    if not args.execute:
        print("\nNOT EXECUTED. Add --execute to send the single request.")
        return 0

    try:
        ctx = preflight(os.environ, runs_dir, run_dir)
    except PreflightError as exc:
        print(f"\nSTOPPED before sending anything. The one-attempt marker was NOT created.\nReason: {exc}")
        return 2

    if sys.stdin.isatty() and not args.yes:
        answer = input("\nType RUN to send the single request, anything else to abort: ").strip()
        if answer != "RUN":
            ctx["client"].close()
            print("Aborted by user. Nothing was sent. The one-attempt marker was NOT created.")
            return 0

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
    record["client_error"] = redact(error_text, api_key) if error_text else None

    data = None
    if raw_text is not None:
        try:
            data = json.loads(raw_text)
        except ValueError:
            record["unparseable_body_excerpt"] = redact(raw_text[:4000], api_key)
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

    # Redact, then verify no key material remains, before anything touches disk.
    safe_record = redact_obj(record, api_key)
    serialized = json.dumps(safe_record, indent=2, ensure_ascii=False)
    safe_raw = json.dumps(redact_obj(raw_to_save, api_key), indent=2, ensure_ascii=False) if raw_to_save is not None else ""
    if api_key and (api_key in serialized or api_key in safe_raw):
        print("STOPPED WRITING: key material detected in output after redaction. Nothing was saved.")
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
