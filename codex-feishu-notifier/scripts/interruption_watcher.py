#!/usr/bin/env python3
"""Watch Codex rollout files as a fallback for unreliable lifecycle hooks."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import notifier

POLL_SECONDS = 1.0
LAUNCH_LABEL = "com.codex.feishu-notifier.watcher"


def sessions_root() -> Path:
    explicit = os.environ.get("CODEX_SESSIONS_DIR")
    if explicit:
        return Path(explicit).expanduser()
    codex_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    return codex_home / "sessions"


def offsets_path(data_dir: Path) -> Path:
    return data_dir / "interruption_watcher_offsets.json"


def pid_path(data_dir: Path) -> Path:
    return data_dir / "interruption_watcher.pid"


def lock_path(data_dir: Path) -> Path:
    return data_dir / "interruption_watcher.lock"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def launch_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCH_LABEL}"


def discover_rollouts(root: Path) -> list[Path]:
    try:
        return sorted(path for path in root.rglob("*.jsonl") if path.is_file())
    except OSError:
        return []


def latest_recent_completion(
    root: Path, max_age_seconds: int = 3600
) -> tuple[Path, dict[str, Any]] | None:
    now = time.time()
    latest: tuple[float, Path, dict[str, Any]] | None = None
    paths = discover_rollouts(root)
    paths.sort(
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    for path in paths[:50]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        not isinstance(record, dict)
                        or record.get("type") != "event_msg"
                    ):
                        continue
                    payload = record.get("payload")
                    if (
                        not isinstance(payload, dict)
                        or payload.get("type") != "task_complete"
                    ):
                        continue
                    completed_at = payload.get("completed_at")
                    if not isinstance(completed_at, (int, float)):
                        completed_at = record_epoch(record)
                    if not isinstance(completed_at, (int, float)):
                        continue
                    if now - float(completed_at) > max_age_seconds:
                        continue
                    if latest is None or float(completed_at) > latest[0]:
                        latest = (float(completed_at), path, dict(payload))
        except OSError:
            continue
    return (latest[1], latest[2]) if latest is not None else None


def load_offsets(data_dir: Path) -> tuple[dict[str, int], bool]:
    value = notifier.read_json(offsets_path(data_dir), {})
    if not isinstance(value, dict):
        return {}, False
    files = value.get("files")
    if not isinstance(files, dict):
        return {}, False
    offsets = {
        str(path): int(offset)
        for path, offset in files.items()
        if isinstance(offset, int) and offset >= 0
    }
    return offsets, bool(value.get("initialized"))


def save_offsets(data_dir: Path, offsets: dict[str, int]) -> None:
    notifier.atomic_write_json(
        offsets_path(data_dir),
        {"initialized": True, "files": offsets, "updated_at": time.time()},
    )


def initialize_offsets(root: Path, data_dir: Path) -> dict[str, int]:
    offsets, initialized = load_offsets(data_dir)
    if initialized:
        return offsets
    for path in discover_rollouts(root):
        with contextlib.suppress(OSError):
            offsets[str(path)] = path.stat().st_size
    save_offsets(data_dir, offsets)
    return offsets


def rollout_session_metadata(path: Path) -> dict[str, str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= 80:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    break
                return {
                    "session_id": str(
                        payload.get("session_id") or payload.get("id") or ""
                    ),
                    "cwd": str(payload.get("cwd") or ""),
                }
    except OSError:
        pass
    return {"session_id": "", "cwd": ""}


def lifecycle_event(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    metadata = rollout_session_metadata(path)
    return {
        "session_id": metadata["session_id"],
        "turn_id": str(payload.get("turn_id") or ""),
        "cwd": metadata["cwd"],
        "transcript_path": str(path),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "duration_ms": payload.get("duration_ms"),
    }


def record_epoch(record: dict[str, Any]) -> float | None:
    value = record.get("timestamp")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_open_task(path: Path) -> dict[str, Any]:
    """Find the latest started turn that has not reached a terminal event."""
    active: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "task_started":
                    active = dict(payload)
                elif event_type == "user_message" and active:
                    active["message"] = str(payload.get("message") or "")
                elif event_type in {"task_complete", "turn_aborted"}:
                    if str(payload.get("turn_id") or "") == str(
                        active.get("turn_id") or ""
                    ):
                        active = {}
    except OSError:
        return {}
    return active


def send_task_start(path: Path, payload: dict[str, Any]) -> None:
    event = lifecycle_event(path, payload)
    event.update(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": str(payload.get("message") or payload.get("prompt") or ""),
        }
    )
    notifier.handle_hook(event)


def send_task_completion(path: Path, payload: dict[str, Any]) -> None:
    event = lifecycle_event(path, payload)
    event.update(
        {
            "hook_event_name": "Stop",
            "last_assistant_message": str(payload.get("last_agent_message") or ""),
        }
    )
    notifier.handle_hook(event)


def _assistant_progress_text(payload: dict[str, Any]) -> str:
    if payload.get("role") != "assistant" or payload.get("phase") != "commentary":
        return ""
    content = payload.get("content")
    texts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return notifier.safe_progress_text(" ".join(texts))


def _tool_progress_text(payload: dict[str, Any]) -> str:
    name = str(payload.get("name") or "").strip().lower()
    namespace = str(payload.get("namespace") or "").strip().lower()
    qualified = f"{namespace}.{name}" if namespace else name
    if not qualified:
        return ""
    if "apply_patch" in qualified:
        return "正在更新项目文件"
    if any(marker in qualified for marker in ("imagegen", "image_gen")):
        return "正在生成或调整图片"
    if "view_image" in qualified:
        return "正在检查图片内容"
    if namespace == "web" or "search_query" in qualified or "browser" in qualified:
        return "正在查询并核对资料"
    if any(marker in qualified for marker in ("update_plan", "request_user_input")):
        return "正在整理任务计划和下一步"
    if any(marker in qualified for marker in ("wait", "write_stdin")):
        return "正在等待后台任务并读取结果"
    if any(marker in qualified for marker in ("exec", "terminal", "command")):
        return "正在运行本地检查或构建"
    if any(marker in qualified for marker in ("lark", "feishu", "message")):
        return "正在处理飞书通知"
    return "正在执行下一项任务操作"


def progress_step_from_record(record: dict[str, Any]) -> str:
    if record.get("type") != "response_item":
        return ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    payload_type = str(payload.get("type") or "")
    if payload_type == "message":
        return _assistant_progress_text(payload)
    if payload_type in {
        "function_call",
        "custom_tool_call",
        "computer_call",
        "web_search_call",
        "mcp_call",
    }:
        return _tool_progress_text(payload)
    return ""


def send_task_progress(
    path: Path,
    payload: dict[str, Any],
    step: str,
    observed_at: float | None,
    data_dir: Path,
) -> None:
    event = lifecycle_event(path, payload)
    event["prompt"] = str(payload.get("message") or "")
    ok, error = notifier.refresh_running_card(
        event,
        data_dir,
        step,
        observed_at,
    )
    if not ok and error:
        notifier.log(f"实时进度卡片更新失败：{error}")


def interruption_event(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    event = lifecycle_event(path, payload)
    event["notification_status"] = "interrupted"
    return event


def duration_seconds(payload: dict[str, Any]) -> int | None:
    duration_ms = payload.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        return max(0, int(float(duration_ms) / 1000))
    started_at = payload.get("started_at")
    completed_at = payload.get("completed_at")
    if isinstance(started_at, (int, float)) and isinstance(completed_at, (int, float)):
        return max(0, int(float(completed_at) - float(started_at)))
    return None


def send_interruption(path: Path, payload: dict[str, Any], data_dir: Path) -> None:
    config, loaded_path = notifier.load_config()
    if not bool(config.get("enabled", True)):
        return
    elapsed = duration_seconds(payload)
    try:
        minimum = max(0, int(config.get("min_duration_seconds", 0)))
    except (TypeError, ValueError):
        minimum = 0
    event = interruption_event(path, payload)
    if (
        elapsed is not None
        and elapsed < minimum
        and notifier.close_short_turn(event, data_dir)
    ):
        return
    if notifier.should_suppress_notification(event, config, data_dir):
        return
    notice = notifier.build_notice(
        event,
        config,
        elapsed,
        started_at_epoch=event.get("started_at"),
    )
    notice = notifier.attach_turn_progress(notice, event, data_dir)
    key = notifier.event_key(event)
    notice["_event_key"] = key
    notice["_event"] = notifier.delivery_event(event)
    active_message_id = notifier.running_message_id(event, data_dir)
    if active_message_id:
        notice["_running_message_id"] = active_message_id
    try:
        notifier.enqueue_notice(data_dir, key, notifier.sanitize_notice(notice, config))
        notifier.ensure_delivery_worker(data_dir)
    except (OSError, TimeoutError) as exc:
        notifier.log(f"无法更新中断通知队列：{exc}")
        return
    notifier.log(f"中断通知已持久化 key={key}")


def scan_once(
    root: Path,
    offsets: dict[str, int],
    on_interruption: Callable[[Path, dict[str, Any]], None],
    on_start: Callable[[Path, dict[str, Any]], None] | None = None,
    on_complete: Callable[[Path, dict[str, Any]], None] | None = None,
    on_progress: (
        Callable[[Path, dict[str, Any], str, float | None], None] | None
    ) = None,
    *,
    process_new_files: bool = False,
    active_turns: dict[str, dict[str, Any]] | None = None,
    paths: list[Path] | None = None,
) -> bool:
    if active_turns is None:
        active_turns = {}
    changed = False
    for path in paths if paths is not None else discover_rollouts(root):
        key = str(path)
        try:
            size = path.stat().st_size
            new_file = key not in offsets
            if new_file and not process_new_files:
                # A resumed conversation can copy old interruption records into a
                # newly created rollout. Start at its current EOF so historical
                # events never produce fresh notifications.
                offsets[key] = size
                changed = True
                continue
            if new_file:
                # A resumed/forked rollout can copy recent completed turns. Do
                # not replay its initial contents. Recover only the currently
                # open turn so its future completion can still refresh a card.
                offsets[key] = size
                changed = True
                active = latest_open_task(path)
                started_at = active.get("started_at") if active else None
                if (
                    active
                    and isinstance(started_at, (int, float))
                    and time.time() - float(started_at) <= 60
                ):
                    active_turns[key] = active
                    if on_start is not None:
                        on_start(path, active)
                continue
            offset = offsets.get(key, 0)
            if size < offset:
                # The rollout was compacted or rewritten after we recorded its
                # position.  Its retained prefix can contain completed turns
                # from long ago, so replaying from byte zero would turn them
                # into fresh notifications.  Resume at EOF and wait only for
                # subsequently appended records.
                offsets[key] = size
                changed = True
                active_turns.pop(key, None)
                continue
            with path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    offset = handle.tell()
                    try:
                        record = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if record.get("type") == "response_item":
                        active = active_turns.get(key)
                        if not active:
                            active = latest_open_task(path)
                            if active:
                                active_turns[key] = active
                        step = progress_step_from_record(record)
                        if active and step and on_progress is not None:
                            on_progress(path, active, step, record_epoch(record))
                        continue
                    if record.get("type") != "event_msg":
                        continue
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        active_turns[key] = dict(payload)
                        continue
                    if event_type == "user_message":
                        active = active_turns.get(key) or latest_open_task(path)
                        if active:
                            active["message"] = str(payload.get("message") or "")
                            active_turns[key] = active
                            start_payload = dict(active)
                            if on_start is not None:
                                on_start(path, start_payload)
                        continue
                    if event_type == "task_complete":
                        if on_complete is not None:
                            on_complete(path, payload)
                        if str(payload.get("turn_id") or "") == str(
                            active_turns.get(key, {}).get("turn_id") or ""
                        ):
                            active_turns.pop(key, None)
                        continue
                    if (
                        event_type == "turn_aborted"
                        and payload.get("reason") == "interrupted"
                    ):
                        on_interruption(path, payload)
                        if str(payload.get("turn_id") or "") == str(
                            active_turns.get(key, {}).get("turn_id") or ""
                        ):
                            active_turns.pop(key, None)
        except (OSError, TimeoutError, ValueError, TypeError) as exc:
            notifier.log(f"watcher file={path.name} error={type(exc).__name__}: {exc}")
            continue
        if offsets.get(key) != offset:
            offsets[key] = offset
            changed = True
    return changed


def watcher_lock_is_held(data_dir: Path) -> bool:
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = lock_path(data_dir).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return True
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    return False


def _launchctl(
    arguments: list[str], timeout: int = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def write_launch_agent(data_dir: Path) -> Path:
    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
        ],
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(data_dir / "interruption_watcher.log"),
        "StandardErrorPath": str(data_dir / "interruption_watcher.log"),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
    return path


def stop_standalone_watcher(data_dir: Path) -> None:
    stored = notifier.read_json(pid_path(data_dir), {})
    pid = stored.get("pid") if isinstance(stored, dict) else None
    if isinstance(pid, int) and pid > 1:
        # A stale pid file must never terminate an unrelated reused PID.
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            )
            command = process.stdout.strip()
            if str(Path(__file__).resolve()) in command and command.endswith(" run"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while watcher_lock_is_held(data_dir) and time.monotonic() < deadline:
        time.sleep(0.1)


def install_watcher_service(data_dir: Path) -> tuple[bool, str]:
    if os.uname().sysname != "Darwin":
        return ensure_watcher(data_dir) == 0, "background watcher"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _launchctl(["bootout", launch_target()])
    stop_standalone_watcher(data_dir)
    path = write_launch_agent(data_dir)
    result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(path)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"无法安装通知监听服务：{detail[-400:]}"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _launchctl(["enable", launch_target()])
        _launchctl(["kickstart", "-k", launch_target()])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if watcher_lock_is_held(data_dir):
            return True, str(path)
        time.sleep(0.1)
    return False, "通知监听服务已安装，但进程尚未保持运行"


def ensure_watcher(data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    if watcher_lock_is_held(data_dir):
        return 0
    log_file = data_dir / "interruption_watcher.log"
    try:
        with log_file.open("a", encoding="utf-8") as stream:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "run"],
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
                close_fds=True,
            )
    except OSError as exc:
        notifier.log(f"无法启动中断监听器：{exc}")
        return 1
    return 0


def run_watcher(data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path(data_dir).open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return 0
    notifier.atomic_write_json(pid_path(data_dir), {"pid": os.getpid()})
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    root = sessions_root()
    offsets = initialize_offsets(root, data_dir)
    active_path = data_dir / "watcher_active.json"
    restored = notifier.read_json(active_path, {})
    active_turns: dict[str, dict[str, Any]] = (
        restored if isinstance(restored, dict) else {}
    )
    active_turns = {
        path: payload
        for path, payload in active_turns.items()
        if isinstance(payload, dict) and Path(path).is_file()
    }
    known_paths: list[Path] = []
    next_discovery = 0.0
    next_heartbeat_at = time.monotonic() + 10
    notifier.ensure_delivery_worker(data_dir)
    try:
        while running:
            # Discover history periodically, poll only recent/active files each
            # second. A resumed old task is picked up on the next discovery.
            if time.monotonic() >= next_discovery:
                known_paths = []
                for path in discover_rollouts(root):
                    with contextlib.suppress(OSError):
                        if (
                            path.stat().st_mtime > time.time() - 3600
                            or str(path) in active_turns
                        ):
                            known_paths.append(path)
                next_discovery = time.monotonic() + 15
            if scan_once(
                root,
                offsets,
                lambda path, payload: send_interruption(path, payload, data_dir),
                send_task_start,
                send_task_completion,
                lambda path, payload, step, observed_at: send_task_progress(
                    path, payload, step, observed_at, data_dir
                ),
                process_new_files=True,
                active_turns=active_turns,
                paths=known_paths,
            ):
                save_offsets(data_dir, offsets)
                notifier.atomic_write_json(
                    active_path, notifier.sanitize_notice(active_turns)
                )
            if time.monotonic() >= next_heartbeat_at:
                notifier.ensure_delivery_worker(data_dir)
                for path_string, payload in list(active_turns.items()):
                    send_task_progress(Path(path_string), payload, "", None, data_dir)
                next_heartbeat_at = time.monotonic() + 10
            time.sleep(POLL_SECONDS)
    finally:
        stored = notifier.read_json(pid_path(data_dir), {})
        if isinstance(stored, dict) and stored.get("pid") == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                pid_path(data_dir).unlink()
        lock_handle.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("ensure", "run", "status", "install", "replay-latest"),
    )
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    args = parser.parse_args()
    data_dir = notifier.resolve_data_dir()
    if args.command == "ensure":
        return ensure_watcher(data_dir)
    if args.command == "install":
        ok, detail = install_watcher_service(data_dir)
        print(json.dumps({"installed": ok, "detail": detail}, ensure_ascii=False))
        return 0 if ok else 1
    if args.command == "replay-latest":
        latest = latest_recent_completion(
            sessions_root(), max(60, args.max_age_seconds)
        )
        if latest is None:
            print(json.dumps({"replayed": False, "reason": "no recent completion"}))
            return 1
        send_task_completion(*latest)
        print(
            json.dumps(
                {
                    "replayed": True,
                    "turn_id": str(latest[1].get("turn_id") or ""),
                }
            )
        )
        return 0
    if args.command == "status":
        stored = notifier.read_json(pid_path(data_dir), {})
        pid = stored.get("pid") if isinstance(stored, dict) else None
        running = watcher_lock_is_held(data_dir)
        print(
            json.dumps(
                {
                    "running": running,
                    "pid": pid if running else None,
                    "launch_agent_installed": launch_agent_path().is_file(),
                }
            )
        )
        return 0 if running else 1
    return run_watcher(data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
