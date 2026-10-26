"""Editorial content for the public /discover page.

Everything here is written by hand and describes only what Flintel actually does today:
  * it searches Reddit (no other platform is searched yet);
  * it labels the posts it finds by buying intent and sentiment and links every result to its source post;
  * a search can be limited to a time range, and only covers posts Flintel has already stored;
  * it does not guarantee leads, and it is not a complete view of Reddit.

There is NO conversation data, trend score, ranking, count or "live" indicator in this module, and none must be added
here: a card that launches a search carries only the text of the search to run (`query`), which opens the home page
with the search box pre-filled (nothing is submitted). Real conversation evidence appears only in a user's own search
results, each linked to its Reddit post. Tests (tests/test_discover_menu.py) enforce these rules.
"""
from urllib.parse import quote

CATEGORIES = (
    {"id": "opportunity-signals", "label": "Opportunity signals",
     "blurb": "Posts where someone asks for a tool, a recommendation or a replacement."},
    {"id": "competitor-complaints", "label": "Competitor complaints",
     "blurb": "What people say is wrong with a product, and why they are leaving it."},
    {"id": "pain-points", "label": "Pain points and unmet needs",
     "blurb": "Problems people describe in their own words."},
    {"id": "emerging-discussions", "label": "Emerging discussions",
     "blurb": "How to look at newer posts, and what a search can and cannot show."},
)

# kind "Guide" = editorial text about how to read results; kind "Search idea" = an example search to run.
_CARDS = {
    "lead": {
        "category": "opportunity-signals", "kind": "Guide", "art": "signal",
        "title": "How to find people asking for a tool on Reddit",
        "summary": ("A post that asks for a recommendation, an alternative or a replacement is the clearest buying "
                    "signal in a Reddit search. Flintel labels each post it finds by buying intent and links it to "
                    "the original, so you can read the thread before you decide whether to reach out."),
        "href": "/find-buyers-on-reddit", "href_label": "Read the guide",
        "query": "Find people asking for a tool recommendation",
    },
    "switching": {
        "category": "opportunity-signals", "kind": "Search idea", "art": "switch",
        "title": "Spot people who are about to switch",
        "summary": "Look for posts that say someone is leaving a product, comparing options or asking for alternatives.",
        "query": "Find people looking for alternatives",
    },
    "buyers-vs-talk": {
        "category": "opportunity-signals", "kind": "Guide", "art": "labels",
        "title": "Tell a buyer from a bystander",
        "summary": ("A qualified lead asks for something to buy or try. Opinions, testimonials and competitor "
                    "promotion are labelled lower, and Flintel shows which is which."),
        "href": "/find-buyers-on-reddit", "href_label": "How the labels work",
    },
    "source": {
        "category": "opportunity-signals", "kind": "Guide", "art": "source",
        "title": "Read the source, not only the summary",
        "summary": ("Every result links back to its Reddit post with the subreddit and the stored post date, where "
                    "one is available, so you can check it yourself."),
        "href": "/find-buyers-on-reddit", "href_label": "See an example workflow",
    },
    "recommendations": {
        "category": "opportunity-signals", "kind": "Search idea", "art": "signal",
        "title": "Recommendation requests",
        "summary": "Posts that ask what other people use for a task show what someone is ready to try.",
        "query": "Find people asking what tool others use for a task",
    },
    "competitor": {
        "category": "competitor-complaints", "kind": "Search idea", "art": "complaint",
        "title": "What people complain about in a competitor",
        "summary": ("Search a competitor or a product category and read the recurring complaints about price, support, "
                    "reliability or missing features."),
        "query": "Find complaints about project management software",
    },
    "alternatives": {
        "category": "competitor-complaints", "kind": "Search idea", "art": "switch",
        "title": "Read the alternatives threads closely",
        "summary": ("People who ask for an alternative often name the feature or the price that made them leave. "
                    "Those details are the useful part of the thread."),
        "query": "Find people looking for alternatives to email marketing tools",
    },
    "pricing": {
        "category": "competitor-complaints", "kind": "Search idea", "art": "price",
        "title": "Pricing frustration",
        "summary": "Search for posts about cost, plan limits and surprise charges for a type of software.",
        "query": "Find posts complaining about software pricing",
    },
    "pain-points": {
        "category": "pain-points", "kind": "Search idea", "art": "pain",
        "title": "Find customer pain points",
        "summary": "Describe a market in plain words and read how people describe their own problems in it.",
        "query": "Find customer pain points",
    },
    "tool-exists": {
        "category": "pain-points", "kind": "Search idea", "art": "question",
        "title": "“Is there a tool for…” posts",
        "summary": "Unmet needs often show up as a question: someone asks whether a tool exists for what they are doing.",
        "query": "Find people asking whether a tool exists for their problem",
    },
    "validate": {
        "category": "pain-points", "kind": "Guide", "art": "check",
        "title": "Check an idea against what people actually write",
        "summary": ("Before you build, search the problem in everyday words and read the posts. Flintel shows what was "
                    "posted and stored; it does not say how big a market is."),
        "query": "Find customer pain points in small business accounting",
        "href": "/find-buyers-on-reddit", "href_label": "See an example workflow",
    },
    "time-range": {
        "category": "emerging-discussions", "kind": "Search idea", "art": "time",
        "title": "Narrow a search to recent posts",
        "summary": ("Add a time range such as “today” or “the last week”. Results come from posts "
                    "Flintel has already stored, so the very newest posts may not be there yet."),
        "query": "Find posts about AI note-taking tools from the last week",
    },
    "compare-windows": {
        "category": "emerging-discussions", "kind": "Guide", "art": "compare",
        "title": "Compare two time windows yourself",
        "summary": ("Run the same search for the last week and for the last month, then read what changed. This is a "
                    "manual comparison, not an automated trend."),
        "query": "Find posts about remote work tools from the last month",
    },
    "limits": {
        "category": "emerging-discussions", "kind": "Guide", "art": "limits",
        "title": "What a search cannot tell you",
        "summary": ("Flintel searches Reddit only, from posts it has stored. It is not a complete view of Reddit and it "
                    "does not guarantee leads."),
        "href": "/find-buyers-on-reddit", "href_label": "Read the limits",
    },
}

