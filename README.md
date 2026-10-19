# ParityKeeper v1.2

Cross-platform graphical PAR2 archive-integrity manager for Windows (MultiPar) and Debian Linux (`par2`). Tracks checksums and PAR2 recovery sets on a per-folder basis.

**This is a source and release staging bundle, not an automatically published GitHub repository.**

## Layout

- `Windows/` — Tkinter application, MultiPar integration, launcher, EXE build script, tests.
- `Debian/` — Tkinter application, native `par2` integration, source launcher, tests.
- `Documentation/` — v1.2 technical specification (Markdown and PDF), code audit and findings, and test reports.
- `dist/` — prebuilt Debian `.deb` package for release distribution.

## Windows

Install Python 3.10+ and MultiPar. See [`Windows/README.md`](Windows/README.md). Run `Windows/Start_ParityKeeper.bat` or `Windows/Build_Windows_EXE.bat` for the Windows executable builder.

## Debian

Install the packaged application (with dependencies):

```sh
sudo apt install ./dist/paritykeeper_1.2.0-1_all.deb
paritykeeper
```

Or follow [`Debian/README.md`](Debian/README.md) to run from source.

## Regression tests

Windows, from `Windows/`:

```sh
python -m unittest discover -s tests -v
```

Linux, from `Debian/`:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The included automated repair tests use fake PAR2 executables. **Before using this app on valuable data, run its built-in real-PAR2 self-test using disposable files**, and keep independent copies of the archive and `.paritykeeper` inventory.

## Security and privacy

Do not commit `.paritykeeper/` directories from your actual backup disks. They may contain private inventories and recovery data. Inspect all files for sensitive data before making a repository public.

## Documentation

Begin with [`Documentation/ParityKeeper_v1.2_Complete_Technical_Specification.md`](Documentation/ParityKeeper_v1.2_Complete_Technical_Specification.md) and [`Documentation/ParityKeeper_v1.2_Code_Audit_and_Fixes.md`](Documentation/ParityKeeper_v1.2_Code_Audit_and_Fixes.md).

## License

No software license has been selected. Repository visibility and licensing should be confirmed by the owner before publication.
