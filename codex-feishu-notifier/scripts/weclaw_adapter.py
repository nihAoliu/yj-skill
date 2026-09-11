#!/usr/bin/env python3
"""Small, secret-safe adapter around the standalone WeClaw ClawBot client."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Any


PLUGIN_NAME = "codex-clawbot-notifier"


def read_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def weclaw_home() -> Path:
    explicit = os.environ.get("WECLAW_HOME")
    return Path(explicit).expanduser() if explicit else Path.home() / ".weclaw"


def discover_accounts() -> list[dict[str, str]]:
    """Return non-secret account metadata from the existing WeClaw login."""
    accounts_dir = weclaw_home() / "accounts"
    discovered: list[dict[str, str]] = []
    try:
        files = sorted(accounts_dir.glob("*.json"))
    except OSError:
        return []
    for path in files:
        if path.name.endswith(".sync.json"):
            continue
        value = read_json(path, {})
        if not isinstance(value, dict):
            continue
        bot_id = str(value.get("ilink_bot_id") or "").strip()
        user_id = str(value.get("ilink_user_id") or "").strip()
        if not bot_id.endswith("@im.bot") or not user_id.endswith("@im.wechat"):
            continue
        discovered.append(
            {
                "account": bot_id,
                "target": user_id,
                "source": str(path),
            }
        )
    return discovered


def _candidate_executables(configured: str = "") -> list[Path | str]:
    candidates: list[Path | str] = []
    environment = os.environ.get("CODEX_CLAWBOT_WECLAW_PATH")
    if environment:
        candidates.append(Path(environment).expanduser())
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            "weclaw",
            Path.home() / ".local" / "bin" / "weclaw",
            Path.home() / "Documents" / "HLTV Monitor" / ".tools" / "bin" / "weclaw",
        ]
    )
    return candidates


def resolve_executable(configured: str = "") -> str | None:
    seen: set[str] = set()
    for candidate in _candidate_executables(configured):
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(candidate, str):
            found = shutil.which(candidate)
            if found:
                return found
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run(
    executable: str,
    arguments: list[str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *arguments],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-400:] if text else f"exit code {result.returncode}"


def _api_is_listening() -> bool:
    address = os.environ.get("CODEX_CLAWBOT_API_ADDR", "127.0.0.1:18011")
    host, separator, port_text = address.rpartition(":")
    if not separator or not host:
        return False
    try:
        with socket.create_connection((host, int(port_text)), timeout=0.4):
            return True
    except (OSError, ValueError):
        pass
    # Codex sandboxes can deny loopback connects while still allowing a
    # read-only view of the local listening table.
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        result = subprocess.run(
            [lsof, "-nP", f"-iTCP:{int(port_text)}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def status(executable: str) -> tuple[bool, str]:
    try:
        result = _run(executable, ["status"], 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法检查微信 ClawBot 状态：{exc}"
    output = f"{result.stdout}\n{result.stderr}".lower()
    running = result.returncode == 0 and "not running" not in output and "stale" not in output
    if not running and _api_is_listening():
        return True, "微信 ClawBot 本地服务端口已在线"
    return running, _detail(result)


def ensure_running(executable: str) -> tuple[bool, str]:
    running, detail = status(executable)
    if running:
        return True, detail
    try:
        result = _run(executable, ["start"], 12)
    except subprocess.TimeoutExpired:
        return False, "启动微信 ClawBot 后台服务超时"
    except OSError as exc:
        return False, f"无法启动微信 ClawBot 后台服务：{exc}"
    if result.returncode != 0:
        return False, f"微信 ClawBot 启动失败：{_detail(result)}"
    running, status_detail = status(executable)
    if not running:
        return False, f"微信 ClawBot 启动后仍未在线：{status_detail}"
    return True, status_detail


def send_text(
    executable: str,
    target: str,
    message: str,
    timeout_seconds: int = 10,
) -> tuple[bool, str]:
    """Send without a shell so message contents are never interpreted as code."""
    try:
        result = _run(
            executable,
            ["send", "--to", target, "--text", message],
            timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"微信通知发送超时（{timeout_seconds} 秒）"
    except OSError as exc:
        return False, f"无法调用微信 ClawBot：{exc}"
    if result.returncode == 0:
        return True, ""
    return False, f"微信 ClawBot 发送失败：{_detail(result)}"
