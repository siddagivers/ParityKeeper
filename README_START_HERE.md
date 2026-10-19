# ParityKeeper v1.2 — Windows and Debian Safety Update

This distribution includes:

- `Deliverables/ParityKeeper_Windows_v1.2.zip` — source, tests, Windows .bat launcher and Windows EXE build script; requires Python and a separately installed MultiPar.
- `Deliverables/paritykeeper_1.2.0-1_all.deb` — installable Debian package; APT installs the `par2` and `python3-tk` dependencies.
- `Deliverables/ParityKeeper_Debian_v1.2_source.zip` — Debian source, tests, README and run-from-source helper.
- `Documentation/ParityKeeper_v1.2_Code_Audit_and_Fixes.pdf` — 10-page issue-by-issue technical review.
- `Documentation/ParityKeeper_v1.2_Complete_Technical_Specification.pdf` — full specification (v1.2 amendments + historical v1.1 reconstruction guide).
- `Documentation/*.md` — fully editable Markdown versions.
- `Documentation/*test_results.txt` — saved automated test results (Windows 17/17; Debian 20/20).
- `Documentation/Original_User_Code_Analysis_Report.md` — report used as audit input.
- `Documentation/SHA256SUMS.txt` — file checksums.

Upgrade caution: Opening an old inventory via **Scan** may migrate SQLite from NOCASE to BINARY. An independent pre-migration `.sqlite3` backup is created under archive snapshots. Preserve `.paritykeeper/` and another physical copy before the first migration on an important HDD.

Debian: `sudo apt install ./paritykeeper_1.2.0-1_all.deb` then run `paritykeeper`.
Windows: unpack the Windows ZIP, run `Start_ParityKeeper.bat`, select `par2j64.exe` in Settings; or compile `ParityKeeper.exe` using `Build_Windows_EXE.bat`.

**Production verification is still required:** simulated-engine tests do not validate actual error recovery. Run GUI **Test PAR2/Test MultiPar** on the installation computer with a disposable sample. Do not test repair by corrupting your only archived file.
