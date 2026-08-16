# Omarchy Calendar

An Itsycal-style calendar and agenda for the Omarchy bar. Calendar data stays in
private XDG directories and the shell talks to a small JSON backend rather than
to network services or credential stores.

## Requirements

- Python 3.11 or newer, standard library only
- `khal` 0.14.0
- `vdirsyncer` 0.20.0 for synchronized calendars
- A secret lookup command such as `secret-tool`, `pass`, or a private script

The backend and generated configuration are verified against the exact CLI and
configuration behavior of khal 0.14.0 and vdirsyncer 0.20.0.

## Setup

All generated files use the `omarchy-calendar` namespace:

- configuration: `$XDG_CONFIG_HOME/omarchy-calendar`, default `~/.config/omarchy-calendar`
- calendar data: `$XDG_DATA_HOME/omarchy-calendar`, default `~/.local/share/omarchy-calendar`
- sync and OAuth state: `$XDG_STATE_HOME/omarchy-calendar`, default `~/.local/state/omarchy-calendar`
- khal cache: the normal `$XDG_CACHE_HOME/khal` location

Directories are mode `0700`; generated configuration and status files are mode
`0600`. Setup refuses to place generated files inside the plugin source tree.

Initialize a local, writable calendar:

```sh
./bin/omarchy-calendar-setup local
```

Generic CalDAV uses an argv-based secret lookup. Each
`--password-arg` adds one argument without invoking a shell:

```sh
./bin/omarchy-calendar-setup caldav \
  --url https://calendar.example.com/dav/ \
  --username me@example.com \
  --password-command secret-tool \
  --password-arg lookup \
  --password-arg service \
  --password-arg omarchy-calendar \
  --password-arg account \
  --password-arg me@example.com
```

For iCloud, create an app-specific password and keep it in the selected secret
store. The setup command supplies the CalDAV URL:

```sh
./bin/omarchy-calendar-setup icloud \
  --username me@icloud.com \
  --password-command pass \
  --password-arg show \
  --password-arg calendars/icloud
```

Google synchronization uses vdirsyncer's browser-based OAuth flow. Create a
Google OAuth desktop client, store its client secret outside this repository,
and expose it through a lookup command:

```sh
./bin/omarchy-calendar-setup google \
  --client-id CLIENT_ID.apps.googleusercontent.com \
  --client-secret-command pass \
  --client-secret-arg show \
  --client-secret-arg calendars/google-client-secret
```

Remote setup writes configuration, runs `vdirsyncer discover`, then performs an
initial sync. Add `--configure-only` to any remote setup command to generate and
inspect configuration without network access. A subsequent remote setup replaces
the current remote profile but never removes the separate local calendar.
OAuth tokens are written by vdirsyncer to the private XDG state directory.

Install the checked-in systemd user units and enable a sync every 15 minutes:

```sh
./bin/omarchy-calendar-setup install-timer
```

This copies the sync command to `~/.local/bin`, copies the service and timer to
the user systemd directory, reloads the user manager, and enables the timer.

## Backend protocol

Run `bin/omarchy-calendar-backend` and send exactly one UTF-8 JSON object on
standard input. It writes exactly one compact JSON object plus a newline to
standard output. Application errors are JSON responses; subprocess diagnostics
never share stdout. Requests are limited to 64 KiB, responses to 1 MiB, list
ranges to 366 days, and list results to 256 events. Commands use argv arrays,
closed stdin, and explicit 30-second or 5-minute timeouts.

Every request requires `action`. An optional `requestId` string is copied into a
successful response. Unknown fields and incorrectly typed values are rejected.

### List events

```json
{"action":"list","start":"2026-07-01","end":"2026-08-01","calendars":["local"]}
```

Returns `{"ok":true,"events":[...]}`. Dates use `YYYY-MM-DD`; `end` must be
after `start`. Omit `calendars` to include all calendars.

### Create an event

```json
{"action":"create","title":"Review","start":"2026-07-14T10:00","end":"2026-07-14T10:30","calendar":"local","location":"Desk","description":"Quarterly plan"}
```

Timed values use local `YYYY-MM-DDTHH:MM`. For all-day events, set `allDay` to
`true` and use `YYYY-MM-DD` for both values. `calendar`, `location`, and
`description` are optional. `sync` defaults to `true`; set it to `false` when a
caller is batching writes.

khal commits the event to the local vdir before vdirsyncer runs. A remote sync
failure is returned in the successful create response under `sync` and recorded
in `$XDG_STATE_HOME/omarchy-calendar/sync-status.json`; it does not discard or
misreport the local creation.

### Calendars and status

```json
{"action":"calendars"}
{"action":"status"}
```

`calendars` returns khal's configured names. `status` reports whether local and
remote configuration exist, installed command versions, and the bounded last
sync result. It does not contact a remote service.

Errors have this form:

```json
{"ok":false,"error":{"code":"invalid_request","message":"..."}}
```

## Validation

Tests use temporary HOME and XDG roots plus fake commands for failures and
ordering. When khal 0.14.0 is installed, an isolated real-CLI compatibility test
creates and lists an event.

```sh
./scripts/validate
```

## License

MIT. The calendar grid and bar integration derive from Omarchy's MIT-licensed
clock plugin.
