from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "omarchy-calendar"
BACKEND = ROOT / "bin" / "omarchy-calendar-backend"
SETUP = ROOT / "bin" / "omarchy-calendar-setup"
SYNC = ROOT / "bin" / "omarchy-calendar-sync"


class IsolatedEnvironment(unittest.TestCase):
    def __init__(self, method_name: str = "runTest") -> None:
        super().__init__(method_name)
        self.temporary = tempfile.TemporaryDirectory()
        self.bin = Path()
        self.env: dict[str, str] = {}

    def setUp(self) -> None:
        root = Path(self.temporary.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "PATH": f"{self.bin}:{self.env.get('PATH', '')}",
                "COMMAND_LOG": str(root / "commands.log"),
            }
        )
        Path(self.env["HOME"]).mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_command(self, name: str, content: str) -> Path:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_backend(self, request: object, *, raw: bytes | None = None) -> dict[str, Any]:
        payload = raw if raw is not None else json.dumps(request).encode()
        result = subprocess.run(
            [str(BACKEND)],
            input=payload,
            capture_output=True,
            env=self.env,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stderr, b"")
        return json.loads(result.stdout)


FAKE_KHAL = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ["COMMAND_LOG"])
with log.open("a") as stream:
    stream.write("khal " + json.dumps(args) + "\n")
if "--version" in args:
    print("khal, version 0.14.0")
elif "printcalendars" in args:
    print("local")
    print("work")
elif "list" in args:
    if os.environ.get("HUGE_OUTPUT") == "1":
        print("x" * (2 * 1024 * 1024))
        raise SystemExit(0)
    print("[]")
    print(json.dumps([{"uid":"one","title":"Standup","start":"2030-01-02 10:00","end":"2030-01-02 10:30","calendar":"work","all-day":"False"}]))
    print("[]")
elif "new" in args:
    print(json.dumps([{"uid":"new","title":"Planning","start":"2030-01-03 09:00","end":"2030-01-03 10:00","calendar":"local","all-day":"False"}]))
else:
    raise SystemExit(2)
'''

FAKE_VDIRSYNCER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write("vdirsyncer " + json.dumps(args) + "\n")
if "--version" in args:
    print("vdirsyncer, version 0.20.0")
elif os.environ.get("FAIL_SYNC") == "1" and "sync" in args:
    print("remote unavailable", file=sys.stderr)
    raise SystemExit(4)
'''


