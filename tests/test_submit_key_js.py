"""
tests/test_submit_key_js.py — the front-end half of retry idempotency, run in
node: a fresh submit_key on every intentional /search submit, the native
fallback re-post keeping the same key, and deliberate credit denials
(X-Flintel-Denial) never being re-posted.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = [ROOT / "templates" / "index.html", ROOT / "templates" / "chat.html"]


def _helpers(path):
    text = path.read_text(encoding="utf-8")
    start = text.index("  function setFreshSubmitKey(form) {")
    end = text.index("  }\n", text.index("  function flintelSearchResponseAction(response) {")) + 4
    return text[start:end]


def test_both_templates_share_the_same_helpers():
    assert _helpers(TEMPLATES[0]) == _helpers(TEMPLATES[1])


@pytest.mark.parametrize("path", TEMPLATES, ids=["index", "chat"])
def test_key_is_set_before_every_search_formdata_and_never_on_fallback(path):
    text = path.read_text(encoding="utf-8")
    assert text.count('<input type="hidden" name="submit_key"') == 2   # both /search forms carry the key
    for m in re.finditer(r"setFreshSubmitKey\(form\);", text):
        nxt = text[m.end():m.end() + 160]
        assert "new FormData(form)" in nxt
    for fallback in re.finditer(r"(fallbackForm|form)\.submit\(\);", text):
        before = text[max(0, fallback.start() - 600):fallback.start()]
        assert "setFreshSubmitKey" not in before                     # a native retry keeps the key
    assert text.count("if (!response.ok) throw") == (1 if path.name == "chat.html" else 0)   # chat's GET-only navigateTo


NODE_SCRIPT = r"""
%s
const results = {};
function resp(status, header) {
  return { ok: status >= 200 && status < 300, status,
           headers: { get: (n) => (n === 'X-Flintel-Denial' ? header : null) } };
}
results.classify = [
  flintelSearchResponseAction(resp(429, 'credits-exhausted')),
  flintelSearchResponseAction(resp(503, 'credit-store-unavailable')),
  flintelSearchResponseAction(resp(200, null)),
  flintelSearchResponseAction(resp(500, null)),
  flintelSearchResponseAction(resp(502, null)),
  flintelSearchResponseAction(resp(503, null)),
];
const el = { value: 'server-rendered' };
const form = { querySelector: (s) => (s === 'input[name="submit_key"]' ? el : null) };
globalThis.window = globalThis;
setFreshSubmitKey(form); const k1 = el.value;
setFreshSubmitKey(form); const k2 = el.value;
const saved = globalThis.crypto.randomUUID;
Object.defineProperty(globalThis.crypto, 'randomUUID', { value: undefined, configurable: true });
setFreshSubmitKey(form); const k3 = el.value;
Object.defineProperty(globalThis.crypto, 'randomUUID', { value: saved, configurable: true });
setFreshSubmitKey({ querySelector: () => null });
results.keys = [k1, k2, k3];
console.log(JSON.stringify(results));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_helpers_in_node():
    out = subprocess.run(["node", "-e", NODE_SCRIPT % _helpers(TEMPLATES[0])], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    res = json.loads(out.strip().splitlines()[-1])
    assert res["classify"] == ["show", "show", "ok", "repost", "repost", "repost"]
    k1, k2, k3 = res["keys"]
    assert k1 != "server-rendered" and k1 != k2 and k2 != k3
    pattern = re.compile(r"^[A-Za-z0-9-]{16,64}$")                  # what the server accepts
    assert all(pattern.match(k) for k in (k1, k2, k3)) and re.fullmatch(r"[0-9a-f]{32}", k3)
