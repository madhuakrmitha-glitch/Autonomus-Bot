"""Platform specifics: Windows (reference), macOS and Linux.

Browser and desktop automation must run in the logged-in user's *graphical* session on every
platform: a Task Scheduler "at log on" task on Windows, a LaunchAgent limited to the Aqua session
on macOS. Services and daemons cannot reach the desktop.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
from pathlib import Path
from typing import Literal

Platform = Literal["windows", "macos", "linux"]


def current() -> Platform:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def default_browser_channel(platform: Platform) -> str:
    return {"windows": "msedge", "macos": "chrome", "linux": "chromium"}[platform]


def default_profile_dir(platform: Platform, attachment_id: str) -> str:
    return {
        "windows": f"C:/OMCA/profiles/{attachment_id}",
        "macos": f"~/Library/Application Support/OMCA/profiles/{attachment_id}",
        "linux": f"~/.local/share/omca/profiles/{attachment_id}",
    }[platform]


def adapt_example(text: str, platform: Platform) -> str:
    """Rewrite the Windows-flavoured example configuration for another platform (text-preserving)."""
    if platform == "windows":
        return text
    channel = default_browser_channel(platform)
    att = f"vm1-{channel}"
    text = text.replace("vm1-edge", att)
    text = re.sub(r"channel: msedge", f"channel: {channel}", text)
    text = re.sub(r"user_data_dir: C:/OMCA/profiles/[\w-]+", f'user_data_dir: "{default_profile_dir(platform, att)}"', text)
    text = text.replace("display_name: Windows VM one", "display_name: Worker one")
    if platform == "macos":
        text = text.replace("# Run it in the VM's interactive desktop session (Task Scheduler: \"At log on\", \"Run only when user\n"
                            "# is logged on\"), never as a service", "# Run it in the logged-in user's Aqua session (`omca service install`), never as a daemon")
        text = re.sub(r"#   msedge\.exe --remote-debugging-port=9222 --user-data-dir=C:\\OMCA\\profiles\\attach",
                      '#   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 '
                      '--user-data-dir="$HOME/Library/Application Support/OMCA/profiles/attach"', text)
    return text


# ---------------------------------------------------------------------------
# macOS LaunchAgent
# ---------------------------------------------------------------------------

LABEL_PREFIX = "ai.omca"


def launch_agent_label(mode: str) -> str:
    return f"{LABEL_PREFIX}.{'worker' if mode == 'worker' else 'coordinator'}"


def omca_arguments(mode: str, worker_id: str, worker_config: str = "config/worker.yaml") -> list[str]:
    if mode == "all-in-one":
        return ["omca", "run", "--with-worker", worker_id]
    if mode == "coordinator":
        return ["omca", "run"]
    if mode == "worker":
        return ["omca", "worker", "--config", worker_config]
    raise ValueError(f"unknown mode {mode}")


def launch_agent_plist(*, mode: str, app_dir: Path, uv: str, worker_id: str = "vm1", keep_display_awake: bool = False,
                       log_dir: Path | None = None) -> bytes:
    """A per-user LaunchAgent that runs only in the GUI (Aqua) session and keeps the Mac awake.

    ``caffeinate -i`` prevents idle sleep while OMCA runs; ``-d`` also keeps the display awake, which
    raw desktop input needs (a locked or sleeping screen blocks synthetic input).
    """
    label = launch_agent_label(mode)
    logs = log_dir or (Path.home() / "Library" / "Logs" / "OMCA")
    flags = "-di" if keep_display_awake else "-i"
    data = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/caffeinate", flags, uv, "run", *omca_arguments(mode, worker_id)],
        "WorkingDirectory": str(app_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / f"{label}.out.log"),
        "StandardErrorPath": str(logs / f"{label}.err.log"),
        "EnvironmentVariables": {
            # Homebrew (Apple silicon and Intel) for ffmpeg/ffprobe; launchd does not read shell profiles
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }
    return plistlib.dumps(data)


def launch_agent_path(mode: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launch_agent_label(mode)}.plist"


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for cand in (Path.home() / ".local" / "bin" / "uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if cand.exists():
            return str(cand)
    raise FileNotFoundError("uv was not found; install it first (brew install uv)")


def gui_domain() -> str:
    return f"gui/{os.getuid()}"  # type: ignore[attr-defined]
