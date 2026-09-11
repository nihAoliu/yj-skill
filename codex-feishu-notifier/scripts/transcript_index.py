"""Bounded incremental metadata index. No model/API calls or transcript writes."""

from collections import OrderedDict
import json
from pathlib import Path

_CACHE = OrderedDict()
FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")


def metadata(event):
    result = {
        "model": event.get("model", ""),
        "effort": event.get("reasoning_effort") or event.get("effort", ""),
        "tokens": None,
    }
    if not event.get("transcript_path"):
        return result
    path = Path(event["transcript_path"]).expanduser()
    try:
        stat = path.stat()
        key = str(path)
        state = _CACHE.get(key)
        if (
            not state
            or state["inode"] != stat.st_ino
            or stat.st_size < state["offset"]
            or (stat.st_size == state["offset"] and stat.st_mtime_ns != state["mtime"])
        ):
            state = {
                "inode": stat.st_ino,
                "offset": 0,
                "mtime": 0,
                "active": "",
                "total": {},
                "turns": OrderedDict(),
            }
        with path.open("rb") as handle:
            handle.seek(state["offset"])
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    handle.seek(start)
                    break
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    if not line.endswith(b"\n"):
                        handle.seek(start)
                        break
                    continue
                if not isinstance(record, dict) or not isinstance(
                    record.get("payload"), dict
                ):
                    continue
                payload = record["payload"]
                if record.get("type") == "turn_context":
                    turn = str(payload.get("turn_id") or "")
                    state["active"] = turn
                    if turn not in state["turns"]:
                        state["turns"][turn] = {
                            "baseline": dict(state["total"]),
                            "current": dict(state["total"]),
                        }
                    entry = state["turns"][turn]
                    if payload.get("model"):
                        entry["model"] = payload["model"]
                    collaboration = payload.get("collaboration_mode") or {}
                    settings = (
                        collaboration.get("settings") or {}
                        if isinstance(collaboration, dict)
                        else {}
                    )
                    effort = (
                        payload.get("effort")
                        or payload.get("reasoning_effort")
                        or settings.get("reasoning_effort")
                    )
                    if effort:
                        entry["effort"] = effort
                    while len(state["turns"]) > 128:
                        state["turns"].popitem(last=False)
                if (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "token_count"
                ):
                    info = payload.get("info") or {}
                    if not isinstance(info, dict):
                        continue
                    usage = info.get("total_token_usage") or {}
                    if not isinstance(usage, dict):
                        continue
                    total = {
                        field: usage[field]
                        for field in FIELDS
                        if isinstance(usage.get(field), int)
                        and not isinstance(usage[field], bool)
                    }
                    entry = state["turns"].get(state["active"])
                    if entry is not None:
                        last = info.get("last_token_usage") or {}
                        # Only establish zero when the first observed total equals
                        # that same request's usage. Otherwise report unavailable.
                        if (
                            not entry["baseline"]
                            and isinstance(last, dict)
                            and total.get("total_tokens") is not None
                            and total.get("total_tokens") == last.get("total_tokens")
                        ):
                            entry["baseline"] = {field: 0 for field in total}
                        entry["current"] = total
                    state["total"] = total
            state["offset"] = handle.tell()
        state["mtime"] = stat.st_mtime_ns
        _CACHE[key] = state
        _CACHE.move_to_end(key)
        while len(_CACHE) > 16:
            _CACHE.popitem(last=False)
        entry = state["turns"].get(str(event.get("turn_id") or state["active"]), {})
        result.update(
            {field: entry[field] for field in ("model", "effort") if field in entry}
        )
        baseline, current = entry.get("baseline", {}), entry.get("current", {})
        for field in FIELDS:
            if (
                field in baseline
                and field in current
                and current[field] >= baseline[field]
            ):
                result[field] = current[field] - baseline[field]
        result["tokens"] = result.get("total_tokens")
        result["token_source"] = (
            "cumulative_delta" if result["tokens"] is not None else "unavailable"
        )
    except (OSError, ValueError, TypeError):
        pass
    return result
