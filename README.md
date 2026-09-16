# PullDex Card Data

Public, versioned **card reference data** for [PullDex](https://github.com/RyanAce97) — a
Pokémon TCG Living Pokédex tracker.

This repository is the canonical catalogue of Pokémon TCG **sets and cards** that
PullDex uses to build and update its local reference database. A future version of
PullDex will read [`manifest.json`](manifest.json), download any changed set files,
validate them, and merge the reference data into each user's local database **without
replacing it**.

---

## ⚠️ What this repository is (and is not)

**This repository contains public card reference data only.**

It **does NOT** contain, and must never contain:

- any user's PullDex SQLite database
- collection ownership or card quantities
- binder layouts or binder membership
- profiles or profile settings
- any other private or user-specific information

Every file here is safe to publish. The data describes *which cards exist*, not
*which cards anyone owns*.

---

## Repository structure

```
PullDex-Card-Data/
├── manifest.json          # Catalogue index: versions + checksums for every set
├── sets/                  # One JSON file per set
│   ├── base1.json
│   ├── me55.json          # 30th Celebration
│   ├── me55c.json         # 30th Celebration: Classic Collection
│   └── … (one file per set)
├── scripts/
│   ├── export_card_data.py    # Regenerates manifest.json + sets/*.json
│   └── validate_card_data.py  # Validates the catalogue
└── README.md
```

Card **images are not stored here.** Each card carries an `image_url` pointing at the
existing remote image host (e.g. `https://images.scrydex.com/pokemon/<id>/medium`).
PullDex continues to use those remote URLs.

---

## `manifest.json` format

The manifest is the single entry point a client reads first. It is designed to scale
to hundreds of sets.

```json
{
  "schema_version": 1,
  "data_version": 1,
  "updated_at": "2026-09-16T14:16:27Z",
  "set_count": 177,
  "card_count": 20783,
  "sets": [
    {
      "id": "me55",
      "name": "30th Celebration",
      "series": "Mega Evolution",
      "release_date": "2026-09-16",
      "file": "sets/me55.json",
      "card_count": 161,
      "version": 1,
      "sha256": "…"
    }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `schema_version` | Version of the manifest/set-file **format** itself. Currently `1`. |
| `data_version` | Monotonic version of the whole catalogue. Bumps when any set changes. |
| `updated_at` | ISO-8601 UTC timestamp of the last content change. Stable across no-op re-runs. |
| `set_count` / `card_count` | Totals across the catalogue (for quick sanity checks). |
| `sets[]` | One entry per set. |
| `sets[].id` | Set identifier, matches PullDex `api_set_id` (e.g. `me55`). |
| `sets[].file` | Path to the set's JSON file, relative to the repo root. |
| `sets[].version` | Per-set version. Incremented **only** when that set's content hash changes. |
| `sets[].sha256` | SHA-256 of the exact bytes of the set file. The authoritative change-detector. |

A client decides whether to re-download a set by comparing its stored `sha256`
(or `version`) for that set against the manifest. No need to download unchanged sets.

---

## Set JSON format

Each `sets/<id>.json` file describes one set and all of its cards.

```json
{
  "set": {
    "id": "me55c",
    "name": "30th Celebration: Classic Collection",
    "series": "Mega Evolution",
    "release_date": "2026-09-16"
  },
  "card_count": 30,
  "cards": [
    {
      "api_card_id": "me55c-100",
      "card_number": "100/102",
      "rarity": "LEGEND",
      "variant": null,
      "image_url": "https://images.scrydex.com/pokemon/me55c-100/medium",
      "national_dex_number": 488,
      "species_name": "cresselia"
    }
  ]
}
```

### Card fields

These map directly onto the PullDex `Card` / `PokemonSpecies` models. Field names and
meanings are kept identical to PullDex so the future updater can consume them without
translation.

| Field | Type | PullDex mapping | Notes |
|-------|------|-----------------|-------|
| `api_card_id` | string | `Card.api_card_id` | Unique card identifier (e.g. `me55-102`). |
| `card_number` | string | `Card.card_number` | The set's printed number. Classic Collection cards keep their **historical** numbers (e.g. `100/102`). |
| `rarity` | string \| null | `Card.rarity` | e.g. `Common`, `Double Rare`, `Secret Rare`. |
| `variant` | string \| null | `Card.variant` | Currently always `null` (reserved). |
| `image_url` | string \| null | `Card.image_url` | Remote front-image URL. |
| `national_dex_number` | int \| null | `PokemonSpecies.national_dex_number` | `null` for Trainer / Energy cards. |
| `species_name` | string \| null | `PokemonSpecies.name` | `null` for Trainer / Energy cards. |

**Pokémon vs. Trainer/Energy:** Pokémon cards have a non-null `national_dex_number`
and `species_name`. Trainer and Energy cards have both set to `null`. Regional forms
(Alolan, Galarian, Hisuian) map to their base species' National Pokédex number — e.g.
Alolan Meowth and Galarian Meowth both map to dex `52` (Meowth).

No collection, ownership, quantity, binder, or profile fields exist in this schema by
design.

---

## Data versioning

- **`schema_version`** changes only if the *shape* of these files changes (a breaking
  format change). Clients should refuse data with a `schema_version` they don't
  understand.
- **`data_version`** is a whole-catalogue counter that increases whenever any set's
  content changes.
- **per-set `version` + `sha256`** let a client update only the sets that actually
  changed.

The exporter preserves a set's `version` (and the catalogue `updated_at`) when the
content hash is unchanged, so re-running with no data changes is a no-op.

---

## Regenerating the catalogue (exporter)

The catalogue is generated deterministically from the PullDex **seed database**
(the public reference DB that ships with PullDex — it contains no user data).

```bash
python scripts/export_card_data.py
```

Options:

```bash
# Point at a specific seed database
python scripts/export_card_data.py --seed-db /path/to/pulldex_backup_initial_import.db

# CI mode: don't write, exit non-zero if the committed output is stale
python scripts/export_card_data.py --check
```

The exporter:

- opens the seed database **read-only** and never reads `collection` or `profiles`
- reads only `sets`, `cards`, and `pokemon_species`
- sorts sets by `(release_date, id)` and cards by a stable natural key
- writes canonical JSON (sorted keys, 2-space indent, trailing newline)
- computes a SHA-256 per set file and records it in the manifest
- **fails loudly** if a required field is missing or a card references an unknown set
  (it never silently drops malformed cards)

Running the exporter twice against unchanged data produces byte-for-byte identical
output.

**Requirements:** Python 3.9+ standard library only. No third-party dependencies.

---

## Validating the catalogue

```bash
python scripts/validate_card_data.py
```

This verifies:

- `manifest.json` is valid JSON and in canonical deterministic form
- every set file referenced by the manifest exists
- every manifest `sha256` matches the actual file bytes
- no duplicate `api_card_id` values across the whole catalogue
- no duplicate set IDs
- every card has all required fields
- **no collection / profile / binder / user-specific fields are present**
- `set_count` and `card_count` totals are consistent
- there are no stray set files not referenced by the manifest

It also asserts the verified **30th Celebration** content:

- `me55` contains exactly **161** cards (`me55-1`…`me55-158`, plus `me55-R/G/B`)
- `me55c` contains exactly **30** cards
- the known corrections are preserved:
  `me55-102` = Jirachi ex, `me55-89` = Alolan Meowth, `me55-100` = Yveltal,
  `me55-101` = Galarian Meowth, `me55-110` = Jangmo-o
- the RGB Mew cards (`me55-R/G/B`) retain PullDex's `Secret Rare` rarity

Exit code `0` means all checks passed.

---

## How PullDex will consume this repository (future)

> The client-side updater is **not** implemented yet. This section documents the
> intended design so the data format above is fit for purpose.

A future PullDex version will:

1. **Fetch `manifest.json`** from this repository.
2. **Compare** the manifest's per-set `sha256` / `version` against what it has already
   applied.
3. **Download only changed set files.**
4. **Validate** each downloaded set file (JSON well-formed, `sha256` matches the
   manifest, required fields present, no user-data fields).
5. **Back up the user's local database** before applying any changes.
6. **Merge** the reference data into the user's existing database:
   - insert new sets and cards
   - update changed reference fields (name, rarity, image URL, species mapping)
   - **never** touch the user's collection, quantities, binder, or profiles
   - **never** replace or overwrite the user's database file
7. Record the new `data_version` so the next check is incremental.

The guiding rule is identical to this repository's rule: **reference data is public
and replaceable; user data is private and sacred.** The updater adds and updates
reference rows in place and leaves all user-owned data untouched.
