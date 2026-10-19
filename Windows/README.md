# ParityKeeper v1.2 — Windows Data-Safety Update

**Important:** This v1.2 release corrects the case-sensitive filename issue, adds second-pass SHA256 verification before committing new recovery groups, protects damaged-file quarantine, logs GUI operations to persistent rotating files, clarifies confirmation of inventory-only deletions, and validates mirror inventories. It records diagnostic job state and adds a read-only **Review leftover PAR2** button.

**Upgrading from v1.1:** no need to regenerate all existing PAR2 sets. When you open an old archive and run Scan, its `NOCASE` inventory is converted to `BINARY` path matching; the complete SQLite database is backed up first as `snapshots/inventory_before_case_migration_<UUID>.sqlite3`. Ensure the archive has free disk space before migrating. Do not mix concurrent Windows and Linux access to the same archive. Before updating real backup drives, run the built-in **Test MultiPar** on the target OS with its installed real recovery executable.

For an issue-by-issue implementation audit and all remaining limitations, see **Documentation/ParityKeeper_v1.2_Code_Audit_and_Fixes.md** in the complete distribution. The older v1.1 instructions follow for general workflows; wherever they describe outdated behavior, the v1.2 audit takes precedence.

---

# ParityKeeper v1.1 — Windows PAR2 archive guardian

A Windows desktop GUI for managing a changing archival folder with **MultiPar**.
This application does **not** implement its own error-correction engine: all PAR2
creation, verification, and repair is performed by MultiPar's `par2j64.exe`.
No external Python packages are needed to run the GUI.

**Important:** Run **Test MultiPar** before using this with real backups. The
application workflows were tested with a simulated command-line utility during
development; the real Windows MultiPar binary was not executable in the build
environment. Do not rely on this as your only backup until you have tested a
real corruption/recovery cycle on disposable files.

## Installing and starting

1. Download and install **MultiPar** from the developer's GitHub releases:
   <https://github.com/Yutaka-Sawada/MultiPar/releases>
2. Install Python for Windows 3.10 or newer from <https://www.python.org/downloads/windows/>.
   Enable **Add Python to PATH**. The standard Python installer includes Tkinter.
3. Extract `ParityKeeper_Windows_v1.1.zip` and double-click `Start_ParityKeeper.bat`.
4. Select your backup folder (e.g. `E:\Backup`) and `par2j64.exe` (normally
   `C:\Program Files\MultiPar\par2j64.exe`; browse if installed elsewhere).
5. Click **Test MultiPar**. It uses a disposable temporary folder, creates PAR2,
   corrupts a temporary file, repairs it, and verifies the original bytes and
   Unicode filenames. **Do not begin a multi-TB run unless this test passes.**
6. Select the drive and use **Scan for changes**. Approve protection for new
   files only after checking estimated PAR2 disk usage.

### Optional single-file Windows executable

Run `Build_Windows_EXE.bat` on **Windows** with Python installed. It installs
PyInstaller and produces `dist\ParityKeeper.exe`. You can then run the EXE
without Python, but **MultiPar must still be installed separately**. A prebuilt
Windows EXE is not included in this archive.

## How to use the buttons

| Control | What it does |
|---|---|
| **Scan for changes** | Recursively scan the selected folder using names, sizes and timestamps only. Show NEW, CHANGED and MISSING items. Does *not* trust timestamps for integrity. |
| **Apply checked changes** | Check which NEW files should be protected and which edited/deleted files are intentional. Hash, build, verify, and commit new PAR2 sets. |
| **CRC / SHA256 check** | Read **every byte** of every managed file and compare it to saved CRC32 **and** SHA256; report CORRUPT, MISSING, UNREADABLE and missing PAR volumes. |
| **Repair checked files** | Use original PAR2 sets to repair corrupted/missing files; verify the restored SHA256. Checks are per file, but the MultiPar operation works at the recovery-set level. |
| **Verify PAR sets** | Run MultiPar's full `verify` operation on every active recovery set, with workload-based progress/ETA. |
| **Rebuild PAR sets** | Rebuild missing/damaged PAR2 files *only after* verifying all original members against approved SHA256. No bad data is silently accepted. |
| **Compare mirror** | Compare the two drives' **saved inventory baselines**, read-only. Finds files only in one inventory or different saved SHA256 values. **Does not copy or checksum the mirror's current bytes.** |
| **STOP** | Stop the current operation. Groups already verified and committed remain protected. Run Scan again to resume. |

## New in v1.1: CPU-thread sliders, time estimates and hover help

**Two independent sliders in Recovery Settings**, each from **1 to 10**:

