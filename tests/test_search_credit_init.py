"""
tests/test_search_credit_init.py — with CREDIT_ENFORCEMENT_DISABLED=true,
importing database.py must not create the credit collection or its index
(the only credit-related server operation at import). Each case imports the
real database.py in a fresh Python process with pymongo.MongoClient replaced
by mongomock and dummy connection strings — no real Mongo, no network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mongomock")
ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r"""
import json, sys
import mongomock, pymongo
pymongo.MongoClient = mongomock.MongoClient            # database.py does `from pymongo import MongoClient`
import database
names = database.db3.list_collection_names()
info = database.db3["flintel_credit_usage"].index_information() if "flintel_credit_usage" in names else {}
print(json.dumps({"collections": sorted(names), "credit_indexes": {k: bool(v.get("unique")) for k, v in info.items()}}))
"""


def _import_database(kill_switch):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MONGODB", "CREDIT_"))}
    env.update({"SESSION_SECRET_KEY": "test-only-session-key-not-for-deployment",
                "MONGODB_URI": "mongodb://primary.invalid:27017", "MONGODB3": "mongodb://third.invalid:27017",
                "MONGODB2": "", "MONGODB4": "", "PYTHONPATH": str(ROOT)})
    if kill_switch is not None:
        env["CREDIT_ENFORCEMENT_DISABLED"] = kill_switch
    out = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_kill_switch_import_creates_no_credit_collection_or_index():
    state = _import_database("true")
    assert "flintel_credit_usage" not in state["collections"] and state["credit_indexes"] == {}


@pytest.mark.parametrize("value", [None, "false", "1"])
def test_enforcement_on_creates_the_unique_owner_index(value):
    state = _import_database(value)
    assert "flintel_credit_usage" in state["collections"]
    assert state["credit_indexes"].get("owner_key_1") is True
