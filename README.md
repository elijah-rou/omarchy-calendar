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

Requests are at most 64 KiB and include an optional `requestId`. Output consists
of bounded progress objects followed by exactly one final result:

```json
{"action":"list","requestId":"list-1"}
{"action":"add","requestId":"add-1","name":"Personal","color":"#5e81ac","url":"https://calendar.example/private.ics"}
{"action":"add","requestId":"add-2","name":"Work","url":"https://calendar.example/work.ics","username":"me","password":"app-password"}
{"action":"remove","requestId":"remove-1","id":"opaque-subscription-id"}
{"action":"refresh","requestId":"refresh-1"}
```

Add fetches, parses, and imports a candidate before committing it. Remove updates
metadata and calendar data before clearing only that subscription's Secret
Service item. Refresh handles feeds independently and preserves each last-good
calendar if fetching, validation, or import fails.

Fetching uses Python's standard-library HTTPS client with explicit timeouts and
redirect, response, event, and imported-data limits. HTTP URLs, userinfo,
fragments, invalid hosts and ports, and HTTPS downgrade redirects are rejected.
Basic Authorization is removed on cross-origin redirects. Accepted content must
parse with `python-icalendar`; `khal import --batch --include-calendar` creates
the per-UID vdir data used by the widget.

Run the same refresh path manually or from the checked-in systemd user timer:

```sh
omarchy-calendar sync
```

## Backend protocol

Run `omarchy-calendar request` and send one UTF-8 JSON object followed by a
newline. The read-only backend supports `list`, `calendars`, and `status`.
Create, update, and delete requests return a `read_only` error.

```json
{"action":"list","start":"2026-07-01","end":"2026-08-01","calendars":["subscription-id"]}
{"action":"calendars"}
{"action":"status"}
```

List dates are inclusive, including a same-day range. Requests are limited to
64 KiB, responses to 1 MiB, ranges to 366 days, and results to 256 events. With
zero subscriptions, `list` and `calendars` return empty results without invoking
khal.

`calendars` returns subscription IDs and display metadata with `writable:false`.
`status` reports `readOnly:true`, the subscription count and nonsecret metadata,
a sanitized last refresh result, and installed khal and python-icalendar
versions. It does not access Secret Service or a remote feed.

Generated paths use the `omarchy-calendar` namespace:

- metadata and khal config: `$XDG_CONFIG_HOME/omarchy-calendar`
- read-only vdir data: `$XDG_DATA_HOME/omarchy-calendar/calendars`
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
