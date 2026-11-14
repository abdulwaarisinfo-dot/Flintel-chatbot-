# Indirect and symptom-style demand: what the engine misses (analysis only, no behaviour change)

Posts such as "my invoices are always late and I dread month end" express a need without naming the product or asking for a recommendation. The synthetic set has 8 such items and the engine
misses them: recall on that slice is 0/8 although overall synthetic recall is 0.94. That slice is the weakest part and the synthetic number hides it.

Why they miss: qualification requires a need frame (request, recommend, price, compare, replace, problem, wish) AND alignment with the profile's own words (identifiers, offer, problem, audience, place).
A symptom post has the problem but not the user's offer vocabulary, so alignment fails; this is the intended precision trade-off.

Options (none applied; thresholds and rules were deliberately not changed to raise a synthetic score):
1. Let the user add "problem phrases" in their own words (deterministic, auditable, costs the user effort).
2. User-editable synonyms for their offer.
3. Embedding similarity between profile and post. Needs real measurement on the 100-conversation set first, and it adds paid calls.
4. Surface weak symptom matches as "Needs review" only, never "High potential". Cheapest, but must be measured for noise on real data.
Decision should follow the real-data evaluation (`evaluation/real_eval/README.md`), not the synthetic set.
