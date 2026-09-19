"""Durable checkpoints: only completed turns advance the recovery pointer."""
import json
import os
import uuid
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Invalid memory metadata: {path.name}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recovery_candidates(state: dict) -> list[dict]:
    candidates = []
    seen = set()
    for item in [state, *reversed(state.get("history", []))]:
        session_id = item.get("session_id")
        if not isinstance(session_id, str) or not session_id or session_id in seen:
            continue
        candidate = {"session_id": session_id}
        if "event_count" in item:
            count = item["event_count"]
            if type(count) is not int or count <= 0:
                raise ValueError("Invalid memory event boundary")
            candidate["event_count"] = count
        candidates.append(candidate)
        seen.add(session_id)
    return candidates


def commit_session(path: Path, session_id: str, recovery: dict, event_count: int) -> None:
    state = read_json(path)
    history = state.get("history", [])
    previous = state.get("session_id")
    if previous and previous != session_id:
        checkpoint = {"session_id": previous}
        if "event_count" in state:
            checkpoint["event_count"] = state["event_count"]
        history = [*history, checkpoint]
    write_json(path, {"session_id": session_id, "event_count": event_count,
                      "history": history[-50:], "recovery": recovery})