- **Small-file MultiPar CPU threads** (default **6**). Used for PAR2 creation,
  creation verification, ordinary PAR verification and repair of grouped small
  files. MultiPar gets `/lc6` with the default setting.
- **Large-file MultiPar CPU threads** (default **2**). Used for the same phases
  on a single large-file recovery set. MultiPar gets `/lc2` by default.

These are **CPU threads inside a single MultiPar process**, not a request for
6 simultaneous PAR2 processes. ParityKeeper deliberately performs PAR2 sets
sequentially on one HDD, avoiding excessive disk seeks. You may **move either
slider while protecting a folder**; the new value takes effect on the **next
MultiPar subprocess** (for example, the verification immediately after the
current creation process, or the next PAR2 group). It **cannot** retroactively
change the thread count inside an already-running MultiPar process. Move a
slider before starting work if you want a specific count for the first set.
This uses MultiPar's documented `/lcN` option, which supports 1–32 threads;
v1.1 intentionally restricts the UI to 1–10.

The progress area displays:

- **Measured SHA256/CRC32 work**: exact stored total bytes of tracked files,
  real bytes read, and estimated completion time calculated from elapsed time
  and achieved effective processing throughput.
- **PAR2 creation, verification, repair and rebuild**: expected cumulative
  source/verification work derived from PAR2 group sizes; MultiPar reported
  percentages are used where its console output provides them. The time estimate
  updates as the work progresses. **Encoding time, HDD seeking, parity-volume
  writes, and damage severity are not fully predictable from file size**, so
  the PAR2 ETA is approximate and can change, especially with different thread
  counts or many tiny files.
- **Before the first measured run**, the program reports the known file data
  and says that it will calculate an ETA after measuring the actual device and
  workload. Later confirmations can reuse throughput observed in previous
  operations from `%APPDATA%\ParityKeeper\settings.json` (per operation
  type). It does not invent a fixed 7-TB duration.
- **Metadata-only scans**: number of files found is reported; total file count
  is unknown until the scan finishes, so no fictitious ETA is shown.

**Hover the pointer over any main button, settings field, slider, selection
control, or result table** for built-in help about what it does. The tooltip
appears after approximately 0.65 seconds. This is especially important for
Approving edited files and MISSING deletions.

**Upgrading from v1.0:** extract v1.1 to another folder and run it normally.
It reads the same `.paritykeeper\inventory.sqlite3` and PAR2 sets without
converting or reimporting your archive; settings stored under `%APPDATA%`
carry over. Do not run v1.0 and v1.1 against the same archive simultaneously.

### Understanding NEW, CHANGED and MISSING

- **NEW** is checked by default. You are asked whether you want to create
  recovery sets immediately. New files get protected in incremental batches,
  so cancelling partway through the first import does **not** mean starting
  over: run Scan and only unprotected files are proposed again.
- **CHANGED** is **unchecked** by default. A changed file may be a valid edit
  *or* disk damage; only you can approve replacing the previously saved
  checksum and PAR baseline. **If unsure, first run CRC/SHA256 check and repair.**
  To accept a legitimate edit, tick it and Apply. The app also checks all
  other files sharing its old PAR set against their original hashes before
  replacing that set.
- **MISSING** is **unchecked** by default. Scan asks if *all* the missing files
  were intentionally deleted. If so, you can approve their removal and PAR
  regrouping; otherwise leave them tracked so they may be repaired. Mixed
  cases can be approved individually by ticking specific rows.
- The GUI shows 2,000 rows at a time so folders with hundreds of thousands
  of photos do not freeze the table. Use **Filter** and previous/next page.
  **Check matching / Uncheck matching** act on *all* entries matching a filter,
  not just those currently on screen.

### File and recovery grouping defaults

- Files **under 16 MiB**: bundled by their parent directory into groups of at
  most **256 MiB** or **256 files**; **12%** target redundancy. The generator
  chooses enough recovery blocks to recover one fully missing small file per
  group if every other file is intact (actual cost can exceed 12% for tiny
  groups). It can also repair corruption spread over files within the number
  of available recovery blocks.
- Files **16 MiB and larger**: standalone PAR2 sets with **8%** recovery data
  by default. It can repair damage to part of the file, but **cannot restore
  the entire missing large file** with only 8% redundancy. Recover such a
  loss from your second mirror/backup.
- Settings are editable at the top of the app, and apply to **new/rebuilt**
  recovery sets. Old sets keep their original redundancy unless regenerated.
