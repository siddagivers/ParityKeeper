# ParityKeeper Changelog

## v1.2.0 — 19 October 2026

- Migrated file inventory primary keys and scan table from SQLite NOCASE to BINARY with preserved pre-migration database copy.
- Removed case-insensitive `.casefold()` logic for PAR group membership and deletion matching.
- Rehashed every source file after recovery-set creation and verification, before committing new checksum baselines.
- Added explicit native Windows MultiPar bitmask and Linux numeric PAR2 return-code helpers.
- Synchronized GUI CPU-thread slider updates with worker subprocess-launch reads.
- Added persistent rotating GUI activity logs.
- Hardened internal path resolution and source-file symlink/junction handling.
- Improved quarantine collision protection and handling of incomplete damaged-original relocation.
- Validated presence of both inventories before comparison.
- Clarified that intentional deletion approval removes inventory entries only.
- Added best-effort diagnostic per-batch operation checkpoint; automatic approval replay is prohibited.
- Added read-only GUI review of orphan PAR2 directories; no automatic deletion.
- Added 8 new safety regression scenarios and v1.2 technical audit documentation.

## Previous v1.1
See the original v1.1 release for its complete changelog.
