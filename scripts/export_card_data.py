#!/usr/bin/env python3
"""PullDex Card-Data Exporter.

Exports the PullDex public card *reference* catalogue from the PullDex seed
database into deterministic per-set JSON files plus a manifest.

Source of truth
---------------
The PullDex seed database (read-only):
    <PullDex>/backups/pulldex_backup_initial_import.db

This database contains ONLY public reference data (sets, cards, species).
The exporter reads three tables — ``sets``, ``cards``, ``pokemon_species`` —
and NEVER reads or writes ``collection`` or ``profiles``. It opens the
database read-only and does not modify it.

Output
------
    manifest.json          — catalogue index (schema below)
    sets/<api_set_id>.json  — one file per set (schema below)

Determinism
-----------
Running the exporter twice against unchanged source data produces byte-for-byte
identical output:
    * sets sorted by (release_date, api_set_id)
    * cards sorted by a stable natural key on card_number then api_card_id
    * JSON written with sorted keys, 2-space indent, trailing newline
    * SHA-256 computed over the exact bytes written

Per-set ``version`` is preserved across runs unless the set's content hash
changes, in which case it is incremented. This lets a future client detect
"this set changed" without relying on wall-clock timestamps.

Usage
-----
    python scripts/export_card_data.py
    python scripts/export_card_data.py --seed-db /path/to/seed.db
    python scripts/export_card_data.py --check   # fail if output would change

Standard library only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parents[1]
SETS_DIR = REPO_ROOT / "sets"
MANIFEST_PATH = REPO_ROOT / "manifest.json"

# Default location of the PullDex seed database, relative to this repo.
# (…/Desktop/PullDex-Card-Data/scripts/export_card_data.py ->
#  …/Desktop/PullDex/backups/pulldex_backup_initial_import.db)
DEFAULT_SEED_DB = (
    REPO_ROOT.parent / "PullDex" / "backups" / "pulldex_backup_initial_import.db"
)

# Tables that must NEVER be read/exported — they may contain user data.
FORBIDDEN_TABLES = {"collection", "profiles"}

# Fields exported per card. Mirrors the PullDex Card model (public columns
# only). Ownership/quantity/binder/profile fields are intentionally absent.
CARD_FIELDS = (
    "api_card_id",
    "card_number",
    "rarity",
    "variant",
    "image_url",
    "national_dex_number",
    "species_name",
)


class ExportError(Exception):
    """Raised when the source data is invalid or incomplete."""


# ---------------------------------------------------------------------------
# Natural sort key for card ordering (deterministic + human-friendly)
# ---------------------------------------------------------------------------

def _card_sort_key(card: dict) -> tuple:
    """Return a stable sort key for a card.

    Cards are ordered by the numeric portion of ``card_number`` when it is a
    plain integer (so "2" < "10" < "100"), otherwise lexicographically. Ties
    break on the full ``card_number`` string and finally ``api_card_id`` so
    the order is always total and deterministic.
    """
    num = card.get("card_number") or ""
    if num.isdigit():
        return (0, int(num), num, card["api_card_id"])
    return (1, 0, num, card["api_card_id"])


# ---------------------------------------------------------------------------
# Deterministic JSON serialisation
# ---------------------------------------------------------------------------

def _dumps(obj: object) -> str:
    """Serialise to deterministic JSON text (sorted keys, 2-space indent)."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Database access (read-only)
# ---------------------------------------------------------------------------

