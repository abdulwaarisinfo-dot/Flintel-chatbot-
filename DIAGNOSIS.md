# FLINTEL — Marhala 1: DIAGNOSIS (koi production code change nahi)

Scope: sirf padhna, read-only offline scripts chalana, aur yeh document. `git add/commit/push` kuch nahi hua. Marhala 2 aap ke approval ke baad.

---

## 0. Pehle yeh parh lein: kya verify HUA, kya NAHI

| Cheez | Status |
|---|---|
| Code tracing (`file:line`) | Verified, current code se spot-check kiya |
| Bridge ranker ka actor-filter bug | **Proven offline** (`diagnostics/bridge_rank_probe.py`) |
| Bridge padding (8 genuine → 25 return) aur timeout fallback | **Proven offline** (`diagnostics/bridge_padding_probe.py`) |
| Per-platform cap (limit 25 → max 12 Reddit) | **Proven offline** (scratch simulation, real `get_matched_signals` helpers patched) |
| Bridge ki asal accuracy (buyer_demand precision / provider_supply recall) | **NAHI naapa ja saka.** Repo mein koi bridge result file nahi. README ke AUC numbers *purani embedding baseline* ke hain, bridge ke nahi. `validate.py` ko real LLM + Mongo chahiye, mere paas credentials nahi (aur maine maange bhi nahi) |
| Live interpreter "AI agent dhoondne wale" ke liye actor filters emit karta hai? | **NAHI test hua.** Interpreter prompt ka apna worked example (`query_interpreter.py:107-117`) is query se qareeb hai, is liye likely hai, lekin yeh andaza hai, saboot nahi |
| Bridge ka real latency vs 25s timeout | NAHI naapa |
| Scan time 75–130s | Sirf aap ke logs se, maine reproduce nahi kiya |
| Render par `RESPONSE_TIMEOUT` | Aap ke mutabiq 360s; code default 180 (`config.py:241`). Maine env nahi dekha |
| Signal doc ka real schema (`_id` ObjectId? ingestion timestamp? late embedding?) | **NAHI pata.** Repo mein ingestion-timestamp field ka koi zikr nahi. `diagnostics/watermark_probe.py` aap ki machine par chalana hai |
| Real seller→buyer examples | Repo data mein **koi real example nahi**. Sirf synthetic jumlay `embedding_diagnostic.py:1015-1017`. Neeche ke 5 examples **constructed traces** hain (real code par chalaye, real posts nahi) — unko real data na samjhein |

Seedhi baat: "bridge par bharosa" ka sawal accuracy number ke bina poori tarah band nahi hota. Neeche verdict structural saboot par hai, measured precision par nahi.

---

## 1. Flow ka nakshा (a–h)

### a. `get_matched_signals` (`logics.py:978`)
- 3 clusters (primary `flintel_signals`, MONGODB2, MONGODB4) parallel `ThreadPoolExecutor(3)` se fetch.
- Cosine similarity, query items par max, threshold `SIGNAL_EMBEDDING_SIMILARITY_THRESHOLD=0.35` (`config.py:350`). **Matcher mein koi intent filter nahi** — "kisi ne AI agent ka zikr kiya" aur "kisi ko AI agent chahiye" dono ek hi score paate hain.
- x4 pool: bridge ON ho to candidates ka wide pool bheja jata hai (`logics.py:1709-1805`); bridge ke baad `_apply_cap_and_limit(bridge_ordered, effective_max_per_platform, limit)` (`logics.py:1805`).
- `INTENT_BRIDGE_ENABLED` default **off** (`config.py:413-439`). Off ho to poora intent layer bypass — sirf cosine.
- Cap: `effective_max_per_platform = max(MAX_POSTS_PER_PLATFORM, limit // 2)` (`logics.py:1682`). Limit 25 → Reddit max 12; limit 50 → 25. Yani "25 Reddit posts" maangne par bhi 12 se zyada mumkin nahi (proven).

