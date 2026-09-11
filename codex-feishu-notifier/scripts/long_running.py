#!/usr/bin/env python3
"""Install and control a per-user macOS service for the WeClaw bridge."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import time

import weclaw_adapter


LABEL = "com.codex.clawbot-notifier.weclaw"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launch_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def managed_executable_path() -> Path:
    return (
        Path.home()
        / ".local"
        / "share"
        / weclaw_adapter.PLUGIN_NAME
        / "bin"
        / "weclaw"
    )


def stage_executable(source: str) -> str:
    """Copy out of privacy-protected folders so launchd can load the binary."""
    source_path = Path(source).expanduser().resolve()
    target = managed_executable_path()
    if source_path == target.resolve():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".weclaw.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(source_path, temporary_path)
        os.chmod(temporary_path, 0o700)
        os.replace(temporary_path, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()
    return str(target)


def _launchctl(arguments: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _write_plist(executable: str) -> Path:
    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path.home() / ".local" / "share" / weclaw_adapter.PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(data_dir, 0o700)
    value = {
        "Label": LABEL,
        "ProgramArguments": [executable, "start", "--foreground"],
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(data_dir / "weclaw-service.log"),
        "StandardErrorPath": str(data_dir / "weclaw-service.error.log"),
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


def wait_until_running(executable: str, seconds: float = 4.0) -> tuple[bool, str]:
    deadline = time.monotonic() + seconds
    detail = ""
    while time.monotonic() < deadline:
        running, detail = weclaw_adapter.status(executable)
        if running:
            return True, detail
        time.sleep(0.25)
    return False, detail


def install_service(executable: str) -> tuple[bool, str]:
    if os.uname().sysname != "Darwin":
        return weclaw_adapter.ensure_running(executable)
    path = _write_plist(executable)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _launchctl(["bootout", launch_target()])
    # Hand ownership to launchd so there is only one bridge process.
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [executable, "stop"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    release_deadline = time.monotonic() + 5
    while weclaw_adapter._api_is_listening() and time.monotonic() < release_deadline:
        time.sleep(0.2)
    # WeClaw also uses a single-instance lock that can outlive the listening socket briefly.
    time.sleep(0.5)
    result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(path)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"无法安装微信 ClawBot 常驻服务：{detail[-400:]}"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _launchctl(["enable", launch_target()])
        _launchctl(["kickstart", "-k", launch_target()])
    running, detail = wait_until_running(executable)
    if not running:
        return False, f"微信 ClawBot 常驻服务已安装，但尚未在线：{detail}"
    return True, str(path)


def kickstart_if_installed(executable: str) -> tuple[bool, str]:
    path = launch_agent_path()
    if not path.is_file():
        return False, "常驻服务尚未安装"
    try:
        result = _launchctl(["kickstart", launch_target()])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法唤醒微信 ClawBot 常驻服务：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"无法唤醒微信 ClawBot 常驻服务：{detail[-400:]}"
    return wait_until_running(executable)
