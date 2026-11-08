"""A partially analysed answer says so in the page (both templates), and a complete one shows nothing extra."""
import json
from pathlib import Path

import pytest

from tests.test_ui_answer_blocks import _block

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("template", ["chat.html", "index.html"])
def test_partial_analysis_note(integrity_browser, template):
    js = _block((ROOT / "templates" / template).read_text(), "function appendAnalysisCoverage(", "p")
    pg = integrity_browser.new_page()
    try:
        pg.set_content(f"<div id=c></div><script>{js}</script>")
        run = lambda d: pg.evaluate("(d)=>{const c=document.getElementById('c');c.innerHTML='';appendAnalysisCoverage(d,c);return c.innerText}", d)
        assert run({}) == "" and run({"analysis_coverage": {"batches": 3, "failed_batches": 0, "posts": 26}}) == ""
        txt = run({"analysis_coverage": {"batches": 84, "failed_batches": 2, "posts": 1000}})
        assert "may be incomplete" in txt and "2 of 84 batches" in txt and "1000 posts were retrieved" in txt
        assert "1 of 1 batch " in run({"analysis_coverage": {"batches": 1, "failed_batches": 1, "posts": 5}}) + " "
    finally:
        pg.close()