### b. `get_evidence_with_topup` (`logics.py:1929`) + `stream_answer` polling
- Routes polling loop: `routes.py:1905-1971`. `if len(matched) < effective_evidence_limit` → wait; `elapsed >= RESPONSE_TIMEOUT` (`routes.py:1946`) par ek akhri `get_evidence_with_topup` aur fallback. Exit `len(matched) >= effective_evidence_limit` (`routes.py:1970`).
- Non-streaming: `index.py:1556-1557` wahi target-or-timeout.
- Few posts: target tak na pahunche to poora timeout (180/360s) wait, phir jo mila woh (ya Google stub fallback `flintel.py:581/629/667-675`).
- **Har poll full rescan** — `get_matched_signals` poori window dobara scan karta hai (aap ke logs: 75–130s). Topic evidence cache (`save_topic_evidence_cache`, `logics.py:1894`) matched posts store karta hai, **koi watermark / scan-state nahi**, is liye top-up bhi poora rescan hai.

### c. `intent_bridge.py`
- `_run_bridge` (`:153`) → interpret → cache lookup → classify (batches of 17, parallel) → `_intent_matches` → `_rank_passing` (`:248`) → **`_fill_to_n` (`:240`, def `:479`)**.
- `_fill_to_n`: agar `len(ranked) < n`, pehle `non_passing` (jo intent filter mein **fail** hue) append karta hai, phir `original`. Yani fail-hue posts jaan-boojh kar wapas daale jate hain.
- Timeout (`rerank_with_intent`, `:103-139`): `FuturesTimeout`, executor error, `_FAIL` — teeno `return candidates` (poore unfiltered candidates). **Haan, unfiltered posts seedha LLM tak jate hain.** Proven: classifier 3s vs timeout 1s → 100 of 100 unfiltered wapas.
- Short-circuit: interpreter fail (`:~145`, `return None`) ya bridge fail → originals.

### d. `intent_prototype/`
- `schemas.py`: intents (buyer_demand, provider_supply, hiring, general_discussion, irrelevant…), `actor_direction` classification se *derive* hota hai (intent + actor_type + actor_role).
- `doc_classifier.py`: LLM per post, 17/batch, cache `classification_cache.py` keyed by `post_url`.
- `query_interpreter.py`: "AI agent dhoondne wale" jaisi query par expected plan: `intent_include=[buyer_demand]`, `intent_exclude=[provider_supply, hiring, irrelevant]`, aur (prompt ke apne example ke mutabiq) **`actor_direction_filter=["company_buying"]`, `actor_type_filter=["company"]`, `min_commercial_signal≈0.4`**. (Untested live.)
- `ranker.py` hard gates: actor_direction / actor_type / min_commercial_signal / min_specificity.
- **BUG (proven):** `_rank_passing` classifier ke asli actor/specificity fields ko `None/0.0` se overwrite karke ranker ko deta hai (`normalize_classification({"actor_type": None, "actor_role": None, "specificity": 0.0, ...})`) → `actor_direction=None` → agar interpreter ne actor filter diya to ranker **sab drop** karta hai (probe: `kept 0 of 5, filtered_reasons {actor_direction: 5}`), phir bridge drop hue posts ko **original order mein wapas** append kar deta hai. Net: ranking effect zero. Cached hits mein `commercial_signal` bhi 0.0 forced (probe `cached_buyer`). `weights.json` `calibrated: False`.

### e. `CLAUDE_ANALYSIS_SYSTEM_PROMPT` (`logics.py`)
- Step 7 (`:2129`), Step 8 (`:2143`).
- **`PREFER RELATED SIGNALS OVER "no_results"` (`:2511-2525`)**: absolute rule jo seller / indirect posts ko "related signal" ke tor par dikhane ki ijazat deti hai → prompt khud buyer-vs-seller boundary dheeli karta hai. Strict mode mein yeh conflict karega.
- `POST-COUNT LIMIT` (`:2560-2567`): answer text mein max 7 posts (`MAX_CHAT_EVIDENCE_POSTS`, `config.py:195`). Post *cards* poore `merged_pool` se aate hain, text 7 tak. "25 do" aur "7 limit" ek doosre se takrate hain.

