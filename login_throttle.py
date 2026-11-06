"""
login_throttle.py - bounded failed-login allowance (no new infrastructure).

Counts FAILED e-mail/password logins in a fixed window, per (client address + account) and per client address, in the same
TTL-expiring Mongo collection the sign-up code limiter already uses (documents expire by `expires_at`). Keys are keyed hashes, so no
e-mail address or IP address is stored. A success clears the pair counter. If the shared store errors, a bounded per-process counter takes over (so brute force is still limited per worker, never unlimited) and a warning is logged;
it never raises into the login route.

Why per (address + account) and not per account: an attacker elsewhere cannot lock a victim out by guessing their address. The per-address
allowance bounds one client trying many accounts. Limits are per window and enforced across workers by the store (atomic $inc).
Pure module: the collection and clock are injected.
"""
import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("flintel.login_throttle")


def _h(*parts):
    return hashlib.sha256("\x1f".join(str(p or "") for p in parts).encode()).hexdigest()[:32]


class LoginThrottle:
    def __init__(self, collection, window_seconds=900, max_pair=8, max_ip=40, clock=None):
        self.c = collection
        self.window = max(60, int(window_seconds))
        self.max_pair = max(1, int(max_pair))
        self.max_ip = max(1, int(max_ip))
        self.now = clock or (lambda: datetime.now(timezone.utc))
        self._mem = {}                       # per-process fallback counters, used ONLY while the shared store is failing
        self._lock = threading.Lock()

    @staticmethod
    def _aware(dt):
        return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _keys(self, ip, email):
        return (("login:pair:" + _h(ip, email), self.max_pair), ("login:ip:" + _h(ip), self.max_ip))

    def retry_after(self, ip, email):
        """Seconds until the next attempt is allowed (0 = allowed). Does not count anything."""
        try:
            now = self.now()
            worst = 0
            for key, limit in self._keys(ip, email):
                doc = self.c.find_one({"_id": key})
                start = self._aware(doc.get("window_start")) if doc else None
                if start and int(doc.get("fails", 0)) >= limit:
                    left = int(self.window - (now - start).total_seconds())
                    worst = max(worst, left)
            return max(0, worst)
        except Exception as exc:
            log.warning(f"login throttle store unavailable, using this process's own counters: {exc}")
            return self._mem_retry_after(ip, email)

    def record_failure(self, ip, email):
        try:
            now = self.now()
            cutoff = now - timedelta(seconds=self.window)
            for key, _ in self._keys(ip, email):
                hit = self.c.update_one({"_id": key, "window_start": {"$gt": cutoff}}, {"$inc": {"fails": 1}})
                if not hit.matched_count:
                    self.c.update_one({"_id": key},
                                      {"$set": {"window_start": now, "fails": 1,
                                                "expires_at": now + timedelta(seconds=self.window * 2)}}, upsert=True)
        except Exception as exc:
            log.warning(f"login throttle store unavailable, counting in this process only: {exc}")
            self._mem_fail(ip, email)

    # ── in-process fallback (bounded per worker; NOT shared across workers, so the effective limit is up to workers x limit while the store is down) ──
    def _mem_prune(self, now):
        for k in [k for k, (start, _) in self._mem.items() if (now - start).total_seconds() >= self.window]:
            del self._mem[k]

    def _mem_fail(self, ip, email):
        now = self.now()
        with self._lock:
            self._mem_prune(now)
            for key, _ in self._keys(ip, email):
                start, n = self._mem.get(key, (now, 0))
                self._mem[key] = (start, n + 1)

    def _mem_retry_after(self, ip, email):
        now = self.now()
        with self._lock:
            self._mem_prune(now)
            worst = 0
            for key, limit in self._keys(ip, email):
                start, n = self._mem.get(key, (None, 0))
                if start and n >= limit:
                    worst = max(worst, int(self.window - (now - start).total_seconds()))
            return max(0, worst)

    def clear(self, ip, email):
        with self._lock:
            self._mem.pop(self._keys(ip, email)[0][0], None)
        try:
            self.c.delete_one({"_id": self._keys(ip, email)[0][0]})
        except Exception as exc:
            log.warning(f"login throttle clear failed: {exc}")
