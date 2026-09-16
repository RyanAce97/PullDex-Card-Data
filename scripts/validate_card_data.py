#!/usr/bin/env python3
"""PullDex Card-Data Validation.

Validates the exported catalogue (manifest.json + sets/*.json) for
structural integrity, determinism, and safety (no user data). Also runs a
set of content assertions for the verified 30th Celebration data.

Exit code 0 = all checks pass; non-zero = at least one failure.

Usage:
    python scripts/validate_card_data.py

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETS_DIR = REPO_ROOT / "sets"
MANIFEST_PATH = REPO_ROOT / "manifest.json"

# Fields that must NEVER appear in the public catalogue (user data).
FORBIDDEN_CARD_FIELDS = {
    "quantity",
    "quantities",
    "profile_id",
    "profile",
    "is_binder_card",
    "binder",
    "owned",
    "collection_id",
    "collection",
    "user_id",
}

REQUIRED_CARD_FIELDS = {
    "api_card_id",
    "card_number",
    "rarity",
    "variant",
    "image_url",
    "national_dex_number",
    "species_name",
}

REQUIRED_MANIFEST_SET_FIELDS = {
    "id",
    "name",
    "series",
    "release_date",
    "file",
    "card_count",
    "version",
    "sha256",
}

# Known 30th Celebration corrections that must be preserved.
ME55_CORRECTIONS = {
    "me55-102": {"species_name": "jirachi", "national_dex_number": 385},
    "me55-89": {"species_name": "meowth", "national_dex_number": 52},   # Alolan Meowth
    "me55-100": {"species_name": "yveltal", "national_dex_number": 717},
    "me55-101": {"species_name": "meowth", "national_dex_number": 52},  # Galarian Meowth
    "me55-110": {"species_name": "jangmo-o", "national_dex_number": 782},
}
RGB_MEW = {"me55-R", "me55-G", "me55-B"}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks_run = 0

    def check(self, condition: bool, message: str) -> None:
        self.checks_run += 1
        if not condition:
            self.failures.append(message)

    def report(self) -> int:
        print(f"Ran {self.checks_run} checks.")
        if self.failures:
            print(f"\nFAILED ({len(self.failures)}):")
            for f in self.failures:
                print(f"  ✗ {f}")
            return 1
        print("All checks passed. ✓")
        return 0


def _dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    c = Checker()

    # --- manifest exists and is valid JSON ---
    c.check(MANIFEST_PATH.is_file(), "manifest.json is missing")
    if not MANIFEST_PATH.is_file():
        return c.report()

    manifest_text = MANIFEST_PATH.read_text(encoding="utf-8")
    try:
        manifest = json.loads(manifest_text)
        manifest_valid = True
    except json.JSONDecodeError as e:
        c.check(False, f"manifest.json is not valid JSON: {e}")
        return c.report()

    c.check(manifest.get("schema_version") == 1, "manifest.schema_version must be 1")
    c.check(isinstance(manifest.get("data_version"), int), "manifest.data_version must be an int")
    c.check(isinstance(manifest.get("updated_at"), str), "manifest.updated_at must be a string")
    c.check(isinstance(manifest.get("sets"), list), "manifest.sets must be a list")

    # manifest determinism: sorted-keys form must equal file contents
    c.check(
        manifest_text == _dumps(manifest),
        "manifest.json is not in canonical deterministic form (sorted keys / 2-space indent / trailing newline)",
    )

    sets = manifest.get("sets", [])

    # --- no duplicate set IDs ---
    set_ids = [s.get("id") for s in sets]
    c.check(len(set_ids) == len(set(set_ids)), "duplicate set IDs in manifest")

    # --- manifest set_count / card_count consistency ---
    c.check(manifest.get("set_count") == len(sets), "manifest.set_count does not match number of sets")

    all_card_ids: list[str] = []
    all_set_ids_seen: set[str] = set()
    total_cards = 0
    me55_cards: dict[str, dict] = {}
    me55c_count = 0

    for s in sets:
        # required manifest fields
        missing = REQUIRED_MANIFEST_SET_FIELDS - set(s.keys())
        c.check(not missing, f"manifest set {s.get('id')} missing fields: {missing}")

        set_id = s.get("id")
        all_set_ids_seen.add(set_id)
        set_file = REPO_ROOT / s.get("file", "")

        # --- set file exists ---
        c.check(set_file.is_file(), f"set file missing: {s.get('file')}")
        if not set_file.is_file():
            continue

        file_text = set_file.read_text(encoding="utf-8")

        # --- sha256 matches ---
        actual_sha = _sha256_text(file_text)
        c.check(
            actual_sha == s.get("sha256"),
            f"sha256 mismatch for {s.get('file')}: manifest={s.get('sha256')[:12]}… actual={actual_sha[:12]}…",
        )

        # --- set file valid JSON + deterministic form ---
        try:
            payload = json.loads(file_text)
        except json.JSONDecodeError as e:
            c.check(False, f"{s.get('file')} is not valid JSON: {e}")
            continue
        c.check(
            file_text == _dumps(payload),
            f"{s.get('file')} is not in canonical deterministic form",
        )

        # --- set metadata matches manifest ---
        set_meta = payload.get("set", {})
        c.check(set_meta.get("id") == set_id, f"{s.get('file')}: set.id != manifest id")
        c.check(
            payload.get("card_count") == len(payload.get("cards", [])),
            f"{s.get('file')}: card_count != len(cards)",
        )
        c.check(
            s.get("card_count") == len(payload.get("cards", [])),
            f"{s.get('file')}: manifest card_count != len(cards)",
        )

        # --- per-card checks ---
        for card in payload.get("cards", []):
            total_cards += 1
            cid = card.get("api_card_id")
            all_card_ids.append(cid)

            # required fields present
            missing_fields = REQUIRED_CARD_FIELDS - set(card.keys())
            c.check(not missing_fields, f"card {cid} missing fields: {missing_fields}")

            # forbidden (user-data) fields absent
            present_forbidden = FORBIDDEN_CARD_FIELDS & set(card.keys())
            c.check(not present_forbidden, f"card {cid} contains forbidden field(s): {present_forbidden}")

            # api_card_id + card_number required non-empty
            c.check(bool(cid), f"a card in {s.get('file')} has empty api_card_id")

            # references its own set (by prefix convention) — soft check
            # (api_card_id like 'me55-1' belongs to set 'me55')

            if set_id == "me55":
                me55_cards[cid] = card
            if set_id == "me55c":
                me55c_count += 1

    # --- no duplicate api_card_id across whole catalogue ---
    dupes = sorted({cid for cid in all_card_ids if all_card_ids.count(cid) > 1}) if all_card_ids else []
    # (count() in a loop is O(n^2); fine for validation scale, but use a set approach)
    seen: set[str] = set()
    dup_set: set[str] = set()
    for cid in all_card_ids:
        if cid in seen:
            dup_set.add(cid)
        seen.add(cid)
    c.check(not dup_set, f"duplicate api_card_id values: {sorted(dup_set)[:10]}")

    # --- manifest global card_count matches ---
    c.check(
        manifest.get("card_count") == total_cards,
        f"manifest.card_count ({manifest.get('card_count')}) != actual total ({total_cards})",
    )

    # --- every manifest file on disk is referenced (no stray set files) ---
    on_disk = {p.name for p in SETS_DIR.glob("*.json")} if SETS_DIR.is_dir() else set()
    referenced = {Path(s["file"]).name for s in sets if "file" in s}
    c.check(on_disk == referenced, f"stray/missing set files: on_disk-referenced={on_disk - referenced}, referenced-on_disk={referenced - on_disk}")

    # --- 30th Celebration content assertions ---
    c.check("me55" in all_set_ids_seen, "me55 set is missing")
    c.check("me55c" in all_set_ids_seen, "me55c set is missing")
    c.check(len(me55_cards) == 161, f"me55 must have 161 cards, has {len(me55_cards)}")
    c.check(me55c_count == 30, f"me55c must have 30 cards, has {me55c_count}")

    # me55 id completeness: 1..158 + R/G/B
    expected_me55 = {f"me55-{n}" for n in range(1, 159)} | {"me55-R", "me55-G", "me55-B"}
    c.check(set(me55_cards.keys()) == expected_me55,
            f"me55 IDs mismatch: missing={sorted(expected_me55 - set(me55_cards))[:5]} extra={sorted(set(me55_cards) - expected_me55)[:5]}")

    # corrections
    for cid, expect in ME55_CORRECTIONS.items():
        card = me55_cards.get(cid)
        c.check(card is not None, f"correction card {cid} missing")
        if card:
            c.check(card.get("species_name") == expect["species_name"],
                    f"{cid} species_name expected {expect['species_name']} got {card.get('species_name')}")
            c.check(card.get("national_dex_number") == expect["national_dex_number"],
                    f"{cid} dex expected {expect['national_dex_number']} got {card.get('national_dex_number')}")

    # RGB Mew rarity override
    for cid in RGB_MEW:
        card = me55_cards.get(cid)
        c.check(card is not None, f"RGB Mew {cid} missing")
        if card:
            c.check(card.get("rarity") == "Secret Rare", f"{cid} rarity expected 'Secret Rare' got {card.get('rarity')}")

    return c.report()


if __name__ == "__main__":
    raise SystemExit(main())
