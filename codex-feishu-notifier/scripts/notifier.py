#!/usr/bin/env python3
"""Codex lifecycle hook that sends completion notices to Feishu."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
from collections import deque
import re
import urllib.error
import urllib.parse
import urllib.request
import delivery
import transcript_index

PLUGIN_NAME = "codex-feishu-notifier"
LEGACY_PLUGIN_NAME = "codex-clawbot-notifier"
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "transport": "webhook",
    "webhook_url": "",
    "lark_cli_path": str(Path.home() / ".local" / "bin" / "lark-cli"),
    "lark_profile": "",
    "lark_chat_id": "",
    "card_notifications": True,
    "lark_completed_template_id": "",
    "lark_incomplete_template_id": "",
    "lark_interrupted_template_id": "",
    "lark_running_template_id": "",
    "lark_completed_template_version": "",
    "lark_incomplete_template_version": "",
    "lark_interrupted_template_version": "",
    "lark_running_template_version": "",
    "notify_on_start": True,
    "notify_on_finish_reply": True,
    "prompt_summary_chars": 160,
    "title": "✅ Codex 项目完成",
    "min_duration_seconds": 0,
    "include_last_message": False,
    "include_result_summary": True,
    "result_summary_chars": 140,
    "preview_chars": 280,
    "send_timeout_seconds": 10,
    "retries": 2,
    "max_pending_per_run": 10,
    "notify_ambient_suggestions": False,
    "notify_auto_review": False,
    "live_progress_notifications": True,
    "progress_update_interval_seconds": 12,
    "progress_heartbeat_seconds": 30,
    "progress_recent_steps": 3,
    "start_notice_delay_seconds": 10,
    "retry_base_seconds": 5,
    "retry_max_seconds": 900,
    "max_delivery_attempts": 12,
    "state_retention_days": 30,
    "summary_excluded_projects": [],
}


AMBIENT_SUGGESTION_PROMPT_MARKERS = (
    "generate 0 to 3 hyperpersonalized suggestions for what this user can do with codex",
    "upholding safety and compliance standards for codex ambient suggestions",
)


def log(message: str) -> None:
    line = json.dumps(
        {
            "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "plugin": PLUGIN_NAME,
            "message": redact_text(message),
        },
        ensure_ascii=False,
    )
    if Path(sys.argv[0]).stem == "interruption_watcher" and "run" in sys.argv:
        try:
            path = resolve_data_dir() / "watcher-events.log"
            if path.exists() and path.stat().st_size > 2_000_000:
                os.replace(path, path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as output:
                os.chmod(path, 0o600)
                output.write(line + "\n")
            return
        except OSError:
            pass
    print(line, file=sys.stderr)


def resolve_data_dir() -> Path:
    explicit = os.environ.get("CODEX_FEISHU_NOTIFIER_DATA_DIR") or os.environ.get(
        "CODEX_CLAWBOT_DATA_DIR"
    )
    if explicit:
        return Path(explicit).expanduser()
    legacy_path = Path.home() / ".local" / "share" / LEGACY_PLUGIN_NAME
    if legacy_path.exists():
        return legacy_path
    return Path.home() / ".local" / "share" / PLUGIN_NAME


def global_config_path() -> Path:
    explicit = os.environ.get("CODEX_FEISHU_NOTIFIER_CONFIG") or os.environ.get(
        "CODEX_CLAWBOT_CONFIG"
    )
    if explicit:
        return Path(explicit).expanduser()
    legacy_path = Path.home() / ".config" / LEGACY_PLUGIN_NAME / "config.json"
    if legacy_path.exists():
        return legacy_path
    return Path.home() / ".config" / PLUGIN_NAME / "config.json"


def read_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback
    except (OSError, json.JSONDecodeError) as exc:
        # Never overwrite a corrupted durable queue with an empty fallback.
        if path.name in {
            "feishu_pending.json",
            "feishu_sent.json",
            "feishu_failed.json",
        }:
            raise OSError(
                f"无法读取通知持久化文件 {path.name}：{type(exc).__name__}"
            ) from exc
        return fallback


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        with contextlib.suppress(OSError):
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def load_config() -> tuple[dict[str, Any], Path | None]:
    config = dict(DEFAULT_CONFIG)
    path = global_config_path()
    value = read_json(path, None)
    loaded_path: Path | None = None
    if isinstance(value, dict):
        config.update(value)
        loaded_path = path
    overrides = {
        "transport": os.environ.get("CODEX_FEISHU_TRANSPORT"),
        "webhook_url": (
            os.environ.get("CODEX_FEISHU_WEBHOOK_URL")
            or os.environ.get("FEISHU_WEBHOOK_URL")
        ),
        "lark_cli_path": os.environ.get("CODEX_FEISHU_LARK_CLI_PATH"),
        "lark_profile": os.environ.get("CODEX_FEISHU_LARK_PROFILE"),
        "lark_chat_id": os.environ.get("CODEX_FEISHU_LARK_CHAT_ID"),
        "lark_completed_template_id": os.environ.get(
            "CODEX_FEISHU_COMPLETED_TEMPLATE_ID"
        ),
        "lark_incomplete_template_id": os.environ.get(
            "CODEX_FEISHU_INCOMPLETE_TEMPLATE_ID"
        ),
        "lark_interrupted_template_id": os.environ.get(
            "CODEX_FEISHU_INTERRUPTED_TEMPLATE_ID"
        ),
        "lark_running_template_id": os.environ.get("CODEX_FEISHU_RUNNING_TEMPLATE_ID"),
    }
    for key, value in overrides.items():
        if value:
            config[key] = value
    return config, loaded_path


def ensure_interruption_watcher(data_dir: Path) -> None:
    script = Path(__file__).with_name("interruption_watcher.py")
    if not script.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(script), "ensure"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # The lifecycle hook must never block a Codex turn.
        return


@contextlib.contextmanager
def state_lock(data_dir: Path, wait_seconds: float = 2.0) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    # Persistent inode + kernel lock: process death releases it automatically.
    lock_path = data_dir / ".state.v2.lock"
    deadline = time.monotonic() + wait_seconds
    handle = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a+")
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("notification state is busy")
                time.sleep(0.02)
        yield
    finally:
        handle.close()


def safe_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


def session_state_path(data_dir: Path, session_id: str) -> Path:
    return data_dir / "sessions" / f"{safe_id(session_id)}.json"


def record_session_start(event: dict[str, Any], data_dir: Path) -> None:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return
    atomic_write_json(
        session_state_path(data_dir, session_id),
        {
            "session_id": session_id,
            "cwd": str(event.get("cwd") or ""),
            "started_at": time.time(),
        },
    )


def session_duration_seconds(event: dict[str, Any], data_dir: Path) -> int | None:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return None
    state = read_json(session_state_path(data_dir, session_id), {})
    started_at = state.get("started_at") if isinstance(state, dict) else None
    if not isinstance(started_at, (int, float)):
        return None
    return max(0, int(time.time() - float(started_at)))


def turn_state_path(data_dir: Path, session_id: str) -> Path:
    return data_dir / "turns" / f"{safe_id(session_id)}.json"


def turn_card_state_path(data_dir: Path, event: dict[str, Any]) -> Path | None:
    """Return a cross-rollout state path for one stable Codex turn."""
    turn_id = str(event.get("turn_id") or "").strip()
    session_id = str(event.get("session_id") or "").strip()
    identity = turn_id or session_id
    if not identity:
        return None
    return data_dir / "turn_cards" / f"{safe_id(identity)}.json"


def read_turn_card_state(event: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    path = turn_card_state_path(data_dir, event)
    if path is None:
        return {}
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def _write_turn_card_state(
    event: dict[str, Any], data_dir: Path, state: dict[str, Any]
) -> None:
    path = turn_card_state_path(data_dir, event)
    if path is not None:
        atomic_write_json(path, state)


def record_turn_start(event: dict[str, Any], data_dir: Path) -> None:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return
    turn_id = str(event.get("turn_id") or "")
    event_started_at = event.get("started_at")
    started_at = (
        float(event_started_at)
        if isinstance(event_started_at, (int, float))
        else time.time()
    )
    path = turn_state_path(data_dir, session_id)
    with state_lock(data_dir):
        existing = read_json(path, {})
        if (
            not isinstance(existing, dict)
            or str(existing.get("turn_id") or "") != turn_id
        ):
            existing = {
                "session_id": session_id,
                "turn_id": turn_id,
                "started_at": started_at,
            }
        if event_is_auto_review(event):
            existing["notification_kind"] = "auto_review"
        elif event_is_ambient_suggestions(event):
            existing["notification_kind"] = "ambient_suggestions"
        elif event_prompt_text(event):
            existing["notification_kind"] = "user"
        atomic_write_json(path, existing)
        card_state = read_turn_card_state(event, data_dir)
        if str(card_state.get("turn_id") or "") != turn_id:
            card_state = {
                "session_id": session_id,
                "turn_id": turn_id,
                "started_at": started_at,
                "latest_step": "已接收任务，正在准备执行",
                "recent_steps": [],
                "step_count": 0,
            }
        config, _ = load_config()
        prompt = redact_text(prompt_summary(event, config))
        if prompt:
            card_state["prompt"] = prompt
        if existing.get("notification_kind"):
            card_state["notification_kind"] = existing["notification_kind"]
        _write_turn_card_state(event, data_dir, card_state)


def claim_running_notice(event: dict[str, Any], data_dir: Path) -> bool:
    """Claim the start notification once when duplicate hook sources are active."""
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return False
    turn_id = str(event.get("turn_id") or "")
    path = turn_state_path(data_dir, session_id)
    with state_lock(data_dir):
        state = read_json(path, {})
        if not isinstance(state, dict) or str(state.get("turn_id") or "") != turn_id:
            return False
        card_state = read_turn_card_state(event, data_dir)
        if (
            state.get("running_message_id")
            or state.get("running_notice_claimed_at")
            or card_state.get("running_message_id")
            or card_state.get("running_notice_claimed_at")
        ):
            return False
        claimed_at = time.time()
        state["running_notice_claimed_at"] = claimed_at
        card_state.update(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "started_at": card_state.get("started_at") or state.get("started_at"),
                "running_notice_claimed_at": claimed_at,
            }
        )
        atomic_write_json(path, state)
        _write_turn_card_state(event, data_dir, card_state)
    return True


def save_running_message_id(
    event: dict[str, Any], data_dir: Path, message_id: str, error: str = ""
) -> None:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return
    turn_id = str(event.get("turn_id") or "")
    path = turn_state_path(data_dir, session_id)
    with state_lock(data_dir):
        state = read_json(path, {})
        same_turn = (
            isinstance(state, dict) and str(state.get("turn_id") or "") == turn_id
        )
        if not same_turn:
            state = {}
        if message_id:
            state["running_message_id"] = message_id
            state.pop("running_notice_error", None)
        elif error:
            state["running_notice_error"] = compact_preview(error, 300)
            state.pop("running_notice_claimed_at", None)
        if same_turn:
            atomic_write_json(path, state)
        card_state = read_turn_card_state(event, data_dir)
        card_state.update(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "started_at": card_state.get("started_at") or state.get("started_at"),
            }
        )
        if message_id:
            card_state["running_message_id"] = message_id
            card_state["last_card_update_at"] = time.time()
            card_state.pop("running_notice_error", None)
        elif error:
            card_state["running_notice_error"] = compact_preview(error, 300)
            card_state.pop("running_notice_claimed_at", None)
        _write_turn_card_state(event, data_dir, card_state)


def running_message_id(event: dict[str, Any], data_dir: Path) -> str:
    card_state = read_turn_card_state(event, data_dir)
    card_message_id = str(card_state.get("running_message_id") or "").strip()
    if card_message_id.startswith("om_"):
        return card_message_id
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return ""
    state = read_json(turn_state_path(data_dir, session_id), {})
    if not isinstance(state, dict):
        return ""
    event_turn_id = str(event.get("turn_id") or "")
    state_turn_id = str(state.get("turn_id") or "")
    if event_turn_id and state_turn_id and event_turn_id != state_turn_id:
        return ""
    message_id = str(state.get("running_message_id") or "").strip()
    return message_id if message_id.startswith("om_") else ""


def turn_duration_seconds(event: dict[str, Any], data_dir: Path) -> int | None:
    card_state = read_turn_card_state(event, data_dir)
    card_started_at = card_state.get("started_at")
    if isinstance(card_started_at, (int, float)):
        return max(0, int(time.time() - float(card_started_at)))
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return None
    state = read_json(turn_state_path(data_dir, session_id), {})
    started_at = state.get("started_at") if isinstance(state, dict) else None
    if not isinstance(started_at, (int, float)):
        return None
    return max(0, int(time.time() - float(started_at)))


def turn_started_at(event: dict[str, Any], data_dir: Path) -> float | None:
    card_state = read_turn_card_state(event, data_dir)
    card_started_at = card_state.get("started_at")
    if isinstance(card_started_at, (int, float)):
        return float(card_started_at)
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return None
    state = read_json(turn_state_path(data_dir, session_id), {})
    started_at = state.get("started_at") if isinstance(state, dict) else None
    return float(started_at) if isinstance(started_at, (int, float)) else None


def event_duration_seconds(event: dict[str, Any]) -> int | None:
    """Return duration embedded in a rollout lifecycle event, when available."""
    duration_ms = event.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        return max(0, int(float(duration_ms) / 1000))
    started_at = event.get("started_at")
    completed_at = event.get("completed_at")
    if isinstance(started_at, (int, float)) and isinstance(completed_at, (int, float)):
        return max(0, int(float(completed_at) - float(started_at)))
    return None


def event_prompt_text(event: dict[str, Any]) -> str:
    """Return hook fields that may contain the submitted user prompt."""
    values: list[str] = []
    for key in ("prompt", "user_prompt", "user_message", "input"):
        value = event.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, dict)):
            with contextlib.suppress(TypeError, ValueError):
                values.append(json.dumps(value, ensure_ascii=False))
    return "\n".join(values)


def prompt_summary(event: dict[str, Any], config: dict[str, Any]) -> str:
    """Create a short local-only description of the submitted task."""
    try:
        limit = max(80, min(240, int(config.get("prompt_summary_chars", 160))))
    except (TypeError, ValueError):
        limit = 160
    source = event_prompt_text(event).replace("\x00", "").strip()
    if not source:
        return "执行本轮 Codex 任务。"
    request_marker = re.compile(r"my request for codex\s*:\s*", re.IGNORECASE)
    matches = list(request_marker.finditer(source))
    if matches:
        source = source[matches[-1].end() :]
    for tag in ("hook_prompt", "environment_context", "image"):
        source = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            " ",
            source,
            flags=re.IGNORECASE | re.DOTALL,
        )
    source = re.sub(r"```.*?```", " ", source, flags=re.DOTALL)
    meaningful: list[str] = []
    seen: set[str] = set()
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if (
            lowered.startswith("# files mentioned")
            or lowered.startswith("files mentioned")
            or lowered.startswith("<image")
            or re.match(r"^[-*+]\s+/(?:users|var|private|tmp)/", lowered)
        ):
            continue
        line = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = " ".join(line.split()).strip("；;，,。 ")
        if not line or line in seen:
            continue
        seen.add(line)
        meaningful.append(line)
        if len("；".join(meaningful)) >= limit or len(meaningful) >= 4:
            break
    if not meaningful:
        return "执行本轮 Codex 任务。"
    summary = re.sub(r"[。！？!?]+", "；", "；".join(meaningful))
    summary = re.sub(r"[；;]+", "；", summary).strip("；,， ")
    clipped = compact_preview(summary, limit).rstrip("；,，。 ")
    return clipped if clipped.endswith("…") else clipped + "。"


def is_ambient_suggestions_prompt(text: str) -> bool:
    text = re.sub(r"\A\s*#\s*Overview\s*\n", "", text, flags=re.I)
    normalized = " ".join(text.lower().split())
    return any(
        normalized.startswith(marker) for marker in AMBIENT_SUGGESTION_PROMPT_MARKERS
    )


def event_is_ambient_suggestions(event: dict[str, Any]) -> bool:
    return (
        event.get("notification_kind") in ("ambient_suggestions", "ambient_safety")
        or event.get("source") in ("ambient_suggestions", "ambient_safety")
        or is_ambient_suggestions_prompt(event_prompt_text(event))
    )


def is_auto_review_model(model: str) -> bool:
    normalized = model.strip().lower().replace("_", "-")
    return normalized.startswith("codex-auto-review")


def event_is_auto_review(event: dict[str, Any]) -> bool:
    for key in ("model", "model_id", "model_slug"):
        value = event.get(key)
        if isinstance(value, str) and is_auto_review_model(value):
            return True
    metadata = transcript_execution_metadata(event)
    return is_auto_review_model(str(metadata.get("model") or ""))


def turn_notification_kind(event: dict[str, Any], data_dir: Path) -> str:
    session_id = str(event.get("session_id") or "")
    if not session_id:
        return ""
    state = read_json(turn_state_path(data_dir, session_id), {})
    if not isinstance(state, dict):
        return ""
    if event.get("turn_id") and state.get("turn_id") != event["turn_id"]:
        return ""
    return str(state.get("notification_kind") or "")


def codex_logs_candidates() -> list[Path]:
    codex_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    return database_candidates(codex_home, "logs")


def session_is_ambient_suggestions_by_logs(event: dict[str, Any]) -> bool:
    """Identify Codex's hidden suggestion generator and safety-check turns."""
    session_id = str(event.get("session_id") or "").strip()
    if not session_id:
        return False
    marker_conditions = " OR ".join(
        "LOWER(feedback_log_body) LIKE ?" for _ in AMBIENT_SUGGESTION_PROMPT_MARKERS
    )
    query = f"""
        SELECT 1
        FROM logs
        WHERE thread_id = ?
          AND target = 'codex_core::session::handlers'
          AND ({marker_conditions})
        LIMIT 1
    """
    parameters = (
        session_id,
        *(f"%{marker}%" for marker in AMBIENT_SUGGESTION_PROMPT_MARKERS),
    )
    for database_path in codex_logs_candidates():
        if not database_path.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.4,
            )
            if connection.execute(query, parameters).fetchone():
                return True
        except (sqlite3.Error, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return False


def should_suppress_notification(
    event: dict[str, Any], config: dict[str, Any], data_dir: Path
) -> bool:
    kind = turn_notification_kind(event, data_dir)
    # Explicit lifecycle metadata wins; legacy prompt/log heuristics remain
    # compatibility fallbacks only, never the reason to suppress ordinary turns.
    origin = event.get("notification_kind") or event.get("source")
    if isinstance(origin, str):
        if origin in {"ambient_suggestions", "ambient_safety"} and not config.get(
            "notify_ambient_suggestions", False
        ):
            return True
        if origin == "auto_review" and not config.get("notify_auto_review", False):
            return True
    if not bool(config.get("notify_auto_review", False)) and (
        event_is_auto_review(event) or kind == "auto_review"
    ):
        return True
    if bool(config.get("notify_ambient_suggestions", False)):
        return False
    return (
        event_is_ambient_suggestions(event)
        or kind == "ambient_suggestions"
        or (
            not kind
            and not event_prompt_text(event)
            and session_is_ambient_suggestions_by_logs(event)
        )
    )


def format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remaining_seconds} 秒"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours} 小时 {remaining_minutes} 分"


