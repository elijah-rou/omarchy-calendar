from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from backend import calendar_backend, subscriptions

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "omarchy-calendar"
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
    print(json.dumps([{"uid":"event-1","title":"Planning","start":"2030-01-02 10:00","end":"2030-01-02 11:00","calendar":"feed","all-day":"False"}]))
else: raise SystemExit(2)
'''


class Isolated(unittest.TestCase):
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

    def protocol(self, request: dict[str, Any]) -> dict[str, Any]:
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

    def test_url_and_basic_pair_validation(self) -> None:
        invalid = ("http://example.test/feed", "https://user:pass@example.test/feed", "https://example.test/feed#secret", "https://example.test/%zz", " https://example.test/feed")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(subscriptions.SubscriptionError):
                subscriptions.validated_url(url)
        with self.assertRaisesRegex(subscriptions.SubscriptionError, "together"):
            subscriptions.add_subscription({"action": "add", "name": "x", "url": "https://example.test", "username": "x"}, lambda *_args: None)

    def test_basic_authorization_is_stripped_on_cross_origin_redirect(self) -> None:
        handler = subscriptions.SafeRedirectHandler()
        request = urllib.request.Request(
            "https://one.example.test/feed",
            headers={"Authorization": "Basic secret", "Accept": "text/calendar"},
        )
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://two.example.test/feed")
        self.assertNotIn("Authorization", redirected.headers)
        self.assertEqual(redirected.headers["Accept"], "text/calendar")
        same_origin = handler.redirect_request(request, None, 302, "Found", {}, "https://one.example.test/next")
        self.assertEqual(same_origin.headers["Authorization"], "Basic secret")

    def test_oversized_and_event_bounded_calendar(self) -> None:
        with (
            self.assertRaises(subscriptions.SubscriptionError),
            mock.patch.object(subscriptions, "fetch_feed", return_value=b"x" * (subscriptions.MAX_FEED_BYTES + 1)),
        ):
            subscriptions.add_subscription({"action": "add", "name": "x", "url": "https://example.test/feed"}, lambda *_args: None)
        many = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + b"BEGIN:VEVENT\r\nUID:x\r\nEND:VEVENT\r\n" * (subscriptions.MAX_EVENTS_PER_FEED + 1) + b"END:VCALENDAR\r\n"
        with self.assertRaises(subscriptions.SubscriptionError):
            subscriptions.validate_icalendar(many)

    def test_lock_excludes_refresh(self) -> None:
        lock = subscriptions.acquire_lock()
        try:
            with self.assertRaisesRegex(subscriptions.SubscriptionError, "already running"):
                subscriptions.refresh_subscriptions()
        finally:
            lock.close()

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
    def test_zero_subscriptions_lists_and_calendars_without_khal(self) -> None:
        (self.bin / "khal").unlink()
        self.assertEqual(self.protocol({"action": "calendars"}), {"ok": True, "calendars": []})
        listed = self.protocol({"action": "list", "start": "2030-01-01", "end": "2030-01-01"})
        self.assertEqual(listed, {"ok": True, "events": []})

    def test_list_preserves_inclusive_dates_and_normalizes_events(self) -> None:
        item = self.add()["subscription"]
        response = self.protocol({"action": "list", "requestId": "r", "start": "2030-01-02", "end": "2030-01-02", "calendars": [item["id"]]})
        self.assertTrue(response["ok"])
        self.assertEqual(response["events"][0]["start"], "2030-01-02T10:00")
        self.assertEqual(response["requestId"], "r")

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

    def test_request_bounds_and_range_validation(self) -> None:
        response = self.protocol({"action": "list", "start": "2030-01-01", "end": "2032-01-01"})
        self.assertEqual(response["error"]["code"], "invalid_request")
        result = subprocess.run([str(WRAPPER), "request"], input=b"{" + b" " * calendar_backend.MAX_REQUEST_BYTES, capture_output=True, env=self.env, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "request_too_large")