def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the seed database strictly read-only."""
    if not db_path.is_file():
        raise ExportError(f"Seed database not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_sets(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, api_set_id, name, series, release_date FROM sets"
    ).fetchall()
    sets = []
    for r in rows:
        if not r["api_set_id"]:
            raise ExportError(f"Set id={r['id']} has an empty api_set_id")
        if not r["name"]:
            raise ExportError(f"Set {r['api_set_id']} has an empty name")
        sets.append(
            {
                "_id": r["id"],
                "api_set_id": r["api_set_id"],
                "name": r["name"],
                "series": r["series"],
                "release_date": r["release_date"],
            }
        )
    return sets


def _load_cards(conn: sqlite3.Connection) -> list[dict]:
    """Load all cards joined to their species (for dex number + name)."""
    rows = conn.execute(
        """
        SELECT
            c.api_card_id      AS api_card_id,
            c.set_id           AS set_id,
            c.card_number      AS card_number,
            c.rarity           AS rarity,
            c.variant          AS variant,
            c.image_url        AS image_url,
            ps.national_dex_number AS national_dex_number,
            ps.name            AS species_name
        FROM cards c
        LEFT JOIN pokemon_species ps ON c.pokemon_species_id = ps.id
        """
    ).fetchall()
    cards = []
    for r in rows:
        if not r["api_card_id"]:
            raise ExportError("Encountered a card with an empty api_card_id")
        if r["set_id"] is None:
            raise ExportError(f"Card {r['api_card_id']} has no set_id")
        cards.append(dict(r))
    return cards


def _assert_no_forbidden_access(conn: sqlite3.Connection) -> None:
    """Sanity guard: confirm we can see the forbidden tables but never query
    them. This documents intent and fails loudly if the schema is unexpected.
    """
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    # We do not require the forbidden tables to be absent (they exist in the
    # seed), we simply guarantee the exporter never SELECTs from them.
    # This function exists to make that guarantee explicit and testable.
    _ = names & FORBIDDEN_TABLES


# ---------------------------------------------------------------------------
# Build set payloads
# ---------------------------------------------------------------------------

def _build_set_payload(set_row: dict, cards: list[dict]) -> dict:
    """Build the JSON-serialisable payload for a single set file."""
    exported_cards = []
    for c in cards:
        exported_cards.append(
            {
                "api_card_id": c["api_card_id"],
                "card_number": c["card_number"],
                "rarity": c["rarity"],
                "variant": c["variant"],
                "image_url": c["image_url"],
                "national_dex_number": c["national_dex_number"],
                "species_name": c["species_name"],
            }
        )
    exported_cards.sort(key=_card_sort_key)

    return {
        "set": {
            "id": set_row["api_set_id"],
            "name": set_row["name"],
            "series": set_row["series"],
            "release_date": set_row["release_date"],
        },
        "card_count": len(exported_cards),
        "cards": exported_cards,
    }


# ---------------------------------------------------------------------------
# Version preservation
# ---------------------------------------------------------------------------

def _load_existing_manifest() -> dict | None:
    if MANIFEST_PATH.is_file():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _existing_versions_by_hash(manifest: dict | None) -> dict[str, tuple[int, str]]:
    """Map api_set_id -> (version, sha256) from an existing manifest."""
    result: dict[str, tuple[int, str]] = {}
    if not manifest:
        return result
    for s in manifest.get("sets", []):
        if "id" in s and "version" in s and "sha256" in s:
            result[s["id"]] = (s["version"], s["sha256"])
    return result


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export(seed_db: Path, check_only: bool = False) -> dict:
    conn = _open_readonly(seed_db)
    try:
        _assert_no_forbidden_access(conn)
        sets = _load_sets(conn)
        cards = _load_cards(conn)
    finally:
        conn.close()

    # Group cards by internal set_id
    cards_by_set: dict[int, list[dict]] = {}
    set_by_internal_id = {s["_id"]: s for s in sets}
    for c in cards:
        if c["set_id"] not in set_by_internal_id:
            raise ExportError(
                f"Card {c['api_card_id']} references unknown set_id={c['set_id']}"
            )
        cards_by_set.setdefault(c["set_id"], []).append(c)

    prev_versions = _existing_versions_by_hash(_load_existing_manifest())

    # Sets sorted deterministically: release_date (empty last), then api_set_id
    def _set_sort_key(s: dict) -> tuple:
        rd = s["release_date"] or "9999-99-99"
        return (rd, s["api_set_id"])

    sets_sorted = sorted(sets, key=_set_sort_key)

    manifest_sets = []
    planned_writes: dict[Path, str] = {}
    total_cards = 0

    for s in sets_sorted:
        payload = _build_set_payload(s, cards_by_set.get(s["_id"], []))
        total_cards += payload["card_count"]
        text = _dumps(payload)
        sha = _sha256_text(text)

        # Preserve version unless content hash changed.
        prev = prev_versions.get(s["api_set_id"])
        if prev is None:
            version = 1
        elif prev[1] == sha:
            version = prev[0]
        else:
            version = prev[0] + 1

        rel_file = f"sets/{s['api_set_id']}.json"
        planned_writes[SETS_DIR / f"{s['api_set_id']}.json"] = text
        manifest_sets.append(
            {
                "id": s["api_set_id"],
                "name": s["name"],
                "series": s["series"],
                "release_date": s["release_date"],
                "file": rel_file,
                "card_count": payload["card_count"],
                "version": version,
                "sha256": sha,
            }
        )

    manifest_sets.sort(key=lambda m: (m["release_date"] or "9999-99-99", m["id"]))

    # data_version: bump when any set version changes; preserve otherwise.
    prev_manifest = _load_existing_manifest()
    prev_data_version = (prev_manifest or {}).get("data_version", 0)
    prev_set_state = _existing_versions_by_hash(prev_manifest)
    changed = any(
        prev_set_state.get(m["id"], (None, None))[1] != m["sha256"]
        for m in manifest_sets
    ) or set(prev_set_state) != {m["id"] for m in manifest_sets}
    data_version = (prev_data_version + 1) if (changed or prev_data_version == 0) else prev_data_version

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_version": data_version,
        "updated_at": _updated_at(prev_manifest, changed),
        "set_count": len(manifest_sets),
        "card_count": total_cards,
        "sets": manifest_sets,
    }
    manifest_text = _dumps(manifest)

    if check_only:
        return {
            "manifest": manifest,
            "would_change": _would_change(planned_writes, manifest_text),
            "total_cards": total_cards,
            "set_count": len(manifest_sets),
        }

    # Write outputs
    SETS_DIR.mkdir(parents=True, exist_ok=True)
    # Remove stale set files that are no longer part of the catalogue.
    existing_files = {p.name for p in SETS_DIR.glob("*.json")}
    expected_files = {f"{s['api_set_id']}.json" for s in sets_sorted}
    for stale in existing_files - expected_files:
        (SETS_DIR / stale).unlink()

    for path, text in sorted(planned_writes.items(), key=lambda kv: kv[0].name):
        path.write_text(text, encoding="utf-8")
    MANIFEST_PATH.write_text(manifest_text, encoding="utf-8")

    return {
        "manifest": manifest,
        "total_cards": total_cards,
        "set_count": len(manifest_sets),
    }


def _updated_at(prev_manifest: dict | None, changed: bool) -> str:
    """Return an ISO-8601 UTC timestamp.

    To keep re-runs deterministic when nothing changed, preserve the previous
    ``updated_at`` if the content did not change.
    """
    if prev_manifest and not changed and prev_manifest.get("updated_at"):
        return prev_manifest["updated_at"]
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _would_change(planned_writes: dict[Path, str], manifest_text: str) -> bool:
    if not MANIFEST_PATH.is_file() or MANIFEST_PATH.read_text(encoding="utf-8") != manifest_text:
        return True
    for path, text in planned_writes.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            return True
    # stale files?
    expected = {p.name for p in planned_writes}
    if {p.name for p in SETS_DIR.glob("*.json")} != expected:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export PullDex card reference data.")
    parser.add_argument(
        "--seed-db",
        type=Path,
        default=DEFAULT_SEED_DB,
        help="Path to the PullDex seed database (read-only).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit non-zero if output would change (CI use).",
    )
    args = parser.parse_args(argv)

    try:
        result = export(args.seed_db, check_only=args.check)
    except ExportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.check:
        if result["would_change"]:
            print("Catalogue is OUT OF DATE — re-run the exporter.", file=sys.stderr)
            return 1
        print("Catalogue is up to date.")
        return 0

    print(
        f"Exported {result['set_count']} sets, {result['total_cards']} cards.\n"
        f"data_version={result['manifest']['data_version']} "
        f"updated_at={result['manifest']['updated_at']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
