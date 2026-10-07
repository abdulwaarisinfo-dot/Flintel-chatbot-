# AUDIT_REPORT: "7 buyers mile" ka sach kya hai?

**Status: ADHOORA. Run ka production data is sandbox mein nahi mila.**
Is session mein Mongo/Render ka koi access ya export nahi hai (MONGODB* env set nahi, koi `audit_evidence`/log file nahi). Credentials maangna aapke apne rule ke khilaf hai, is liye maine nahi maange. Is wajah se **run-specific har ginti "maloom nahi"** hai. Neeche sirf wo likha hai jo code se saabit hota hai. Run ka data aap `diagnostics/audit_run.py` se nikal kar de sakte hain.

Koi maujooda file nahi badli. Nayi files: `AUDIT_REPORT.md`, `audit_evidence.json`, `diagnostics/audit_run.py`.

## SAABIT HUA (data se)
Yahan "data" = repo ka code (file:line), run ka data nahi.

1. **"7"/"6" ginti LLM ka apna likha text hai.** Strict mode mein prompt ke aakhir mein `_strict_count_note(n)` lagta hai (`logics.py:3582`, use hota hai `:4013`, `:4216`). Wo model ko kehta hai "jitne results list karo unki ginti opening mein likho". Ginti ko listed/saved posts se milane wala koi code nahi hai. Non-strict mode mein prompt sirf "never more than 7 posts" (`MAX_CHAT_EVIDENCE_POSTS`=7) kehta hai, to "7" ek cap bhi ho sakta hai, ginti nahi.
2. **`STRICT_INTENT_MODE` ka default false hai** (`config.py:475`), aur `INTENT_BRIDGE_ENABLED` default false (`config.py:433`). Production mein ye on the ya nahi, maloom nahi. Agar off the, to na classifier chala, na strict patch.
3. **Dedupe sirf exact `post_url` string par hota hai**: narrow path `logics.py:2515-2558` (`seen_urls`), bridge path `_apply_cap_and_limit` (`:2385-2395`), strict patch (`key = sig.get("post_url") or id(sig)`, `:6686` ke aas-paas). Title par dedupe kahin nahi. Jis post ka `post_url` khali hai wo kabhi dedupe nahi hoti (`id(sig)` hamesha alag).
4. **Non-strict mode mein LLM ki listed posts par koi dedupe nahi.** `_patch_post_urls_into_answer` (`logics.py:6780`) index se URL chipkata hai; `seen`/dedupe wahan nahi hai. Agar model ek hi `[Post N]` do baar list kare, to dono entries ko wohi URL/title mil jayega. Ye "2 posts ka URL/title ek jaisa" ki ek code-supported wajah hai (sirf wajah-ye-ho-sakti-hai, run par saabit nahi).
5. **Strict mode mein ye duplicate nahi guzar sakta**: `_strict_patch_answer` duplicate sig ko drop karta hai (`dropped += 1`). To agar 7 mein 2 identical dikhe, to ya strict off tha, ya URL-string alag the (jaise trailing slash, `old.reddit`, `?utm`), jo exact-URL dedupe se bach jate hain.
6. **Job listing / aam discussion ko rokne wala koi rule-based check nahi.** Sirf embedding similarity (0.35 cosine), aur (agar on) LLM classifier. "Seller/job listing" ko hard-reject karne wala deterministic code mujhe nahi mila.
7. **Strict patch drops log/persist nahi hote.** `_strict_patch_answer` `state["dropped"]` rakhta hai magar log nahi karta, aur final `claude_answer` patch ke BAAD save hota hai. Raw LLM JSON aur LLM ko di gayi `[Post N]` list kahin save nahi hoti (stage 5, 6, 7 production data se dobara nahi banaye ja sakte).
8. **Follow-up sirf saved `claude_answer` mein listed posts dekhta hai** (`routes.py:198`), isliye "7 dikhao" par jo milta hai wo post-patch listing hai, retrieved pool nahi.