### f. Summaries kaise bante hain / koi verify karta hai?
- Summary LLM likhta hai (`analyze_with_claude`, `logics.py:2961`) context se (`build_claude_post_context :2650`, `_format_posts_block :2700`). **Koi step summary ko post text se verify nahi karta.**
- `_finalize_answer_and_results` (`:5372-5400`), `_best_matching_post` (`:5407`), `_resolve_answer_post` (`:5498`), `_patch_post_urls_into_answer` (`:5524`) title/URL patch karte hain.
- Kamzoriyan (meri apni enrichment code mein bhi): index guard kamzor (koi ek shared word kaafi), title match substring + first-hit-wins. Do milte-julte titles mein galat post ka link/summary lag sakta hai. LLM title thoda badal de to patch miss ya galat hit.

### g. Padding sites (poori list)
1. `intent_bridge._fill_to_n` — non-passing posts (`:479`).
2. `_fill_to_n` second loop — `original` candidates.
3. `rerank_with_intent` timeout/exception/fail → poore `candidates` (`:103-139`).
4. `_rank_passing` ki ranker-drop undo (original order mein wapsi).
5. `flintel.py:667-675` Google stub fill + `merge_matched_and_google_results :629`.
6. `_timeout_fallback_answer` (`logics.py:4961`) — jo ho wahi dikha deta hai.
7. `routes.py:1946` timeout par jo mila use hi final maan lena.
8. Prompt rule `PREFER RELATED SIGNALS` (`:2511`) — LLM-level padding.
9. `get_evidence_with_topup` min top-up (`TOPIC_CACHE_MIN_TOPUP`, `config.py:215`) — cache se purani posts mila kar count poora karna.

### h. Rescan problem
- Har poll poori window rescan: **haan** (get_matched_signals koi since-parameter nahi leta).
- Interpreter LLM call aur bridge classify har poll dobara chalte hain; interpreter result cache nahi hota (sirf per-post classification cached hai) → har poll par ek extra LLM call + sab uncached posts dobara classify.
- Cache document mein kya: matched posts + topic metadata. Koi `last_scanned_at` / watermark nahi.
- "Naya doc aaya" ke liye DB field: **repo mein koi ingestion timestamp referenced nahi.**
  - `created_utc` = post ka *apna* waqt, DB insert ka nahi. Late-ingested purani post chhoot jaegi. Index sirf primary par (`database.py:142-149`).
  - ObjectId `_id` free index hai **agar** `_id` ObjectId ho (projection `_id` exclude karti hai, unverified).
  - Agar embedding baad mein attach hoti hai (external "Background Service #1"), to dono fields miss karenge.
  - **Sahi field watermark_probe.py ke output se tay hoga.** Tab tak koi field "sahi" declare karna andaza hoga.

---

## 2. Masla 1 — Buyer-intent accuracy

| Masla kahan hota hai (file:line) | Asal wajah | Saboot | Proposed fix | Risk | Test kaise |
|---|---|---|---|---|---|
| `logics.py:978` matcher; `config.py:350` | Matcher sirf topic-similarity naapta hai, intent nahi. Bridge default OFF (`config.py:413`) | Code mein koi intent filter nahi | Strict mode mein bridge ON + gate (neeche) | Latency +; bridge-fail ka plan chahiye | Unit: bridge OFF vs ON par same candidates, ordering/filter assert |
| `intent_bridge.py:248` `_rank_passing` | Classifier ke real actor/specificity fields zero/None se overwrite | `bridge_rank_probe.py`: kept 0/5, order unchanged | Real classification fields ranker ko do; actor gate ya to real data par ya hata do | Ranker behaviour badlega (naya ordering) | Probe dobara chalao: order change + sahi drop |
| `intent_bridge.py:479` `_fill_to_n` | Fail-hue posts wapas | `bridge_padding_probe.py`: 8 genuine → 25 return, 17 padding | Strict mode: `_fill_to_n` skip | Kam results (yehi maqsad) | Test: 8 pass → exactly 8 |
| `intent_bridge.py:103-139` timeout | Failure par unfiltered candidates | Probe: 100/100 unfiltered | Strict mode: timeout par `[]`/cached-classified only, kabhi raw nahi | Timeout par khali jawab — "no_results" honest hai | Test: slow classifier → result sirf already-classified-passing |
| `ranker.py` weights | `calibrated: False` | `weights.json` | Calibrate sirf labelled data ke baad (Marhala 2 mein nahi) | — | `validate.py` user ki machine par |

