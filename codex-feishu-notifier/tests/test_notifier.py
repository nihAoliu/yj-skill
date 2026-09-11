from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


notifier = load_script("notifier")
interruption_watcher = load_script("interruption_watcher")


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.body


class NotifierTests(unittest.TestCase):
    def setUp(self):
        # Unit tests must never start background services or use live Feishu.
        for name in ("ensure_delivery_worker", "ensure_interruption_watcher"):
            patcher = mock.patch.object(notifier, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_renamed_plugin_keeps_existing_config_and_state_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            legacy_config = (
                home / ".config" / notifier.LEGACY_PLUGIN_NAME / "config.json"
            )
            legacy_config.parent.mkdir(parents=True)
            legacy_config.write_text("{}", encoding="utf-8")
            legacy_data = home / ".local" / "share" / notifier.LEGACY_PLUGIN_NAME
            legacy_data.mkdir(parents=True)
            with mock.patch.object(notifier.Path, "home", return_value=home):
                config_path = notifier.global_config_path()
                data_path = notifier.resolve_data_dir()
        self.assertEqual("codex-feishu-notifier", notifier.PLUGIN_NAME)
        self.assertEqual(legacy_config, config_path)
        self.assertEqual(legacy_data, data_path)

    def test_build_message_omits_assistant_text_when_summary_is_disabled(self):
        event = {
            "cwd": "/work/example",
            "last_assistant_message": "private implementation details",
        }
        config = dict(notifier.DEFAULT_CONFIG)
        config["include_result_summary"] = False
        message = notifier.build_message(event, config, 61)
        self.assertIn("对话：example", message)
        self.assertIn("运行时长：1 分 1 秒", message)
        self.assertNotIn("private implementation details", message)

    def test_build_message_can_include_compact_preview(self):
        config = dict(notifier.DEFAULT_CONFIG)
        config["include_result_summary"] = False
        config["include_last_message"] = True
        config["preview_chars"] = 40
        event = {"cwd": "/work/example", "last_assistant_message": "done\n" + "x" * 80}
        message = notifier.build_message(event, config, None)
        self.assertIn("结果：", message)
        self.assertIn("…", message)

    def test_default_result_summary_is_one_sentence_without_model_call(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "cwd": "/work/example",
            "last_assistant_message": (
                "## 已完成\n\n"
                "- 插件已经安装。\n"
                "- 飞书测试通知发送成功！\n\n"
                "[查看插件](https://example.com)"
            ),
        }
        message = notifier.build_message(event, config, None)
        result_line = next(
            line for line in message.splitlines() if line.startswith("结果：")
        )
        self.assertIn("已完成；插件已经安装；飞书测试通知发送成功", result_line)
        self.assertEqual(1, result_line.count("。"))
        self.assertNotIn("https://", result_line)

    def test_result_summary_omits_writing_artifact_fences(self):
        summary = notifier.one_sentence_result(
            "故事大纲已经生成完成。\n\n"
            ':::writing{variant="document" id="12345"}\n'
            "# UU远程广告故事大纲\n\n"
            "产品在故事中段自然进入。\n"
            ":::"
        )
        self.assertNotIn(":::writing", summary)
        self.assertNotIn("variant=", summary)
        self.assertIn("UU远程广告故事大纲", summary)

    def test_build_notice_maps_card_variables_and_completed_status(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "conversation_title": "飞书通知卡片接入",
            "project_name": "Codex Feishu Notifier",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "last_assistant_message": "卡片接入已完成并通过验证。",
        }
        notice = notifier.build_notice(event, config, 18)
        self.assertEqual("completed", notice["status"])
        self.assertEqual("飞书通知卡片接入", notice["task_name"])
        self.assertEqual("Codex Feishu Notifier", notice["project_name"])
        self.assertEqual("GPT-5.6 Sol · 极高", notice["model_label"])
        self.assertEqual("18 秒", notice["duration"])
        self.assertIn("卡片接入已完成", notice["result_summary"])

    def test_build_notice_uses_incomplete_card_when_user_action_is_required(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "cwd": "/work/example",
            "last_assistant_message": "需要你确认机器人是否可以加入公司群。",
        }
        notice = notifier.build_notice(event, config, 18)
        self.assertEqual("incomplete", notice["status"])
        self.assertEqual("等待确认", notice["failure_type"])

    def test_completed_writing_artifact_may_contain_confirmation_language(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "cwd": "/work/example",
            "last_assistant_message": (
                "故事大纲已经生成完成。\n\n"
                ':::writing{variant="document" id="12345"}\n'
                "## 正式写稿前需要确认的真实信息\n\n"
                "这里是已经交付的完整故事大纲。\n"
                ":::\n\n"
                "下一步可以继续扩写脚本。"
            ),
        }
        notice = notifier.build_notice(event, config, 18)
        self.assertEqual("completed", notice["status"])

    def test_confirmation_outside_writing_artifact_remains_incomplete(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "cwd": "/work/example",
            "last_assistant_message": (
                ':::writing{variant="document" id="12345"}\n'
                "已完成的初稿。\n"
                ":::\n\n"
                "请确认最终采用哪个方向。"
            ),
        }
        notice = notifier.build_notice(event, config, 18)
        self.assertEqual("incomplete", notice["status"])
        self.assertEqual("等待确认", notice["failure_type"])

    def test_build_notice_uses_interrupted_status_and_recorded_times(self):
        config = dict(notifier.DEFAULT_CONFIG)
        event = {
            "notification_status": "interrupted",
            "last_assistant_message": "已经完成数据读取，正在生成结果。",
            "started_at": 1_785_343_377,
            "completed_at": 1_785_343_455,
        }
        notice = notifier.build_notice(event, config, 78)
        self.assertEqual("interrupted", notice["status"])
        self.assertEqual("1 分 18 秒", notice["duration"])
        self.assertIn("本轮任务已中断", notice["result_summary"])
        self.assertIn("中断前进展", notice["result_summary"])
        self.assertEqual("", notice["failure_type"])

    def test_formats_approximate_token_count(self):
        self.assertEqual("982", notifier.format_token_estimate(982))
        self.assertEqual("18.6K", notifier.format_token_estimate(18_640))
        self.assertEqual("1.2M", notifier.format_token_estimate(1_240_000))

    def test_reads_current_turn_model_effort_and_token_delta(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            transcript = Path(temporary_directory) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {"total_tokens": 10_000}
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn_context",
                                "payload": {
                                    "turn_id": "turn-2",
                                    "model": "gpt-5.6-sol",
                                    "effort": "xhigh",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "total_token_usage": {"total_tokens": 28_640}
                                    },
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            metadata = notifier.transcript_execution_metadata(
                {
                    "turn_id": "turn-2",
                    "transcript_path": str(transcript),
                }
            )
        self.assertEqual("gpt-5.6-sol", metadata["model"])
        self.assertEqual("xhigh", metadata["effort"])
        self.assertEqual(18_640, metadata["tokens"])

    def test_reads_last_result_from_transcript_when_hook_field_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            transcript = Path(temporary_directory) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "task_complete",
                                    "last_agent_message": "任务已完成并通过验证。",
                                },
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"type": "token_count"},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            result = notifier.last_assistant_message(
                {"transcript_path": str(transcript)}
            )
        self.assertEqual("任务已完成并通过验证。", result)

    def test_uses_codex_conversation_title_instead_of_project_code(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("""
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    name TEXT,
                    first_user_message TEXT NOT NULL
                )
                """)
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (
                    "thread-123",
                    "自动生成标题",
                    "监测 Codex 项目完成通知",
                    "一段很长的首条消息",
                ),
            )
            connection.commit()
            connection.close()
            event = {
                "session_id": "thread-123",
                "cwd": "/work/g-p-6a646e8195508191a636eb00b5672eae",
            }
            with mock.patch.object(
                notifier, "codex_state_candidates", return_value=[database]
            ):
                message = notifier.build_message(
                    event, dict(notifier.DEFAULT_CONFIG), None
                )
        self.assertIn("对话：监测 Codex 项目完成通知", message)
        self.assertNotIn("g-p-", message)

    def test_project_code_falls_back_to_unnamed_conversation(self):
        event = {"cwd": "/work/g-p-6a646e8195508191a636eb00b5672eae"}
        message = notifier.build_message(event, dict(notifier.DEFAULT_CONFIG), None)
        self.assertIn("对话：未命名对话", message)
        self.assertNotIn("g-p-", message)

    def test_accepts_only_official_feishu_webhook_urls(self):
        valid, _ = notifier.validate_webhook_url(
            "https://open.feishu.cn/open-apis/bot/v2/hook/example"
        )
        invalid, _ = notifier.validate_webhook_url(
            "https://example.com/open-apis/bot/v2/hook/example"
        )
        self.assertTrue(valid)
        self.assertFalse(invalid)

    def test_send_notice_posts_text_to_feishu_webhook(self):
        config = dict(notifier.DEFAULT_CONFIG)
        config.update(
            {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "retries": 0,
            }
        )
        response = FakeResponse(b'{"code":0,"msg":"success"}')
        with mock.patch.object(
            notifier.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            ok, error = notifier.send_notice(config, "hello")
        self.assertTrue(ok)
        self.assertEqual("", error)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            {"msg_type": "text", "content": {"text": "hello"}},
            payload,
        )
        self.assertEqual(10, urlopen.call_args.kwargs["timeout"])

    def test_send_notice_reports_feishu_rejection(self):
        config = dict(notifier.DEFAULT_CONFIG)
        config.update(
            {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                "retries": 0,
            }
        )
        response = FakeResponse(b'{"code":19024,"msg":"Key Words Not Found"}')
        with mock.patch.object(
            notifier.urllib.request, "urlopen", return_value=response
        ):
            ok, error = notifier.send_notice(config, "hello")
        self.assertFalse(ok)
        self.assertIn("Key Words Not Found", error)

    def test_send_notice_uses_company_lark_cli_bot_without_shell(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "retries": 0,
                }
            )
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "identity": "bot",
                        "data": {"message_id": "om_test"},
                    }
                ),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", return_value=completed
            ) as run:
                ok, error = notifier.send_notice(config, "company hello")
        self.assertTrue(ok)
        self.assertEqual("", error)
        arguments = run.call_args.args[0]
        self.assertEqual(str(executable), arguments[0])
        self.assertIn("company-profile", arguments)
        self.assertIn("oc_company", arguments)
        self.assertIn("company hello", arguments)
        self.assertEqual(False, run.call_args.kwargs["check"])

    def test_send_notice_uses_complete_card_2_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "lark_completed_template_id": "AAqCompleted",
                    "retries": 0,
                }
            )
            notice = {
                "status": "completed",
                "task_name": "卡片接入",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "token_estimate": "18.6K",
                "started_at": "2026-07-30 00:05",
                "duration": "18 秒",
                "completed_at": "2026-07-30 00:08",
                "result_summary": "测试通过。",
            }
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_card"}}),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", return_value=completed
            ) as run:
                ok, error = notifier.send_notice(config, notice)
        self.assertTrue(ok)
        self.assertEqual("", error)
        arguments = run.call_args.args[0]
        self.assertIn("--msg-type", arguments)
        self.assertIn("interactive", arguments)
        content = json.loads(arguments[arguments.index("--content") + 1])
        self.assertEqual("2.0", content["schema"])
        self.assertEqual("green", content["header"]["template"])
        self.assertEqual("卡片接入", content["header"]["title"]["content"])
        serialized = json.dumps(content, ensure_ascii=False)
        self.assertIn("GPT-5.6 Sol · 极高", serialized)
        self.assertIn("18.6K", serialized)
        self.assertIn("测试通过", serialized)
        self.assertNotIn("template_id", serialized)

    def test_incomplete_json_card_includes_failure_type_tag(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "lark_incomplete_template_id": "AAqIncomplete",
                    "retries": 0,
                }
            )
            notice = {
                "status": "incomplete",
                "task_name": "失败标签测试",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "token_estimate": "18.6K",
                "started_at": "2026-07-30 00:05",
                "duration": "18 秒",
                "completed_at": "2026-07-30 00:08",
                "failure_type": "等待确认",
                "result_summary": "需要用户确认。",
            }
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_card"}}),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", return_value=completed
            ) as run:
                ok, error = notifier.send_notice(config, notice)
        self.assertTrue(ok)
        self.assertEqual("", error)
        arguments = run.call_args.args[0]
        content = json.loads(arguments[arguments.index("--content") + 1])
        self.assertEqual("red", content["header"]["template"])
        tag_texts = [
            item["text"]["content"] for item in content["header"]["text_tag_list"]
        ]
        self.assertEqual(["需要处理", "等待确认"], tag_texts)

    def test_interrupted_json_card_uses_orange_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "lark_interrupted_template_id": "AAqInterrupted",
                    "retries": 0,
                }
            )
            notice = {
                "status": "interrupted",
                "task_name": "中断卡片测试",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "token_estimate": "8.2K",
                "started_at": "2026-07-30 12:00",
                "duration": "1 分 18 秒",
                "completed_at": "2026-07-30 12:01",
                "result_summary": "本轮任务已中断；当前进度已保留。",
            }
            completed = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_card"}}),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", return_value=completed
            ) as run:
                ok, error = notifier.send_notice(config, notice)
        self.assertTrue(ok)
        self.assertEqual("", error)
        arguments = run.call_args.args[0]
        content = json.loads(arguments[arguments.index("--content") + 1])
        self.assertEqual("2.0", content["schema"])
        self.assertEqual("orange", content["header"]["template"])
        self.assertEqual(
            "已中断",
            content["header"]["text_tag_list"][0]["text"]["content"],
        )

    def test_running_card_is_shared_and_marks_task_in_progress(self):
        card = notifier.running_card_content(
            {
                "task_name": "卡片刷新测试",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "started_at": "2026-08-01 21:00",
                "duration": "18 秒",
                "updated_at": "21:00:18",
                "current_step": "正在验证飞书卡片刷新",
            }
        )
        self.assertEqual("2.0", card["schema"])
        self.assertTrue(card["config"]["update_multi"])
        self.assertEqual("blue", card["header"]["template"])
        self.assertIn("卡片刷新测试", card["header"]["title"]["content"])
        serialized = json.dumps(card, ensure_ascii=False)
        self.assertIn("当前步骤", serialized)
        self.assertIn("正在验证飞书卡片刷新", serialized)
        self.assertNotIn("template_id", serialized)
        for element in card["body"]["elements"]:
            if element.get("tag") != "column_set":
                continue
            for column in element.get("columns", []):
                self.assertNotIn("corner_radius", column)

    def test_prompt_summary_prefers_request_and_removes_attachment_noise(self):
        event = {
            "prompt": (
                "# Files mentioned by the user:\n"
                "- /var/folders/example/screenshot.png\n\n"
                "## My request for Codex:\n"
                "把飞书执行中卡片接入通知插件，并将过长的任务说明压缩成一句话。\n"
                "同时保留现有终态卡片刷新逻辑。"
            )
        }
        config = dict(notifier.DEFAULT_CONFIG)
        config["prompt_summary_chars"] = 100
        summary = notifier.prompt_summary(event, config)
        self.assertIn("飞书执行中卡片", summary)
        self.assertIn("终态卡片刷新", summary)
        self.assertNotIn("/var/folders", summary)
        self.assertLessEqual(len(summary), 101)

    def test_running_template_receives_prompt_variable(self):
        config = dict(notifier.DEFAULT_CONFIG)
        config["lark_running_template_id"] = "AAqRunning"
        content = notifier.template_card_content(
            config,
            {
                "status": "running",
                "task_name": "执行中模板",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "started_at": "2026-08-01 21:00",
                "prompt": "接入执行中模板并验证变量。",
            },
        )
        self.assertIsNotNone(content)
        assert content is not None
        self.assertEqual("AAqRunning", content["data"]["template_id"])
        self.assertEqual(
            "接入执行中模板并验证变量。",
            content["data"]["template_variable"]["prompt"],
        )

    def test_start_hook_sends_only_one_running_card_for_duplicate_hooks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            executable = data_dir / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                }
            )
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "start-session",
                "turn_id": "start-turn",
                "cwd": "/work/example",
                "prompt": "运行通知测试",
            }
            with (
                mock.patch.object(notifier, "resolve_data_dir", return_value=data_dir),
                mock.patch.object(notifier, "load_config", return_value=(config, None)),
                mock.patch.object(notifier, "ensure_interruption_watcher"),
                mock.patch.object(
                    notifier,
                    "session_is_ambient_suggestions_by_logs",
                    return_value=False,
                ),
                mock.patch.object(
                    notifier,
                    "send_lark_cli_running_card",
                    return_value=(True, "", "om_running"),
                ) as send,
            ):
                notifier.handle_hook(event)
                notifier.handle_hook(event)
                send.assert_not_called()
                queue = notifier.load_queue(data_dir)
                self.assertEqual(1, len(queue))
                queue[0]["next_attempt_at"] = 0
                notifier.atomic_write_json(notifier.queue_path(data_dir), queue)
                notifier.drain_queue(config, data_dir)
            state = notifier.read_json(
                notifier.turn_state_path(data_dir, "start-session"), {}
            )
        send.assert_called_once()
        self.assertEqual("om_running", state["running_message_id"])

    def test_terminal_notice_updates_running_card_then_replies(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "lark_completed_template_id": "AAqCompleted",
                    "retries": 0,
                }
            )
            notice = {
                "status": "completed",
                "task_name": "原位刷新",
                "project_name": "Notifier",
                "model_label": "GPT-5.6 Sol · 极高",
                "token_estimate": "8.2K",
                "started_at": "2026-08-01 21:00",
                "duration": "20 秒",
                "completed_at": "2026-08-01 21:01",
                "result_summary": "测试通过。",
                "_event_key": "event-1",
                "_running_message_id": "om_running",
            }
            success = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {}}),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", side_effect=[success, success]
            ) as run:
                ok, error = notifier.send_notice(config, notice)
        self.assertTrue(ok)
        self.assertEqual("", error)
        update_arguments = run.call_args_list[0].args[0]
        reply_arguments = run.call_args_list[1].args[0]
        self.assertIn("api", update_arguments)
        self.assertIn("PATCH", update_arguments)
        self.assertIn("/open-apis/im/v1/messages/om_running", update_arguments)
        self.assertIn("+messages-reply", reply_arguments)
        self.assertIn("om_running", reply_arguments)

    def test_card_update_failure_falls_back_to_new_terminal_card(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                    "lark_completed_template_id": "AAqCompleted",
                    "retries": 0,
                }
            )
            notice = {
                "status": "completed",
                "task_name": "降级测试",
                "_running_message_id": "om_running",
            }
            failed = mock.Mock(
                returncode=1,
                stdout="",
                stderr=json.dumps({"ok": False, "error": {"message": "update failed"}}),
            )
            success = mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "data": {"message_id": "om_new"}}),
                stderr="",
            )
            with mock.patch.object(
                notifier.subprocess, "run", side_effect=[failed, success]
            ) as run:
                ok, error = notifier.send_notice(config, notice)
        self.assertTrue(ok)
        self.assertEqual("", error)
        self.assertIn("api", run.call_args_list[0].args[0])
        self.assertIn("+messages-send", run.call_args_list[1].args[0])

    def test_interruption_event_key_deduplicates_copied_rollouts(self):
        first = {
            "session_id": "session-a",
            "turn_id": "turn-shared",
            "notification_status": "interrupted",
        }
        copied = {
            "session_id": "session-b",
            "turn_id": "turn-shared",
            "notification_status": "interrupted",
        }
        self.assertEqual(notifier.event_key(first), notifier.event_key(copied))

    def test_completion_event_key_deduplicates_copied_rollouts(self):
        first = {
            "session_id": "session-a",
            "turn_id": "turn-shared",
            "last_assistant_message": "完成。",
        }
        copied = {
            "session_id": "session-b",
            "turn_id": "turn-shared",
            "last_assistant_message": "完成。",
        }
        self.assertEqual(notifier.event_key(first), notifier.event_key(copied))

    def test_watcher_detects_only_newly_appended_interruption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": "session-1",
                            "cwd": "/work/example",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            offsets = {str(rollout): rollout.stat().st_size}
            with rollout.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "turn_aborted",
                                "turn_id": "turn-1",
                                "reason": "interrupted",
                                "started_at": 100,
                                "completed_at": 118,
                                "duration_ms": 18_000,
                            },
                        }
                    )
                    + "\n"
                )
            found = []
            changed = interruption_watcher.scan_once(
                root,
                offsets,
                lambda path, payload: found.append((path, payload)),
            )
        self.assertTrue(changed)
        self.assertEqual(1, len(found))
        self.assertEqual("turn-1", found[0][1]["turn_id"])

    def test_watcher_does_not_replay_when_a_rollout_shrinks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "compacted.jsonl"
            historical = (
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "historical-turn",
                            "last_agent_message": "这是一条旧完成记录。",
                        },
                    }
                )
                + "\n"
            )
            rollout.write_text(historical, encoding="utf-8")
            offsets = {str(rollout): len(historical.encode("utf-8")) + 100}
            completions = []
            changed = interruption_watcher.scan_once(
                root,
                offsets,
                lambda _path, _payload: None,
                on_complete=lambda _path, payload: completions.append(payload),
            )
            expected_offset = rollout.stat().st_size
        self.assertTrue(changed)
        self.assertEqual([], completions)
        self.assertEqual(expected_offset, offsets[str(rollout)])

    def test_watcher_detects_start_and_completion_without_codex_hooks(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "session-1",
                            "cwd": "/work/example",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            offsets = {str(rollout): rollout.stat().st_size}
            records = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "turn-1",
                        "started_at": 100,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "排查通知问题",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "问题已完成修复。",
                        "started_at": 100,
                        "completed_at": 118,
                        "duration_ms": 18_000,
                    },
                },
            ]
            with rollout.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            starts = []
            completions = []
            changed = interruption_watcher.scan_once(
                root,
                offsets,
                lambda _path, _payload: None,
                lambda path, payload: starts.append((path, payload)),
                lambda path, payload: completions.append((path, payload)),
                active_turns={},
            )
        self.assertTrue(changed)
        self.assertEqual(1, len(starts))
        self.assertEqual("turn-1", starts[0][1]["turn_id"])
        self.assertEqual("排查通知问题", starts[0][1]["message"])
        self.assertEqual(1, len(completions))
        self.assertEqual("问题已完成修复。", completions[0][1]["last_agent_message"])

    def test_watcher_extracts_commentary_and_safe_tool_progress(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session-1", "cwd": "/work/example"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            offsets = {str(rollout): rollout.stat().st_size}
            records = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "turn-1",
                        "started_at": 100,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "构建动态卡片"},
                },
                {
                    "timestamp": "2026-08-03T01:00:10Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [
                            {"type": "output_text", "text": "正在设计实时进度卡片。"}
                        ],
                    },
                },
                {
                    "timestamp": "2026-08-03T01:00:12Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "exec_command"},
                },
            ]
            with rollout.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            progress = []
            interruption_watcher.scan_once(
                root,
                offsets,
                lambda _path, _payload: None,
                lambda _path, _payload: None,
                lambda _path, _payload: None,
                lambda _path, payload, step, observed_at: progress.append(
                    (payload, step, observed_at)
                ),
                active_turns={},
            )
        self.assertEqual(2, len(progress))
        self.assertEqual("turn-1", progress[0][0]["turn_id"])
        self.assertIn("实时进度卡片", progress[0][1])
        self.assertEqual("正在运行本地检查或构建", progress[1][1])

    def test_progress_state_redacts_credentials_and_survives_copied_rollout(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            first = {
                "session_id": "session-a",
                "turn_id": "shared-turn",
                "prompt": "实现实时卡片",
            }
            notifier.record_turn_start(first, data_dir)
            notifier.save_running_message_id(first, data_dir, "om_running")
            notifier.record_turn_progress(
                first,
                data_dir,
                "正在配置 app_secret=very-secret-value 和 cli_aaaaaaaaaaaaaaaa",
                100,
            )
            copied = {"session_id": "session-b", "turn_id": "shared-turn"}
            snapshot = notifier.turn_progress_snapshot(copied, data_dir)
            message_id = notifier.running_message_id(copied, data_dir)
        self.assertEqual("om_running", message_id)
        self.assertEqual("1", snapshot["step_count"])
        self.assertIn("[已隐藏]", snapshot["current_step"])
        self.assertNotIn("very-secret-value", snapshot["current_step"])
        self.assertNotIn("cli_aaa", snapshot["current_step"])

    def test_live_progress_refresh_updates_existing_json_card(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            executable = data_dir / "lark-cli"
            executable.write_text("binary", encoding="utf-8")
            config = dict(notifier.DEFAULT_CONFIG)
            config.update(
                {
                    "transport": "lark-cli",
                    "lark_cli_path": str(executable),
                    "lark_profile": "company-profile",
                    "lark_chat_id": "oc_company",
                }
            )
            event = {
                "session_id": "session-a",
                "turn_id": "turn-a",
                "cwd": "/work/example",
                "prompt": "实现实时卡片",
            }
            notifier.record_turn_start(event, data_dir)
            notifier.save_running_message_id(event, data_dir, "om_running")
            with (
                mock.patch.object(notifier, "load_config", return_value=(config, None)),
                mock.patch.object(
                    notifier,
                    "session_is_ambient_suggestions_by_logs",
                    return_value=False,
                ),
                mock.patch.object(
                    notifier, "update_lark_cli_card", return_value=(True, "")
                ) as update,
            ):
                ok, error = notifier.refresh_running_card(
                    event,
                    data_dir,
                    "正在验证动态刷新",
                    force=True,
                )
                update.assert_not_called()
                notifier.drain_queue(config, data_dir)
            refreshed_notice = update.call_args.args[2]
        self.assertTrue(ok)
        self.assertEqual("", error)
        self.assertEqual("running", refreshed_notice["status"])
        self.assertEqual("正在验证动态刷新", refreshed_notice["current_step"])
        self.assertEqual("1", refreshed_notice["step_count"])

    def test_lifecycle_fallback_routes_rollout_events_through_hook_handler(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            rollout = Path(temporary_directory) / "rollout.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session-1", "cwd": "/work/example"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                interruption_watcher.notifier, "handle_hook"
            ) as handle:
                interruption_watcher.send_task_start(
                    rollout,
                    {
                        "turn_id": "turn-1",
                        "started_at": 100,
                        "message": "开始任务",
                    },
                )
                interruption_watcher.send_task_completion(
                    rollout,
                    {
                        "turn_id": "turn-1",
                        "started_at": 100,
                        "completed_at": 118,
                        "duration_ms": 18_000,
                        "last_agent_message": "任务已完成。",
                    },
                )
        start_event = handle.call_args_list[0].args[0]
        complete_event = handle.call_args_list[1].args[0]
        self.assertEqual("UserPromptSubmit", start_event["hook_event_name"])
        self.assertEqual("开始任务", start_event["prompt"])
        self.assertEqual("Stop", complete_event["hook_event_name"])
        self.assertEqual(18_000, complete_event["duration_ms"])
        self.assertEqual("任务已完成。", complete_event["last_assistant_message"])

    def test_watcher_does_not_replay_old_interruptions_from_resumed_rollout(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "resumed.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_aborted",
                            "turn_id": "old-turn",
                            "reason": "interrupted",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            offsets = {}
            found = []
            changed = interruption_watcher.scan_once(
                root,
                offsets,
                lambda path, payload: found.append((path, payload)),
            )
            rollout_size = rollout.stat().st_size
        self.assertTrue(changed)
        self.assertEqual([], found)
        self.assertEqual(rollout_size, offsets[str(rollout)])

    def test_new_resumed_rollout_recovers_only_current_open_turn(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            rollout = root / "resumed.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {"id": "session-new", "cwd": "/work/example"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "old-turn",
                        "started_at": 900,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "旧任务"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "old-turn",
                        "completed_at": 950,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": "current-turn",
                        "started_at": 990,
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "当前任务"},
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            offsets = {}
            starts = []
            completions = []
            with mock.patch.object(
                interruption_watcher.time, "time", return_value=1000
            ):
                changed = interruption_watcher.scan_once(
                    root,
                    offsets,
                    lambda _path, _payload: None,
                    lambda _path, payload: starts.append(payload),
                    lambda _path, payload: completions.append(payload),
                    process_new_files=True,
                    active_turns={},
                )
        self.assertTrue(changed)
        self.assertEqual([], completions)
        self.assertEqual(1, len(starts))
        self.assertEqual("current-turn", starts[0]["turn_id"])
        self.assertEqual("当前任务", starts[0]["message"])

    def test_turn_duration_is_measured_from_latest_user_prompt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            event = {"session_id": "session-1", "turn_id": "turn-1"}
            with mock.patch.object(notifier.time, "time", return_value=100.0):
                notifier.record_turn_start(event, data_dir)
            with mock.patch.object(notifier.time, "time", return_value=118.9):
                duration = notifier.turn_duration_seconds(event, data_dir)
        self.assertEqual(18, duration)

    def test_rollout_completion_duration_is_used_when_hook_state_is_missing(self):
        self.assertEqual(
            263,
            notifier.event_duration_seconds(
                {
                    "started_at": 100,
                    "completed_at": 364,
                    "duration_ms": 263_684,
                }
            ),
        )

    def test_ambient_suggestion_prompt_is_recorded_for_later_stop_filtering(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            event = {
                "session_id": "ambient-session",
                "turn_id": "ambient-turn",
                "prompt": (
                    "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions "
                    "for what this user can do with Codex in this local project"
                ),
            }
            notifier.record_turn_start(event, data_dir)
            stop_event = {"session_id": "ambient-session"}
            kind = notifier.turn_notification_kind(stop_event, data_dir)
        self.assertEqual("ambient_suggestions", kind)

    def test_ambient_suggestion_session_is_found_in_codex_logs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "logs_2.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("""
                CREATE TABLE logs (
                    thread_id TEXT,
                    target TEXT,
                    feedback_log_body TEXT
                )
                """)
            connection.execute(
                "INSERT INTO logs VALUES (?, ?, ?)",
                (
                    "ambient-session",
                    "codex_core::session::handlers",
                    (
                        "Submission UserInput: You are an expert at upholding safety "
                        "and compliance standards for Codex ambient suggestions."
                    ),
                ),
            )
            connection.commit()
            connection.close()
            with mock.patch.object(
                notifier, "codex_logs_candidates", return_value=[database]
            ):
                detected = notifier.session_is_ambient_suggestions_by_logs(
                    {"session_id": "ambient-session"}
                )
        self.assertTrue(detected)

    def test_auto_review_model_is_suppressed_from_rollout_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            transcript = data_dir / "rollout.jsonl"
            records = [
                {
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "review-turn",
                        "model": "codex-auto-review",
                        "effort": "low",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "review-turn",
                        "last_agent_message": "approve",
                    },
                },
            ]
            transcript.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            event = {
                "session_id": "review-session",
                "turn_id": "review-turn",
                "transcript_path": str(transcript),
            }
            suppressed = notifier.should_suppress_notification(
                event, dict(notifier.DEFAULT_CONFIG), data_dir
            )
        self.assertTrue(suppressed)

    def test_stop_hook_does_not_send_auto_review_notification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            config = dict(notifier.DEFAULT_CONFIG)
            with (
                mock.patch.object(notifier, "resolve_data_dir", return_value=data_dir),
                mock.patch.object(notifier, "load_config", return_value=(config, None)),
                mock.patch.object(
                    notifier, "send_notice", return_value=(True, "")
                ) as send,
            ):
                notifier.handle_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "review-session",
                        "turn_id": "review-turn",
                        "model": "codex-auto-review",
                        "last_assistant_message": "approve",
                    }
                )
        send.assert_not_called()

    def test_stop_hook_does_not_send_ambient_suggestion_notification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            config = dict(notifier.DEFAULT_CONFIG)
            with (
                mock.patch.object(notifier, "resolve_data_dir", return_value=data_dir),
                mock.patch.object(notifier, "load_config", return_value=(config, None)),
                mock.patch.object(notifier, "ensure_interruption_watcher"),
                mock.patch.object(
                    notifier, "send_notice", return_value=(True, "")
                ) as send,
            ):
                notifier.handle_hook(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "ambient-session",
                        "turn_id": "ambient-turn",
                        "prompt": (
                            "Generate 0 to 3 hyperpersonalized suggestions for what "
                            "this user can do with Codex in this local project"
                        ),
                    }
                )
                notifier.handle_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "ambient-session",
                        "turn_id": "ambient-turn",
                        "cwd": "/work/example",
                    }
                )
            queue = notifier.load_queue(data_dir)
        send.assert_not_called()
        self.assertEqual([], queue)

    def test_stop_hook_still_sends_normal_user_task_notification(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            config = dict(notifier.DEFAULT_CONFIG)
            with (
                mock.patch.object(notifier, "resolve_data_dir", return_value=data_dir),
                mock.patch.object(notifier, "load_config", return_value=(config, None)),
                mock.patch.object(notifier, "ensure_interruption_watcher"),
                mock.patch.object(
                    notifier,
                    "session_is_ambient_suggestions_by_logs",
                    return_value=False,
                ),
                mock.patch.object(
                    notifier, "send_notice", return_value=(True, "")
                ) as send,
            ):
                notifier.handle_hook(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "user-session",
                        "turn_id": "user-turn",
                        "prompt": "修复并验证飞书通知插件。",
                    }
                )
                notifier.handle_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "user-session",
                        "turn_id": "user-turn",
                        "cwd": "/work/example",
                        "last_assistant_message": "插件已完成并通过验证。",
                    }
                )
                send.assert_not_called()
                notifier.drain_queue(config, data_dir)
        send.assert_called_once()

    def test_feishu_queue_does_not_reuse_legacy_wechat_queue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            legacy = data_dir / "pending.json"
            legacy.write_text(
                json.dumps([{"key": "old-wechat", "message": "old"}]),
                encoding="utf-8",
            )
            notifier.enqueue_notice(data_dir, "new-feishu", "new")
            self.assertEqual(
                ["new-feishu"],
                [item["key"] for item in notifier.load_queue(data_dir)],
            )
            self.assertIn("old-wechat", legacy.read_text(encoding="utf-8"))

    def test_queue_is_deduplicated_and_drained(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            notifier.enqueue_notice(data_dir, "same-key", "hello")
            notifier.enqueue_notice(data_dir, "same-key", "hello")
            self.assertEqual(1, len(notifier.load_queue(data_dir)))
            with mock.patch.object(notifier, "send_notice", return_value=(True, "")):
                sent, pending, error = notifier.drain_queue(
                    dict(notifier.DEFAULT_CONFIG), data_dir
                )
            self.assertEqual((1, 0, ""), (sent, pending, error))
            self.assertIn("same-key", notifier.load_ledger(data_dir))

    def test_structured_card_notice_is_preserved_in_retry_queue(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            notice = {
                "status": "completed",
                "task_name": "卡片通知",
                "project_name": "Notifier",
                "duration": "1 秒",
                "completed_at": "2026-07-30 00:08",
                "result_summary": "完成。",
            }
            notifier.enqueue_notice(data_dir, "card-key", notice)
            queued = notifier.load_queue(data_dir)
            self.assertEqual(notice, queued[0]["notice"])
            with mock.patch.object(
                notifier, "send_notice", return_value=(True, "")
            ) as send:
                result = notifier.drain_queue(dict(notifier.DEFAULT_CONFIG), data_dir)
            self.assertEqual((1, 0, ""), result)
            self.assertEqual(notice, send.call_args.args[1])

    def test_environment_override_loads_webhook_without_changing_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(
                    notifier, "global_config_path", return_value=config_path
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "CODEX_FEISHU_WEBHOOK_URL": (
                            "https://open.feishu.cn/open-apis/bot/v2/hook/from-env"
                        )
                    },
                ),
            ):
                config, loaded_path = notifier.load_config()
        self.assertEqual(config_path, loaded_path)
        self.assertTrue(config["webhook_url"].endswith("/from-env"))


if __name__ == "__main__":
    unittest.main()
