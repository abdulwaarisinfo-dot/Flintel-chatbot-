"""
FLINTEL INTENT PROTOTYPE — standalone, read-only, zero production coupling.

Built to answer one question with measurements: does an explicit intent
layer between retrieval and ranking move the numbers embedding_diagnostic.py
measured on real Flintel data (same-topic intent AUC 0.542, topic-centered
0.523, P@10 on buyer_demand 0.10)?

Nothing here imports flintel.py, logics.py, config.py or database.py.
Nothing here writes to MongoDB. `validate.py selftest` proves both.

    python intent_prototype/validate.py plan
    python intent_prototype/validate.py selftest
    python intent_prototype/make_tier1_sample.py select
    python intent_prototype/validate.py classify --yes
    python intent_prototype/validate.py run
"""

__version__ = "0.1.0-prototype"
