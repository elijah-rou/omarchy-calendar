# Repository guide

Standalone MIT-licensed Omarchy shell calendar plugin.

## Architecture

- `BarWidget.qml`, `Panel.qml`, `Model.js`: presentation and Omarchy shell integration.
- `backend/`: bounded Python JSON protocol over stdin/stdout; no shell interpolation.
- `bin/`: user commands and interactive setup.
- `systemd/`: user synchronization units.
- `tests/`: hermetic Python and JavaScript tests.

Credentials, OAuth tokens, synchronized calendars, and generated user config must never enter this repository. Use isolated XDG paths under `omarchy-calendar`.

## Validation

Run `./scripts/validate`. Treat warnings as failures where practical. Validate the manifest with `omarchy plugin validate .` on Omarchy.