## 3. Masla 2 — Buyer vs Seller

| Masla kahan hota hai (file:line) | Asal wajah | Saboot | Proposed fix | Risk | Test kaise |
|---|---|---|---|---|---|
| `logics.py:2511-2525` PREFER RELATED | Prompt seller/indirect ko dikhane ki ijazat deta hai | Prompt text | `STRICT_INTENT_MODE=True` par is rule ko conditional karna (prompt text sirf flag ke peeche, flag off = byte-identical) | Prompt change — aap ne pehle mana kiya tha; **explicit approval chahiye** | Snapshot test: flag off par prompt unchanged |
| `intent_bridge.py:479` fill | Seller (`provider_supply`) padding se wapas aata hai | Probe: non-passing wapas | Strict: no fill | Kam count | Test: provider_supply post kabhi result mein nahi |
| `intent_bridge.py:248` actor gates | Actor direction (company_buying vs selling) effective nahi | Probe (0/5) | Real actor fields propagate | Wrong actor classification se false drops | Labelled set par precision/recall (aap ki machine) |
| `doc_classifier.py` | LLM classifier, koi hybrid rule nahi ("we offer / DM me / our agency" jaise seller markers) | Code padha, kisi deterministic seller guard ka saboot nahi | Chhota deterministic seller-marker veto buyer_demand par | False veto ("we need an agency" buyer hai) | Constructed examples neeche |

### Constructed traces (real code par, **real data nahi**)
1. Post "We build AI agents for SMBs, DM me" → classifier `provider_supply`; `_fill_to_n` ise phir bhi shamil kar deta hai jab passing < n. (padding probe: non-passing items result mein)
2. "Hiring an AI agent developer" → `hiring`, exclude list mein, lekin fill se wapas.
3. Timeout par: koi bhi post (seller, irrelevant) seedha LLM context mein, prompt `PREFER RELATED` use "related signal" bana sakta hai.
4. "Anyone tried AI agents? Thoughts?" (mention-only, `general_discussion`) — cosine ≥0.35 pass karta hai, matcher mein koi intent gate nahi.
5. Actor filter ke saath real buyer "I run a clinic, need AI agent for bookings" → ranker `actor_direction None` se drop, phir original order mein wapas: buyer rehta hai lekin ranking ka faida nahi, aur seller bhi usi jagah rehta hai.

Yeh sab *mechanism* ke traces hain. Real precision number aap ko neeche ke command se nikalna hoga.

## 4. Masla 3 — Result/evidence quality

| Masla kahan hota hai (file:line) | Asal wajah | Saboot | Proposed fix | Risk | Test kaise |
|---|---|---|---|---|---|
| `logics.py:2961` summary generation | LLM free-text, koi verification nahi | Code mein verifier nahi | Strict: summary ko post text se grounded karo — ya to post ka original snippet dikhao, ya LLM summary par "key terms present in post" check | Extra check, kuch summaries hat sakti hain | Test: summary mein aisi term jo post mein nahi → flag |
| `logics.py:5407` `_best_matching_post` | Substring title, first-hit-wins | Code | Exact-normalised match + URL-keyed lookup; ambiguity par patch na karo | Kam links patch honge | Test: do milte-julte titles |
| `logics.py:5498-5524` resolve/patch | Weak index guard (1 shared word) | Code (meri enrichment bhi) | Guard ko threshold-based (Jaccard ≥ x) banao | Threshold tuning | Test: unrelated title same word |
| `flintel.py:667-675`, `:629` | Google stub fill se non-signal posts | Code | Strict: stub fill band | Kam count | Test: stub never in strict output |
| `logics.py:2560` POST-COUNT 7 | Answer text 7, cards zyada → mismatch | Config 195 | Strict: text aur cards ek hi list se | Prompt/ UI sync | Test: count equal |
| Per-post metadata (URL/title/subreddit/summary) | Aligned object ek hi post se banne ki guarantee nahi (LLM text + separate patch) | Patch step ka wujood | Card ko DB doc se banao (URL-keyed), LLM sirf summary dey, aur LLM ka URL ignore ho | Medium refactor | Test: har card ke 4 fields ek hi doc se |

