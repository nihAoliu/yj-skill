#!/usr/bin/env python3
"""Configure Codex completion notices for a Feishu webhook or application bot."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

import notifier

PLUGIN_NAME = notifier.PLUGIN_NAME


def default_config_path() -> Path:
    return notifier.global_config_path()


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
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def read_existing(path: Path) -> dict[str, Any]:
    value = notifier.read_json(path, {})
    return value if isinstance(value, dict) else {}


def webhook_from_env_file(path: Path) -> str:
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in raw.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
            continue
        key, value = trimmed.split("=", 1)
        if key.strip() == "FEISHU_WEBHOOK_URL":
            return value.strip().strip("\"'")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("webhook", "lark-cli"),
        default="webhook",
        help="发送方式：群 Webhook 或飞书 CLI 应用机器人",
    )
    parser.add_argument("--webhook-url", default="", help="飞书群自定义机器人 Webhook")
    parser.add_argument(
        "--from-env-file",
        type=Path,
        default=None,
        help="从已有环境文件读取 FEISHU_WEBHOOK_URL",
    )
    parser.add_argument(
        "--lark-cli-path",
        default=str(Path.home() / ".local" / "bin" / "lark-cli"),
        help="飞书 CLI 可执行文件路径",
    )
    parser.add_argument("--lark-profile", default="", help="飞书 CLI profile")
    parser.add_argument("--lark-chat-id", default="", help="目标飞书群 ID")
    parser.add_argument(
        "--completed-template-id",
        default=None,
        help="已完成状态的飞书卡片模板 ID",
    )
    parser.add_argument(
        "--incomplete-template-id",
        default=None,
        help="未完成状态的飞书卡片模板 ID",
    )
    parser.add_argument(
        "--interrupted-template-id",
        default=None,
        help="任务中断状态的飞书卡片模板 ID",
    )
    parser.add_argument(
        "--running-template-id",
        default=None,
        help="旧版兼容字段；完整 JSON 卡片模式下不再使用",
    )
    parser.add_argument("--completed-template-version", default=None)
    parser.add_argument("--incomplete-template-version", default=None)
    parser.add_argument("--interrupted-template-version", default=None)
    parser.add_argument("--running-template-version", default=None)
    parser.add_argument("--no-cards", action="store_true", help="改用纯文字通知")
    parser.add_argument(
        "--no-start-notice",
        action="store_true",
        help="不在任务开始时发送可刷新的执行中卡片",
    )
    parser.add_argument(
        "--no-finish-reply",
        action="store_true",
        help="更新执行中卡片后不再发送一条简短的完成提醒",
    )
    parser.add_argument(
        "--no-live-progress",
        action="store_true",
        help="不在任务运行时持续刷新耗时和最新步骤",
    )
    parser.add_argument(
        "--progress-update-seconds",
        type=int,
        default=12,
        help="新步骤触发卡片更新的最短间隔（5–60 秒）",
    )
    parser.add_argument(
        "--progress-heartbeat-seconds",
        type=int,
        default=30,
        help="没有新步骤时刷新运行时间的间隔（12–300 秒）",
    )
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--conversation-name", default=None)
    parser.add_argument("--title", default="✅ Codex 项目完成")
    parser.add_argument("--min-duration-seconds", type=int, default=0)
    parser.add_argument(
        "--prompt-summary-chars",
        type=int,
        default=160,
        help="执行中卡片 prompt 变量的最大字符数（80–240）",
    )
    parser.add_argument("--include-last-message", action="store_true")
    parser.add_argument(
        "--notify-ambient-suggestions",
        action="store_true",
        help="同时通知 Codex 自动生成的后台项目建议（默认忽略）",
    )
    parser.add_argument(
        "--no-result-summary",
        action="store_true",
        help="不在通知中附带一句话结果",
    )
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--start-delay-seconds", type=int, default=10)
    parser.add_argument("--summary-excluded-project", action="append", default=[])
    args = parser.parse_args()
    supplied = {word.split("=", 1)[0] for word in sys.argv[1:] if word.startswith("--")}
    if args.enable and args.disable:
        parser.error("--enable 与 --disable 不能同时使用")

    config_path = args.config.expanduser()
    config = read_existing(config_path)
    existing = dict(config)
    if "--transport" not in supplied:
        args.transport = config.get("transport", "webhook")
    for flag, attr in (
        ("--lark-cli-path", "lark_cli_path"),
        ("--lark-profile", "lark_profile"),
        ("--lark-chat-id", "lark_chat_id"),
    ):
        if flag not in supplied:
            setattr(args, attr, config.get(attr, getattr(args, attr)))
    for legacy_key in ("target", "weclaw_path", "auto_start"):
        config.pop(legacy_key, None)
    if args.transport == "lark-cli":
        lark_config = {
            "lark_cli_path": args.lark_cli_path,
            "lark_profile": args.lark_profile.strip(),
            "lark_chat_id": args.lark_chat_id.strip(),
        }
        valid, detail = notifier.validate_lark_cli_config(lark_config)
        if not valid:
            print(detail, file=sys.stderr)
            return 2
        config.pop("webhook_url", None)
        config.update(lark_config)
    else:
        webhook_url = (
            args.webhook_url.strip()
            or os.environ.get("CODEX_FEISHU_WEBHOOK_URL", "").strip()
            or os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
            or (webhook_from_env_file(args.from_env_file) if args.from_env_file else "")
            or config.get("webhook_url", "")
        )
        valid, detail = notifier.validate_webhook_url(webhook_url)
        if not valid:
            print(detail, file=sys.stderr)
            return 2
        for key in ("lark_cli_path", "lark_profile", "lark_chat_id"):
            config.pop(key, None)
        config["webhook_url"] = webhook_url
    config.update(
        {
            "enabled": True,
            "transport": args.transport,
            "title": args.title,
            "min_duration_seconds": max(0, args.min_duration_seconds),
            "include_last_message": args.include_last_message,
            "include_result_summary": not args.no_result_summary,
            "card_notifications": not args.no_cards,
            "notify_on_start": not args.no_start_notice,
            "notify_on_finish_reply": not args.no_finish_reply,
            "live_progress_notifications": not args.no_live_progress,
            "progress_update_interval_seconds": max(
                5, min(60, args.progress_update_seconds)
            ),
            "progress_heartbeat_seconds": max(
                max(5, min(60, args.progress_update_seconds)),
                min(300, args.progress_heartbeat_seconds),
            ),
            "prompt_summary_chars": max(80, min(240, args.prompt_summary_chars)),
            "send_timeout_seconds": 10,
            "retries": 2,
            "max_pending_per_run": 10,
            "notify_ambient_suggestions": args.notify_ambient_suggestions,
        }
    )
    if args.project_name is not None:
        config["project_name"] = args.project_name
    if args.conversation_name is not None:
        config["conversation_name"] = args.conversation_name
    template_values = {
        "lark_completed_template_id": args.completed_template_id,
        "lark_incomplete_template_id": args.incomplete_template_id,
        "lark_interrupted_template_id": args.interrupted_template_id,
        "lark_running_template_id": args.running_template_id,
        "lark_completed_template_version": args.completed_template_version,
        "lark_incomplete_template_version": args.incomplete_template_version,
        "lark_interrupted_template_version": args.interrupted_template_version,
        "lark_running_template_version": args.running_template_version,
    }
    for key, value in template_values.items():
        if value is not None:
            config[key] = value.strip()
    # Patch only explicitly supplied preferences. Existing enable state, retry
    # policy and transport credentials must never change as a side effect.
    option_keys = {
        "--transport": ["transport"],
        "--webhook-url": ["webhook_url"],
        "--from-env-file": ["webhook_url"],
        "--lark-cli-path": ["lark_cli_path"],
        "--lark-profile": ["lark_profile"],
        "--lark-chat-id": ["lark_chat_id"],
        "--title": ["title"],
        "--min-duration-seconds": ["min_duration_seconds"],
        "--include-last-message": ["include_last_message"],
        "--no-result-summary": ["include_result_summary"],
        "--no-cards": ["card_notifications"],
        "--no-start-notice": ["notify_on_start"],
        "--no-finish-reply": ["notify_on_finish_reply"],
        "--no-live-progress": ["live_progress_notifications"],
        "--progress-update-seconds": ["progress_update_interval_seconds"],
        "--progress-heartbeat-seconds": ["progress_heartbeat_seconds"],
        "--prompt-summary-chars": ["prompt_summary_chars"],
        "--notify-ambient-suggestions": ["notify_ambient_suggestions"],
        "--project-name": ["project_name"],
        "--conversation-name": ["conversation_name"],
    }
    for state in ("completed", "incomplete", "interrupted", "running"):
        for field in ("id", "version"):
            option_keys[f"--{state}-template-{field}"] = [
                f"lark_{state}_template_{field}"
            ]
    if existing:
        candidate = config
        config = dict(existing)
        for flag, keys in option_keys.items():
            if flag in supplied:
                for key in keys:
                    if key in candidate:
                        config[key] = candidate[key]
        if "--transport" in supplied:
            for key in ("webhook_url", "lark_cli_path", "lark_profile", "lark_chat_id"):
                if key in candidate:
                    config[key] = candidate[key]
    else:
        # New configurations are opt-in too.
        config["enabled"] = False
    if args.enable or args.disable:
        config["enabled"] = args.enable
    if "--start-delay-seconds" in supplied:
        config["start_notice_delay_seconds"] = max(
            0, min(300, args.start_delay_seconds)
        )
    if "--summary-excluded-project" in supplied:
        config["summary_excluded_projects"] = args.summary_excluded_project
    atomic_write_json(config_path, config)
    print(f"飞书通知配置已保存：{config_path}")
    if args.transport == "lark-cli":
        print(
            f"公司应用机器人：profile={args.lark_profile} " f"chat={args.lark_chat_id}"
        )
    else:
        print("Webhook 配置已保存（地址不显示）。")

    if not args.test:
        return 0
    environment = os.environ.copy()
    environment["CODEX_CLAWBOT_CONFIG"] = str(config_path)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("notifier.py")), "send-test"],
        env=environment,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