def format_token_estimate(tokens: int | None) -> str:
    if not isinstance(tokens, int) or tokens < 0:
        return "未记录"
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        value = f"{tokens / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{value}K"
    value = f"{tokens / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{value}M"


def format_model_label(model: str, effort: str) -> str:
    normalized_model = model.strip()
    if not normalized_model:
        return "未记录"
    exact_names = {
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-terra": "GPT-5.6 Terra",
    }
    display_model = exact_names.get(normalized_model.lower())
    if not display_model:
        parts = normalized_model.split("-")
        if parts and parts[0].lower() == "gpt":
            display_model = "GPT-" + "-".join(parts[1:])
        else:
            display_model = normalized_model
    effort_names = {
        "low": "低",
        "medium": "中",
        "high": "高",
        "xhigh": "极高",
        "max": "最高",
        "ultra": "极致",
    }
    display_effort = effort_names.get(effort.strip().lower(), effort.strip())
    return f"{display_model} · {display_effort}" if display_effort else display_model


def transcript_execution_metadata(event: dict[str, Any]) -> dict[str, Any]:
    """Read incremental model/usage metadata; unknown values remain unknown."""
    return transcript_index.metadata(event)


def last_assistant_message(event: dict[str, Any]) -> str:
    for key in ("last_assistant_message", "assistant_message", "message"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    transcript_path = event.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return ""
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return ""
    target_turn_id = str(event.get("turn_id") or "").strip()
    recent_lines: deque[str] = deque(maxlen=400)
    target_message = ""
    active_turn_id = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                recent_lines.append(line)
                if not target_turn_id:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "turn_context":
                    active_turn_id = str(payload.get("turn_id") or "").strip()
                    continue
                if (
                    record.get("type") == "event_msg"
                    and payload.get("type") == "turn_aborted"
                    and str(payload.get("turn_id") or "").strip() == target_turn_id
                ):
                    break
                if active_turn_id != target_turn_id:
                    continue
                if record.get("type") == "event_msg":
                    if payload.get("type") == "task_complete":
                        value = payload.get("last_agent_message")
                        if isinstance(value, str) and value.strip():
                            target_message = value.strip()
                    elif payload.get("type") == "agent_message":
                        value = payload.get("message")
                        if isinstance(value, str) and value.strip():
                            target_message = value.strip()
                if (
                    record.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                ):
                    texts: list[str] = []
                    content = payload.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            text = item.get("text")
                            if isinstance(text, str) and text.strip():
                                texts.append(text.strip())
                    if texts:
                        target_message = "\n".join(texts)
    except OSError:
        return ""
    if target_turn_id:
        return target_message
    for line in reversed(recent_lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "event_msg":
            if payload.get("type") == "task_complete":
                value = payload.get("last_agent_message")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if payload.get("type") == "agent_message":
                value = payload.get("message")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if (
            record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
        ):
            texts: list[str] = []
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(text.strip())
            if texts:
                return "\n".join(texts)
    return ""


def compact_preview(text: str, limit: int) -> str:
    one_line = " ".join(text.replace("\x00", "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: max(0, limit - 1)].rstrip() + "…"


def one_sentence_result(text: str, limit: int = 140) -> str:
    """Mechanically condense an existing answer; this never calls a model."""
    meaningful: list[str] = []
    inside_code_block = False
    for raw_line in text.replace("\x00", "").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            inside_code_block = not inside_code_block
            continue
        if inside_code_block or not line:
            continue
        if line.startswith(":::writing{") or line == ":::":
            continue
        if line.lower().startswith("to view this in the codex app"):
            break
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)", "", line)
        line = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = " ".join(line.split())
        if line:
            meaningful.append(line)
        if len(meaningful) >= 4:
            break
    if not meaningful:
        return ""
    sentence = "；".join(meaningful)
    sentence = re.sub(r"[。！？!?]+", "；", sentence)
    sentence = re.sub(r"[；;]+", "；", sentence).strip("；,， ")
    if not sentence:
        return ""
    clipped = compact_preview(sentence, max(40, min(300, limit))).rstrip("；,， ")
    return clipped if clipped.endswith("…") else clipped + "。"


def codex_state_candidates() -> list[Path]:
    codex_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    return database_candidates(codex_home, "state")


def database_candidates(root: Path, prefix: str) -> list[Path]:
    paths = list(root.glob(f"{prefix}_*.sqlite")) + list(
        (root / "sqlite").glob(f"{prefix}_*.sqlite")
    )

    def version(path):
        match = re.search(r"_(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    return sorted(paths, key=version, reverse=True)


def conversation_title(event: dict[str, Any]) -> str:
    """Best-effort lookup of the current title without reading chat contents."""
    for key in ("conversation_title", "thread_title", "session_title"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return compact_preview(value, 80)
    session_id = str(event.get("session_id") or "").strip()
    if not session_id:
        return ""
    for database_path in codex_state_candidates():
        if not database_path.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.4,
            )
            columns = {
                column[1] for column in connection.execute("PRAGMA table_info(threads)")
            }
            names = [
                name
                for name in ("name", "title", "first_user_message")
                if name in columns
            ]
            if not names or "id" not in columns:
                continue
            expressions = [f"NULLIF({name}, '')" for name in names]
            expression = (
                "COALESCE(" + ",".join(expressions) + ")"
                if len(names) > 1
                else expressions[0]
            )
            row = connection.execute(
                f"SELECT {expression} FROM threads WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone()
        except (sqlite3.Error, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
        if row and isinstance(row[0], str) and row[0].strip():
            return compact_preview(row[0], 80)
    return ""


def display_conversation_name(event: dict[str, Any], config: dict[str, Any]) -> str:
    configured = str(config.get("conversation_name") or "").strip()
    if configured:
        return compact_preview(configured, 80)
    title = conversation_title(event)
    if title:
        return title
    cwd = str(event.get("cwd") or "").strip()
    directory_name = Path(cwd).name if cwd else ""
    if directory_name.startswith("g-p-") or len(directory_name) > 80:
        return "未命名对话"
    return directory_name or "未命名对话"


def project_name_from_agents_file(directory: Path) -> str:
    for candidate in (directory, *list(directory.parents)[:5]):
        agents_path = candidate / "AGENTS.md"
        try:
            raw = agents_path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        match = re.search(r"ChatGPT project [“\"]([^”\"]+)[”\"]", raw)
        if match:
            return compact_preview(match.group(1), 80)
    return ""


def display_project_name(event: dict[str, Any], config: dict[str, Any]) -> str:
    configured = str(config.get("project_name") or "").strip()
    if configured:
        return compact_preview(configured, 80)
    event_name = str(event.get("project_name") or "").strip()
    if event_name:
        return compact_preview(event_name, 80)
    cwd = str(event.get("cwd") or "").strip()
    if not cwd:
        return "Codex"
    directory = Path(cwd).expanduser()
    project_name = project_name_from_agents_file(directory)
    if project_name:
        return project_name
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists() and candidate.name:
            return compact_preview(candidate.name, 80)
        if candidate == candidate.parent:
            break
    directory_name = directory.name
    if directory_name.startswith("g-p-") or len(directory_name) > 80:
        return "Codex"
    return directory_name or "Codex"


ACTION_NEEDED_MARKERS = (
    "需要你确认",
    "请确认",
    "需要确认",
    "需要你提供",
    "请提供",
    "需要你补充",
    "请补充",
    "需要你授权",
    "请授权",
    "需要你允许",
    "请允许",
    "等待你",
    "等你回复",
    "need your confirmation",
    "need you to",
    "please provide",
    "please approve",
)
BLOCKED_MARKERS = (
    "无法完成",
    "未能完成",
    "尚未完成",
    "执行受阻",
    "被阻塞",
    "权限不足",
    "permission denied",
    "blocked",
    "failed",
)
SUCCESS_MARKERS = (
    "已完成",
    "完成并",
    "已成功",
    "发送成功",
    "测试通过",
    "验证通过",
    "构建通过",
    "部署完成",
    "done",
    "completed",
    "succeeded",
)


def status_control_text(text: str) -> str:
    """Return only assistant control text used to decide terminal status.

    Reusable writing artifacts and code samples can legitimately contain phrases
    such as "需要确认" without asking the user for input.  Status detection must
    therefore ignore their contents and inspect only the assistant's surrounding
    message.
    """
    control_text = re.sub(
        r"(?ms)^:::writing\{[^\r\n]*\}[^\r\n]*(?:\r?\n).*?^:::[ \t]*$",
        " ",
        text,
    )
    return re.sub(r"```.*?```", " ", control_text, flags=re.DOTALL)


def result_status(text: str) -> str:
    normalized = " ".join(status_control_text(text).lower().split())
    if not normalized:
        return "incomplete"
    if any(marker in normalized for marker in ACTION_NEEDED_MARKERS):
        return "incomplete"
    if any(marker in normalized for marker in BLOCKED_MARKERS) and not any(
        marker in normalized for marker in SUCCESS_MARKERS
    ):
        return "incomplete"
    return "completed"


def failure_type(text: str) -> str:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in ("授权", "approve", "approval")):
        return "等待授权"
    if any(
        marker in normalized
        for marker in ("需要你提供", "请提供", "需要你补充", "请补充")
    ):
        return "缺少信息"
    if any(
        marker in normalized
        for marker in ("权限不足", "permission denied", "forbidden")
    ):
        return "权限受限"
    if any(
        marker in normalized
        for marker in ("确认", "等待你", "等你回复", "confirmation")
    ):
        return "等待确认"
    if any(marker in normalized for marker in BLOCKED_MARKERS):
        return "执行失败"
    return "暂未完成"


def result_summary(
    event: dict[str, Any], config: dict[str, Any], status: str = "completed"
) -> str:
    source = last_assistant_message(event)
    if bool(config.get("include_result_summary", True)):
        try:
            result_chars = max(
                60, min(300, int(config.get("result_summary_chars", 140)))
            )
        except (TypeError, ValueError):
            result_chars = 140
        result = one_sentence_result(source, result_chars)
        if result:
            if status == "interrupted":
                prefix = "本轮任务已中断；中断前进展："
                return compact_preview(prefix + result, result_chars)
            return result
    elif bool(config.get("include_last_message")):
        try:
            preview_chars = max(40, min(1000, int(config.get("preview_chars", 280))))
        except (TypeError, ValueError):
            preview_chars = 280
        preview = compact_preview(source, preview_chars)
        if preview:
            return preview
    if status == "interrupted":
        return "本轮任务已中断；当前进度已保留，可回到 Codex 继续处理。"
    return "Codex 已结束本轮处理；请在任务中查看最终回复。"


def build_notice(
    event: dict[str, Any],
    config: dict[str, Any],
    duration_seconds: int | None,
    started_at_epoch: float | None = None,
) -> dict[str, str]:
    source = last_assistant_message(event)
    requested_status = str(event.get("notification_status") or "").strip()
    actual_status = str(event.get("turn_status") or "").strip().lower()
    if not requested_status:
        requested_status = {
            "failed": "incomplete",
            "error": "incomplete",
            "interrupted": "interrupted",
            "cancelled": "interrupted",
            "waiting_for_input": "incomplete",
            "waiting_for_approval": "incomplete",
        }.get(actual_status, "")
    status = (
        requested_status
        if requested_status in {"completed", "incomplete", "interrupted"}
        else result_status(source)
    )
    completed_at_epoch = event.get("completed_at")
    now = (
        dt.datetime.fromtimestamp(float(completed_at_epoch)).astimezone()
        if isinstance(completed_at_epoch, (int, float))
        else dt.datetime.now().astimezone()
    )
    event_started_at = event.get("started_at")
    if isinstance(event_started_at, (int, float)):
        started_at = dt.datetime.fromtimestamp(float(event_started_at)).astimezone()
    elif isinstance(started_at_epoch, (int, float)):
        started_at = dt.datetime.fromtimestamp(started_at_epoch).astimezone()
    elif duration_seconds is not None:
        started_at = now - dt.timedelta(seconds=duration_seconds)
    else:
        started_at = None
    metadata = transcript_execution_metadata(event)
    return {
        "status": status,
        "task_name": display_conversation_name(event, config),
        "project_name": display_project_name(event, config),
        "model_label": format_model_label(
            str(metadata.get("model") or ""),
            str(metadata.get("effort") or ""),
        ),
        "token_estimate": format_token_estimate(metadata.get("tokens")),
        "token_usage": {
            field: metadata.get(field) for field in transcript_index.FIELDS
        },
        "status_source": "lifecycle" if requested_status else "text_heuristic",
        "status_scope": "turn",
        "started_at": (
            started_at.strftime("%Y-%m-%d %H:%M") if started_at else "未记录"
        ),
        "duration": format_duration(duration_seconds) or "未记录",
        "completed_at": now.strftime("%Y-%m-%d %H:%M"),
        "failure_type": (
            (
                {
                    "failed": "执行失败",
                    "error": "执行失败",
                    "waiting_for_input": "等待回复",
                    "waiting_for_approval": "等待授权",
                }.get(actual_status)
                or failure_type(source)
            )
            if status == "incomplete"
            else ""
        ),
        "result_summary": result_summary(event, config, status),
    }


def build_running_notice(
    event: dict[str, Any], config: dict[str, Any], started_at_epoch: float | None
) -> dict[str, Any]:
    now_epoch = time.time()
    actual_started_at = (
        float(started_at_epoch)
        if isinstance(started_at_epoch, (int, float))
        else now_epoch
    )
    metadata = transcript_execution_metadata(event)
    model_label = format_model_label(
        str(metadata.get("model") or event.get("model") or ""),
        str(metadata.get("effort") or event.get("reasoning_effort") or ""),
    )
    return {
        "status": "running",
        "task_name": display_conversation_name(event, config),
        "project_name": display_project_name(event, config),
        "model_label": model_label,
        "token_estimate": (
            format_token_estimate(metadata.get("tokens"))
            if isinstance(metadata.get("tokens"), int)
            else "计算中"
        ),
        "started_at": dt.datetime.fromtimestamp(actual_started_at)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M"),
        "duration": format_duration(max(0, int(now_epoch - actual_started_at)))
        or "0 秒",
        "completed_at": "—",
        "updated_at": dt.datetime.fromtimestamp(now_epoch)
        .astimezone()
        .strftime("%H:%M:%S"),
        "failure_type": "",
        "prompt": prompt_summary(event, config),
        "current_step": "已接收任务，正在准备执行",
        "recent_steps": [],
        "step_count": "0",
        "result_summary": "Codex 正在执行本轮任务；结束后此卡片会自动刷新结果。",
    }


def lark_md_escape(value: Any, limit: int = 180) -> str:
    """Escape dynamic text so it cannot inject card Markdown or HTML."""
    text = compact_preview(str(value or ""), limit)
    replacements = {
        "&": "&amp;",
        "<": "&#60;",
        ">": "&#62;",
        "*": "&#42;",
        "~": "&#126;",
        "[": "&#91;",
        "]": "&#93;",
        "(": "&#40;",
        ")": "&#41;",
        "#": "&#35;",
        "_": "&#95;",
        "`": "&#96;",
    }
    return "".join(replacements.get(character, character) for character in text)


def codex_task_card_content(notice: dict[str, Any]) -> dict[str, Any]:
    """Build a complete Card 2.0 JSON payload without a Feishu template."""
    status = str(notice.get("status") or "running")
    styles = {
        "running": {
            "header": "blue",
            "accent": "blue",
            "background": "blue-50",
            "tag": "运行中",
            "focus": "当前步骤",
            "summary": "Codex 任务正在运行",
        },
        "completed": {
            "header": "green",
            "accent": "green",
            "background": "green-50",
            "tag": "本轮结束",
            "focus": "本次结果",
            "summary": "Codex 本轮处理已结束",
        },
        "incomplete": {
            "header": "red",
            "accent": "red",
            "background": "red-50",
            "tag": "需要处理",
            "focus": "未完成原因",
            "summary": "Codex 任务需要处理",
        },
        "interrupted": {
            "header": "orange",
            "accent": "orange",
            "background": "orange-50",
            "tag": "已中断",
            "focus": "中断时进展",
            "summary": "Codex 任务已中断",
        },
    }
    style = styles.get(status, styles["running"])
    task_name = compact_preview(str(notice.get("task_name") or "未命名对话"), 80)
    project_name = compact_preview(str(notice.get("project_name") or "Codex"), 80)
    model_label = lark_md_escape(notice.get("model_label") or "未记录", 80)
    token_estimate = lark_md_escape(notice.get("token_estimate") or "未记录", 32)
    started_at = lark_md_escape(notice.get("started_at") or "未记录", 40)
    duration = lark_md_escape(notice.get("duration") or "未记录", 40)
    completed_at = lark_md_escape(notice.get("completed_at") or "—", 40)
    updated_at = compact_preview(str(notice.get("updated_at") or ""), 20)
    step_count = lark_md_escape(notice.get("step_count") or "0", 16)
    prompt = compact_preview(str(notice.get("prompt") or "执行本轮 Codex 任务。"), 220)
    if status == "running":
        focus_text = compact_preview(
            str(notice.get("current_step") or "正在等待下一条运行进展"), 180
        )
    else:
        focus_text = compact_preview(
            str(notice.get("result_summary") or "请在 Codex 中查看最终结果。"), 260
        )

    subtitle_time = (
        f"最后更新 {updated_at}"
        if status == "running" and updated_at
        else f"结束于 {notice.get('completed_at') or '未记录'}"
    )
    tags = [
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": str(style["tag"])},
            "color": str(style["accent"]),
        }
    ]
    if status == "incomplete" and notice.get("failure_type"):
        tags.append(
            {
                "tag": "text_tag",
                "text": {
                    "tag": "plain_text",
                    "content": compact_preview(str(notice.get("failure_type")), 20),
                },
                "color": "orange",
            }
        )

    metric_fields = [
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**模型**\n{model_label}"},
        },
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**Token**\n{token_estimate}"},
        },
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**开始**\n{started_at}"},
        },
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**用时**\n{duration}"},
        },
        {
            "is_short": True,
            "text": {"tag": "lark_md", "content": f"**步骤**\n{step_count}"},
        },
        {
            "is_short": True,
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**更新**\n{lark_md_escape(updated_at or '刚刚', 20)}"
                    if status == "running"
                    else f"**完成**\n{completed_at}"
                ),
            },
        },
    ]
    elements: list[dict[str, Any]] = [
        {"tag": "div", "fields": metric_fields},
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": str(style["background"]),
                    "padding": "12px 12px 12px 12px",
                    "vertical_spacing": "4px",
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"**<font color='{style['accent']}'>"
                                f"{style['focus']}</font>**"
                            ),
                        },
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": focus_text,
                                "lines": 4,
                            },
                        },
                    ],
                }
            ],
        },
    ]

    raw_recent = notice.get("recent_steps")
    recent: list[tuple[str, str]] = []
    if isinstance(raw_recent, list):
        for item in raw_recent:
            if isinstance(item, dict):
                stamp = compact_preview(str(item.get("at") or ""), 8)
                text = compact_preview(str(item.get("text") or ""), 120)
            else:
                stamp = ""
                text = compact_preview(str(item or ""), 120)
            if text and text != focus_text:
                recent.append((stamp, text))
    if recent:
        recent_lines = "\n".join(
            f"- <font color='grey'>{lark_md_escape(stamp, 8)}</font> {lark_md_escape(text, 120)}"
            for stamp, text in recent[-3:]
        )
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "background_style": "grey-50",
                        "padding": "12px 12px 12px 12px",
                        "vertical_spacing": "4px",
                        "elements": [
                            {"tag": "markdown", "content": "**最近进展**"},
                            {
                                "tag": "markdown",
                                "content": recent_lines,
                                "text_size": "notation",
                            },
                        ],
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "background_style": "grey-50",
                    "padding": "12px 12px 12px 12px",
                    "vertical_spacing": "4px",
                    "elements": [
                        {"tag": "markdown", "content": "**任务目标**"},
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": prompt,
                                "lines": 3,
                            },
                        },
                    ],
                }
            ],
        }
    )
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "enable_forward": True,
            "summary": {"content": f"{style['summary']}：{task_name}"},
        },
        "header": {
            "template": str(style["header"]),
            "title": {"tag": "plain_text", "content": task_name},
            "subtitle": {
                "tag": "plain_text",
                "content": f"{project_name} · {subtitle_time}",
            },
            "icon": {"tag": "standard_icon", "token": "ai-common_colorful"},
            "text_tag_list": tags,
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def running_card_content(notice: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible alias for callers that build only the start card."""
    return codex_task_card_content(notice)


def notice_to_text(notice: dict[str, Any], config: dict[str, Any]) -> str:
    status = str(notice.get("status") or "completed")
    if status == "completed":
        title = str(config.get("title") or DEFAULT_CONFIG["title"])
    elif status == "interrupted":
        title = "⏹️ Codex 任务已中断"
    else:
        title = "⚠️ Codex 任务未完成"
    return "\n".join(
        [
            title,
            f"对话：{notice.get('task_name') or '未命名对话'}",
            f"项目：{notice.get('project_name') or 'Codex'}",
            f"模型：{notice.get('model_label') or '未记录'}",
            f"Token：{notice.get('token_estimate') or '未记录'}",
            f"开始时间：{notice.get('started_at') or '未记录'}",
            f"完成时间：{notice.get('completed_at') or '未记录'}",
            f"运行时长：{notice.get('duration') or '未记录'}",
            *(
                [f"未完成类型：{notice.get('failure_type') or '暂未完成'}"]
                if notice.get("status") == "incomplete"
                else []
            ),
            f"结果：{notice.get('result_summary') or '请在任务中查看最终回复。'}",
        ]
    )


def build_message(
    event: dict[str, Any], config: dict[str, Any], duration_seconds: int | None
) -> str:
    return notice_to_text(build_notice(event, config, duration_seconds), config)


def event_key(event: dict[str, Any]) -> str:
    turn_id = str(event.get("turn_id") or "").strip()
    if turn_id:
        status = (
            "interrupted"
            if str(event.get("notification_status") or "") == "interrupted"
            else "turn"
        )
        # Resuming or forking a conversation can copy one turn into a rollout
        # with a different session id. The Codex turn id remains stable and is
        # therefore the reliable cross-file deduplication key.
        return hashlib.sha256(f"{status}\x1f{turn_id}".encode("utf-8")).hexdigest()
    fields = [
        str(event.get("session_id") or ""),
        str(event.get("turn_id") or ""),
        str(event.get("cwd") or ""),
        last_assistant_message(event),
    ]
    return hashlib.sha256(
        "\x1f".join(fields).encode("utf-8", errors="replace")
    ).hexdigest()


def send_notice(
    config: dict[str, Any], notice: str | dict[str, Any]
) -> tuple[bool, str]:
    notice = sanitize_notice(notice, config)
    transport = str(config.get("transport") or "webhook").strip().lower()
    message = notice_to_text(notice, config) if isinstance(notice, dict) else notice
    if transport == "lark-cli":
        valid, detail = validate_lark_cli_config(config)
        if isinstance(notice, dict) and bool(config.get("card_notifications", True)):
            running_message = str(notice.get("_running_message_id") or "").strip()
            if running_message.startswith("om_"):

                def sender(timeout: int) -> tuple[bool, str]:
                    updated, update_error = update_lark_cli_card(
                        config, running_message, notice, timeout
                    )
                    if updated:
                        if bool(config.get("notify_on_finish_reply", True)):
                            return send_lark_cli_finish_reply(
                                config, running_message, notice, timeout
                            )
                        return True, ""
                    fallback_ok, fallback_error = send_lark_cli_card(
                        config, notice, timeout
                    )
                    if fallback_ok:
                        return True, ""
                    return (
                        False,
                        f"更新原卡片失败：{update_error}；重新发送也失败：{fallback_error}",
                    )

            else:
                sender = lambda timeout: send_lark_cli_card(config, notice, timeout)
        else:
            sender = lambda timeout: send_lark_cli_text(config, message, timeout)
    elif transport == "webhook":
        webhook_url = str(config.get("webhook_url") or "").strip()
        valid, detail = validate_webhook_url(webhook_url)
        sender = lambda timeout: post_feishu_text(webhook_url, message, timeout)
    else:
        return False, f"不支持的飞书发送方式：{transport}"
    if not valid:
        return False, detail
    try:
        timeout_seconds = max(3, min(30, int(config.get("send_timeout_seconds", 10))))
    except (TypeError, ValueError):
        timeout_seconds = 10
    try:
        retries = max(0, min(4, int(config.get("retries", 2))))
    except (TypeError, ValueError):
        retries = 2
    error = "未知发送错误"
    for attempt in range(retries + 1):
        ok, error = sender(timeout_seconds)
        if ok:
            return True, ""
        if attempt < retries:
            time.sleep(1 + attempt)
    return False, error


def validate_webhook_url(webhook_url: str) -> tuple[bool, str]:
    if not webhook_url:
        return False, "缺少飞书 Webhook；请运行 configure.py"
    try:
        parsed = urllib.parse.urlparse(webhook_url)
    except ValueError:
        return False, "飞书 Webhook 地址无效"
    allowed_hosts = {"open.feishu.cn", "open.larksuite.com"}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
    ):
        return False, "飞书 Webhook 地址无效"
    return True, ""


def validate_lark_cli_config(config: dict[str, Any]) -> tuple[bool, str]:
    executable = Path(str(config.get("lark_cli_path") or "")).expanduser()
    profile = str(config.get("lark_profile") or "").strip()
    chat_id = str(config.get("lark_chat_id") or "").strip()
    if not executable.is_file():
        return False, "没有找到飞书 CLI"
    if not profile:
        return False, "缺少飞书 CLI profile"
    if not chat_id.startswith("oc_"):
        return False, "飞书群 ID 无效"
    return True, ""


def parse_lark_cli_result(
    completed: subprocess.CompletedProcess[str],
) -> tuple[bool, str]:
    ok, error, _ = decode_lark_cli_result(completed)
    return ok, error


def decode_lark_cli_result(
    completed: subprocess.CompletedProcess[str],
) -> tuple[bool, str, dict[str, Any]]:
    raw = (completed.stdout or completed.stderr or "").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"飞书 CLI 返回无效响应：{compact_preview(raw, 300)}", {}
    if (
        completed.returncode == 0
        and isinstance(value, dict)
        and (value.get("ok") is True or value.get("code") == 0)
    ):
        return True, "", value
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error
        else:
            detail = value.get("msg") or error or value
    else:
        detail = value
    return (
        False,
        f"飞书 CLI 发送失败：{compact_preview(str(detail), 300)}",
        value if isinstance(value, dict) else {},
    )


def run_lark_cli(arguments: list[str], timeout_seconds: int) -> tuple[bool, str]:
    ok, error, _ = run_lark_cli_json(arguments, timeout_seconds)
    return ok, error


def run_lark_cli_json(
    arguments: list[str], timeout_seconds: int
) -> tuple[bool, str, dict[str, Any]]:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"飞书 CLI 执行失败：{compact_preview(str(exc), 300)}", {}
    return decode_lark_cli_result(completed)


def send_lark_cli_text(
    config: dict[str, Any], message: str, timeout_seconds: int
) -> tuple[bool, str]:
    executable = str(Path(str(config.get("lark_cli_path") or "")).expanduser())
    profile = str(config.get("lark_profile") or "").strip()
    chat_id = str(config.get("lark_chat_id") or "").strip()
    idempotency_key = (
        f"codex-{hashlib.sha256(message.encode('utf-8')).hexdigest()[:32]}"
    )
    arguments = [
        executable,
        "--profile",
        profile,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--text",
        message,
        "--idempotency-key",
        idempotency_key,
        "--format",
        "json",
    ]
    return run_lark_cli(arguments, timeout_seconds)


def card_template(config: dict[str, Any], notice: dict[str, Any]) -> tuple[str, str]:
    status = str(notice.get("status") or "completed")
    suffix = (
        status
        if status in {"completed", "incomplete", "interrupted", "running"}
        else "completed"
    )
    return (
        str(config.get(f"lark_{suffix}_template_id") or "").strip(),
        str(config.get(f"lark_{suffix}_template_version") or "").strip(),
    )


def card_variables(notice: dict[str, Any]) -> dict[str, str]:
    status = str(notice.get("status") or "completed")
    if status == "running":
        return {
            key: str(notice.get(key) or "")
            for key in (
                "task_name",
                "project_name",
                "model_label",
                "token_estimate",
                "started_at",
                "duration",
                "completed_at",
                "prompt",
                "result_summary",
            )
        }
    variables = {
        key: str(notice.get(key) or "")
        for key in (
            "task_name",
            "project_name",
            "model_label",
            "token_estimate",
            "started_at",
            "duration",
            "completed_at",
            "result_summary",
        )
    }
    if status == "incomplete":
        variables["failure_type"] = str(notice.get("failure_type") or "暂未完成")
    return variables


def template_card_content(
    config: dict[str, Any], notice: dict[str, Any]
) -> dict[str, Any] | None:
    template_id, template_version = card_template(config, notice)
    if not template_id:
        return None
    data: dict[str, Any] = {
        "template_id": template_id,
        "template_variable": card_variables(notice),
    }
    if template_version:
        data["template_version_name"] = template_version
    return {"type": "template", "data": data}


def notice_idempotency_key(notice: dict[str, Any], prefix: str, fallback: str) -> str:
    seed = str(notice.get("_event_key") or fallback)
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}-{digest[: max(8, 49 - len(prefix))]}"[:50]


def send_lark_cli_card(
    config: dict[str, Any], notice: dict[str, Any], timeout_seconds: int
) -> tuple[bool, str]:
    executable = str(Path(str(config.get("lark_cli_path") or "")).expanduser())
    profile = str(config.get("lark_profile") or "").strip()
    chat_id = str(config.get("lark_chat_id") or "").strip()
    content = codex_task_card_content(notice)
    serialized = json.dumps(
        content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    idempotency_key = notice_idempotency_key(notice, "codex-card", serialized)
    arguments = [
        executable,
        "--profile",
        profile,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--msg-type",
        "interactive",
        "--content",
        serialized,
        "--idempotency-key",
        idempotency_key,
        "--format",
        "json",
    ]
    return run_lark_cli(arguments, timeout_seconds)


def send_lark_cli_running_card(
    config: dict[str, Any], notice: dict[str, Any], timeout_seconds: int
) -> tuple[bool, str, str]:
    executable = str(Path(str(config.get("lark_cli_path") or "")).expanduser())
    profile = str(config.get("lark_profile") or "").strip()
    chat_id = str(config.get("lark_chat_id") or "").strip()
    content = codex_task_card_content(notice)
    serialized = json.dumps(
        content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    arguments = [
        executable,
        "--profile",
        profile,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--msg-type",
        "interactive",
        "--content",
        serialized,
        "--idempotency-key",
        notice_idempotency_key(notice, "codex-start", serialized),
        "--format",
        "json",
    ]
    ok, error, response = run_lark_cli_json(arguments, timeout_seconds)
    data = response.get("data") if isinstance(response, dict) else None
    message_id = str(data.get("message_id") or "") if isinstance(data, dict) else ""
    if ok and not message_id.startswith("om_"):
        return False, "飞书已接收执行中卡片，但没有返回消息 ID", ""
    return ok, error, message_id


def update_lark_cli_card(
    config: dict[str, Any],
    message_id: str,
    notice: dict[str, Any],
    timeout_seconds: int,
) -> tuple[bool, str]:
    if not message_id.startswith("om_"):
        return False, "执行中卡片消息 ID 无效"
    content = codex_task_card_content(notice)
    executable = str(Path(str(config.get("lark_cli_path") or "")).expanduser())
    profile = str(config.get("lark_profile") or "").strip()
    payload = {
        "content": json.dumps(
            content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    }
    arguments = [
        executable,
        "--profile",
        profile,
        "api",
        "PATCH",
        f"/open-apis/im/v1/messages/{message_id}",
        "--as",
        "bot",
        "--data",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "--format",
        "json",
    ]
    return run_lark_cli(arguments, timeout_seconds)


SENSITIVE_PROGRESS_PATTERN = re.compile(
    r"(?i)\b(app[_ -]?secret|access[_ -]?token|refresh[_ -]?token|"
    r"authorization|password|passwd|api[_ -]?key)\b\s*[:=]\s*[^\s,;]+"
)
OPAQUE_SECRET_PATTERN = re.compile(r"(?i)\b(?:sk|xox[baprs]|cli)_[a-z0-9_-]{8,}\b")


def redact_text(text: str) -> str:
    value = re.sub(
        r"""(?ix)(["']?(?:app[_\ -]?secret|access[_\ -]?token|refresh[_\ -]?token|authorization|password|passwd|api[_\ -]?key)["']?\s*[:=]\s*)(?:["'][^"']*["']|(?:Bearer\s+)?[^\s,;}]+)""",
        r"\1[已隐藏]",
        str(text),
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [已隐藏]", value)
    value = re.sub(
        r"https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[^\s\"'<>]+",
        "[Webhook 已隐藏]",
        value,
    )
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[私钥已隐藏]",
        value,
        flags=re.S,
    )
    return OPAQUE_SECRET_PATTERN.sub("[已隐藏]", value)


def sanitize_notice(notice: Any, config: dict[str, Any] | None = None) -> Any:
    if isinstance(notice, str):
        return redact_text(notice)
    if isinstance(notice, list):
        return [sanitize_notice(value) for value in notice]
    if not isinstance(notice, dict):
        return notice
    result = {
        key: (value if key.startswith("_") else sanitize_notice(value))
        for key, value in notice.items()
    }
    config = config or {}
    excluded = config.get("summary_excluded_projects", [])
    hide = (
        notice.get("project_name") in excluded if isinstance(excluded, list) else False
    )
    if hide or not config.get("include_result_summary", True):
        if hide or not config.get("include_last_message", False):
            result["result_summary"] = "内容未附带，请在 Codex 查看本轮回复。"
    if hide:
        result.update(prompt="内容未附带", current_step="运行中", recent_steps=[])
    return result


def delivery_event(event: dict[str, Any]) -> dict[str, Any]:
    # Never persist the raw request, assistant output, secrets or transcript path
    # in routing metadata. Filtering has already happened at the observer.
    return {key: event[key] for key in ("session_id", "turn_id") if key in event}


def safe_progress_text(text: str, limit: int = 180) -> str:
    """Make a concise progress line without forwarding commands or credentials."""
    value = redact_text(text).replace("\x00", " ")
    value = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    value = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"https?://\S+", "链接", value)
    value = SENSITIVE_PROGRESS_PATTERN.sub(r"\1=[已隐藏]", value)
    value = OPAQUE_SECRET_PATTERN.sub("[已隐藏]", value)
    value = re.sub(r"^#{1,6}\s*", "", value.strip())
    value = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)", "", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return compact_preview(value, max(40, min(240, limit))).strip("；;，,。 ")


def record_turn_progress(
    event: dict[str, Any],
    data_dir: Path,
    step: str,
    observed_at: float | None = None,
) -> dict[str, Any]:
    cleaned = safe_progress_text(step)
    if not cleaned:
        return read_turn_card_state(event, data_dir)
    epoch = float(observed_at) if isinstance(observed_at, (int, float)) else time.time()
    with state_lock(data_dir):
        state = read_turn_card_state(event, data_dir)
        if bool(state.get("terminal")):
            return state
        state.update(
            {
                "session_id": str(
                    event.get("session_id") or state.get("session_id") or ""
                ),
                "turn_id": str(event.get("turn_id") or state.get("turn_id") or ""),
                "started_at": state.get("started_at")
                or event.get("started_at")
                or epoch,
            }
        )
        if cleaned != str(state.get("latest_step") or ""):
            recent = state.get("recent_steps")
            if not isinstance(recent, list):
                recent = []
            recent.append(
                {
                    "at": dt.datetime.fromtimestamp(epoch)
                    .astimezone()
                    .strftime("%H:%M"),
                    "text": cleaned,
                }
            )
            state["recent_steps"] = recent[-10:]
            state["latest_step"] = cleaned
            state["step_count"] = max(0, int(state.get("step_count") or 0)) + 1
            state["progress_dirty_at"] = epoch
        _write_turn_card_state(event, data_dir, state)
    return state


def turn_progress_snapshot(event: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    state = read_turn_card_state(event, data_dir)
    recent = state.get("recent_steps")
    if not isinstance(recent, list):
        recent = []
    return {
        "current_step": str(state.get("latest_step") or "已接收任务，正在准备执行"),
        "recent_steps": recent[-10:],
        "step_count": str(max(0, int(state.get("step_count") or 0))),
        "prompt": str(state.get("prompt") or ""),
    }


def attach_turn_progress(
    notice: dict[str, Any], event: dict[str, Any], data_dir: Path
) -> dict[str, Any]:
    snapshot = turn_progress_snapshot(event, data_dir)
    notice.update(
        {
            "current_step": snapshot["current_step"],
            "recent_steps": snapshot["recent_steps"],
            "step_count": snapshot["step_count"],
            "prompt": snapshot["prompt"]
            or notice.get("prompt")
            or "执行本轮 Codex 任务。",
        }
    )
    return notice


def save_turn_card_context(
    event: dict[str, Any], data_dir: Path, notice: dict[str, Any]
) -> None:
    with state_lock(data_dir):
        state = read_turn_card_state(event, data_dir)
        prompt = str(notice.get("prompt") or "").strip()
        if prompt:
            state["prompt"] = prompt
        _write_turn_card_state(event, data_dir, state)


def mark_turn_terminal(event: dict[str, Any], data_dir: Path, status: str) -> None:
    with state_lock(data_dir):
        state = read_turn_card_state(event, data_dir)
        state["terminal"] = True
        state["terminal_status"] = status
        state["terminal_at"] = time.time()
        _write_turn_card_state(event, data_dir, state)


def close_short_turn(event: dict[str, Any], data_dir: Path) -> bool:
    """Suppress a short task only when no running card is visible or in flight."""
    with state_lock(data_dir):
        state = read_turn_card_state(event, data_dir)
        if running_message_id(event, data_dir) or state.get("running_send_started_at"):
            return False
        state.update(
            terminal=True,
            terminal_status=event.get("notification_status", "completed"),
            terminal_at=time.time(),
        )
        _write_turn_card_state(event, data_dir, state)
        return True


def refresh_running_card(
    event: dict[str, Any],
    data_dir: Path,
    step: str = "",
    observed_at: float | None = None,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    """Persist a new step and refresh the shared running card when due."""
    state = (
        record_turn_progress(event, data_dir, step, observed_at)
        if step
        else read_turn_card_state(event, data_dir)
    )
    config, _ = load_config()
    if (
        not bool(config.get("enabled", True))
        or not bool(config.get("live_progress_notifications", True))
        or not bool(config.get("card_notifications", True))
        or str(config.get("transport") or "").strip().lower() != "lark-cli"
        or bool(state.get("terminal"))
        or should_suppress_notification(event, config, data_dir)
    ):
        return True, ""
    message_id = running_message_id(event, data_dir)
    if not message_id:
        return True, ""
    now = time.time()
    try:
        update_interval = max(
            5, min(60, int(config.get("progress_update_interval_seconds", 12)))
        )
    except (TypeError, ValueError):
        update_interval = 12
    try:
        heartbeat = max(
            update_interval,
            min(300, int(config.get("progress_heartbeat_seconds", 30))),
        )
    except (TypeError, ValueError):
        heartbeat = 30
    last_update = max(
        float(state.get("last_card_update_at") or 0),
        float(state.get("last_card_queued_at") or 0),
    )
    dirty_at = float(state.get("progress_dirty_at") or 0)
    if not force:
        if now - last_update < update_interval:
            return True, ""
        if dirty_at <= last_update and now - last_update < heartbeat:
            return True, ""
    started_at = turn_started_at(event, data_dir)
    notice = build_running_notice(event, config, started_at)
    snapshot = turn_progress_snapshot(event, data_dir)
    notice.update(snapshot)
    notice["_event_key"] = event_key(event)
    notice["_event"] = delivery_event(event)
    notice["_delivery_kind"] = "progress"
    enqueue_notice(
        data_dir, "progress:" + event_key(event), sanitize_notice(notice, config)
    )
    with state_lock(data_dir):
        latest = read_turn_card_state(event, data_dir)
        latest["last_card_queued_at"] = now
        _write_turn_card_state(event, data_dir, latest)
    ensure_delivery_worker(data_dir)
    return True, ""


def finish_reply_text(notice: dict[str, Any]) -> str:
    status = str(notice.get("status") or "completed")
    task_name = str(notice.get("task_name") or "Codex 任务")
    if status == "interrupted":
        return f"⏹️「{task_name}」已中断，当前进展已刷新到上方卡片。"
    if status == "incomplete":
        return f"⚠️「{task_name}」需要处理，详情已刷新到上方卡片。"
    return f"✅「{task_name}」本轮已结束，结果已刷新到上方卡片。"


def send_lark_cli_finish_reply(
    config: dict[str, Any],
    message_id: str,
    notice: dict[str, Any],
    timeout_seconds: int,
) -> tuple[bool, str]:
    executable = str(Path(str(config.get("lark_cli_path") or "")).expanduser())
    profile = str(config.get("lark_profile") or "").strip()
    text = finish_reply_text(notice)
    arguments = [
        executable,
        "--profile",
        profile,
        "im",
        "+messages-reply",
        "--as",
        "bot",
        "--message-id",
        message_id,
        "--text",
        text,
        "--idempotency-key",
        notice_idempotency_key(notice, "codex-finish", text),
        "--format",
        "json",
    ]
    return run_lark_cli(arguments, timeout_seconds)


def post_feishu_text(
    webhook_url: str, message: str, timeout_seconds: int
) -> tuple[bool, str]:
    payload = {
        "msg_type": "text",
        "content": {"text": message},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > 20 * 1024:
        return False, "飞书通知超过 20 KB 限制"
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(64 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        return False, f"飞书 HTTP {exc.code}: {compact_preview(raw, 300)}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"飞书网络错误：{compact_preview(str(reason), 300)}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"飞书返回无效响应：{compact_preview(raw, 300)}"
    if isinstance(value, dict) and (
        value.get("code") == 0 or value.get("StatusCode") == 0
    ):
        return True, ""
    if isinstance(value, dict):
        detail = value.get("msg") or value.get("StatusMessage") or value
    else:
        detail = value
    return False, f"飞书拒绝发送：{compact_preview(str(detail), 300)}"


def queue_path(data_dir: Path) -> Path:
    # Keep the former WeChat queue untouched so migration never floods Feishu
    # with historical notifications.
    return data_dir / "feishu_pending.json"


def ledger_path(data_dir: Path) -> Path:
    return data_dir / "feishu_sent.json"


def load_queue(data_dir: Path) -> list[dict[str, Any]]:
    value = read_json(queue_path(data_dir), [])
    if not isinstance(value, list) or any(
        not isinstance(item, dict) or not item.get("key") for item in value
    ):
        raise OSError("通知队列格式无效，已保留原文件")
    return value


def load_ledger(data_dir: Path) -> dict[str, float]:
    value = read_json(ledger_path(data_dir), {})
    if not isinstance(value, dict):
        raise OSError("通知发送记录格式无效，已保留原文件")
    now = time.time()
    return {
        str(key): float(timestamp)
        for key, timestamp in value.items()
        if isinstance(timestamp, (int, float)) and now - float(timestamp) < 30 * 86400
    }


def enqueue_notice(data_dir: Path, key: str, notice: str | dict[str, Any]) -> None:
    delivery.enqueue(sys.modules[__name__], data_dir, key, notice)


def drain_queue(config: dict[str, Any], data_dir: Path) -> tuple[int, int, str]:
    return delivery.drain(sys.modules[__name__], config, data_dir)


def ensure_delivery_worker(data_dir: Path) -> None:
    delivery.ensure_worker(sys.modules[__name__], data_dir)


def handle_hook(event: dict[str, Any]) -> int:
    data_dir = resolve_data_dir()
    hook_event = str(event.get("hook_event_name") or "")
    if hook_event == "Interrupt":
        event = dict(event, hook_event_name="Stop", notification_status="interrupted")
        hook_event = "Stop"
    if hook_event == "SessionStart":
        try:
            record_session_start(event, data_dir)
        except OSError as exc:
            log(f"无法记录任务开始时间：{exc}")
        ensure_interruption_watcher(data_dir)
        ensure_delivery_worker(data_dir)
        return 0
    if hook_event == "UserPromptSubmit":
        try:
            record_turn_start(event, data_dir)
        except (OSError, TimeoutError) as exc:
            log(f"无法记录本轮开始时间：{exc}")
        ensure_interruption_watcher(data_dir)
        config, _ = load_config()
        if (
            not bool(config.get("enabled", True))
            or not bool(config.get("notify_on_start", True))
            or str(config.get("transport") or "").strip().lower() != "lark-cli"
            or not bool(config.get("card_notifications", True))
            or should_suppress_notification(event, config, data_dir)
        ):
            return 0
        notice = build_running_notice(event, config, turn_started_at(event, data_dir))
        try:
            save_turn_card_context(event, data_dir, notice)
        except (OSError, TimeoutError) as exc:
            log(f"无法保存执行中卡片上下文：{exc}")
        notice["_event_key"] = event_key(event)
        notice["_event"] = delivery_event(event)
        notice["_delivery_kind"] = "start"
        try:
            enqueue_notice(
                data_dir, "start:" + event_key(event), sanitize_notice(notice, config)
            )
            ensure_delivery_worker(data_dir)
        except (OSError, TimeoutError) as exc:
            log(f"无法登记执行中通知：{exc}")
        return 0
    if hook_event != "Stop":
        return 0
    config, loaded_path = load_config()
    if not bool(config.get("enabled", True)):
        return 0
    if should_suppress_notification(event, config, data_dir):
        log("已忽略 Codex 后台内部会话")
        return 0
    duration_seconds = event_duration_seconds(event)
    if duration_seconds is None:
        duration_seconds = turn_duration_seconds(event, data_dir)
    if duration_seconds is None:
        duration_seconds = session_duration_seconds(event, data_dir)
    try:
        minimum = max(0, int(config.get("min_duration_seconds", 0)))
    except (TypeError, ValueError):
        minimum = 0
    if (
        duration_seconds is not None
        and duration_seconds < minimum
        and close_short_turn(event, data_dir)
    ):
        return 0
    notice = build_notice(
        event,
        config,
        duration_seconds,
        started_at_epoch=turn_started_at(event, data_dir),
    )
    notice = attach_turn_progress(notice, event, data_dir)
    key = event_key(event)
    notice["_event_key"] = key
    notice["_event"] = delivery_event(event)
    active_message_id = running_message_id(event, data_dir)
    if active_message_id:
        notice["_running_message_id"] = active_message_id
    try:
        enqueue_notice(data_dir, key, sanitize_notice(notice, config))
        ensure_delivery_worker(data_dir)
    except (OSError, TimeoutError) as exc:
        log(f"无法更新重试队列：{exc}")
        return 0
    log(f"完成通知已持久化 key={key}")
    return 0


def read_hook_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        log(f"忽略无效的钩子输入：{exc}")
        return {}
    return value if isinstance(value, dict) else {}


def command_send_test(config: dict[str, Any], data_dir: Path) -> int:
    now = time.time_ns()
    event = {
        "session_id": f"manual-test-{now}",
        "turn_id": f"manual-test-{now}",
        "cwd": str(Path.cwd()),
        "last_assistant_message": "飞书公司应用机器人通知链路测试成功。",
    }
    test_config = dict(config)
    test_config["title"] = "🧪 Codex 飞书通知测试"
    test_config["include_last_message"] = True
    enqueue_notice(data_dir, event_key(event), build_notice(event, test_config, 0))
    sent, pending, error = drain_queue(config, data_dir)
    if sent:
        print("测试通知已发送。")
        return 0
    print(f"测试通知发送失败，已排队（{pending} 条）：{error}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "hook",
            "send-test",
            "flush",
            "dry-run",
            "show-config",
            "status",
            "retry-failed",
        ),
        nargs="?",
        default="hook",
    )
    args = parser.parse_args()
    if args.command == "hook":
        return handle_hook(read_hook_event())
    config, loaded_path = load_config()
    data_dir = resolve_data_dir()
    if args.command == "status":
        print(
            json.dumps(
                delivery.health(sys.modules[__name__], config, data_dir),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "retry-failed":
        count = delivery.retry_failed(sys.modules[__name__], data_dir)
        ensure_delivery_worker(data_dir)
        print(json.dumps({"requeued": count, "enabled": bool(config.get("enabled"))}))
        return 0
    if args.command == "send-test":
        return command_send_test(config, data_dir)
    if args.command == "flush":
        sent, pending, error = drain_queue(config, data_dir)
        print(
            json.dumps(
                {"sent": sent, "pending": pending, "error": error}, ensure_ascii=False
            )
        )
        return 0 if not error else 1
    if args.command == "show-config":
        visible_config = dict(config)
        if visible_config.get("webhook_url"):
            visible_config["webhook_url"] = (
                "https://open.feishu.cn/open-apis/bot/v2/hook/***"
            )
        print(
            json.dumps(
                {
                    "path": str(loaded_path) if loaded_path else None,
                    "config": visible_config,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    event = {
        "cwd": str(Path.cwd()),
        "last_assistant_message": "示例：Codex 已完成实现与验证。",
    }
    print(build_message(event, config, 3661))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
