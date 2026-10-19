# ParityKeeper v1.2 — Debian Data-Safety Update

**Important:** This v1.2 release corrects the case-sensitive filename issue, adds second-pass SHA256 verification before committing new recovery groups, protects damaged-file quarantine, logs GUI operations to persistent rotating files, clarifies confirmation of inventory-only deletions, and validates mirror inventories. It records diagnostic job state and adds a read-only **Review leftover PAR2** button.

**Upgrading from v1.1:** no need to regenerate all existing PAR2 sets. When you open an old archive and run Scan, its `NOCASE` inventory is converted to `BINARY` path matching; the complete SQLite database is backed up first as `snapshots/inventory_before_case_migration_<UUID>.sqlite3`. Ensure the archive has free disk space before migrating. Do not mix concurrent Windows and Linux access to the same archive. Before updating real backup drives, run the built-in **Test PAR2** on the target OS with its installed real recovery executable.

For an issue-by-issue implementation audit and all remaining limitations, see **Documentation/ParityKeeper_v1.2_Code_Audit_and_Fixes.md** in the complete distribution. The older v1.1 instructions follow for general workflows; wherever they describe outdated behavior, the v1.2 audit takes precedence.

---

# ParityKeeper v1.1 — Debian/Linux native GUI

ParityKeeper is a Tkinter GUI around Debian's **par2cmdline** program (`par2`). It protects user-selected backup folders with PAR2 redundancy, a SQLite inventory, SHA-256 and CRC32 checksums, and explicitly approved update/repair actions. It does **not** replace a second independent backup.

## Installation (Debian 13 or Debian 12, graphical desktop)

```bash
sudo apt install ./paritykeeper_1.1.0-1_all.deb
paritykeeper
```

APT installs dependencies `python3`, `python3-tk`, and `par2`. You can also find **ParityKeeper** in your desktop's Utilities application menu. Use your normal desktop user, **not** `sudo paritykeeper`.

If your archive is on a separate HDD, mount it **read-write** with user permission first (e.g. under `/mnt/backup` or `/media/$USER/<drive>`). ParityKeeper needs access to `.paritykeeper/` in the chosen archive root. If the archive is mounted read-only, inspection will work only in limited circumstances; creation and inventory updates will fail.

Settings: `~/.config/paritykeeper/settings.json` (or `$XDG_CONFIG_HOME/paritykeeper/settings.json`). Data and recovery PAR2 sets live in **`<selected-root>/.paritykeeper/`**, not in your home directory.

## First use

1. Open `paritykeeper`. Verify the PAR2 executable is `/usr/bin/par2`.
2. Click **Test PAR2**. This creates disposable PAR2 recovery files, corrupts a sample, and verifies that the installed **real** `par2` command can recover it. The application has automated regression tests, but the real PAR2 engine must pass this test on your actual Debian machine.
3. Select a **small test folder** (not your 7 TB production data) and click **Scan for changes**.
4. Confirm the additions and click **Apply checked changes**. Run **CRC / SHA256 check** and **Verify PAR sets** to confirm both checksums and recovery files.
5. Only after testing: select your actual backup root. Protection proceeds one group at a time; committed groups remain in the inventory if the run is interrupted.

## Features

