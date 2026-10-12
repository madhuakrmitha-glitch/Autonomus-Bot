"""Command-line interface: ``omca <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__

DEFAULT_CONFIG = os.environ.get("OMCA_CONFIG", "config/application.yaml")


def _load(path: str):
    from .config import load_config

    return load_config(path)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> int:
    """Create config/application.yaml and config/worker.yaml from the shipped examples."""
    root = Path(args.dir).resolve()
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    pkg_examples = Path(__file__).resolve().parent.parent.parent / "config"
    for name in ("application.example.yaml", "worker.example.yaml"):
        src = pkg_examples / name
        dst = cfg_dir / name.replace(".example", "")
        if dst.exists():
            print(f"exists, not overwritten: {dst}")
            continue
        if not src.exists():
            print(f"example not found: {src}", file=sys.stderr)
            return 1
        shutil.copyfile(src, dst)
        print(f"created {dst}")
    print("Next: store secrets with `omca secrets set <keyring:...>` and fill owner, model and worker settings.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .runtime import serve

    cfg = _load(args.config)
    asyncio.run(serve(cfg, with_worker=args.with_worker))
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from .config import load_worker_config
    from .runtime import run_worker

    asyncio.run(run_worker(load_worker_config(args.config)))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main

    main()
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator
    from .models.probes import probe_ok, run_probes

    cfg = _load(args.config)
    coord = Coordinator(cfg)
    if args.action == "list":
        rows = []
        for r in cfg.models.routes:
            st = coord.registry.state(r)
            rows.append({"id": r.id, "provider": r.provider, "model": r.model, "license": r.license,
                         "modalities": r.modalities, "billing": r.billing, "usable": st.usable, "reasons": st.reasons})
        _print({"roles": cfg.models.roles.model_dump(), "routes": rows})
        return 0
    targets = [r.id for r in cfg.models.routes] if args.route == "all" else [args.route]
    ok_all = True
    for rid in targets:
        results = asyncio.run(run_probes(coord.router, coord.registry, rid))
        ok = probe_ok(results, coord.registry.get(rid))
        ok_all &= ok
        print(f"route {rid}: {'PASS' if ok else 'FAIL'}")
        for r in results:
            print(f"  {'ok ' if r.passed else 'XX '} {r.probe:28s} {json.dumps(r.detail, default=str)[:160]}")
    return 0 if ok_all else 2


def cmd_budget(args: argparse.Namespace) -> int:
    from .budget import BudgetLedger
    from .db import Database

    cfg = _load(args.config)
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    if args.action == "reconcile":
        ledger.reconcile(args.call_id, float(args.amount), note=args.note or "operator reconciliation")
        print("reconciled")
        return 0
    _print(ledger.status().model_dump())
    if args.action == "ledger":
        _print(ledger.ledger_rows())
    return 0


def cmd_endpoint(args: argparse.Namespace) -> int:
    from .budget import BudgetLedger
    from .db import Database
    from .models.endpoints import start_window, stop_window

    cfg = _load(args.config)
    ledger = BudgetLedger(Database(cfg.storage.database_path), cfg)
    if args.action == "list":
        _print([dict(r) for r in ledger.db.query("SELECT * FROM endpoint_sessions ORDER BY started_at DESC LIMIT 20")])
        return 0
    route = cfg.models.route(args.route)
    if route is None:
        print(f"unknown route {args.route}", file=sys.stderr)
        return 1
    if args.action == "start":
        sid = asyncio.run(start_window(ledger, route, args.hours))
        print(f"endpoint window {sid} started for {args.hours} h; it will be paused and settled automatically")
        return 0
    cost = asyncio.run(stop_window(ledger, route, args.session_id))
    print(f"paused; settled {cost:.2f} {cfg.budget.currency}")
    return 0


def cmd_telegram(args: argparse.Namespace) -> int:
    from .channels.commands import OwnerInterface
    from .coordinator import Coordinator

    cfg = _load(args.config)
    coord = Coordinator(cfg)
    if coord.notifier.owner_ids():
        print(f"An owner is already configured or paired: {coord.notifier.owner_ids()}")
        return 1
    code = OwnerInterface(coord).start_pairing()
    print(f"Send this message to the bot from the owner's Telegram account within "
          f"{cfg.telegram.pairing_code_ttl_seconds // 60} minutes:\n\n  /pair {code}\n")
    print("Then add the paired numeric user id to owner.telegram_user_ids in the configuration.")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    import getpass

    from .secrets import store_keyring
    from .util import random_token as token

    value = token(32) if args.generate else getpass.getpass(f"value for {args.ref}: ")
    store_keyring(args.ref, value)
    print(f"stored {args.ref}" + (" (generated; shown once below)\n" + value if args.generate and args.show else ""))
    return 0


def cmd_contracts(args: argparse.Namespace) -> int:
    from .schemas import export_contracts

    out = Path(args.out)
    paths = export_contracts(out)
    for p in paths:
        print(f"wrote {p}")
    return 0


def cmd_openclaw(args: argparse.Namespace) -> int:
    from .integrations.openclaw import translate

    cfg = _load(args.config)
    print(translate(cfg, mcp_command=args.mcp_command))
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator

    coord = Coordinator(_load(args.config))
    _print(coord.apply_retention())
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    from .coordinator import Coordinator

    coord = Coordinator(_load(args.config))
    if args.task_id:
        t = coord.tasks.by_short_id(args.task_id) or coord.tasks.get(args.task_id)
        if t is None:
            print("unknown task", file=sys.stderr)
            return 1
        print(coord.status_text(t))
        _print({"result": t.result, "events": coord.tasks.events(t.id)[-30:]})
        return 0
    for t in coord.tasks.list(limit=args.limit):
        print(f"{t.id}  {t.status.value:<17} {t.workflow:<17} {t.goal[:70]}")
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    from .fixture_runner import run_fixture_suite

    return run_fixture_suite(repeat=args.repeat, out_dir=Path(args.out), extra=args.pytest_args)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="omca", description=f"Open Model Computer Assistant {__version__}")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create configuration files from the examples")
    s.add_argument("--dir", default=".")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run the coordinator service (API, scheduler, Telegram)")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--with-worker", metavar="WORKER_ID", help="also run this worker in-process (single-VM setup)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("worker", help="run a worker in this VM's interactive session")
    s.add_argument("--config", default=os.environ.get("OMCA_WORKER_CONFIG", "config/worker.yaml"))
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("mcp", help="run the stdio MCP server for OpenClaw")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("models", help="list routes or run capability probes")
    s.add_argument("action", choices=["list", "probe"])
    s.add_argument("route", nargs="?", default="all")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("budget", help="budget status, ledger and reconciliation")
    s.add_argument("action", nargs="?", choices=["status", "ledger", "reconcile"], default="status")
    s.add_argument("call_id", nargs="?")
    s.add_argument("amount", nargs="?")
    s.add_argument("--note")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("endpoint", help="start/stop an approved window on a time-billed dedicated endpoint")
    s.add_argument("action", choices=["start", "stop", "list"])
    s.add_argument("route", nargs="?")
    s.add_argument("session_id", nargs="?")
    s.add_argument("--hours", type=float, default=1.0)
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_endpoint)

    s = sub.add_parser("telegram", help="owner pairing for direct mode")
    s.add_argument("action", choices=["pair"])
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_telegram)

    s = sub.add_parser("secrets", help="store a secret in the OS credential store")
    s.add_argument("action", choices=["set"])
    s.add_argument("ref", help="keyring:SERVICE/USERNAME")
    s.add_argument("--generate", action="store_true", help="generate a random token")
    s.add_argument("--show", action="store_true", help="print a generated token once")
    s.set_defaults(fn=cmd_secrets)

    s = sub.add_parser("contracts", help="export contracts/schemas.json and contracts/openapi.yaml")
    s.add_argument("action", choices=["export"])
    s.add_argument("--out", default="contracts")
    s.set_defaults(fn=cmd_contracts)

    s = sub.add_parser("openclaw", help="print the OpenClaw configuration translated from application.yaml")
    s.add_argument("action", choices=["config"])
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--mcp-command", default="omca")
    s.set_defaults(fn=cmd_openclaw)

    s = sub.add_parser("retention", help="apply retention now")
    s.add_argument("action", choices=["run"])
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_retention)

    s = sub.add_parser("tasks", help="list tasks or show one")
    s.add_argument("task_id", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("fixtures", help="run the acceptance fixture suite (repeated) and record results")
    s.add_argument("--repeat", type=int, default=3)
    s.add_argument("--out", default="data/fixture-results")
    s.add_argument("pytest_args", nargs="*")
    s.set_defaults(fn=cmd_fixtures)

    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
