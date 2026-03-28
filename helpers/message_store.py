"""Persistent message store for Matrix messages.

The chat bridge sync loop consumes events, making them unavailable to
tools that query room history separately. This store captures messages
as they arrive and makes them available to matrix_read.

Messages are stored per-room in a JSON file, capped at MAX_MESSAGES_PER_ROOM
to prevent unbounded growth.
"""

import json
import os
import time
from pathlib import Path

MAX_MESSAGES_PER_ROOM = 200


def _store_path() -> Path:
    candidates = [
        Path(__file__).parent.parent / "data" / "message_store.json",
        Path("/a0/usr/plugins/matrix_chat/data/message_store.json"),
    ]
    for p in candidates:
        if p.exists():
            return p
    path = candidates[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_store() -> dict:
    path = _store_path()
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_store(store: dict):
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(store, f)
    os.chmod(path, 0o600)


def store_message(room_id: str, message: dict):
    """Store a single message from a Matrix event.

    `message` should contain at minimum: event_id, sender, timestamp, body, type.
    """
    store = _load_store()
    room_key = str(room_id)

    if room_key not in store:
        store[room_key] = []

    # Avoid duplicates by event_id
    event_id = message.get("event_id")
    existing_ids = {m.get("event_id") for m in store[room_key]}
    if event_id in existing_ids:
        return

    store[room_key].append(message)

    # Cap per-room storage
    if len(store[room_key]) > MAX_MESSAGES_PER_ROOM:
        store[room_key] = store[room_key][-MAX_MESSAGES_PER_ROOM:]

    _save_store(store)


def get_messages(room_id: str, limit: int = 50) -> list:
    """Retrieve stored messages for a room, most recent last."""
    store = _load_store()
    messages = store.get(str(room_id), [])
    return messages[-limit:]


def get_all_rooms() -> dict:
    """Return a dict of room_id -> basic info for all rooms with stored messages."""
    store = _load_store()
    rooms = {}
    for room_id, messages in store.items():
        if messages:
            last_msg = messages[-1]
            rooms[room_id] = {
                "room_id": room_id,
                "message_count": len(messages),
                "last_sender": last_msg.get("sender", ""),
                "last_timestamp": last_msg.get("timestamp", 0),
            }
    return rooms