- **Scan for changes**: fast metadata-based compare (size + modification time); reports NEW, CHANGED, MISSING. Metadata scans do *not* detect silent bitrot.
- **Apply checked changes**: hashes approved files; creates PAR2 recovery sets; verifies them before recording the new baseline. CHANGED and MISSING entries require explicit user confirmation. Old PAR2 sets are kept until replacement is created and verified.
- **CRC / SHA256 check**: reads every registered byte, emits errors immediately and provides an ETA after speed is measured.
- **Repair checked files**: asks `par2 repair` to restore damaged/missing data, then checks every restored member against saved SHA256+CRC32. Recovery works only if sufficient PAR2 data is present. A checked file may share a recovery set with other files; all flagged damaged members in that set must be selected together.
- **Verify PAR sets / Rebuild PAR sets**: independently verify recovery sets or replace incomplete sets *only after source SHA256 values still match*.
- **Compare mirror**: read-only comparison of saved approved inventories on two disks. It does not synchronize or copy files.
- **Small/large file settings**: grouped PAR2 sets for small files; single-file recovery sets for large files. Default 12% (small) / 8% (large), with extra small-group blocks sufficient to recover the largest one small file when possible.
- **Threads**: two 1–10 sliders, default **6 for small** and **2 for large**. Each newly started `par2` operation gets `-tN`. Hashing-parallelism `-T` is capped at 2 to avoid excessive HDD seeks. A slider change applies to the **next** `par2` process, not an already-running process. This is a per-process CPU-thread limit, not concurrent PAR2 creation jobs.
- **Tooltips** for all major buttons, fields, and options, plus progress, measured processing speed and approximate ETA.

## Windows and Linux interoperability

The SQLite inventory schema and `.paritykeeper/par2/` folder layout come from the Windows v1.1 release. **PAR2 format is portable**, but differences in filename encoding, Windows path separators, case-sensitive names, and native engine implementations can prevent a Linux PAR2 program from opening or correctly resolving *some* sets created using Windows MultiPar. Before using Linux ParityKeeper on an existing Windows-managed archive, create a complete copy of a **small** example (both source files and their `.paritykeeper` group), and run Linux PAR2 verification and recovery against the copy. Do not use Rebuild/Apply on an existing Windows archive until you have established compatibility. Linux is case-sensitive; the original inventory schema treats names case-insensitively, so avoid filenames that differ only by case.

## Safety and limitations

- This program uses a real **external recovery engine** (`par2`). It is **not a backup/mirroring tool** and never substitutes for your 8 TB mirror. PAR2 redundancy cannot restore an entirely missing giant video if its size exceeds the saved redundancy capacity.
- Par2's exit codes are numeric **not bit flags**: 0 success, 1 damaged but repair possible, 2 repair impossible, 3–8 various failures. This Debian version handles these differently from MultiPar on Windows.
- `par2` normally keeps backups of damaged originals (if produced); ParityKeeper relocates newly created `<filename>.<number>` backups to `.paritykeeper/quarantine/`. It does **not** use the Windows-specific MultiPar `/b` option or pass `-p` (purge).
- To avoid inventing performance figures, ETA comes from measured work. A checksum scan knows its byte total; PAR2 operations estimate remaining work from source sizes and reported progress. Estimates may change with HDD seek patterns.
- PAR2 operations process recovery sets **sequentially**. Moving a large number of small files into one PAR2 group may be inefficient; adjust group size/count settings.
- The PAR2 index and volumes are on the **same** disk by default, so physical disk failure can lose both the data and recovery blocks. Keep a separate mirror.
- Filesystem symlinks are deliberately skipped. Folder names must not contain newline characters. This release is intended for local backup disks; avoid modifying the folder concurrently.
- This `.deb` is unsigned and user-built, not a Debian repository package. Review the source before using it for important data.

## Developer verification

```bash
python3 -m unittest discover -s tests -v
```

The automated test suite uses a **simulated** PAR2 executable, not real parity data. The GUI **Test PAR2** button is the required real-engine functional test after installing on Debian.

## Package contents

- `/usr/lib/paritykeeper/`: Python GUI, backend and progress module
- `/usr/bin/paritykeeper`: shell launcher
- `/usr/share/applications/paritykeeper.desktop`: desktop menu entry
- `/usr/share/icons/hicolor/scalable/apps/paritykeeper.svg`: icon
- `/usr/share/doc/paritykeeper/`: documentation

Uninstall program without touching backup folders:

```bash
sudo apt remove paritykeeper
```

`.paritykeeper/` folders and archive content remain untouched.
