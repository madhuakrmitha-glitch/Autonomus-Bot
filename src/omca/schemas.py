"""Generate the published contract files from the code that enforces them."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .contracts import CONTRACT_MODELS
from .policy.operations import OPERATIONS


def contract_document() -> dict[str, Any]:
    contracts = {name: model.model_json_schema(mode="validation") for name, model in sorted(CONTRACT_MODELS.items())}
    operations = {}
    for name, spec in sorted(OPERATIONS.items()):
        operations[name] = {
            "description": spec.description,
            "executor": spec.executor,
            "capability": spec.capability,
            "needs_ui_lease": spec.needs_ui_lease,
            "retry_safe": spec.retry_safe,
            "arguments": spec.args.model_json_schema(mode="validation"),
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Open Model Computer Assistant application contracts",
        "version": __version__,
        "note": ("Proposed application interfaces, not OpenClaw API schemas. Schema validation does not establish "
                 "permission: every operation is also classified by the policy gate (see docs/policy.md)."),
        "contracts": contracts,
        "operations": operations,
    }


def openapi_document() -> dict[str, Any]:
    from .api import create_app
    from .config import config_from_dict
    from .coordinator import Coordinator

    with tempfile.TemporaryDirectory() as td:
        cfg = config_from_dict({"storage": {"data_dir": f"{td}/data", "workspace_root": f"{td}/ws"},
                                "telegram": {"mode": "disabled"}})
        coord = Coordinator(cfg)
        doc = create_app(coord).openapi()
        coord.db.close()
    return doc


def export_contracts(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    schemas = out_dir / "schemas.json"
    schemas.write_text(json.dumps(contract_document(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    openapi = out_dir / "openapi.yaml"
    openapi.write_text(yaml.safe_dump(openapi_document(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return [schemas, openapi]
