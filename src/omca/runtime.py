"""Process wiring: coordinator service, in-process worker and standalone worker process."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .config import ApplicationConfig, BrowserAttachment, SmtpConfig, WorkerProcessConfig
from .coordinator import Coordinator
from .secrets import require, resolve

log = logging.getLogger("omca")


def build_channel(config: ApplicationConfig):
    if config.telegram.mode == "disabled":
        return None
    token = resolve(config.telegram.bot_token_ref)
    if not token:
        log.warning("Telegram bot token is not available (%s); owner messages %s",
                    config.telegram.bot_token_ref,
                    "go to the web UI only" if config.ui.enabled else "cannot be delivered")
        return None
    from .channels.telegram import TelegramBotApi

    return TelegramBotApi(token, config.telegram.api_base)


def build_worker_handlers(attachments: list[BrowserAttachment], *, smtp: SmtpConfig | None = None,
                          allowed_extensions: list[str] | None = None, max_text_chars: int = 20000,
                          capabilities: list[str] | None = None) -> tuple[dict[str, Any], Any]:
    caps = set(capabilities or ["browser", "video", "document", "spreadsheet", "email", "calendar"])
    handlers: dict[str, Any] = {}
    browser = None
    if "browser" in caps:
        try:
            from .worker.browser import BrowserManager, make_browser_handlers

            browser = BrowserManager(attachments, max_text_chars)
            handlers.update(make_browser_handlers(browser, allowed_extensions=allowed_extensions))
        except ImportError:
            log.warning("Playwright is not installed; browser capability disabled")
    if "video" in caps:
        from .worker.media import make_media_handlers

        handlers.update(make_media_handlers())
    if caps & {"spreadsheet", "email", "calendar", "desktop", "document"}:
        from .worker.office import make_office_handlers

        handlers.update(make_office_handlers(smtp))
    return handlers, browser


class InProcessWorker:
    """Runs a worker inside the coordinator process (single-VM prototype and tests).

    Browser automation still needs an interactive desktop session: run ``omca run`` from the
    logged-in user's session (e.g. a Task Scheduler "at log on" task), not as a service.
    """

    def __init__(self, coord: Coordinator, worker_id: str, workspace: Path, capabilities: list[str] | None = None,
                 smtp: SmtpConfig | None = None):
        from .worker.agent import InProcessTransport, WorkerAgent

        entry = coord.config.worker(worker_id)
        if entry is None:
            raise ValueError(f"worker {worker_id} is not in the configuration")
        atts = [a for a in coord.config.browser.attachments if a.worker_id == worker_id]
        caps = capabilities or list(entry.capabilities)
        handlers, self.browser = build_worker_handlers(
            atts, smtp=smtp, allowed_extensions=coord.config.policy.download_allowed_extensions,
            max_text_chars=coord.config.browser.max_page_text_chars, capabilities=caps)
        self.agent = WorkerAgent(worker_id, InProcessTransport(coord, worker_id), workspace=workspace,
                                 capabilities=caps, attachments=atts, handlers=handlers, claim_wait_seconds=1.0)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.agent.start()
        self._task = asyncio.ensure_future(self.agent.run_loop_registered())

    async def stop(self) -> None:
        self.agent.stop()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.browser is not None:
            await self.browser.close()


async def serve(config: ApplicationConfig, *, with_worker: str | None = None) -> None:
    import uvicorn

    from .api import create_app
    from .channels.commands import OwnerInterface

    coord = Coordinator(config, channel=build_channel(config))
    iface = OwnerInterface(coord)
    app = create_app(coord, iface)
    await coord.start()
    extra: list[asyncio.Task] = []
    workers: list[InProcessWorker] = []
    stop = asyncio.Event()
    if config.telegram.mode == "direct" and coord.notifier.channel is not None:
        from .channels.telegram import TelegramPoller

        poller = TelegramPoller(coord.notifier.channel, iface, coord.db, config.telegram.poll_timeout_seconds)
        extra.append(asyncio.ensure_future(poller.run(stop)))
    if with_worker:
        w = InProcessWorker(coord, with_worker, Path(config.storage.data_dir) / "worker" / with_worker)
        await w.start()
        workers.append(w)
    ucfg = uvicorn.Config(app, host=config.server.host, port=config.server.port, log_level="info",
                          ssl_certfile=config.server.tls_cert_file, ssl_keyfile=config.server.tls_key_file)
    server = uvicorn.Server(ucfg)
    try:
        await server.serve()
    finally:
        stop.set()
        for w in workers:
            await w.stop()
        for t in extra:
            t.cancel()
        await coord.stop()


async def run_worker(cfg: WorkerProcessConfig) -> None:
    from .worker.agent import HttpTransport, WorkerAgent

    token = require(cfg.credential_ref, f"worker {cfg.worker_id} credential")
    transport = HttpTransport(cfg.coordinator_url, cfg.worker_id, token, cfg.ca_file)
    handlers, browser = build_worker_handlers(cfg.attachments, smtp=cfg.smtp,
                                              allowed_extensions=cfg.allowed_download_extensions,
                                              capabilities=list(cfg.capabilities))
    agent = WorkerAgent(cfg.worker_id, transport, workspace=cfg.workspace_dir, capabilities=list(cfg.capabilities),
                        attachments=cfg.attachments, handlers=handlers, claim_wait_seconds=cfg.claim_wait_seconds)
    try:
        await agent.run()
    finally:
        if browser is not None:
            await browser.close()
