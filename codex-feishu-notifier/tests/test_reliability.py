from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import notifier as n
import delivery
import transcript_index
import configure


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(
            n.DEFAULT_CONFIG, transport="lark-cli", start_notice_delay_seconds=0
        )
        for name, value in (
            ("load_config", (self.config, None)),
            ("resolve_data_dir", self.root),
            ("session_is_ambient_suggestions_by_logs", False),
        ):
            p = mock.patch.object(n, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for name in ("ensure_delivery_worker", "ensure_interruption_watcher"):
            p = mock.patch.object(n, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(n, "send_notice", return_value=(True, ""))
        self.send = p.start()
        self.addCleanup(p.stop)
        transcript_index._CACHE.clear()
        self.event = {"session_id": "s", "turn_id": "t"}

    def enqueue(self, key="a", **fields):
        n.enqueue_notice(
            self.root,
            key,
            dict(status="completed", task_name="test", _event=self.event, **fields),
        )

    def test_disabled_drain_never_sends(self):
        self.enqueue()
        self.assertEqual(
            0, n.drain_queue(dict(self.config, enabled=False), self.root)[0]
        )
        self.send.assert_not_called()
        self.assertEqual(1, len(n.load_queue(self.root)))

    def test_retry_does_not_block_other_tasks(self):
        n.enqueue_notice(self.root, "a", "first")
        n.enqueue_notice(self.root, "b", "second")
        self.send.side_effect = [(False, "offline"), (True, "")]
        sent, pending, _ = n.drain_queue(self.config, self.root)
        self.assertEqual((1, 1), (sent, pending))
        self.assertGreater(n.load_queue(self.root)[0]["next_attempt_at"], n.time.time())
        self.send.reset_mock()
        n.drain_queue(self.config, self.root)
        self.send.assert_not_called()

    def test_network_call_does_not_hold_state_lock(self):
        n.enqueue_notice(self.root, "a", "first")

        def send(*_):
            n.enqueue_notice(self.root, "b", "second")
            return True, ""

        self.send.side_effect = send
        n.drain_queue(self.config, self.root)
        self.assertEqual(["b"], [x["key"] for x in n.load_queue(self.root)])

    def test_single_sender_serializes_network(self):
        n.enqueue_notice(self.root, "a", "first")
        entered, release = threading.Event(), threading.Event()

        def send(*_):
            entered.set()
            release.wait(2)
            return True, ""

        self.send.side_effect = send
        worker = threading.Thread(target=n.drain_queue, args=(self.config, self.root))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(0, n.drain_queue(self.config, self.root)[0])
        finally:
            release.set()
            worker.join(2)
        self.send.assert_called_once()

    def test_queue_preserves_more_than_100(self):
        for number in range(105):
            n.enqueue_notice(self.root, str(number), "message")
        self.assertEqual(105, len(n.load_queue(self.root)))

    def test_dead_letter_preserved_and_explicitly_retried(self):
        self.enqueue()
        self.send.return_value = False, "app_secret=super-private"
        n.drain_queue(dict(self.config, max_delivery_attempts=1), self.root)
        failed = n.read_json(self.root / "feishu_failed.json", [])
        self.assertEqual(1, len(failed))
        self.assertNotIn("super-private", json.dumps(failed))
        self.enqueue()  # Repeated lifecycle events cannot restart exhausted jobs.
        self.assertEqual([], n.load_queue(self.root))
        self.assertEqual(1, delivery.retry_failed(n, self.root))
        self.send.return_value = True, ""
        self.assertEqual(1, n.drain_queue(self.config, self.root)[0])
        self.assertEqual(1, len(list((self.root / "archive").glob("failed-*.json"))))

    def test_corrupt_queue_is_not_silently_replaced(self):
        path = n.queue_path(self.root)
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(OSError):
            self.enqueue()
        self.assertEqual("{broken", path.read_text())

    def test_ledger_commit_recovers_stale_queue_without_resend(self):
        self.enqueue()
        n.atomic_write_json(n.ledger_path(self.root), {"a": n.time.time()})
        n.drain_queue(self.config, self.root)
        self.send.assert_not_called()
        self.assertEqual([], n.load_queue(self.root))

    def test_short_task_skips_delayed_start(self):
        self.config["start_notice_delay_seconds"] = 10
        n.record_turn_start(self.event, self.root)
        self.enqueue("start", _delivery_kind="start")
        self.enqueue("final")
        with mock.patch.object(n, "send_lark_cli_running_card") as start:
            n.drain_queue(self.config, self.root)
            queue = n.load_queue(self.root)
            queue[0]["next_attempt_at"] = 0
            n.atomic_write_json(n.queue_path(self.root), queue)
            n.drain_queue(self.config, self.root)
        start.assert_not_called()
        self.send.assert_called_once()

    def test_terminal_suppresses_late_progress_and_conflicting_terminal(self):
        n.record_turn_start(self.event, self.root)
        n.save_running_message_id(self.event, self.root, "om_run")
        self.enqueue("p", _delivery_kind="progress")
        self.enqueue("final")
        n.enqueue_notice(
            self.root, "late-interrupt", {"status": "interrupted", "_event": self.event}
        )
        with mock.patch.object(n, "update_lark_cli_card") as update:
            n.drain_queue(self.config, self.root)
        update.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(
            "completed",
            n.read_turn_card_state(self.event, self.root)["terminal_status"],
        )

    def test_queue_intent_repairs_terminal_state_after_crash(self):
        self.enqueue()
        n._write_turn_card_state(self.event, self.root, {})
        n.drain_queue(self.config, self.root)
        self.assertTrue(n.read_turn_card_state(self.event, self.root)["terminal"])

    def test_progress_coalesces_and_keeps_new_inflight_snapshot(self):
        n.record_turn_start(self.event, self.root)
        n.save_running_message_id(self.event, self.root, "om_run")
        self.enqueue("p", _delivery_kind="progress", current_step="old")

        def update(*_):
            self.enqueue("p", _delivery_kind="progress", current_step="new")
            return True, ""

        with mock.patch.object(n, "update_lark_cli_card", side_effect=update):
            n.drain_queue(self.config, self.root)
        self.assertEqual("new", n.load_queue(self.root)[0]["notice"]["current_step"])
        self.assertNotIn("p", n.load_ledger(self.root))

    def test_late_start_response_preserves_old_turn_message_id(self):
        n.record_turn_start(self.event, self.root)
        n.record_turn_start(dict(self.event, turn_id="next"), self.root)
        n.save_running_message_id(self.event, self.root, "om_old")
        self.assertEqual("om_old", n.running_message_id(self.event, self.root))
        self.assertEqual(
            "", n.running_message_id(dict(self.event, turn_id="next"), self.root)
        )

    def test_minimum_duration_does_not_leave_running_card(self):
        self.config["min_duration_seconds"] = 60
        n.record_turn_start(self.event, self.root)
        n.save_running_message_id(self.event, self.root, "om_run")
        n.handle_hook(
            dict(
                self.event,
                hook_event_name="Stop",
                duration_ms=1000,
                last_assistant_message="完成",
            )
        )
        self.assertEqual(1, len(n.load_queue(self.root)))
        self.assertTrue(n.read_turn_card_state(self.event, self.root)["terminal"])

    def test_explicit_status_overrides_success_words(self):
        notice = n.build_notice(
            dict(
                self.event, turn_status="failed", last_assistant_message="已完成第一步"
            ),
            self.config,
            1,
        )
        self.assertEqual("incomplete", notice["status"])
        self.assertEqual("执行失败", notice["failure_type"])
        self.assertEqual("lifecycle", notice["status_source"])

    def test_interrupt_hook_enqueues_without_network(self):
        n.handle_hook(dict(self.event, hook_event_name="Interrupt"))
        self.assertEqual("interrupted", n.load_queue(self.root)[0]["notice"]["status"])
        self.send.assert_not_called()

    def test_quoted_background_prompt_not_suppressed(self):
        marker = n.AMBIENT_SUGGESTION_PROMPT_MARKERS[0]
        self.assertFalse(n.is_ambient_suggestions_prompt("请解释这个提示词：" + marker))
        n.record_turn_start(dict(self.event, prompt=marker), self.root)
        self.assertEqual(
            "", n.turn_notification_kind(dict(self.event, turn_id="other"), self.root)
        )

    def test_redacts_all_visible_fields_and_hides_excluded_project(self):
        raw = {
            "task_name": '"app_secret": "very-secret"',
            "prompt": "Authorization: Bearer ABCDEFGH",
            "result_summary": "api_key=super-secret",
            "project_name": "private",
            "recent_steps": [{"text": "password=secret"}],
        }
        clean = n.sanitize_notice(raw, self.config)
        for secret in ("very-secret", "ABCDEFGH", "super-secret", "password=secret"):
            self.assertNotIn(secret, json.dumps(clean))
        clean = n.sanitize_notice(
            raw, dict(self.config, summary_excluded_projects=["private"])
        )
        self.assertEqual([], clean["recent_steps"])
        self.assertEqual("内容未附带", clean["prompt"])

    def test_missing_turn_does_not_use_other_turn_reply(self):
        path = self.root / "rollout.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "其他任务结果"},
                }
            )
        )
        self.assertEqual(
            "", n.last_assistant_message(dict(self.event, transcript_path=str(path)))
        )

    def test_incremental_tokens_repeat_context_and_first_turn(self):
        path = self.root / "rollout.jsonl"
        records = [
            {
                "type": "turn_context",
                "payload": {"turn_id": "t", "model": "gpt-5.6-sol"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 100,
                            "input_tokens": 80,
                            "output_tokens": 20,
                        },
                        "last_token_usage": {"total_tokens": 100},
                    },
                },
            },
        ]
        path.write_text("\n".join(json.dumps(x) for x in records) + "\n")
        event = dict(self.event, transcript_path=str(path))
        self.assertEqual(100, n.transcript_execution_metadata(event)["tokens"])
        with path.open("a") as handle:
            handle.write(json.dumps(records[0]) + "\n")
            records[1]["payload"]["info"]["total_token_usage"]["total_tokens"] = 180
            handle.write(json.dumps(records[1]) + "\n")
        result = n.transcript_execution_metadata(event)
        self.assertEqual(180, result["tokens"])
        self.assertEqual(80, result["input_tokens"])
        self.assertEqual(
            path.stat().st_size, transcript_index._CACHE[str(path)]["offset"]
        )

    def test_unknown_token_baseline_not_invented(self):
        path = self.root / "rollout.jsonl"
        path.write_text(
            json.dumps({"type": "turn_context", "payload": {"turn_id": "t"}})
            + "\n"
            + json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 999}},
                    },
                }
            )
        )
        self.assertIsNone(
            n.transcript_execution_metadata(
                dict(self.event, transcript_path=str(path))
            )["tokens"]
        )

    def test_config_edit_preserves_disabled_state_and_preferences(self):
        path = self.root / "config.json"
        original = dict(
            self.config,
            enabled=False,
            transport="webhook",
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            retries=4,
            notify_on_start=False,
            progress_heartbeat_seconds=100,
        )
        n.atomic_write_json(path, original)
        with (
            mock.patch.object(
                sys, "argv", ["configure.py", "--config", str(path), "--title", "new"]
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, configure.main())
        updated = n.read_json(path, {})
        self.assertEqual(dict(original, title="new"), updated)

    def test_health_is_read_only(self):
        self.enqueue()
        before = {
            str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()
        }
        self.assertEqual(1, delivery.health(n, self.config, self.root)["pending"])
        after = {
            str(p): p.stat().st_mtime_ns for p in self.root.rglob("*") if p.is_file()
        }
        self.assertEqual(before, after)
        self.send.assert_not_called()

    def test_kernel_state_lock_releases_after_process_exit(self):
        script = (
            "import sys; from pathlib import Path; import notifier; import os; "
            + "lock=notifier.state_lock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)"
        )
        env = dict(os.environ, PYTHONPATH=str(Path(n.__file__).parent))
        subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=env,
            check=True,
            timeout=5,
        )
        with n.state_lock(self.root, wait_seconds=0.2):
            pass

    def test_worker_retries_without_new_hook_using_fake_cli(self):
        # A real isolated worker process, but a local fake CLI and synthetic IDs.
        # No sockets, no Feishu credentials, no watcher/launchd installation.
        fake = self.root / "fake-cli"
        counter = self.root / "calls"
        fake.write_text(
            f"#!{sys.executable}\nimport json\nfrom pathlib import Path\np=Path({str(counter)!r})\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\nprint(json.dumps({{'code': 500 if n == 0 else 0, 'msg': 'temporary failure' if n == 0 else 'ok', 'data': {{'message_id': 'om_fake'}}}}))\n"
        )
        fake.chmod(0o700)
        config_path = self.root / "isolated-config.json"
        n.atomic_write_json(
            config_path,
            dict(
                self.config,
                lark_cli_path=str(fake),
                lark_profile="fake",
                lark_chat_id="oc_fake",
                retry_base_seconds=1,
            ),
        )
        self.enqueue()
        env = dict(
            os.environ,
            CODEX_FEISHU_NOTIFIER_DATA_DIR=str(self.root),
            CODEX_FEISHU_NOTIFIER_CONFIG=str(config_path),
        )
        worker = subprocess.Popen(
            [sys.executable, str(Path(delivery.__file__)), "run"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 8
            while (
                n.load_queue(self.root)
                and worker.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertEqual([], n.load_queue(self.root))
            self.assertEqual("2", counter.read_text())
            self.assertTrue((self.root / "delivery_health.json").exists())
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.communicate(timeout=5)

    def test_short_turn_with_start_inflight_still_queues_terminal(self):
        self.config["min_duration_seconds"] = 60
        n.record_turn_start(self.event, self.root)
        state = n.read_turn_card_state(self.event, self.root)
        state["running_send_started_at"] = time.time()
        n._write_turn_card_state(self.event, self.root, state)
        n.handle_hook(dict(self.event, hook_event_name="Stop", duration_ms=1000))
        self.assertEqual(1, len(n.load_queue(self.root)))


if __name__ == "__main__":
    unittest.main()