## ANDAZA (data nahi mila)
| Sawal | Status |
|---|---|
| 7 mein se kitni UNIQUE hain | maloom nahi |
| Kitni genuine buyer hain | maloom nahi |
| Duplicate kis run mein, kis wajah se (strict off / URL variant / title) | maloom nahi. Upar #4 aur #5 sirf imkaan hain |
| Job listing/discussion "buyer" kis step par bani (classifier ya LLM) | maloom nahi. Classifier output save nahi hota |
| Us run mein bridge/strict on tha ya off | maloom nahi |
| 6-wali run mein 1 card kyun | maloom nahi (A/B/C/D alag nahi ho sakte jab tak raw LLM JSON na ho) |

## 8 stages ka summary
| # | Stage | Data kahan | Is run ka nateeja |
|---|---|---|---|
| 1 | Router/keywords | message doc: `keywords`, `match_phrases`, `time_window_days`, `unfiltered` (`index.py:713-738`) | maloom nahi (script se milega) |
| 2 | Scan | sirf Render logs: `unlimited scoring \| pool=... passed_threshold=...`, `streamed-score: scanned=... kept=...`, `scan deadline hit` | maloom nahi, DB mein save nahi |
| 3 | Candidates/dedupe | topic evidence cache (`posts`, `evidence_count`) | maloom nahi (script cache dump karta hai) |
| 4 | Bridge/classifier | Render log `intent_bridge: reranked N → M posts` | per-post labels save nahi hote |
| 5 | LLM ko di gayi [Post N] list | kahin nahi | save nahi hota (bara gap) |
| 6 | Raw LLM JSON | kahin nahi | save nahi hota |
| 7 | Strict patch kept/dropped | kahin nahi | save nahi hota, log bhi nahi |
| 8 | Final saved | message doc: `claude_answer`, `results`, (`strict_dropped_posts` agar kabhi bane) | maloom nahi (script se milega) |

## Final posts ka saboot table
Khali hai. Run ka data na hone se kisi post ka verdict likhna andaza hota. `audit_evidence.json` mein har listed post ke liye `verdict: "UNREVIEWED"` ka slot `audit_run.py` khud banata hai, `post_text` seedha signal DB se aata hai (LLM summary se nahi).

## 5 sawal
1. Unique posts / genuine: **maloom nahi.**
2. "7"/"6" kahan se: **code se ye LLM ka apna likha text hai** (#1 upar). Is run mein wo cap tha, pass-count tha ya list-count, maloom nahi.
3. Duplicate kahan se: **maloom nahi.** Code mein URL-string dedupe hai, title dedupe nahi, non-strict listing par dedupe nahi (#3, #4).
4. Job listing / discussion kahan "buyer" bani: **maloom nahi.** Code mein koi deterministic reject rule nahi (#6). Classifier output save na hone se classifier aur LLM ko alag nahi kiya ja sakta.
5. Future mein kya save ho (MISSING DATA list neeche).

## MISSING DATA (production mein save hona chahiye)
1. LLM ko di gayi final `[Post N]` list (index, post_url, title, text hash) per message.
2. Raw LLM JSON (patch se pehle) per message.
3. `_strict_patch_answer` ke kept/dropped counts aur har drop ki wajah, message doc par.
4. Claimed count (jo model ne likha) aur listed count, aik saath save, taake mismatch automatic pakda jaye.
5. Bridge/classifier ke per-post fields (intent, actor, specificity, confidence, pass/fail).
6. Scan counts (`pool`, `passed_threshold`, `scanned`, `kept`, deadline hit) message doc par, sirf Render log par nahi.
7. Flags snapshot per run: `STRICT_INTENT_MODE`, `INTENT_BRIDGE_ENABLED`, code commit SHA.
8. Canonical URL normalised dedupe key (taake URL variants alag na ginay jayein).

## Aap ko ab kya karna hai (credentials mujhe nahi, aap khud)
```
python diagnostics/audit_run.py --list --query-prefix "Find 25 Reddit posts from business owners"
python diagnostics/audit_run.py --chat-id <id> --topic-key <key> --out audit_evidence.json
```
Script read-only hai (sirf find/find_one), `database.py` import nahi karta, `owner_key` redact karta hai. Output file mujhe bhej dein to stage 1, 3, 8 aur saboot table bhar dunga. Stage 2, 4 ke liye Render logs ke wo lines chahiye jo upar table mein likhi hain. Stage 5-7 ke liye data hai hi nahi.
