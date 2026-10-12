"""Shared fixtures: a complete in-process system on deterministic local fixtures."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from omca.channels import RecordingChannel
from omca.config import ApplicationConfig, config_from_dict
from omca.contracts import TERMINAL_STATUSES, TaskStatus
from omca.coordinator import Coordinator
from omca.models import register_fixture
from omca.policy import Identity

from .helpers.brain import Brain
from .helpers.site import FixtureSite

OWNER_ID = 111111
STRANGER_ID = 999999

HAS_PLAYWRIGHT = True
try:  # pragma: no cover
    import playwright  # noqa: F401
except ImportError:  # pragma: no cover
    HAS_PLAYWRIGHT = False
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def pytest_collection_modifyitems(config, items):
    live = os.environ.get("OMCA_LIVE") == "1"
    for item in items:
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="live suite: set OMCA_LIVE=1 and configure real routes"))
        if "browser" in item.keywords and not HAS_PLAYWRIGHT:
            item.add_marker(pytest.mark.skip(reason="playwright not installed"))
        if "video" in item.keywords and not HAS_FFMPEG:
            item.add_marker(pytest.mark.skip(reason="ffmpeg not installed"))


@pytest.fixture(scope="session")
def site(tmp_path_factory) -> FixtureSite:
    s = FixtureSite(tmp_path_factory.mktemp("fixture-site"))
    yield s
    s.close()


@pytest.fixture
def smtp_sink():
    from .helpers.smtp import SmtpSink

    sink = SmtpSink()
    yield sink
    sink.close()


@pytest.fixture
def brain() -> Brain:
    b = Brain()
    register_fixture("brain", b)
    return b


def base_config(tmp: Path, site: FixtureSite | None = None, **overrides: Any) -> dict[str, Any]:
    route = {
        "provider": "fixture", "fixture": "brain", "model": "Qwen/Qwen3.5-35B-A3B", "revision": "fixture",
        "license": "apache-2.0", "open_weights": True, "enabled": True, "modalities": ["text", "image"],
        "billing": "none", "coordinate_system": "normalized_1000", "max_image_side": 640, "context_limit": 200000,
    }
    cfg: dict[str, Any] = {
        "testing": True,
        "owner": {"telegram_user_ids": [OWNER_ID], "display_timezone": "Asia/Kolkata"},
        "telegram": {"mode": "disabled"},
        "storage": {"data_dir": str(tmp / "data"), "workspace_root": str(tmp / "workspace")},
        "models": {"allow_fixture_routes": True, "require_probes": False,
                   "roles": {"planning": "local", "vision": "local", "summarisation": "local", "review": "local"},
                   "routes": [{"id": "local", **route}]},
        "search": {"adapter": "searxng", "retrieval_adapter": "http", "searxng": {"base_url": "http://127.0.0.1:9"},
                   "allowed_private_hosts": ["127.0.0.1"]},
        "workers": [{"id": "vm1", "capabilities": ["browser", "video", "document", "spreadsheet", "email", "calendar"]}],
        "browser": {"attachments": [{"id": "vm1-test", "worker_id": "vm1", "mode": "launch", "channel": "chromium",
                                     "headless": True, "authorized": True, "viewport": [1280, 800],
                                     "allowed_private_origins": ["http://127.0.0.1:*"]}]},
        "limits": {"progress_interval_seconds": 3600},
        "video": {"max_coarse_frames": 4, "frames_per_model_call": 4, "max_dense_frames_per_interval": 6,
                  "max_total_frames": 40, "segment_seconds": 600},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


class System:
    """A started coordinator with an in-process worker and a recording Telegram channel."""

    def __init__(self, config: ApplicationConfig, *, worker: bool = True, smtp: Any = None):
        self.config = config
        self.channel = RecordingChannel()
        self.coord = Coordinator(config, channel=self.channel)
        self.worker = None
        self._with_worker = worker
        self._smtp = smtp

    async def start(self) -> System:
        await self.coord.start()
        if self._with_worker:
            from omca.runtime import InProcessWorker

            self.worker = InProcessWorker(self.coord, "vm1", Path(self.config.storage.data_dir) / "worker", smtp=self._smtp)
            await self.worker.start()
        return self

    async def stop(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
        await self.coord.stop()

    def owner(self) -> Identity:
        return Identity("telegram", str(OWNER_ID))

    async def wait(self, task_id: str, statuses: set[TaskStatus] | None = None, timeout: float = 60.0):
        statuses = statuses or set(TERMINAL_STATUSES)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            t = self.coord.tasks.require(task_id)
            if t.status in statuses:
                return t
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"task {task_id} stuck in {t.status} ({t.blocking_reason}); events: "
                                     f"{self.coord.tasks.events(task_id)[-8:]}")
            await asyncio.sleep(0.1)

    async def pending_approval(self, task_id: str, timeout: float = 30.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            aid = self.coord.db.scalar("SELECT id FROM approvals WHERE task_id=? AND status='PENDING' ORDER BY created_at DESC",
                                       (task_id,))
            if aid:
                return self.coord.approvals.get(aid)
            t = self.coord.tasks.require(task_id)
            if t.terminal:
                return None
            await asyncio.sleep(0.1)
        return None

    async def flush(self) -> None:
        await self.coord.notifier.flush()


@pytest.fixture
async def make_system(tmp_path, site, brain):
    systems: list[System] = []

    async def factory(worker: bool = True, smtp: Any = None, **overrides: Any) -> System:
        cfg = config_from_dict(base_config(tmp_path, site, **overrides))
        s = System(cfg, worker=worker, smtp=smtp)
        await s.start()
        systems.append(s)
        return s

    yield factory
    for s in systems:
        await s.stop()
