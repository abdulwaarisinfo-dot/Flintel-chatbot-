"""SEND-LOCK: typing allowed while a search runs, sending blocked until it finishes.
Static checks on both templates + a node-executed test of setSubmitLoading()."""
import json, pathlib, re, shutil, subprocess
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = {"index": (ROOT / "templates/index.html").read_text(encoding="utf-8"),
         "chat": (ROOT / "templates/chat.html").read_text(encoding="utf-8")}


@pytest.mark.parametrize("name", FILES)
def test_input_never_disabled_and_flag_declared(name):
    s = FILES[name]
    assert "let searchRunning = false;" in s
    assert "searchInput.disabled = isLoading" not in s
    assert "if (searchInput) searchInput.disabled = false;" in s
    assert "searchRunning = !!isLoading;" in s


@pytest.mark.parametrize("name", FILES)
def test_enter_and_submit_guarded(name):
    s = FILES[name]
    assert "if (searchRunning) return; // (SEND-LOCK) Enter must not send" in s
    assert "if (searchRunning) return; // (SEND-LOCK) a search is still running" in s


def test_hero_submit_guarded_locks_and_clears_immediately():
    s = FILES["index"]
    i = s.index("if (e.target.id !== 'search-form') return;")
    body = s[i:i + 6000]
    assert body.index("if (searchRunning) return;") < body.index("new FormData(form)")
    assert body.index("new FormData(form)") < body.index("setSubmitLoading(true)") < body.index("searchInput.value = ''")
    assert "setSubmitLoading(false);\n        showSearchInlineError(errorText, query);" in body


@pytest.mark.parametrize("name", FILES)
def test_soft_submit_clears_right_after_formdata_and_not_after_response(name):
    s = FILES[name]
    i = s.index("function wireUpSoftSearchSubmit()")
    j = s.index("\n  }\n", i)
    body = s[i:j]
    seq = "const formData = new FormData(form);\n      setSubmitLoading(true);"
    assert seq in body
    assert body.index(seq) < body.index("if (searchInput) { searchInput.value = ''; autoGrowSearchInput(searchInput); }")
    assert body.count("searchInput.value = ''") == 1   # the old post-response clear is gone
    assert "searchInput.value = query;" in body        # native-submit fallback restores the prompt


@pytest.mark.parametrize("name", FILES)
def test_no_spinner_swap_and_greyed_disabled_style(name):
    s = FILES[name]
    assert ".submit-btn.loading svg { display: none; }" not in s
    assert ".submit-btn:disabled { background: #2b2a28;" in s


@pytest.mark.parametrize("name", FILES)
def test_error_restore_never_overwrites_next_prompt(name):
    s = FILES[name]
    assert "if (searchInput.value.trim() === '') {" in s and "(Not sent: " in s


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("name", FILES)
def test_setSubmitLoading_behaviour(name):
    s = FILES[name]
    a = s.index("let submitLoadingWatchdogId = null;")
    b = s.index("// ---- Small inline warning row")
    code = s[a:b]
    js = r"""
const store={};
const mk=()=>({disabled:false,value:'',classList:{s:new Set(),toggle(c,on){on?this.s.add(c):this.s.delete(c)},contains(c){return this.s.has(c)}}});
const btn=mk(), input=mk();
const document={getElementById:id=>id==='submit-btn'?btn:id==='search-input'?input:null};
const timers=[]; const setTimeout=(f,ms)=>{timers.push(ms);return timers.length}; const clearTimeout=()=>{};
function showSearchInlineError(){}
%s
const out={};
setSubmitLoading(true);
out.running=searchRunning; out.btnDisabledWhileRunning=btn.disabled; out.inputDisabledWhileRunning=input.disabled;
input.value='prompt 2';                       // typing while running
out.btnStillDisabledAfterTyping=btn.disabled;
out.wd=timers[0];
setSubmitLoading(false);
out.runningAfter=searchRunning; out.btnEnabledAfterFinish=!btn.disabled;
input.value=''; setSubmitLoading(false); out.btnDisabledWhenEmpty=btn.disabled;
console.log(JSON.stringify(out));
""" % code
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o == {"running": True, "btnDisabledWhileRunning": True, "inputDisabledWhileRunning": False,
                 "btnStillDisabledAfterTyping": True, "wd": 200000, "runningAfter": False,
                 "btnEnabledAfterFinish": True, "btnDisabledWhenEmpty": True}
