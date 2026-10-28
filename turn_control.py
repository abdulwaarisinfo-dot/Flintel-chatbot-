"""
turn_control.py — in-process "stop" signal for a running research turn.

A research turn is worked on by ONE streaming generator (routes.stream_answer). The Stop button asks the server to stop that
turn (POST /chat/{chat_id}/cancel). Two things carry the stop:

  * DURABLE: `cancelled_at` on the chat message (index.mark_turn_cancelled). Every process sees it; the stream polls it
    (index.is_turn_cancelled), the save helpers refuse to write onto a stopped message, and a stopped turn is never picked up
    again (page reload, turn-finisher queue).
  * IMMEDIATE, same process: this registry. The generator registers a threading.Event for its turn; the cancel route sets it,
    so the generator notices at its next checkpoint without waiting for a database poll.

The registry only ever holds one entry per running turn and removes it when the generator ends, so it cannot grow without
bound. It is process-local, like the scan registry in logics.py: in a multi-process deployment a stop that lands on another
process is noticed through the durable flag instead (a short poll interval later), never missed.
"""
import threading

_lock = threading.Lock()
_events = {}          # (owner_key, chat_id, turn_id) -> threading.Event


def _key(owner_key, chat_id, turn_id):
    return (owner_key, chat_id, turn_id)


def register(owner_key: str, chat_id: str, turn_id: str) -> threading.Event:
    """The stop event of a running turn (created, or the existing one if this turn is already registered)."""
    with _lock:
        ev = _events.get(_key(owner_key, chat_id, turn_id))
        if ev is None:
            ev = threading.Event()
            _events[_key(owner_key, chat_id, turn_id)] = ev
        return ev


def signal(owner_key: str, chat_id: str, turn_id: str) -> bool:
    """Sets the stop event of a running turn in THIS process. True if a generator was registered for it."""
    with _lock:
        ev = _events.get(_key(owner_key, chat_id, turn_id))
    if ev is None:
        return False
    ev.set()
    return True


def discard(owner_key: str, chat_id: str, turn_id: str, event: threading.Event = None) -> None:
    """Forgets a turn's event (generator finished). With `event`, only if it is still THAT event — a newer run of the same
    turn id must not lose its own registration to an older generator's cleanup."""
    with _lock:
        ev = _events.get(_key(owner_key, chat_id, turn_id))
        if ev is not None and (event is None or ev is event):
            _events.pop(_key(owner_key, chat_id, turn_id), None)


def active_count() -> int:
    with _lock:
        return len(_events)


def is_closed(msg) -> bool:
    """A turn that must not be worked on again: the user stopped it (`cancelled_at`) or it genuinely FAILED (`failed_at`, set
    when the analysis failed) and no answer was stored. Pure dict check, so every module can use it. A stored answer always
    wins: a failure flag next to an answer (another runner finished it) is not a closed turn."""
    m = msg or {}
    if m.get("cancelled_at"):
        return True
    return bool(m.get("failed_at")) and not m.get("claude_answer")
