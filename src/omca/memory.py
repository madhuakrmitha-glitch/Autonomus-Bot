"""Stable owner memory (FR13): preferences, reusable procedures and source freshness rules.

These persist separately from temporary observations. Preferences may guide style and
workflow choices; they never stand in for current facts, which are rechecked against
current sources (freshness rules decide how old a source may be).
"""

from __future__ import annotations

import fnmatch
from datetime import timedelta
from typing import Any

from .db import Database, dumps, loads
from .util import new_id, now_iso, parse_iso, utcnow


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # -- preferences ---------------------------------------------------------------------------
    def set_preference(self, key: str, value: Any, source: str = "owner") -> None:
        if not key or len(key) > 80:
            raise ValueError("preference keys must be 1-80 characters")
        self.db.execute(
            """INSERT INTO preferences(key, value_json, source, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, source=excluded.source,
               updated_at=excluded.updated_at""",
            (key, dumps(value), source, now_iso()),
        )

    def preferences(self) -> dict[str, Any]:
        return {r["key"]: loads(r["value_json"]) for r in self.db.query("SELECT * FROM preferences ORDER BY key")}

    def delete_preference(self, key: str) -> bool:
        return self.db.execute("DELETE FROM preferences WHERE key=?", (key,)).rowcount > 0

    # -- procedures ------------------------------------------------------------------------------
    def save_procedure(self, name: str, description: str, steps: list[str]) -> None:
        existing = self.db.one("SELECT id, version FROM procedures WHERE name=?", (name,))
        if existing:
            self.db.execute("UPDATE procedures SET description=?, steps_json=?, version=version+1, updated_at=? WHERE id=?",
                            (description, dumps(steps), now_iso(), existing["id"]))
        else:
            self.db.execute(
                "INSERT INTO procedures(id, name, description, steps_json, version, updated_at) VALUES (?,?,?,?,1,?)",
                (new_id("prc"), name, description, dumps(steps), now_iso()),
            )

    def procedures(self) -> list[dict[str, Any]]:
        return [{"name": r["name"], "description": r["description"], "steps": loads(r["steps_json"], []),
                 "version": r["version"]} for r in self.db.query("SELECT * FROM procedures ORDER BY name")]

    # -- freshness ---------------------------------------------------------------------------------
    def set_freshness(self, pattern: str, max_age_days: float, note: str | None = None) -> None:
        existing = self.db.one("SELECT id FROM freshness_rules WHERE pattern=?", (pattern,))
        if existing:
            self.db.execute("UPDATE freshness_rules SET max_age_days=?, note=?, updated_at=? WHERE id=?",
                            (max_age_days, note, now_iso(), existing["id"]))
        else:
            self.db.execute("INSERT INTO freshness_rules(id, pattern, max_age_days, note, updated_at) VALUES (?,?,?,?,?)",
                            (new_id("frs"), pattern, max_age_days, note, now_iso()))

    def freshness_rules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT pattern, max_age_days, note FROM freshness_rules ORDER BY pattern")]

    def is_stale(self, url: str, source_date: str | None) -> tuple[bool, str | None]:
        """Whether a source is older than the strictest matching freshness rule."""
        if not source_date:
            return False, None
        dt = parse_iso(source_date if "T" in source_date else f"{source_date}T00:00:00Z")
        if dt is None:
            return False, None
        for rule in self.freshness_rules():
            if fnmatch.fnmatch(url, rule["pattern"]) or rule["pattern"] in url:
                if utcnow() - dt > timedelta(days=rule["max_age_days"]):
                    return True, f"source is older than {rule['max_age_days']:g} days ({rule['pattern']})"
        return False, None

    def prompt_context(self) -> str:
        prefs = self.preferences()
        if not prefs:
            return ""
        lines = [f"- {k}: {v}" for k, v in prefs.items()]
        return "Owner style and workflow preferences (not facts; verify facts against sources):\n" + "\n".join(lines)
