"""PROGRESS-FIRST UI: the plain 'Still gathering results…' row is replaced by the progress block
(intro + bar) from the first moment; stuck-at-0% fixes. Static checks + node-executed behaviour."""
import json, pathlib, re, shutil, subprocess
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = {"index": (ROOT / "templates/index.html").read_text(encoding="utf-8"),
         "chat": (ROOT / "templates/chat.html").read_text(encoding="utf-8")}
NODE = shutil.which("node")


def _fn_end(src, start):
    """index just past the closing brace of the function that begins at `start` (naive brace match)."""
    i = src.index("{", src.index(")", start))
    depth = 0
    while True:
        c = src[i]
        depth += (c == "{") - (c == "}")
        i += 1
        if depth == 0:
            return i


@pytest.mark.parametrize("name", FILES)
def test_no_js_created_plain_waiting_row(name):
    s = FILES[name]
    # the only JS mention left is the regex that detects the server-rendered plain row to convert it
    assert "textContent = 'Still gathering" not in s
    assert "Still gathering results for this search…</span>';" not in s
    assert "showSearchProgressPlaceholder(" in s


@pytest.mark.parametrize("name", FILES)
def test_wireUpStream_converts_plain_row_before_phrase_timer(name):
    s = FILES[name]
    i = s.index("function wireUpStream(")
    body = s[i:i + 6000]
    assert body.index("showSearchProgressPlaceholder(plainRow") < body.index("const loadingTextEl")
    assert ".turn-chip[data-requested-at]" in body


@pytest.mark.parametrize("name", FILES)
def test_stall_watchdog_present_and_bounded(name):
    s = FILES[name]
    assert "const STREAM_STALL_MS = 45000;" in s
    assert "if (tries >= 2) return;" in s
    assert "lastEventAt = Date.now();\n      let payload;" in s
    assert s.index("const stallTimer") < s.index("source.onmessage = (event) => {")


@pytest.mark.parametrize("name", FILES)
def test_server_value_no_longer_cancels_client_timer(name):
    s = FILES[name]
    i = s.index("function applySearchProgressPercent(")
    body = s[i:s.index("function startSearchProgressPercentTimer")]
    assert "clearTimeout(blockEl._progressTimer)" not in body
    assert "lastServerAt" in body
    j = s.index("function startSearchProgressPercentTimer")
    tick = s[j:s.index("function initSearchProgressBlocks")]
    assert "serverProgressSeen === 'true'" not in tick


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("name", FILES)
def test_progress_block_behaviour(name):
    s = FILES[name]
    a = s.index("function applySearchProgressPercent(")
    b = _fn_end(s, s.index("function renderSearchProgressBlock("))
    code = s[a:b]
    consts = "const SEARCH_PROGRESS_TRIGGER_SECONDS = 0; const SEARCH_PROGRESS_TIMEOUT_SECONDS = 360;"
    js = r"""
class El{constructor(t){this.tag=t;this.children=[];this.dataset={};this.style={};this.className='';this._text='';this.classList={c:this,contains:(x)=>this.className.split(/\s+/).includes(x)};}
 append(...n){n.forEach(x=>this.children.push(typeof x==='string'?{text:x}:x));}
 appendChild(n){this.children.push(n);return n;}
 insertBefore(n,ref){const i=this.children.indexOf(ref);this.children.splice(i<0?this.children.length:i,0,n);}
 remove(){}
 set textContent(v){this._text=v;} get textContent(){return this._text;}
 set innerHTML(v){this.children=[];} get innerHTML(){return '';}
 walk(f){f(this);this.children.forEach(c=>c.walk&&c.walk(f));}
 qsa(sel){const out=[];const cls=sel.split(',').map(x=>x.trim().replace('.',''));this.walk(n=>{if(n!==this&&cls.some(c=>(n.className||'').split(/\s+/).includes(c)))out.push(n)});return out;}
 querySelector(sel){const cls=sel.replace('.','');let r=null;this.walk(n=>{if(!r&&n!==this&&(n.className||'').split(/\s+/).includes(cls))r=n});return r;}
 querySelectorAll(sel){const r=this.qsa(sel);r.forEach(n=>{n.remove=()=>{this.removeDeep(n)}});return r;}
 removeDeep(t){this.walk(n=>{const i=n.children.indexOf(t);if(i>=0)n.children.splice(i,1)})}
 contains(){return true}
}
const document={createElement:t=>new El(t),createTextNode:t=>({text:t}),body:{contains:()=>true}};
Object.assign=(o,p)=>{for(const k in p)o[k]=p[k];return o};
let timers=[]; const setTimeout=(f,ms)=>{timers.push([f,ms]);return timers.length}; const clearTimeout=()=>{};
__CONSTS__
__CODE__
const row=new El('div'); row.className='loading-row';
showSearchProgressPlaceholder(row,new Date(Date.now()-120000).toISOString());   // 120s ago
const out={};
out.isBlock=row.classList.contains('search-progress-block');
out.hasTrack=!!row.querySelector('.search-progress-bar-track');
out.noChecklistLabel=!row.querySelector('.search-progress-checking-label');
const fire=()=>{const t=timers.splice(0);t.forEach(([f])=>f());};
fire();                                        // setTimeout(0) start + first tick
const pct=()=>row.querySelector('.search-progress-bar-percent').textContent;
out.clientPct=pct();                           // ~33%
applySearchProgressPercent(row,0,'server');    // server says 0 and then goes silent
out.afterServer0=pct();
row.dataset.lastServerAt=String(Date.now()-20000);   // 20s of silence
applySearchProgressPercent(row,50,'client');
out.afterStaleClient=pct();                    // client guess resumes (not stuck)
renderSearchProgressBlock(row,{intro:'REAL INTRO',outro:'REAL OUTRO',checklist:['a','b']},'');
out.keepsBarValue=pct()===out.afterStaleClient;
out.hasChecklist=!!row.querySelector('.search-progress-checklist');
out.hasLabel=!!row.querySelector('.search-progress-checking-label');
out.oneTrack=row.qsa('.search-progress-bar-track').length;
console.log(JSON.stringify(out));
""".replace("__CONSTS__", consts).replace("__CODE__", code)
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["isBlock"] and o["hasTrack"] and o["noChecklistLabel"]
    assert o["clientPct"] in ("33%", "34%", "32%")
    assert o["afterStaleClient"] != "0%" and int(o["afterStaleClient"][:-1]) >= 32
    assert o["keepsBarValue"] and o["hasChecklist"] and o["hasLabel"] and o["oneTrack"] == 1
