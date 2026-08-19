from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Self, cast
from unittest import mock

from backend import calendar_backend, subscriptions

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "omarchy-calendar"
REAL_KHAL = shutil.which("khal")
VALID_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//test//EN\r
BEGIN:VEVENT\r
UID:event-1\r
DTSTAMP:20300101T000000Z\r
DTSTART:20300102T100000Z\r
DTEND:20300102T110000Z\r
SUMMARY:Planning\r
END:VEVENT\r
END:VCALENDAR\r
"""

RECURRING_AND_ALL_DAY_ICS = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//real-khal-regression//EN\r
BEGIN:VEVENT\r
UID:recurring\r
DTSTAMP:20300101T000000Z\r
DTSTART:20300102T100000Z\r
DTEND:20300102T110000Z\r
RRULE:FREQ=DAILY;COUNT=3\r
SUMMARY:Recurring\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:one-day\r
DTSTAMP:20300101T000000Z\r
DTSTART;VALUE=DATE:20300103\r
DTEND;VALUE=DATE:20300104\r
SUMMARY:One day\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:multi-day\r
DTSTAMP:20300101T000000Z\r
DTSTART;VALUE=DATE:20300105\r
DTEND;VALUE=DATE:20300108\r
SUMMARY:Multi day\r
END:VEVENT\r
END:VCALENDAR\r
"""

FAKE_SECRET_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
path = pathlib.Path(os.environ["KEYRING_PATH"])
try: values = json.loads(path.read_text())
except (FileNotFoundError, json.JSONDecodeError): values = {}
key = json.dumps(args[2:] if args[0] == "store" else args[1:], separators=(",", ":"))
if args[0] == "store":
    values[key] = sys.stdin.read()
    path.write_text(json.dumps(values, separators=(",", ":")))
elif args[0] == "lookup":
    if key not in values: raise SystemExit(1)
    sys.stdout.write(values[key])
elif args[0] == "clear":
    if os.environ.get("KEYRING_CLEAR_FAIL") == "1": raise SystemExit(1)
    values.pop(key, None)
    path.write_text(json.dumps(values, separators=(",", ":")))
else: raise SystemExit(2)
'''

FAKE_KHAL = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("khal, version test")
elif "import" in args:
    if os.environ.get("BLOCK_KHAL") == "1": time.sleep(60)
    config = pathlib.Path(args[args.index("-c") + 1]).read_text()
    calendar = args[args.index("--include-calendar") + 1]
    for line in config.splitlines():
        if line.startswith("path = "):
            target = pathlib.Path(json.loads(line[7:]))
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args[-1], target / "event-1.ics")
            break
elif "list" in args:
    calendar = args[args.index("--include-calendar") + 1] if "--include-calendar" in args else "feed"
    print(json.dumps([{"uid":"event-1","title":"Planning","start":"2030-01-02 10:00","end":"2030-01-02 11:00","calendar":calendar,"all-day":"False"}]))
else: raise SystemExit(2)
'''