## 5. Masla 4 — Overall: quantity nahi, accuracy

| Masla kahan hota hai (file:line) | Asal wajah | Saboot | Proposed fix | Risk | Test kaise |
|---|---|---|---|---|---|
| `routes.py:1905-1971`, `index.py:1556` | Target count tak wait (timeout tak) | Code | Wait/target options (section 7) | UX | Test: 8 qualify → 8 par exit |
| `logics.py:1682` cap | `limit//2` per platform | Sim: 25→12 | Strict + single-platform query par cap relax | Platform imbalance | Sim test dobara |
| Padding sites (g.1–g.9) | Count poora karne ki aadat | Probes | Strict mode mein sab band, "honest count" message | User ko kam results | Test: 8 → 8 |
| Rescan (h) | Har poll poori window | Logs + code | Incremental rescan (section 9) | Missed docs agar galat watermark | Test e/f/g |

---

## 6. Verdict: kya bridge par bharosa kiya ja sakta hai?

**Abhi nahi — current form mein nahi.** Wajohat:
1. Ranker ke hard gates actor fields ke liye effectively ineffective hain (proven).
2. `_fill_to_n` aur timeout fallback intent filter ko jaan-boojh kar undo karte hain (proven). Bridge ka "filter" sirf tab kaam karta hai jab kaafi posts pass hon *aur* timeout na ho.
3. Weights uncalibrated.
4. Koi measured precision/recall maujood nahi — is liye "kaam karta hai" ka koi saboot nahi, na hi "kaam nahi karta" ka accuracy-level saboot. Structural bugs ka saboot hai.
5. Classifier ka apna per-post accuracy (LLM) alag sawal hai jo yahan unmeasured hai; ho sakta hai woh theek ho aur sirf wiring kharab ho.

Is liye: bridge idea theek ho sakta hai, **wiring nahi**. Strict mode ke bina bridge ON karne se accuracy nahi barhegi.

## 7. Wait / target logic ("8 qualify → 8 do")

| Option | Kaise | Fayda | Nuqsaan |
|---|---|---|---|
| W1 | Do consecutive polls mein qualifying count same raha (stable) aur ≥ `MIN_ANALYSIS_EVIDENCE` → exit | Poora timeout nahi | Slow scan mein early exit |
| W2 | Scan complete flag: agar scan window poori ho chuki (sab 3 clusters done) to target na milne par bhi exit | Sab se sahi: "poora dhoond liya, itne hi hain" | Scan-complete signal add karna padega |
| W3 | Hard cap: `STRICT_WAIT_SECONDS` (e.g. 60–90s) | Predictable | Kam posts |

**Recommendation: W2 + W3 combo.** Scan complete = final, warna cap. Target (25) sirf *upper bound* hai, requirement nahi.

## 8. Embedding / scoring badalna chahiye?

**Nahi.** Saboot: ghalti ranking ya threshold mein nahi, post-retrieval stage (bridge wiring, padding, prompt) mein hai. Embedding sirf recall (candidate pool) deta hai aur uska pool unlimited hai. Threshold 0.35 recall-friendly hai; precision intent layer ka kaam hai. Embedding ko chherna Marhala 2 ke scope mein nahi aana chahiye jab tak labelled data dikhaye ke recall khud kam hai (abhi data nahi).

## 9. Rescan fix design