- A PAR2 set is always **created in a fresh folder, verified, then switched
  in SQLite**. The older recovery set is deleted only after a verified new set
  is committed. An edit/delete can require temporarily storing both versions.

### Data format and folder layout

For a root folder such as `E:\Backup`, ParityKeeper creates:

```text
E:\Backup\
    photo1.jpg
    Albums\photo2.jpg
    ...
    .paritykeeper\
        inventory.sqlite3        SHA256, CRC32, sizes, dates, PAR membership
        operation.lock           prevents simultaneous jobs on the same archive
        snapshots\              last 3 SQLite inventory backups
        par2\
            <unique-set-ID>\
                data.par2
                data.vol....par2
        quarantine\             damaged originals kept by MultiPar /b
```

The `.paritykeeper` directory is excluded from the data scan. Its SQLite
inventory contains **relative paths** and stays with its drive if its drive
letter changes. An operating-system drive failure destroys PAR2 files stored
on the same disk: **this tool is not a substitute for another physical backup**.
Never manually alter the SQLite inventory or PAR set directories.

The app does **not** automatically import pre-existing standalone PAR2 sets.
It creates and tracks its own PAR2 sets. If there are already `.par2` files
elsewhere in the selected source folder, those are currently treated like
ordinary input files, so review the folder first to avoid unnecessarily
protecting old recovery archives again.

### Working with your 10 TB and 8 TB disks

Select the first disk's actual backup folder, Scan, and protect it. After
copying files to the second disk by your normal method, **choose the second
backup folder as the main folder**, Scan and protect that disk independently.
Each HDD keeps its own SQLite inventory and PAR2 sets. Later, you can select
one as the main folder and the other as **Mirror folder** and click **Compare
mirror** to compare the approved baselines. That action is deliberately
read-only: no surprise deletions, no synchronization, no overwrites.

**Capacity matters:** if the files occupy 7 TB (decimal), 8% recovery overhead
would add roughly 560 GB, giving approximately 7.56 TB before filesystem and
PAR2 overhead. If the files occupy 7 **TiB** on an advertised 8 **TB** disk,
even 8% PAR2 will *not* fit. The app checks free space before each PAR set,
but you should decide redundancy and free headroom before protecting TB of
content. If needed, apply a smaller PAR percentage to the tight mirror drive.

### Verification, safety, and limits

- Scan for changes is quick because it does **not** read file contents.
  Only the full CRC/SHA256 check detects silent bit rot without changed size
  or timestamp. Plan to run it regularly, and separately verify PAR2 sets.
- First-time protection involves several passes of SHA256 hashing, MultiPar
  encoding/verification and parity writes. Progress and a workload-based ETA
  are shown while working; actual duration is dependent on the HDD and CPU.
- The tool does not delete files from your source folders on approval of
  **MISSING**; the files must already be absent. It removes their inventory
  entries and regenerates parity for remaining group members.
- On repair, MultiPar `/b` retains damaged originals, and ParityKeeper moves
  newly created backup copies to `.paritykeeper\quarantine` when it can
  identify them. These are **not** automatically deleted.
- Do not alter/copy over source data while creating or verifying parity;
  ParityKeeper checks size and timestamps while hashing and after creation,
  and aborts if they change.
- Windows directory junctions and symlinks are skipped. Files unreadable
  due to permissions/I/O cause warnings rather than being silently removed.
- Source names and relative paths are stored in SQLite and protected using
  UTF-8 file lists for MultiPar. Very long legacy Windows path names can still
  require long-path support in Windows and the associated application.
- The app is not a file synchronizer, a remote/offline backup, a filesystem
  snapshot facility, or a guarantee of recovery when the PAR2 redundancy is
  insufficient. Run it only when programs have finished writing to the
  archive; keep a second physical copy, preferably disconnected when idle.
- If initial protection fails halfway through, keep the folder intact,
  inspect the activity log, and click **Scan for changes** again. Already
  committed groups remain in the inventory and are not regenerated.

## Diagnostics / source tests

```powershell
cd C:\path\to\ParityKeeper_Windows
py -3 -m unittest discover -s tests -v
```

The test suite uses `tests/fake_par2j.py`, which is intentionally **not** a real
PAR2 implementation; it checks the inventory, workflow, CPU `/lcN` arguments
and progress event integration.
Use the GUI's **Test MultiPar** button on Windows to test the *real* external
command-line tool. If it fails, save the activity log and report the complete
MultiPar version, executable path and error message.

### License / third-party dependencies

ParityKeeper source is provided for personal use and modification. MultiPar is
third-party software maintained by Yutaka Sawada and installed/downloaded
separately. See its repository and its applicable license for details.