# The feed, in order. "lead" = one featured story; "trio" = three compact cards in a row; "wide" = a text-and-image row.
FEED = (
    ("lead", ("lead",)),
    ("trio", ("switching", "buyers-vs-talk", "source")),
    ("wide", ("competitor",)),
    ("wide", ("alternatives",)),
    ("trio", ("pain-points", "tool-exists", "pricing")),
    ("wide", ("validate",)),
    ("wide", ("time-range",)),
    ("trio", ("compare-windows", "limits", "recommendations")),
)

# Sidebar: example searches (each opens the home page with the box pre-filled).
RELATED_SEARCHES = (
    "Find people looking for alternatives",
    "Find customer pain points",
    "Find opportunities your competitors miss",
    "Find complaints about project management software",
    "Find posts about AI note-taking tools from the last week",
)

GUIDES = (
    {"label": "Find buyers and leads on Reddit", "href": "/find-buyers-on-reddit"},
    {"label": "Pricing", "href": "/billing/upgrade"},
)

SOURCES_NOTE = "Flintel searches Reddit. Other platforms are not available yet."


def search_href(query: str) -> str:
    """The home page with the search box pre-filled. It never starts a search by itself."""
    return "/?q=" + quote(query, safe="")


def _card(slug: str, extra=None) -> dict:
    card = dict(_CARDS[slug], slug=slug)
    if card.get("query"):
        card["search_href"] = search_href(card["query"])
    if extra:
        card.update(extra)
    return card


def build_feed() -> list:
    """[(layout, [card, ...])]. The first card of each category gets `anchor` (an element id for in-page links)."""
    seen, out = set(), []
    for layout, slugs in FEED:
        cards = []
        for slug in slugs:
            extra = None
            category = _CARDS[slug]["category"]
            if category not in seen:
                seen.add(category)
                extra = {"anchor": category}
            cards.append(_card(slug, extra))
        out.append((layout, cards))
    return out


def build_related_searches() -> list:
    return [{"query": q, "href": search_href(q)} for q in RELATED_SEARCHES]