- **Watermark field:** *Probe ka intezar*. Order of preference: (1) ingestion timestamp field agar probe mile; (2) ObjectId `_id` agar probe confirm kare ke ObjectId hai **aur** late embedding 0% hai; (3) `created_utc` sirf fallback, kyunke late-ingested posts miss karta hai. Late-embedding > 0% ho to watermark ke saath "embedding present" criteria alag se zaruri.
- **Kahan save:** topic evidence cache document mein **ek chhota field** (`scan_watermark`, `scanned_at`) — naya collection nahi, naya index nahi. Write sirf jab delta mila ho ya scan complete ho (MONGODB3 full ho sakta hai, is liye minimal writes). Agar write fail ho to rescan full par fallback, exception nahi.
- **Index:** `created_utc` sirf primary par (`database.py:142-149`); MONGODB2/4 par nahi. `_id` default indexed. Naya index banana production change hai, probe ke baad tay karen.
- **Empty delta:** naye docs 0 → matcher aur bridge **skip**, cache wali posts hi wapas; interpreter call bhi skip (plan cache mein rakh sakte hain ya reuse).
- **Safety:** flag `INCREMENTAL_RESCAN_ENABLED` off = purana behaviour, cache invalid/purana ho to full scan.

## 10. Do solution options

**Option Light (low risk, ~1–1.5 din):**
- `STRICT_INTENT_MODE`: `_fill_to_n` skip, timeout par raw candidates ki jagah `[]`/cached-only, `_rank_passing` mein real classification fields, Google stub fill band, per-platform cap relax.
- Prompt `PREFER RELATED` strict mein conditional (aap ki approval).
- Wait W3 (hard cap).
- Rescan abhi nahi.

**Option Strong (~3–4 din):** Light + incremental rescan (watermark), W2 scan-complete exit, card-from-DB metadata (URL-keyed), summary grounding check, deterministic seller-veto, interpreter plan cache.

**Recommendation: Light pehle, probe results ke saath Strong ka rescan hissa.** Wajah: Light mein 80% accuracy gain (padding+fill+fallback) bina watermark ke andaze ke; rescan ko galat watermark par banane ka risk (silent missed docs) zyada hai.

## 11. Marhala 2: file-wise change list (andaza)

| File | Change | Estimate |
|---|---|---|
| `config.py` | `STRICT_INTENT_MODE`, `INCREMENTAL_RESCAN_ENABLED`, `STRICT_WAIT_SECONDS` flags (default off) | 0.5h |
| `intent_bridge.py` | strict: no fill, no raw fallback, real fields in `_rank_passing` | 3–4h |
| `intent_prototype/ranker.py` / adapter | actor/specificity propagate (agar zaruri) | 2h |
| `logics.py` | cap relax, strict gate, prompt conditional (approval), incremental scan param, `_best_matching_post` tighten | 6–8h |
| `routes.py`, `index.py` | wait/target logic, empty-delta short-circuit | 3–4h |
| `flintel.py` | stub fill strict mein band | 1h |
| `tests/` | tests a–l | 6–8h |

Total: Light ≈ 1–1.5 din, Strong ≈ 3–4 din.

## 12. Aap ki machine par chalane wale commands

```bash
# (1) Watermark / schema — READ-ONLY; apni env vars use karta hai, URIs print nahi karta
python diagnostics/watermark_probe.py --sample 800

# (2) Bridge accuracy — apne labelled set par (LLM + Mongo credentials aap ki machine par)
cd intent_prototype
python validate.py plan       # dekhein kya chalega
python validate.py classify   # LLM calls — cost
python validate.py run        # buyer_demand precision, provider_supply recall

# (3) Offline proofs (credentials nahi chahiye)
python diagnostics/bridge_rank_probe.py
python diagnostics/bridge_padding_probe.py
```

Mujhe in ke output bhej dein (credentials nahi, sirf output). `validate.py` ke exact subcommand/flags apne `intent_prototype/README.md` se confirm karein; maine woh LLM ke saath chalaya nahi.

## 13. Meri apni kamzoriyan (honesty)
- Pehle wali post-enrichment (A–F) ka index guard aur substring matching kamzor hain (section 4).
- `test_intent_bridge.py::test_logics_flag_on_bridge_gets_wide_pool` fail ho raha hai (part3 snapshot par bhi fail tha); iska sabab is diagnosis mein theek nahi kiya gaya.
- Selftest `validate.py`: 121/123, 2 failures `classification_cache.py` ke production-module import/Mongo write ki wajah se.

Marhala 2 tab hi shuru hoga jab aap diagnosis approve karen aur `watermark_probe.py` ka output dein.
