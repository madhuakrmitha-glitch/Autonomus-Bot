"""Cross-platform pieces: macOS configuration, LaunchAgent, desktop key mapping, dotenv secrets."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from omca import cli
from omca.config import load_config, load_worker_config
from omca.platforms import adapt_example, default_browser_channel, launch_agent_plist
from omca.secrets import resolve, validate_ref
from omca.worker.agent import JobFailure
from omca.worker.desktop import backend_for_platform, pyautogui_chord, pywinauto_chord

ROOT = Path(__file__).resolve().parent.parent


def test_init_writes_macos_defaults(tmp_path):
    assert cli.main(["init", "--dir", str(tmp_path), "--platform", "macos"]) == 0
    cfg = load_config(tmp_path / "config" / "application.yaml")
    att = cfg.browser.attachments[0]
    assert att.id == "vm1-chrome" and att.channel == "chrome"
    assert att.user_data_dir == os.path.expanduser("~/Library/Application Support/OMCA/profiles/vm1-chrome")
    worker = load_worker_config(tmp_path / "config" / "worker.yaml")
    assert worker.attachments[0].id == "vm1-chrome" and worker.attachments[0].channel == "chrome"
    assert "C:/" not in (tmp_path / "config" / "application.yaml").read_text()


def test_windows_example_is_unchanged():
    text = (ROOT / "config" / "application.example.yaml").read_text()
    assert adapt_example(text, "windows") == text
    assert default_browser_channel("windows") == "msedge" and default_browser_channel("macos") == "chrome"


@pytest.mark.parametrize("mode,tail", [
    ("all-in-one", ["omca", "run", "--with-worker", "mac1"]),
    ("coordinator", ["omca", "run"]),
    ("worker", ["omca", "worker", "--config", "config/worker.yaml"]),
])
def test_launch_agent_runs_in_the_gui_session(mode, tail):
    plist = plistlib.loads(launch_agent_plist(mode=mode, app_dir=Path("/Users/me/OMCA/app"), uv="/opt/homebrew/bin/uv",
                                              worker_id="mac1"))
    assert plist["LimitLoadToSessionType"] == "Aqua" and plist["ProcessType"] == "Interactive"
    assert plist["ProgramArguments"][:4] == ["/usr/bin/caffeinate", "-i", "/opt/homebrew/bin/uv", "run"]
    assert plist["ProgramArguments"][4:] == tail
    assert plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"] is True
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"]
    assert "-di" in plistlib.loads(launch_agent_plist(mode=mode, app_dir=Path("/x"), uv="uv",
                                                      keep_display_awake=True))["ProgramArguments"]


def test_dotenv_secret_references(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# shared with OpenClaw\nexport OMCA_BRIDGE_TOKEN="abc123"\nOTHER=x\nEMPTY=\n', encoding="utf-8")
    assert validate_ref(f"dotenv:{env}#OMCA_BRIDGE_TOKEN", "f")
    assert resolve(f"dotenv:{env}#OMCA_BRIDGE_TOKEN") == "abc123"
    assert resolve(f"dotenv:{env}#OTHER") == "x"
    assert resolve(f"dotenv:{env}#EMPTY") is None
    assert resolve(f"dotenv:{env}#MISSING") is None
    assert resolve(f"dotenv:{tmp_path}/nope.env#X") is None


def test_key_chords_map_per_platform():
    assert pywinauto_chord("Control+s") == "^s"
    assert pywinauto_chord("Control+Shift+Enter") == "^+{ENTER}"
    assert pywinauto_chord("F5") == "{F5}"
    assert pyautogui_chord("Meta+Shift+4") == ["command", "shift", "4"]
    assert pyautogui_chord("Alt+Tab") == ["option", "tab"]
    for bad in ("", "Hyper+x", "Control+Banana"):
        with pytest.raises(JobFailure):
            pyautogui_chord(bad)
    with pytest.raises(JobFailure):
        pywinauto_chord("Meta+r")


def test_raw_desktop_input_is_unavailable_on_linux():
    with pytest.raises(JobFailure) as e:
        backend_for_platform("linux")
    assert e.value.code.value == "UNSUPPORTED_CAPABILITY"
