"""
tests/test_platform_disclosure_softnav.py - the "Not searched" note of a chat (chat.html, inside <main>) is shown by a direct
page load AND by index.html's sidebar soft-navigation (which swaps in the server-rendered <main> of that chat), always from the
opened conversation's own metadata: moving to a chat without the note removes it, and the home page never shows one.
Real-page rendering; no network.
"""
import pytest

from tests.test_answer_persistence import NOW, ORIGIN, _render

NOTE = "Searched: Reddit. Not searched: LinkedIn, Facebook (not supported yet)."


def _chat(cid, unsearched):
    return {"chat_id": cid, "title": cid, "messages": [{"query": f"Find buyers {cid}", "topic_key": cid, "results": [],
                                                       "requested_at": NOW, "targeting_platform": "reddit"}],
            "unsearched_platforms": unsearched}


def _page(browser, start, chats):
    """The home page (or a chat) with every chat served from `chats`; chat.html gets the metadata the route passes."""
    import jinja2
    from tests.test_answer_persistence import ROOT
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.ChainableUndefined)
    env.globals["url_for"] = lambda *a, **k: "#"
    pages = {"/": env.get_template("index.html").render(request=None, chat=None, chats=[], user=None, chat_id=None,
                                                        pending_stream_topic_key=None, messages=[])}
    for c in chats:
        pages[f"/chat/{c['chat_id']}"] = env.get_template("chat.html").render(
            request=None, chat=c, chats=[], user=None, chat_id=c["chat_id"], pending_stream_topic_key=None,
            messages=c["messages"], selected_platform="Reddit", unsearched_platforms=c["unsearched_platforms"])
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    def handle(route):
        url = route.request.url
        path = url[len(ORIGIN):].split("?")[0] if url.startswith(ORIGIN) else None
        if path in pages:
            return route.fulfill(status=200, headers={"Content-Type": "text/html"}, body=pages[path])
        return route.fulfill(status=200, body="")
    page.route("**/*", handle)
    page.goto(ORIGIN + start)
    return page, errors


NOTE_JS = "() => [...document.querySelectorAll('.platform-notice')].map(e => e.innerText.trim())"


def _go(page, url, turns_for):
    page.evaluate(f"navigateTo('{url}'); 0")
    page.wait_for_function(f"document.querySelector('.turn-block') && document.body.innerText.includes('{turns_for}')")
    page.wait_for_timeout(200)


def test_direct_load_shows_the_note_of_that_conversation(integrity_browser):
    page, errors = _page(integrity_browser, "/chat/a", [_chat("a", ["LinkedIn", "Facebook"])])
    try:
        assert page.evaluate(NOTE_JS) == [NOTE] and errors == []
    finally:
        page.close()


def test_soft_navigation_shows_only_the_opened_conversations_note(integrity_browser):
    chats = [_chat("a", ["LinkedIn", "Facebook"]), _chat("b", []), _chat("c", ["Facebook"])]
    page, errors = _page(integrity_browser, "/", chats)
    try:
        assert page.evaluate(NOTE_JS) == []                                   # the home page has none
        _go(page, "/chat/a", "Find buyers a")
        assert page.evaluate(NOTE_JS) == [NOTE]
        _go(page, "/chat/b", "Find buyers b")
        assert page.evaluate(NOTE_JS) == []                                   # not carried over from the previous conversation
        _go(page, "/chat/c", "Find buyers c")
        assert page.evaluate(NOTE_JS) == ["Searched: Reddit. Not searched: Facebook (not supported yet)."]
        page.evaluate("navigateTo('/'); 0")
        page.wait_for_function("!document.querySelector('.platform-notice')")
        assert errors == []
    finally:
        page.close()
