"""Per-peer "last good speed mode" memory for whale.link.Link.

Plain in-memory dict, keyed by (mycall, peer_call) -> mode_id. Deliberately
has no file I/O: Link takes one of these as a constructor arg so it stays
unit-testable without touching disk. Persisting it across process restarts
(e.g. a JSON file loaded/saved around a Link's lifetime) is a vara_server.py
concern, not implemented here yet.
"""


def last_good_mode(history: dict, mycall: str, peer_call: str):
    return history.get((mycall, peer_call))


def record_good_mode(history: dict, mycall: str, peer_call: str, mode_id: int):
    history[(mycall, peer_call)] = mode_id
