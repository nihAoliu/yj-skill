"""Durable single-writer delivery; lifecycle observers never perform network I/O."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def integer(config, key, default, low, high):
    try:
        return max(low, min(high, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


def enqueue(n, data_dir, key, notice):
    with n.state_lock(data_dir):
        ledger = n.load_ledger(data_dir)
        queue = n.load_queue(data_dir)
        failed = n.read_json(data_dir / "feishu_failed.json", [])
        if not isinstance(failed, list):
            raise OSError("失败通知记录格式无效")
        if any(item.get("key") == key for item in failed):
            return
        progress = (
            isinstance(notice, dict) and notice.get("_delivery_kind") == "progress"
        )
        if key in ledger or (
            not progress and any(item.get("key") == key for item in queue)
        ):
            return
        item = {"key": key, "created_at": time.time(), "attempts": 0}
        item["notice" if isinstance(notice, dict) else "message"] = n.sanitize_notice(
            notice
        )
        if isinstance(notice, dict):
            event = notice.get("_event") or {}
            kind = notice.get("_delivery_kind", "terminal")
            identity = n.turn_card_state_path(data_dir, event)
            if identity and kind == "terminal":
                state = n.read_turn_card_state(event, data_dir)
                if (
                    state.get("terminal_queued_key")
                    and state["terminal_queued_key"] != key
                ) or any(
                    x.get("terminal_identity") == str(identity) and x.get("key") != key
                    for x in queue
                ):
                    n.log(f"忽略同轮迟到终态 key={key}")
                    return
                # Queue is the write-ahead record. Worker also checks queued terminal
                # events, so a crash between these writes cannot regress a card.
                item["terminal_identity"] = str(identity)
            if kind == "start":
                config, _ = n.load_config()
                item["next_attempt_at"] = time.time() + integer(
                    config, "start_notice_delay_seconds", 10, 0, 300
                )
            if kind == "progress":
                # Keep only the freshest unsent snapshot; preserve retry backoff.
                previous = next((x for x in queue if x.get("key") == key), None)
                if previous:
                    previous["notice"] = item["notice"]
                    n.atomic_write_json(n.queue_path(data_dir), queue)
                    return
        queue.append(item)
        n.atomic_write_json(n.queue_path(data_dir), queue)
        if item.get("terminal_identity"):
            state = n.read_turn_card_state(event, data_dir)
            state.update(
                terminal=True,
                terminal_status=notice.get("status"),
                terminal_at=time.time(),
                terminal_queued_key=key,
            )
            n._write_turn_card_state(event, data_dir, state)


def pending_terminal(n, data_dir, event):
    identity = n.turn_card_state_path(data_dir, event)
    return bool(
        identity
        and any(
            x.get("terminal_identity") == str(identity) for x in n.load_queue(data_dir)
        )
    )


def deliver(n, config, data_dir, item):
    payload = item.get("notice", item.get("message", ""))
    if not isinstance(payload, dict):
        return n.send_notice(dict(config, retries=0), payload)
    payload = dict(payload)
    event = payload.get("_event") or {}
    kind = payload.get("_delivery_kind", "terminal")
    if event and n.should_suppress_notification(event, config, data_dir):
        return True, "skipped:filtered"
    state = n.read_turn_card_state(event, data_dir)
    if kind in {"start", "progress"} and (
        state.get("terminal") or pending_terminal(n, data_dir, event)
    ):
        return True, "skipped:terminal-barrier"
    message_id = n.running_message_id(event, data_dir) or payload.get(
        "_running_message_id", ""
    )
    timeout = integer(config, "send_timeout_seconds", 10, 3, 30)
    payload = n.sanitize_notice(payload, config)
    if kind == "start":
        if (
            message_id
            or not config.get("notify_on_start", True)
            or not config.get("card_notifications", True)
            or config.get("transport") != "lark-cli"
        ):
            return True, "skipped:start-disabled-or-existing"
        with n.state_lock(data_dir):
            latest = n.read_turn_card_state(event, data_dir)
            if latest.get("terminal") or pending_terminal(n, data_dir, event):
                return True, "skipped:terminal-barrier"
            latest["running_send_started_at"] = time.time()
            n._write_turn_card_state(event, data_dir, latest)
        started = state.get("started_at")
        if isinstance(started, (int, float)):
            payload["duration"] = n.format_duration(max(0, int(time.time() - started)))
        payload.update(n.turn_progress_snapshot(event, data_dir))
        payload = n.sanitize_notice(payload, config)
        ok, error, message_id = n.send_lark_cli_running_card(config, payload, timeout)
        if ok and not message_id:
            return False, "飞书响应缺少 message_id"
        n.save_running_message_id(event, data_dir, message_id if ok else "", error)
        return ok, error
    if kind == "progress":
        if (
            not message_id
            or not config.get("live_progress_notifications", True)
            or not config.get("card_notifications", True)
            or config.get("transport") != "lark-cli"
        ):
            return True, "skipped:progress-disabled-or-no-card"
        ok, error = n.update_lark_cli_card(config, message_id, payload, timeout)
        with n.state_lock(data_dir):
            latest = n.read_turn_card_state(event, data_dir)
            latest["last_card_attempt_at"] = time.time()
            if ok:
                latest["last_card_update_at"] = time.time()
                latest.pop("last_card_error", None)
            else:
                latest["last_card_error"] = n.redact_text(error)
            n._write_turn_card_state(event, data_dir, latest)
        return ok, error
    if message_id:
        payload["_running_message_id"] = message_id
    return n.send_notice(dict(config, retries=0), payload)


def drain(n, config, data_dir):
    if not config.get("enabled", True):
        return 0, len(n.load_queue(data_dir)), "通知已关闭"
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = os.fdopen(
        os.open(data_dir / ".sender.lock", os.O_CREAT | os.O_RDWR, 0o600), "a+"
    )
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0, len(n.load_queue(data_dir)), ""
        sent, error = 0, ""
        limit = integer(config, "max_pending_per_run", 10, 1, 50)
        with n.state_lock(data_dir):
            queue = n.load_queue(data_dir)
            ledger = n.load_ledger(data_dir)
            cleaned = [x for x in queue if x.get("key") not in ledger]
            if len(cleaned) != len(queue):
                queue = cleaned
                n.atomic_write_json(n.queue_path(data_dir), queue)
            # Repair terminal barrier from durable intent after a crash.
            for item in queue:
                notice = item.get("notice")
                if item.get("terminal_identity") and isinstance(notice, dict):
                    event = notice.get("_event") or {}
                    state = n.read_turn_card_state(event, data_dir)
                    if not state.get("terminal"):
                        state.update(
                            terminal=True,
                            terminal_at=time.time(),
                            terminal_status=notice.get("status"),
                            terminal_queued_key=item.get("key"),
                        )
                        n._write_turn_card_state(event, data_dir, state)
            ready = [
                x
                for x in queue
                if x.get("key") not in ledger
                and float(x.get("next_attempt_at") or 0) <= time.time()
            ][:limit]
        for item in ready:
            try:
                ok, detail = deliver(n, config, data_dir, item)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            key = item.get("key")
            with n.state_lock(data_dir):
                queue = n.load_queue(data_dir)
                ledger = n.load_ledger(data_dir)
                live = next((x for x in queue if x.get("key") == key), None)
                if live is None:
                    continue
                if ok:
                    # Commit dedupe first; stale queue entries are safe on restart.
                    is_progress = (
                        isinstance(item.get("notice"), dict)
                        and item["notice"].get("_delivery_kind") == "progress"
                    )
                    if not is_progress:
                        ledger[str(key)] = time.time()
                        n.atomic_write_json(n.ledger_path(data_dir), ledger)
                    # A newer progress snapshot queued in flight must survive.
                    if live == item:
                        queue.remove(live)
                    sent += int(not detail.startswith("skipped:"))
                else:
                    error = n.redact_text(detail)
                    attempts = int(live.get("attempts") or 0) + 1
                    live.update(
                        attempts=attempts, last_error=error, last_attempt_at=time.time()
                    )
                    delay = min(
                        integer(config, "retry_max_seconds", 900, 5, 86400),
                        integer(config, "retry_base_seconds", 5, 1, 300)
                        * 2 ** min(attempts - 1, 16),
                    )
                    live["next_attempt_at"] = time.time() + delay
                    if attempts >= integer(config, "max_delivery_attempts", 12, 1, 100):
                        failed_path = data_dir / "feishu_failed.json"
                        failed = n.read_json(failed_path, [])
                        if not isinstance(failed, list):
                            raise OSError("失败通知记录格式无效")
                        failed = [x for x in failed if x.get("key") != key]
                        failed.append(dict(live, failed_at=time.time()))
                        n.atomic_write_json(failed_path, failed)
                        queue.remove(live)
                n.atomic_write_json(n.queue_path(data_dir), queue)
            outcome = (
                "skipped"
                if ok and detail.startswith("skipped:")
                else ("sent" if ok else "retry")
            )
            n.log(f"delivery key={key} outcome={outcome} {detail}")
        return sent, len(n.load_queue(data_dir)), error
    finally:
        lock.close()


def ensure_worker(n, data_dir):
    config, _ = n.load_config()
    if not config.get("enabled", True):
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    probe = (data_dir / ".delivery-worker.lock").open("a+")
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        env = dict(os.environ, CODEX_FEISHU_NOTIFIER_DATA_DIR=str(data_dir))
        env["CODEX_FEISHU_NOTIFIER_CONFIG"] = str(n.global_config_path())
        # Release the probe before spawning, otherwise a fast child can see
        # its parent's probe as an existing worker and immediately exit.
        fcntl.flock(probe, fcntl.LOCK_UN)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        probe.close()


def run(n):
    data_dir = n.resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".delivery-worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        alive = True

        def stop(*_):
            nonlocal alive
            alive = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        next_cleanup = 0
        while alive:
            config, _ = n.load_config()
            if not config.get("enabled", True):
                return 0
            try:
                log_path = data_dir / "delivery.log"
                if log_path.exists() and log_path.stat().st_size > 2_000_000:
                    os.replace(log_path, data_dir / "delivery.log.1")
                with (
                    log_path.open("a", encoding="utf-8") as output,
                    contextlib.redirect_stderr(output),
                ):
                    os.chmod(log_path, 0o600)
                    # Reload the enable switch between individual deliveries.
                    drain(n, dict(config, max_pending_per_run=1), data_dir)
                    if time.time() >= next_cleanup:
                        cleanup(n, config, data_dir)
                        next_cleanup = time.time() + 86400
                n.atomic_write_json(
                    data_dir / "delivery_health.json",
                    {"pid": os.getpid(), "heartbeat_at": time.time()},
                )
            except Exception as exc:
                n.atomic_write_json(
                    data_dir / "delivery_health.json",
                    {
                        "pid": os.getpid(),
                        "heartbeat_at": time.time(),
                        "error": n.redact_text(str(exc)),
                    },
                )
            time.sleep(1)
    return 0


def cleanup(n, config, data_dir):
    """Archive only expired terminal records, preserving active and pending tasks."""
    cutoff = time.time() - integer(config, "state_retention_days", 30, 7, 3650) * 86400
    with n.state_lock(data_dir):
        pending = {x.get("terminal_identity") for x in n.load_queue(data_dir)}
        for path in (data_dir / "turn_cards").glob("*.json"):
            state = n.read_json(path, {})
            if (
                isinstance(state, dict)
                and state.get("terminal")
                and float(state.get("terminal_at") or time.time()) < cutoff
                and str(path) not in pending
            ):
                archive = data_dir / "archive" / "turn_cards" / path.name
                archive.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, archive)


def health(n, config, data_dir):
    """Read-only diagnostics: no starts, sends, token calls or state writes."""
    result = {
        "enabled": bool(config.get("enabled")),
        "transport": config.get("transport"),
        "worker": n.read_json(data_dir / "delivery_health.json", {}),
    }
    try:
        queue = n.load_queue(data_dir)
        failed = n.read_json(data_dir / "feishu_failed.json", [])
        result.update(
            pending=len(queue),
            failed=len(failed),
            oldest_pending_seconds=int(
                max(
                    [
                        time.time() - float(x.get("created_at") or time.time())
                        for x in queue
                    ]
                    or [0]
                )
            ),
            recent_errors=[
                {
                    "key": x.get("key"),
                    "attempts": x.get("attempts"),
                    "error": n.redact_text(x.get("last_error", "")),
                }
                for x in queue
                if x.get("last_error")
            ][-5:],
        )
        heartbeat = result["worker"].get("heartbeat_at", 0)
        result["worker_recent"] = time.time() - float(heartbeat) < 120
    except (OSError, ValueError, TypeError) as exc:
        result["error"] = n.redact_text(str(exc))
    return result


def retry_failed(n, data_dir):
    with n.state_lock(data_dir):
        path = data_dir / "feishu_failed.json"
        failed = n.read_json(path, [])
        queue = n.load_queue(data_dir)
        ledger = n.load_ledger(data_dir)
        keys = {x.get("key") for x in queue} | set(ledger)
        count = 0
        for item in failed:
            if item.get("key") not in keys:
                queue.append(dict(item, attempts=0, next_attempt_at=0))
                keys.add(item.get("key"))
                count += 1
        n.atomic_write_json(n.queue_path(data_dir), queue)
        # Archive for diagnosis; no loss of failure details on manual retry.
        if failed:
            n.atomic_write_json(
                data_dir / "archive" / f"failed-{time.time_ns()}.json", failed
            )
        n.atomic_write_json(path, [])
        return count


if __name__ == "__main__":
    import notifier

    raise SystemExit(run(notifier))
