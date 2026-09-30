#!/usr/bin/env python3
"""
DOCUMENT CLASSIFIER — raw post -> DocClassification (spec 2b, 3, 4)
===========================================================================
Batch LLM classification of retrieved candidates. This is the layer the
diagnostic proved is structurally required: raw embedding similarity
separated intent at a topic-centered AUC of 0.523 on real Flintel data,
which is not reliable enough to rank on. The intent signal is present in
the TEXT; it is what survives the compression into a single vector that is
too weak to use. So it is read from the text, here.

WHAT THIS EMITS PER DOCUMENT
  intent + confidence            the gate
  secondary intent + confidence  genuine dual intent only, never a hedge
  actor_type / actor_role        orthogonal to intent, per spec 1
  commercial_signal              is the commercial activity REAL and active
  pain_intensity                 how severe the problem is
  urgency                        how time-pressured the decision is
  specificity                    how concrete the requirement/situation is
  ambiguous / noise              quality flags

The four continuous signals are deliberately NOT collapsed into one score
here — spec 4 keeps them distinct because they answer different questions
and a query can filter on any one of them. opportunity.py is what combines
them, and it needs the query-side topic similarity to do it.

State: none. Network: ceil(N / batch) Claude calls.
"""

import math

from . import schemas
from .llm import claude, parse_json_block

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 900

SYSTEM = f"""You classify social posts for Flintel, a B2B tool that finds real commercial
opportunities in online discussion. For each numbered post, output one JSON object.

INTENT — pick exactly one primary, from these {len(schemas.INTENTS)} labels ONLY:
{schemas.render_intent_definitions('  ')}

WHEN TWO LABELS BOTH SEEM RIGHT — ask what the author is DOING, not what they are
talking about:
{schemas.render_intent_tiebreaks('  ')}

SECONDARY INTENT — only when the post genuinely expresses a SECOND intent that stands
on its own. It is NOT a hedge for uncertainty. If you are merely unsure which single
label applies, set secondary_intent null and set ambiguous true instead.
  Genuine dual: "Shopify checkout has broken twice this month, now pricing BigCommerce"
                -> complaint_pain + solution_evaluation.

ACTOR — orthogonal to intent.
  actor_type: company | individual | analyst | unknown
  actor_role: buyer | seller | seeker | provider | observer | unknown
  A company job ad -> company + seeker. A person wanting a job -> individual + seeker.
  A vendor pitching -> company + seller. A reviewer ranking tools -> analyst + observer.

THE FOUR CONTINUOUS SIGNALS — 0.0 to 1.0. They measure DIFFERENT things. Score each
independently; do not let one drag the others.

  commercial_signal - how REAL and ACTIVE the commercial activity is, regardless of the
    intent label. Raise for: first person about their own situation, concrete stake
    (money, customers, deadline, team), decision language, named constraints. Lower for:
    third-person generalities, punditry, hypotheticals, "companies should...".
      0.15  "businesses really need AI agents these days" (opinion, no stake)
      0.55  "we're thinking about adding a chatbot at some point"
      0.92  "we need a WhatsApp vendor by Q4, budget approved, 10k msgs/day"

  pain_intensity - SEVERITY of the problem. 0.0 unless complaint_pain is primary or
    secondary. Raise for: money lost, production broken, duration, number of people hit.
      0.20  "the dashboard is a bit ugly"
      0.85  "checkout has been down 5 days, we're losing sales and support won't reply"

  urgency - TIME PRESSURE on the decision or action. Independent of commercial_signal:
    a vague request can still be urgent, and a serious buyer can have no deadline.
      0.0   no time framing at all
      0.90  "need this live before Black Friday"

  specificity - CONCRETENESS of the requirement or situation. Independent of severity.
      0.10  "looking for something for automation"
      0.90  "10k WhatsApp msgs/day, Salesforce sync, GDPR, under $500/mo"

FLAGS
  ambiguous - true when you cannot commit to the primary label with real confidence,
              or the post is too short/garbled to read. The post is still returned,
              just ranked lower. Prefer this over inventing a secondary intent.
  noise     - true ONLY for spam, referral/invite codes, pure link dumps, scraped
              boilerplate. This is a CONFIDENT judgement of junk, not uncertainty.

OUTPUT — a JSON array, one object per input post, no prose, no code fence:
[{{"i":1,"intent":"buyer_demand","intent_confidence":0.82,
  "secondary_intent":null,"secondary_confidence":null,
  "actor_type":"company","actor_role":"buyer",
  "commercial_signal":0.74,"pain_intensity":0.0,"urgency":0.45,"specificity":0.68,
  "geography":null,"industry_hint":"ecommerce","ambiguous":false,"noise":false}}]

Return one object for EVERY numbered post, with "i" matching the input number."""


def _render_batch(docs):
    lines = []
    for i, d in enumerate(docs, 1):
        title = (d.get("title") or "")[:MAX_TITLE_CHARS]
        body = (d.get("post_text") or "")[:MAX_BODY_CHARS].replace("\n", " ")
        lines.append(f"[{i}] TITLE: {title}\nTEXT: {body}")
    return "\n\n".join(lines)


def _unclassified(reason):
    """Safe placeholder when a batch fails. Flagged ambiguous, never noise:
    an API failure is not evidence that a post is junk."""
    return schemas.normalize_classification({
        "intent": "general_discussion",
        "intent_confidence": 0.0,
        "actor_type": "unknown",
        "actor_role": "unknown",
        "commercial_signal": 0.0,
        "pain_intensity": 0.0,
        "urgency": 0.0,
        "specificity": 0.0,
        "ambiguous": True,
        "noise": False,
        "_error": reason,
    })


def classify(docs, batch_size=None, model=None, progress=None):
    """Classify a list of normalized docs.

    Returns a list of DocClassification dicts, index-aligned with `docs`.
    A failed batch degrades to ambiguous placeholders for that batch only;
    the run continues.
    """
    batch_size = batch_size or schemas.CLASSIFIER_BATCH_SIZE
    out = [None] * len(docs)
    n_batches = max(1, math.ceil(len(docs) / batch_size))

    for b in range(n_batches):
        start = b * batch_size
        chunk = docs[start:start + batch_size]
        if not chunk:
            continue
        try:
            raw = claude(SYSTEM, _render_batch(chunk), max_tokens=4000, model=model)
            parsed = parse_json_block(raw, expect="array")
        except Exception as exc:                       # noqa: BLE001
            parsed = None
            err = type(exc).__name__
        else:
            err = "unparseable_reply"

        if not isinstance(parsed, list):
            for k in range(len(chunk)):
                out[start + k] = _unclassified(err)
        else:
            by_index = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("i", 0)) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(chunk):
                    by_index[idx] = item
            for k in range(len(chunk)):
                out[start + k] = (schemas.normalize_classification(by_index[k])
                                  if k in by_index else _unclassified("missing_from_reply"))

        if progress:
            progress(min(start + batch_size, len(docs)), len(docs))

    return [c if c is not None else _unclassified("never_assigned") for c in out]


def estimate_calls(n_docs, batch_size=None):
    """Number of Claude calls a classify() run would make. Used by the
    cost preview in validate.py --plan so nothing expensive starts blind."""
    batch_size = batch_size or schemas.CLASSIFIER_BATCH_SIZE
    return max(1, math.ceil(n_docs / batch_size))
