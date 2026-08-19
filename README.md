# Omarchy Calendar

An Itsycal-style, read-only calendar and agenda for the Omarchy bar. Calendar
subscriptions and generated data stay in private XDG directories. The shell
uses bounded JSON protocols rather than contacting feeds or credential stores.

## Requirements

- Python 3.11 or newer
- `python-icalendar`
- `khal` 0.14.0 or newer
- Secret Service and `secret-tool` from libsecret

```sh
omarchy pkg add khal python-icalendar libsecret
omarchy plugin add https://github.com/elijah-rou/omarchy-calendar.git --enable
omarchy plugin disable omarchy.clock
```

## ICS subscriptions

Calendar settings use `omarchy-calendar subscriptions`, a newline-delimited JSON
protocol on standard input and output. It supports at most 16 feeds. Every feed
has an opaque random ID, a display name, an optional color, a private HTTPS ICS
URL, and an optional HTTP Basic username/password pair.

The entire feed URL is treated as a credential because secret Google Calendar
feed URLs grant bearer access. URL, username, and password are stored together
in Secret Service. They never appear in arguments, environment variables,
configuration, status, logs, or errors. The private mode-`0600`
`subscriptions.json` contains only IDs, names, and colors. Secret Service items
use deterministic attributes namespaced to the installation's XDG roots.

For Google Calendar, open **Settings → Integrate calendar** and copy the
**Secret address in iCal format**. For iCloud Calendar, share the calendar,
enable **Public Calendar**, and copy its link. Change the iCloud link's
`webcal://` prefix to `https://` before adding it. Publishing an iCloud calendar
allows anyone with that link to read its events, so treat the link as a secret.

Requests are at most 64 KiB and require a nonempty `requestId` of at most 128
bytes. Output consists
of bounded progress objects followed by exactly one final result:

```json
{"action":"list","requestId":"list-1"}
{"action":"add","requestId":"add-1","name":"Personal","color":"#5e81ac","url":"https://calendar.example/private.ics"}
{"action":"add","requestId":"add-2","name":"Work","url":"https://calendar.example/work.ics","username":"me","password":"app-password"}
{"action":"remove","requestId":"remove-1","id":"opaque-subscription-id"}
{"action":"refresh","requestId":"refresh-1"}
```

Add fetches, parses, and imports a candidate before committing it. Remove stages
metadata and calendar data, then clears only that subscription's Secret Service
item; a clear failure rolls the staged removal back. Failed-add clear failures
are recorded as bounded cleanup-pending IDs and retried by later mutations or
refreshes. Add and remove emit `committing` before their durable boundary.
Cancellation before that stage rolls back; cancellation after it is deferred so
the backend can return the final durable success or warning. Refresh handles
feeds independently and preserves each last-good calendar if fetching,
validation, import, or expected local OS work fails.

Fetching uses Python's standard-library HTTPS client with one monotonic deadline
across DNS, redirects, response headers, and body reads, plus redirect, response,
event, and imported-data limits. HTTP URLs, userinfo, fragments, invalid hosts
and ports, HTTPS downgrade redirects, and destinations resolving to loopback,
link-local, private, reserved, or multicast addresses are rejected. LAN feeds
are intentionally unsupported in v1. Basic Authorization is removed on
cross-origin redirects. DNS is validated before each request, but the standard
client does not pin the validated address, so DNS rebinding between validation
and connection remains a residual risk. Accepted content must
parse with `python-icalendar`; `khal import --batch --include-calendar` creates
the per-UID vdir data used by the widget.

Run the same refresh path manually, or install/update and enable the checked-in
systemd user units:

```sh
omarchy-calendar sync
omarchy-calendar install-timer
```

`install-timer` disables the old timer, stops any running legacy sync service,
installs the current command and its bounded backend runtime, replaces both user
units, runs `daemon-reload`, enables and starts the timer, and removes the obsolete
`~/.local/bin/omarchy-calendar-sync` helper from vdirsyncer-era installations.
The service invokes the current `omarchy-calendar sync` command.

## Backend protocol

Run `omarchy-calendar request` and send one UTF-8 JSON object followed by a
newline. The read-only backend supports `list`, `calendars`, and `status`.
Create, update, and delete requests return a `read_only` error.

```json
{"action":"list","requestId":"list-1","start":"2026-07-01","end":"2026-08-01","calendars":["subscription-id"]}
{"action":"calendars","requestId":"calendars-1"}
{"action":"status","requestId":"status-1"}
```

Every backend request requires the same bounded nonempty `requestId`, which is
echoed in its response. List dates are inclusive, including a same-day range.
Requests are limited to
64 KiB, responses to 1 MiB, ranges to 366 days, and results to 256 events. With
zero subscriptions, `list` and `calendars` return empty results without invoking
khal.

`calendars` returns subscription IDs and display metadata with `writable:false`.
`status` reports `readOnly:true`, the subscription count and nonsecret metadata,
a sanitized last refresh result, and installed khal and python-icalendar
versions. It does not access Secret Service or a remote feed.

Generated paths use the `omarchy-calendar` namespace:

- metadata and khal config: `$XDG_CONFIG_HOME/omarchy-calendar`
- read-only vdir data and installed timer runtime: `$XDG_DATA_HOME/omarchy-calendar`
- refresh status and lock: `$XDG_STATE_HOME/omarchy-calendar`

Directories are mode `0700`; generated files are mode `0600`.

## Validation

Tests use isolated XDG roots and fake Secret Service and khal commands.

```sh
./scripts/validate
```

## License

MIT. The calendar grid and bar integration derive from Omarchy's MIT-licensed
clock plugin.