class BackendProtocolTests(IsolatedEnvironment):
    def setUp(self) -> None:
        super().setUp()
        self.write_command("khal", FAKE_KHAL)
        self.write_command("vdirsyncer", FAKE_VDIRSYNCER)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        config.mkdir(parents=True)
        (config / "khal.conf").write_text("test", encoding="utf-8")

    def test_wrapper_dispatches_request_from_a_symlink(self) -> None:
        link = self.bin / "omarchy-calendar"
        link.symlink_to(WRAPPER)
        result = subprocess.run(
            [str(link), "request"],
            input=b'{"action":"status","requestId":"wrapper"}\n',
            capture_output=True,
            env=self.env,
            timeout=10,
            check=True,
        )
        response = json.loads(result.stdout)
        self.assertTrue(response["ok"])
        self.assertEqual(response["requestId"], "wrapper")

    def test_list_returns_normalized_bounded_events(self) -> None:
        response = self.run_backend(
            {"action": "list", "requestId": "r1", "start": "2030-01-01", "end": "2030-01-04", "calendars": ["work"]}
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["requestId"], "r1")
        self.assertEqual(response["events"][0]["title"], "Standup")
        self.assertEqual(response["events"][0]["calendarId"], "work")
        self.assertEqual(response["events"][0]["start"], "2030-01-02T10:00")
        self.assertFalse(response["events"][0]["allDay"])
        self.assertNotIn("all-day", response["events"][0])
        log = Path(self.env["COMMAND_LOG"]).read_text()
        self.assertIn('"--include-calendar", "work"', log)
        self.assertNotIn("shell=True", log)

    def test_qml_create_contract_uses_calendar_id_and_minute_times(self) -> None:
        response = self.run_backend({
            "action": "create",
            "requestId": "qml-create",
            "calendarId": "local",
            "title": "Planning",
            "start": "2030-01-03T09:00",
            "end": "2030-01-03T10:00",
            "allDay": False,
            "sync": False,
        })
        self.assertTrue(response["ok"])
        self.assertEqual(response["requestId"], "qml-create")
        self.assertEqual(response["event"]["calendarId"], "local")
        lines = Path(self.env["COMMAND_LOG"]).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("khal "))

    def test_create_is_saved_before_failed_sync(self) -> None:
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        (config / "vdirsyncer.conf").write_text("test", encoding="utf-8")
        self.env["FAIL_SYNC"] = "1"
        response = self.run_backend(
            {
                "action": "create",
                "title": "Planning",
                "start": "2030-01-03T09:00",
                "end": "2030-01-03T10:00",
                "description": "Roadmap",
            }
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["event"]["uid"], "new")
        self.assertFalse(response["sync"]["ok"])
        lines = Path(self.env["COMMAND_LOG"]).read_text().splitlines()
        self.assertTrue(lines[0].startswith("khal "))
        self.assertTrue(lines[1].startswith("vdirsyncer "))
        status = json.loads((Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar" / "sync-status.json").read_text())
        self.assertFalse(status["ok"])

    def test_calendars_and_status(self) -> None:
        calendars = self.run_backend({"action": "calendars"})
        self.assertEqual(calendars, {
            "ok": True,
            "calendars": [
                {"id": "local", "name": "local", "writable": True},
                {"id": "work", "name": "work", "writable": True},
            ],
        })
        status = self.run_backend({"action": "status"})
        self.assertTrue(status["configured"])
        self.assertEqual(status["versions"]["khal"], "khal, version 0.14.0")
        self.assertEqual(status["versions"]["vdirsyncer"], "vdirsyncer, version 0.20.0")

    def test_rejects_unknown_fields_ranges_and_oversize_input(self) -> None:
        unknown = self.run_backend({"action": "status", "requestId": "error-id", "extra": True})
        self.assertEqual(unknown["error"]["code"], "invalid_request")
        self.assertEqual(unknown["requestId"], "error-id")
        one_day = self.run_backend({"action": "list", "start": "2030-01-01", "end": "2030-01-01"})
        self.assertTrue(one_day["ok"])
        wide = self.run_backend({"action": "list", "start": "2030-01-01", "end": "2032-01-01"})
        self.assertEqual(wide["error"]["code"], "invalid_request")
        oversize = self.run_backend({}, raw=b"{" + b" " * (64 * 1024))
        self.assertEqual(oversize["error"]["code"], "request_too_large")

    def test_oversize_response_preserves_request_id(self) -> None:
        spec = importlib.util.spec_from_file_location("calendar_backend_test", ROOT / "backend" / "calendar_backend.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.MAX_RESPONSE_BYTES = 128
        encoded = module.encode_response({"ok": True, "requestId": "large-id", "events": ["x" * 512]})
        response = json.loads(encoded)
        self.assertFalse(response["ok"])
        self.assertEqual(response["requestId"], "large-id")
        self.assertEqual(response["error"]["code"], "response_too_large")

    def test_command_output_is_bounded(self) -> None:
        self.env["HUGE_OUTPUT"] = "1"
        response = self.run_backend({
            "action": "list",
            "requestId": "huge",
            "start": "2030-01-01",
            "end": "2030-01-02",
        })
        self.assertFalse(response["ok"])
        self.assertEqual(response["requestId"], "huge")
        self.assertEqual(response["error"]["code"], "command_failed")
        self.assertIn("output exceeded", response["error"]["message"])

    def test_rejects_invalid_values_before_invoking_commands(self) -> None:
        response = self.run_backend(
            {"action": "create", "title": "bad\nname", "start": "2030-01-01T10:00", "end": "2030-01-01T11:00"}
        )
        self.assertEqual(response["error"]["code"], "invalid_request")
        invalid_id = self.run_backend({"action": "status", "requestId": 7})
        self.assertEqual(invalid_id["error"]["code"], "invalid_request")
        self.assertFalse(Path(self.env["COMMAND_LOG"]).exists())


class SetupTests(IsolatedEnvironment):
    def setUp(self) -> None:
        super().setUp()
        self.write_command("secret-tool", "#!/bin/sh\nprintf '%s\\n' fake-secret\n")

    def run_setup(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SETUP), *arguments],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
            check=True,
        )

    def test_local_bootstrap_is_private_and_outside_repository(self) -> None:
        self.run_setup("local")
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "khal.conf"
        calendar = Path(self.env["XDG_DATA_HOME"]) / "omarchy-calendar" / "calendars" / "local"
        self.assertTrue(config.is_file())
        self.assertTrue(calendar.is_dir())
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(calendar.stat().st_mode), 0o700)
        self.assertIn("print_new = event", config.read_text())
        self.assertFalse((Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf").exists())

    def test_caldav_uses_argv_secret_fetch_not_a_stored_password(self) -> None:
        self.run_setup(
            "caldav",
            "--url", "https://calendar.example.test/dav/",
            "--username", "person@example.test",
            "--password-command", "secret-tool",
            "--password-arg", "lookup",
            "--password-arg", "calendar",
            "--configure-only",
        )
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        content = config.read_text()
        self.assertIn('password.fetch = ["command", "secret-tool", "lookup", "calendar"]', content)
        self.assertNotIn("fake-secret", content)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_local_rerun_preserves_remote_configuration(self) -> None:
        self.run_setup(
            "caldav", "--url", "https://example.test/", "--username", "me",
            "--password-command", "secret-tool", "--configure-only",
        )
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        before = config.read_text()
        self.run_setup("local")
        self.assertEqual(config.read_text(), before)
        khal = config.with_name("khal.conf").read_text()
        self.assertIn("[[synced]]", khal)

    def test_icloud_and_google_templates_use_external_secret_fetch(self) -> None:
        self.run_setup(
            "icloud", "--username", "person@icloud.test", "--password-command", "secret-tool", "--configure-only"
        )
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        self.assertIn("https://caldav.icloud.com/", config.read_text())
        self.run_setup(
            "google", "--client-id", "client-id.apps.googleusercontent.com",
            "--client-secret-command", "secret-tool", "--configure-only",
        )
        content = config.read_text()
        self.assertIn('type = "google_calendar"', content)
        self.assertIn('client_secret.fetch = ["command", "secret-tool"]', content)
        self.assertIn(str(Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar" / "google-token.json"), content)

    def test_remote_setup_discovers_then_syncs_with_argv(self) -> None:
        self.write_command("vdirsyncer", FAKE_VDIRSYNCER)
        self.run_setup(
            "caldav", "--url", "https://example.test/", "--username", "me",
            "--password-command", "secret-tool",
        )
        lines = Path(self.env["COMMAND_LOG"]).read_text().splitlines()
        self.assertIn("discover", lines[0])
        self.assertIn("sync", lines[1])

    def test_failed_remote_setup_preserves_active_configuration(self) -> None:
        self.write_command("vdirsyncer", FAKE_VDIRSYNCER)
        self.run_setup(
            "caldav", "--url", "https://working.test/", "--username", "me",
            "--password-command", "secret-tool", "--configure-only",
        )
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        khal = config.with_name("khal.conf")
        khal.write_text("CUSTOM-KHAL\n", encoding="utf-8")
        synced = Path(self.env["XDG_DATA_HOME"]) / "omarchy-calendar" / "calendars" / "synced"
        sentinel = synced / "existing.ics"
        sentinel.write_text("existing", encoding="utf-8")
        before = config.read_text()
        before_khal = khal.read_text()
        self.env["FAIL_SYNC"] = "1"
        result = subprocess.run(
            [
                str(SETUP), "caldav", "--url", "https://broken.test/", "--username", "me",
                "--password-command", "secret-tool",
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(config.read_text(), before)
        self.assertEqual(khal.read_text(), before_khal)
        self.assertEqual(sentinel.read_text(), "existing")

    @unittest.skipUnless(shutil.which("vdirsyncer"), "vdirsyncer is not installed")
    def test_generated_caldav_config_loads_with_vdirsyncer_020(self) -> None:
        executable = shutil.which("vdirsyncer")
        assert executable is not None
        version = subprocess.run(
            [executable, "--version"], text=True, stdout=subprocess.PIPE, check=True
        ).stdout.strip()
        if version != "vdirsyncer, version 0.20.0":
            self.skipTest(f"requires vdirsyncer 0.20.0, found {version}")
        self.run_setup(
            "caldav", "--url", "https://example.test/", "--username", "me",
            "--password-command", "secret-tool", "--configure-only",
        )
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        result = subprocess.run(
            [executable, "-c", str(config), "showconfig"],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        parsed = json.loads(result.stdout)
        remote = parsed["storages"][1]
        self.assertEqual(remote["type"], "caldav")
        self.assertEqual(remote["password.fetch"], ["command", "secret-tool"])


class SyncTests(IsolatedEnvironment):
    def test_sync_records_bounded_failure(self) -> None:
        self.write_command("vdirsyncer", FAKE_VDIRSYNCER)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        config.mkdir(parents=True)
        (config / "vdirsyncer.conf").write_text("test", encoding="utf-8")
        self.env["FAIL_SYNC"] = "1"
        result = subprocess.run([str(SYNC)], env=self.env, capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1)
        status_path = Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar" / "sync-status.json"
        status = json.loads(status_path.read_text())
        self.assertFalse(status["ok"])
        self.assertIn("remote unavailable", status["error"])
        self.assertEqual(stat.S_IMODE(status_path.stat().st_mode), 0o600)


@unittest.skipUnless(shutil.which("khal"), "khal is not installed")
class Khal014CompatibilityTests(IsolatedEnvironment):
    def test_local_config_create_and_list_with_khal_014(self) -> None:
        version = subprocess.run(["khal", "--version"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
        if version != "khal, version 0.14.0":
            self.skipTest(f"requires khal 0.14.0, found {version}")
        self.run_setup_local()
        created = self.run_backend(
            {"action": "create", "title": "Compatibility", "start": "2030-04-05T10:00", "end": "2030-04-05T10:30", "sync": False}
        )
        end_date = self.run_backend(
            {"action": "create", "title": "Inclusive end", "start": "2030-04-06T10:00", "end": "2030-04-06T10:30", "sync": False}
        )
        self.assertTrue(created["ok"], created)
        self.assertTrue(end_date["ok"], end_date)
        listed = self.run_backend({"action": "list", "start": "2030-04-05", "end": "2030-04-06"})
        self.assertEqual(
            [event["title"] for event in listed["events"]],
            ["Compatibility", "Inclusive end"],
        )

    def run_setup_local(self) -> None:
        subprocess.run([str(SETUP), "local"], env=self.env, stdout=subprocess.PIPE, check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
