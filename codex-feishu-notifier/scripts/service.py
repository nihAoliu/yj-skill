#!/usr/bin/env python3
"""Keep the standalone WeClaw ClawBot bridge running for Codex hooks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import long_running
import weclaw_adapter


def load_preferences() -> dict[str, object]:
    path = Path.home() / ".config" / weclaw_adapter.PLUGIN_NAME / "config.json"
    value = weclaw_adapter.read_json(path, {})
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure", "status"), default="ensure", nargs="?")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    preferences = load_preferences()
    configured = str(preferences.get("weclaw_path") or "")
    executable = weclaw_adapter.resolve_executable(configured)
    if not executable:
        if not args.quiet:
            print("没有找到本机 WeClaw；请先运行 configure.py。", file=sys.stderr)
        return 0 if args.command == "ensure" else 1

    if args.command == "status":
        running, detail = weclaw_adapter.status(executable)
        print(
            json.dumps(
                {"running": running, "executable": executable, "detail": detail},
                ensure_ascii=False,
            )
        )
        return 0 if running else 1

    running, detail = weclaw_adapter.status(executable)
    if not running:
        running, detail = long_running.kickstart_if_installed(executable)
    if not running:
        running, detail = weclaw_adapter.ensure_running(executable)
    ok = running
    if not args.quiet or not ok:
        stream = sys.stdout if ok else sys.stderr
        print(
            ("微信 ClawBot 后台服务已在线。" if ok else detail),
            file=stream,
        )
    # SessionStart should never prevent Codex from opening.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