class Isolated(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(tempfile.TemporaryDirectory[str], cast(object, None))
    bin: Path = cast(Path, cast(object, None))
    env: dict[str, str] = cast(dict[str, str], cast(object, None))
    environment: Any = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CACHE_HOME": str(root / "cache"), "KEYRING_PATH": str(root / "keyring.json"),
            "PATH": f"{self.bin}:{self.env.get('PATH', '')}",
        })
        Path(self.env["HOME"]).mkdir()
        self.command("secret-tool", FAKE_SECRET_TOOL)
        self.command("khal", FAKE_KHAL)
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def command(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def add(self, name: str = "Work", url: str = "https://calendar.example.test/private.ics", **extra: str) -> dict[str, Any]:
        request: dict[str, Any] = {"action": "add", "name": name, "url": url, **extra}
        with mock.patch.object(subscriptions, "fetch_feed", return_value=VALID_ICS):
            return subscriptions.add_subscription(request, lambda *_args: None)

    def import_with_real_khal(self, *, color: str = "#123456") -> dict[str, str]:
        if REAL_KHAL is None:
            self.skipTest("real khal is not installed")
        item = {"id": "a" * 32, "name": "Real khal", "color": color}
        subscriptions.ensure_private_directories()
        subscriptions.atomic_write(subscriptions.paths()["metadata"], subscriptions.metadata_bytes([item]))
        import_config = subscriptions.paths()["config"] / "import.conf"
        import_config.write_bytes(subscriptions.khal_config([item], readonly=False))
        feed = subscriptions.paths()["data"] / "real.ics"
        feed.write_bytes(RECURRING_AND_ALL_DAY_ICS)
        subprocess.run(
            [str(REAL_KHAL), "-c", str(import_config), "--no-color", "import", "--batch", "--include-calendar", item["id"], str(feed)],
            check=True, capture_output=True, env=self.env, timeout=30,
        )
        subscriptions.atomic_write(subscriptions.paths()["khal_config"], subscriptions.khal_config([item]))
        return item

    def protocol(self, request: dict[str, Any]) -> dict[str, Any]:
        request.setdefault("requestId", "test-request")
        result = subprocess.run(
            [str(WRAPPER), "request"], input=json.dumps(request).encode() + b"\n",
            capture_output=True, env=self.env, timeout=10, check=True,
        )
        self.assertEqual(result.stderr, b"")
        return json.loads(result.stdout)


class SubscriptionTests(Isolated):
    def test_add_list_and_remove_keep_secrets_out_of_metadata(self) -> None:
        result = self.add(username="person", password="private-password")
        item = result["subscription"]
        metadata_path = subscriptions.paths()["metadata"]
        metadata = metadata_path.read_text()
        self.assertIn("Work", metadata)
        for secret in ("https://", "person", "private-password"):
            self.assertNotIn(secret, metadata)
        self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)
        self.assertEqual(subscriptions.load_subscriptions(), [item])
        removed = subscriptions.remove_subscription({"action": "remove", "id": item["id"]}, lambda *_args: None)
        self.assertEqual(removed["removed"], item)
        self.assertTrue(removed["cleanupComplete"])
        self.assertEqual(subscriptions.load_subscriptions(), [])
        keyring = json.loads(Path(self.env["KEYRING_PATH"]).read_text())
        self.assertEqual(keyring, {})

    def test_multiple_feeds_have_opaque_ids_and_readonly_config(self) -> None:
        first = self.add("One")["subscription"]
        second = self.add("Two", color="#112233")["subscription"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(first["id"]), 32)
        config = subscriptions.paths()["khal_config"].read_text()
        self.assertEqual(config.count("readonly = True"), 2)
        self.assertNotIn("https://", config)
        self.assertNotIn("password", config)

    def test_add_validates_before_commit_and_rolls_back(self) -> None:
        with (
            mock.patch.object(subscriptions, "fetch_feed", return_value=b"not a calendar"),
            self.assertRaisesRegex(subscriptions.SubscriptionError, "valid iCalendar"),
        ):
            subscriptions.add_subscription({"action": "add", "name": "Bad", "url": "https://example.test/bad"}, lambda *_args: None)
        self.assertEqual(subscriptions.load_subscriptions(), [])
        self.assertEqual(json.loads(Path(self.env["KEYRING_PATH"]).read_text()), {})

    def test_remove_clear_failure_leaves_subscription_and_data_intact(self) -> None:
        item = self.add()["subscription"]
        event_path = subscriptions.paths()["calendars"] / item["id"] / "event-1.ics"
        before = event_path.read_bytes()
        os.environ["KEYRING_CLEAR_FAIL"] = "1"
        try:
            with self.assertRaisesRegex(subscriptions.SubscriptionError, "credential could not be removed"):
                subscriptions.remove_subscription({"action": "remove", "id": item["id"]}, lambda *_args: None)
        finally:
            os.environ.pop("KEYRING_CLEAR_FAIL", None)
        self.assertEqual(subscriptions.load_subscriptions(), [item])
        self.assertEqual(event_path.read_bytes(), before)
        self.assertEqual(subscriptions.lookup_secret(item["id"])["url"], "https://calendar.example.test/private.ics")

    def test_failed_add_clear_failure_is_tracked_and_retried(self) -> None:
        os.environ["KEYRING_CLEAR_FAIL"] = "1"
        try:
            with (
                mock.patch.object(subscriptions, "fetch_feed", return_value=b"invalid"),
                self.assertRaisesRegex(subscriptions.SubscriptionError, "cleanup will be retried"),
            ):
                subscriptions.add_subscription({"action": "add", "name": "Bad", "url": "https://example.test/bad"}, lambda *_args: None)
        finally:
            os.environ.pop("KEYRING_CLEAR_FAIL", None)
        pending = subscriptions.load_cleanup_pending()
        self.assertEqual(len(pending), 1)
        subscriptions.refresh_subscriptions()
        self.assertEqual(subscriptions.load_cleanup_pending(), [])
        self.assertEqual(json.loads(Path(self.env["KEYRING_PATH"]).read_text()), {})

    def test_refresh_preserves_each_last_good_independently(self) -> None:
        first = self.add("One")["subscription"]
        second = self.add("Two")["subscription"]
        first_file = subscriptions.paths()["calendars"] / first["id"] / "event-1.ics"
        second_file = subscriptions.paths()["calendars"] / second["id"] / "event-1.ics"
        before_first = first_file.read_bytes()
        before_second = second_file.read_bytes()

        def fetch(credential: dict[str, str]) -> bytes:
            if "calendar.example.test" in credential["url"]:
                raise subscriptions.SubscriptionError("fetch_failed", "calendar feed could not be fetched")
            return VALID_ICS

        # Give the second feed a distinguishable safe URL in its keyring.
        credential = subscriptions.lookup_secret(second["id"])
        credential["url"] = "https://second.example.test/feed"
        subscriptions.store_secret(second["id"], credential)
        with mock.patch.object(subscriptions, "fetch_feed", side_effect=fetch):
            status = subscriptions.refresh_subscriptions()
        self.assertFalse(status["ok"])
        self.assertEqual([entry["ok"] for entry in status["subscriptions"]], [False, True])
        self.assertEqual(first_file.read_bytes(), before_first)
        self.assertEqual(second_file.read_bytes(), before_second)
        self.assertNotIn("https://", subscriptions.paths()["status"].read_text())

    def test_refresh_isolates_expected_os_failures_but_not_programmer_errors(self) -> None:
        first = self.add("One")["subscription"]
        self.add("Two")
        calls = 0

        def refresh(item: dict[str, str]) -> int:
            nonlocal calls
            calls += 1
            if item["id"] == first["id"]:
                raise PermissionError("denied")
            return 7

        with mock.patch.object(subscriptions, "refresh_one", side_effect=refresh):
            status = subscriptions.refresh_subscriptions()
        self.assertEqual(calls, 2)
        self.assertEqual([entry["ok"] for entry in status["subscriptions"]], [False, True])
        with (
            mock.patch.object(subscriptions, "refresh_one", side_effect=AssertionError("bug")),
            self.assertRaisesRegex(AssertionError, "bug"),
        ):
            subscriptions.refresh_subscriptions()

    def test_url_and_basic_pair_validation(self) -> None:
        invalid = ("http://example.test/feed", "https://user:pass@example.test/feed", "https://example.test/feed#secret", "https://example.test/%zz", " https://example.test/feed", "https://127.0.0.1/feed", "https://[::1]/feed", "https://192.168.1.2/feed", "https://169.254.1.2/feed")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(subscriptions.SubscriptionError):
                subscriptions.validated_url(url)
        with self.assertRaisesRegex(subscriptions.SubscriptionError, "together"):
            subscriptions.add_subscription({"action": "add", "name": "x", "url": "https://example.test", "username": "x"}, lambda *_args: None)

    def test_dns_resolved_private_destinations_are_rejected(self) -> None:
        parsed = subscriptions.validated_url("https://calendar.example/feed")
        with (
            mock.patch.object(subscriptions.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.2", 443))]),
            self.assertRaisesRegex(subscriptions.SubscriptionError, "non-public"),
        ):
            subscriptions.validate_public_destination(parsed)
        with mock.patch.object(subscriptions.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            subscriptions.validate_public_destination(parsed)

    def test_basic_authorization_is_stripped_on_cross_origin_redirect(self) -> None:
        handler = subscriptions.SafeRedirectHandler()
        request = urllib.request.Request(
            "https://one.example.test/feed",
            headers={"Authorization": "Basic secret", "Accept": "text/calendar"},
        )
        with mock.patch.object(subscriptions, "validate_public_destination"):
            redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://two.example.test/feed")
        self.assertNotIn("Authorization", redirected.headers)
        self.assertEqual(redirected.headers["Accept"], "text/calendar")
        with mock.patch.object(subscriptions, "validate_public_destination"):
            same_origin = handler.redirect_request(request, None, 302, "Found", {}, "https://one.example.test/next")
        self.assertEqual(same_origin.headers["Authorization"], "Basic secret")

    def test_fetch_deadline_spans_body_reads(self) -> None:
        class Response:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.fp = None

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def geturl(self) -> str:
                return "https://calendar.example/feed"

            def read(self, _size: int) -> bytes:
                return b"chunk"

        opener = mock.Mock()
        opener.open.return_value = Response()
        with (
            mock.patch.object(subscriptions, "validate_public_destination"),
            mock.patch.object(subscriptions.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(subscriptions.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 31.0]),
            self.assertRaisesRegex(subscriptions.SubscriptionError, "timed out"),
        ):
            subscriptions.fetch_feed({"url": "https://calendar.example/feed"})

    def test_oversized_and_event_bounded_calendar(self) -> None:
        with (
            self.assertRaises(subscriptions.SubscriptionError),
            mock.patch.object(subscriptions, "fetch_feed", return_value=b"x" * (subscriptions.MAX_FEED_BYTES + 1)),
        ):
            subscriptions.add_subscription({"action": "add", "name": "x", "url": "https://example.test/feed"}, lambda *_args: None)
        many = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + b"BEGIN:VEVENT\r\nUID:x\r\nEND:VEVENT\r\n" * (subscriptions.MAX_EVENTS_PER_FEED + 1) + b"END:VCALENDAR\r\n"
        with self.assertRaises(subscriptions.SubscriptionError):
            subscriptions.validate_icalendar(many)

    def test_termination_is_honored_before_commit_and_deferred_after(self) -> None:
        subscriptions.reset_commit_state()
        with self.assertRaises(subscriptions.OperationCancelled):
            subscriptions.handle_termination(signal.SIGTERM, None)
        stages: list[str] = []
        subscriptions.reset_commit_state()
        subscriptions.begin_commit(lambda stage, _details: stages.append(stage), "a" * 32)
        subscriptions.handle_termination(signal.SIGTERM, None)
        subscriptions.complete_commit()
        subscriptions.handle_termination(signal.SIGTERM, None)
        self.assertEqual(stages, ["committing"])

    def test_lock_excludes_refresh(self) -> None:
        lock = subscriptions.acquire_lock()
        try:
            with self.assertRaisesRegex(subscriptions.SubscriptionError, "already running"):
                subscriptions.refresh_subscriptions()
        finally:
            lock.close()

    def test_install_timer_updates_units_and_migrates_legacy_helper(self) -> None:
        log = Path(self.temporary.name) / "systemctl.log"
        self.env["SYSTEMCTL_LOG"] = str(log)
        os.environ["SYSTEMCTL_LOG"] = str(log)
        self.command("systemctl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n")
        obsolete = Path(self.env["HOME"]) / ".local" / "bin" / "omarchy-calendar-sync"
        obsolete.parent.mkdir(parents=True)
        obsolete.write_text("#!/bin/sh\nexec vdirsyncer sync\n")
        result = subprocess.run([str(WRAPPER), "install-timer"], capture_output=True, env=self.env, timeout=10, check=True)
        self.assertIn(b"timer installed", result.stdout)
        self.assertFalse(obsolete.exists())
        installed = Path(self.env["HOME"]) / ".local" / "bin" / "omarchy-calendar"
        self.assertTrue(installed.is_file())
        self.assertTrue(installed.stat().st_mode & stat.S_IXUSR)
        help_result = subprocess.run([str(installed), "--help"], capture_output=True, env=self.env, timeout=10, check=True)
        self.assertIn(b"install-timer", help_result.stdout)
        runtime = Path(self.env["XDG_DATA_HOME"]) / "omarchy-calendar" / "runtime"
        self.assertTrue((runtime / "backend" / "subscriptions.py").is_file())
        units = Path(self.env["XDG_CONFIG_HOME"]) / "systemd" / "user"
        for name in ("omarchy-calendar-sync.service", "omarchy-calendar-sync.timer"):
            self.assertEqual((units / name).read_bytes(), (ROOT / "systemd" / name).read_bytes())
        expected_lifecycle = [
            "--user disable --now omarchy-calendar-sync.timer",
            "--user stop omarchy-calendar-sync.service",
            "--user daemon-reload",
            "--user enable --now omarchy-calendar-sync.timer",
        ]
        self.assertEqual(log.read_text().splitlines(), expected_lifecycle)
        (units / "omarchy-calendar-sync.service").write_text("obsolete vdirsyncer service")
        subprocess.run([str(installed), "install-timer"], capture_output=True, env=self.env, timeout=10, check=True)
        self.assertEqual(log.read_text().splitlines(), expected_lifecycle * 2)
        self.assertEqual((units / "omarchy-calendar-sync.service").read_bytes(), (ROOT / "systemd" / "omarchy-calendar-sync.service").read_bytes())
        self.assertIn("omarchy-calendar sync", (units / "omarchy-calendar-sync.service").read_text())
        self.assertNotIn("vdirsyncer", (units / "omarchy-calendar-sync.service").read_text())

    def test_subscription_protocol_list_is_bounded_ndjson(self) -> None:
        self.add()
        result = subprocess.run(
            [str(WRAPPER), "subscriptions"], input=b'{"action":"list","requestId":"one"}\n',
            capture_output=True, env=self.env, timeout=10, check=True,
        )
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["final"])
        self.assertEqual(lines[0]["requestId"], "one")
        oversized = subprocess.run(
            [str(WRAPPER), "subscriptions"], input=b"{" + b" " * subscriptions.MAX_REQUEST_BYTES,
            capture_output=True, env=self.env, timeout=10, check=True,
        )
        self.assertEqual(json.loads(oversized.stdout)["error"]["code"], "request_too_large")


class BackendTests(Isolated):
    def test_request_id_is_required_and_echoed_on_errors(self) -> None:
        result = subprocess.run(
            [str(WRAPPER), "request"], input=b'{"action":"status"}\n',
            capture_output=True, env=self.env, timeout=10, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_request")
        response = self.protocol({"action": "unknown", "requestId": "error-id"})
        self.assertEqual(response["requestId"], "error-id")

    def test_zero_subscriptions_lists_and_calendars_without_khal(self) -> None:
        (self.bin / "khal").unlink()
        self.assertEqual(self.protocol({"action": "calendars"}), {"ok": True, "calendars": [], "requestId": "test-request"})
        listed = self.protocol({"action": "list", "start": "2030-01-01", "end": "2030-01-01"})
        self.assertEqual(listed, {"ok": True, "events": [], "requestId": "test-request"})

    def test_list_preserves_inclusive_dates_and_normalizes_events(self) -> None:
        item = self.add(color="#112233")["subscription"]
        response = self.protocol({"action": "list", "requestId": "r", "start": "2030-01-02", "end": "2030-01-02", "calendars": [item["id"]]})
        self.assertTrue(response["ok"])
        self.assertEqual(response["events"][0]["start"], "2030-01-02T10:00")
        self.assertEqual(response["events"][0]["color"], "#112233")
        self.assertEqual(response["requestId"], "r")

    def test_real_khal_returns_every_recurrence_without_once(self) -> None:
        item = self.import_with_real_khal()
        with mock.patch.object(calendar_backend.shutil, "which", return_value=str(REAL_KHAL)):
            response = calendar_backend.request_list({
                "action": "list", "requestId": "real-recurring", "start": "2030-01-02", "end": "2030-01-04",
                "calendars": [item["id"]],
            })
        recurring = [event for event in response["events"] if event["uid"] == "recurring"]
        self.assertEqual([event["start"][:10] for event in recurring], ["2030-01-02", "2030-01-03", "2030-01-04"])

    def test_real_khal_all_day_end_dates_are_exclusive(self) -> None:
        item = self.import_with_real_khal()
        with mock.patch.object(calendar_backend.shutil, "which", return_value=str(REAL_KHAL)):
            response = calendar_backend.request_list({
                "action": "list", "requestId": "real-all-day", "start": "2030-01-03", "end": "2030-01-08",
                "calendars": [item["id"]],
            })
        by_uid = {event["uid"]: event for event in response["events"] if event["uid"] in {"one-day", "multi-day"}}
        self.assertEqual((by_uid["one-day"]["start"], by_uid["one-day"]["end"]), ("2030-01-03", "2030-01-04"))
        self.assertEqual((by_uid["multi-day"]["start"], by_uid["multi-day"]["end"]), ("2030-01-05", "2030-01-08"))

    def test_readonly_actions_are_rejected(self) -> None:
        for action in ("create", "update", "delete"):
            with self.subTest(action=action):
                response = self.protocol({"action": action})
                self.assertEqual(response["error"]["code"], "read_only")

    def test_status_has_metadata_versions_and_sanitized_refresh(self) -> None:
        item = self.add()["subscription"]
        subscriptions.write_status({"attempted": True, "ok": False, "at": "2030-01-01T00:00:00+00:00", "subscriptions": [{"id": item["id"], "ok": False, "error": {"code": "fetch_failed", "message": "calendar feed could not be fetched"}}]})
        response = self.protocol({"action": "status"})
        self.assertTrue(response["readOnly"])
        self.assertEqual(response["subscriptionCount"], 1)
        self.assertEqual(response["subscriptions"], [item])
        self.assertIn("python-icalendar", response["versions"])
        self.assertNotIn("https://", json.dumps(response))

    def test_qml_subscription_payload_and_commit_contract(self) -> None:
        panel = (ROOT / "Panel.qml").read_text()
        self.assertIn('property string pendingSubscriptionPayload: ""', panel)
        self.assertIn('write(root.pendingSubscriptionPayload + "\\n")', panel)
        self.assertIn('root.pendingSubscriptionPayload = ""', panel)
        self.assertIn('feedUrl.text = ""; feedUsername.text = ""; feedPassword.text = ""', panel)
        self.assertIn('parsed.response.stage === "committing"', panel)
        self.assertIn('subscriptionBusy && !root.subscriptionCommitStarted', panel)
        self.assertIn('subscriptionCancelled = false\n        subscriptionState = "success"', panel)
        bar = (ROOT / "BarWidget.qml").read_text()
        self.assertIn('if (root.opened) root.refresh(); else root.open()', bar)
        self.assertNotIn('root.refresh(); root.open()', bar)

    def test_request_bounds_and_range_validation(self) -> None:
        response = self.protocol({"action": "list", "start": "2030-01-01", "end": "2032-01-01"})
        self.assertEqual(response["error"]["code"], "invalid_request")
        result = subprocess.run([str(WRAPPER), "request"], input=b"{" + b" " * calendar_backend.MAX_REQUEST_BYTES, capture_output=True, env=self.env, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "request_too_large")
