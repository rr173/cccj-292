"""Runtime configuration.

``DB_PATH`` is mutable module-level state so the application factory (and the
test suite, which gives every test a fresh database) can retarget the store
without rebuilding router closures.
"""

import json
import os
import re
from typing import Any

# Default location for the runnable server; tests override via create_app(db_path=...).
DB_PATH: str = os.environ.get("REVIEW_DB_PATH", os.path.join("data", "review.db"))

# Conference rooms are *local configuration*, never managed through the API.
# Point REVIEW_ROOMS_CONFIG at another JSON file to change the venue list.
_DEFAULT_ROOMS_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.json")
ROOMS_CONFIG_PATH: str = os.environ.get("REVIEW_ROOMS_CONFIG", _DEFAULT_ROOMS_CONFIG)


def _normalize_room_key(value: Any) -> str:
    """Collapse case / spaces / underscores / hyphens for tolerant matching."""
    return re.sub(r"[\s_\-/]+", "", str(value).strip().lower())


def load_rooms(config_path: str | None = None) -> list[dict[str, Any]]:
    """Return the locally configured room records.

    Each room is normalised to ``{"id", "code", "name", "capacity", "keys"}``
    where ``keys`` contains every accepted identifier (code, name and aliases).
    """
    path = config_path or ROOMS_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rooms: list[dict[str, Any]] = []
    for index, entry in enumerate(raw.get("rooms", []), start=1):
        if isinstance(entry, str):
            entry = {"code": entry, "name": entry}
        code = str(entry.get("code") or entry.get("id") or entry["name"])
        room_id = entry.get("id", index)
        name = str(entry.get("name") or code)
        aliases = [str(a) for a in entry.get("aliases", [])]
        keys = {
            _normalize_room_key(code),
            _normalize_room_key(name),
            *[_normalize_room_key(a) for a in aliases],
        }
        rooms.append(
            {
                "id": room_id,
                "code": code,
                "name": name,
                "capacity": entry.get("capacity"),
                "keys": {k for k in keys if k},
            }
        )
    return rooms


def public_rooms() -> list[dict[str, Any]]:
    """API-safe room listing (no internal matching-key set)."""
    return [
        {"id": r["id"], "code": r["code"], "name": r["name"], "capacity": r["capacity"]}
        for r in load_rooms()
    ]


def resolve_room(value: Any) -> dict[str, Any] | None:
    """Resolve an editor-supplied room reference to a configured room.

    Accepts the integer id, numeric strings, code, name or any configured
    alias, with tolerant spacing/case/hyphen matching.
    """
    if value is None:
        return None
    rooms = load_rooms()
    # Exact integer / numeric-id reference.
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        numeric = int(value)
        for room in rooms:
            if room["id"] == numeric:
                return room
    key = _normalize_room_key(value)
    if not key:
        return None
    for room in rooms:
        if key in room["keys"]:
            return room
    return None
