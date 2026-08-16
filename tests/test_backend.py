from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "omarchy-calendar"
BACKEND = ROOT / "bin" / "omarchy-calendar-backend"
SETUP = ROOT / "bin" / "omarchy-calendar-setup"
SETUP_REQUEST = ROOT / "bin" / "omarchy-calendar-setup-request"
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
                "SECRET_INPUT_LOG": str(root / "secret-input.log"),
                "KEYRING_PATH": str(root / "keyring.json"),
                "SETUP_MARKER": str(root / "setup.marker"),
                "CHILD_MARKER": str(root / "child.marker"),
                "CLEAR_MARKER": str(root / "clear.marker"),
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

STATEFUL_SECRET_TOOL_SETUP = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
args = sys.argv[1:]
with pathlib.Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write("secret-tool " + json.dumps(args) + "\n")
keyring_path = pathlib.Path(os.environ["KEYRING_PATH"])
try:
    keyring = json.loads(keyring_path.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    keyring = {}
if args and args[0] == "store":
    secret_value = sys.stdin.read()
    with pathlib.Path(os.environ["SECRET_INPUT_LOG"]).open("a") as stream:
        stream.write(secret_value)
    if os.environ.get("FAIL_SECRET_STORE") == "1":
        raise SystemExit(3)
    attributes = args[2:]
    keyring[json.dumps(attributes, separators=(",", ":"))] = secret_value
    keyring_path.write_text(json.dumps(keyring, separators=(",", ":")))
elif args and args[0] == "lookup":
    value = keyring.get(json.dumps(args[1:], separators=(",", ":")))
    if value is None:
        raise SystemExit(1)
    sys.stdout.write(value)
elif args and args[0] == "clear":
    pathlib.Path(os.environ["CLEAR_MARKER"]).write_text(str(os.getpid()))
    if os.environ.get("BLOCK_CLEAR") == "1":
        time.sleep(60)
    if os.environ.get("FAIL_SECRET_CLEAR") == "1":
        raise SystemExit(4)
    keyring.pop(json.dumps(args[1:], separators=(",", ":")), None)
    keyring_path.write_text(json.dumps(keyring, separators=(",", ":")))
else:
    raise SystemExit(2)
'''

ADVANCED_VDIRSYNCER_SETUP = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys, time
args = sys.argv[1:]
with pathlib.Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write("vdirsyncer " + json.dumps(args) + "\n")
config = pathlib.Path(args[args.index("-c") + 1])
content = config.read_text()
phase = args[-1]
if os.environ.get("BLOCK_SETUP") == phase:
    if os.environ.get("SPAWN_TERM_IGNORING_CHILD") == "1":
        child_code = (
            "import os,pathlib,signal,time;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "pathlib.Path(os.environ['CHILD_MARKER']).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        subprocess.Popen([sys.executable, "-c", child_code])
    pathlib.Path(os.environ["SETUP_MARKER"]).write_text(str(os.getpid()))
    time.sleep(60)
if os.environ.get("FAIL_SETUP") == phase:
    print("remote rejected setup", file=sys.stderr)
    raise SystemExit(130 if os.environ.get("CANCEL_SETUP") == "1" else 7)
path_match = re.search(r'^path = (".*")$', content, re.MULTILINE)
if path_match:
    synced = pathlib.Path(json.loads(path_match.group(1)))
    synced.mkdir(parents=True, exist_ok=True)
    if phase == "sync":
        candidate = synced / "remote.ics"
        if os.environ.get("OVERSIZE_CANDIDATE") == "1":
            with candidate.open("wb") as stream:
                stream.truncate(256 * 1024 * 1024 + 1)
        else:
            candidate.write_text("candidate")
token_match = re.search(r'^token_file = (".*")$', content, re.MULTILINE)
if token_match and phase == "discover":
    if os.environ.get("EMIT_GOOGLE_URL") == "1":
        print("diagnostic that must not be forwarded", flush=True)
        print("https://accounts.google.com/o/oauth2/auth?client_id=desktop&scope=calendar", flush=True)
    token = pathlib.Path(json.loads(token_match.group(1)))
    token.write_text(json.dumps({"token": config.parent.name}))
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
        module.MAX_RESPONSE_BYTES = 128  # pyright: ignore[reportAttributeAccessIssue]
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


class WidgetSetupRequestTests(IsolatedEnvironment):
    def setUp(self) -> None:
        super().setUp()
        self.write_command("secret-tool", STATEFUL_SECRET_TOOL_SETUP)
        self.write_command("vdirsyncer", ADVANCED_VDIRSYNCER_SETUP)

    def run_setup_request(
        self, request: object | None = None, *, raw: bytes | None = None
    ) -> tuple[subprocess.CompletedProcess[bytes], list[dict[str, Any]]]:
        payload = raw if raw is not None else json.dumps(request).encode() + b"\n"
        result = subprocess.run(
            [str(SETUP_REQUEST)],
            input=payload,
            capture_output=True,
            env=self.env,
            timeout=10,
            check=True,
        )
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.stderr, b"")
        self.assertEqual(sum(line.get("final") is True for line in lines), 1)
        self.assertTrue(lines[-1]["final"])
        return result, lines

    def start_setup_request(self, request: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [str(SETUP_REQUEST)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(request).encode() + b"\n")
        process.stdin.close()
        return process

    def finish_setup_process(self, process: subprocess.Popen[bytes], timeout: float = 15) -> list[dict[str, Any]]:
        process.wait(timeout=timeout)
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, b"")
        lines = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(sum(line.get("final") is True for line in lines), 1)
        return lines

    def wait_for_file(self, path: Path, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"timed out waiting for {path}")

    def keyring(self) -> dict[str, str]:
        path = Path(self.env["KEYRING_PATH"])
        return json.loads(path.read_text()) if path.exists() else {}

    def test_caldav_stores_stdin_secret_and_emits_progress_then_one_final(self) -> None:
        secret = "super-secret-value"
        result, lines = self.run_setup_request({
            "requestId": "setup-caldav",
            "provider": "caldav",
            "displayName": "Work",
            "username": "person@example.test",
            "url": "https://calendar.example.test/dav/",
            "secret": secret,
        })
        final = lines[-1]
        self.assertTrue(final["ok"])
        self.assertFalse(final["replacesExisting"])
        self.assertTrue(any(line.get("stage") == "discovering" for line in lines))
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        content = config.read_text()
        self.assertIn('password.fetch = ["command", "secret-tool", "lookup", "service", "omarchy-calendar"', content)
        self.assertNotIn(secret, content)
        self.assertEqual(Path(self.env["SECRET_INPUT_LOG"]).read_text(), secret)
        command_log = Path(self.env["COMMAND_LOG"]).read_text()
        self.assertNotIn(secret, command_log)
        self.assertNotIn(secret.encode(), result.stdout)
        self.assertNotIn(secret.encode(), result.stderr)
        self.assertIn('secret-tool ["store", "--label=Omarchy Calendar remote credential"', command_log)
        self.assertIn('vdirsyncer ["-c"', command_log)
        self.assertLess(max(len(line) for line in result.stdout.splitlines()), 16 * 1024)

        status = self.run_backend({"action": "status"})
        self.assertEqual(status["remoteAccount"], {
            "connected": True,
            "provider": "caldav",
            "displayName": "Work",
            "setupMode": "replace",
            "singleProfile": True,
        })

    def test_keyring_attributes_are_exact_namespaced_and_stateful(self) -> None:
        request = {
            "requestId": "exact-one",
            "provider": "caldav",
            "username": "person@example.test",
            "url": "https://calendar.example.test/dav/",
            "secret": "first-value",
        }
        _, first = self.run_setup_request(request)
        self.assertTrue(first[-1]["ok"])
        config_root = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        profile = json.loads((config_root / "remote-profile.json").read_text())
        account_id = hashlib.sha256(
            b"caldav\nperson@example.test\nhttps://calendar.example.test/dav/"
        ).hexdigest()[:32]
        namespace = hashlib.sha256("\n".join((
            str(config_root),
            str(Path(self.env["XDG_DATA_HOME"]) / "omarchy-calendar"),
            str(Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar"),
        )).encode()).hexdigest()[:32]
        attributes = [
            "service", "omarchy-calendar",
            "purpose", "remote-credential",
            "installation", namespace,
            "provider", "caldav",
            "account", account_id,
            "slot", profile["credentialSlot"],
        ]
        commands = [
            json.loads(line.removeprefix("secret-tool "))
            for line in Path(self.env["COMMAND_LOG"]).read_text().splitlines()
            if line.startswith("secret-tool ")
        ]
        self.assertEqual(commands[0], ["store", "--label=Omarchy Calendar remote credential", *attributes])
        config = (config_root / "vdirsyncer.conf").read_text()
        self.assertIn(json.dumps(["command", "secret-tool", "lookup", *attributes]), config)
        self.assertEqual(self.keyring(), {json.dumps(attributes, separators=(",", ":")): "first-value"})

        request["requestId"] = "exact-two"
        request["secret"] = "second-value"
        _, second = self.run_setup_request(request)
        self.assertTrue(second[-1]["ok"])
        updated_profile = json.loads((config_root / "remote-profile.json").read_text())
        updated_attributes = [*attributes[:-1], updated_profile["credentialSlot"]]
        commands = [
            json.loads(line.removeprefix("secret-tool "))
            for line in Path(self.env["COMMAND_LOG"]).read_text().splitlines()
            if line.startswith("secret-tool ")
        ]
        self.assertEqual(commands[-1], ["clear", *attributes])
        self.assertEqual(self.keyring(), {
            json.dumps(updated_attributes, separators=(",", ":")): "second-value"
        })

    def test_missing_profile_failed_same_account_replacement_preserves_active_credential(self) -> None:
        request = {
            "requestId": "active",
            "provider": "caldav",
            "username": "same@example.test",
            "url": "https://same.example.test/dav",
            "secret": "active-value",
        }
        _, first = self.run_setup_request(request)
        self.assertTrue(first[-1]["ok"])
        config_root = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        config_path = config_root / "vdirsyncer.conf"
        before_config = config_path.read_bytes()
        before_keyring = self.keyring()
        (config_root / "remote-profile.json").unlink()

        self.env["FAIL_SETUP"] = "sync"
        request["requestId"] = "failed-replacement"
        request["secret"] = "candidate-value"
        _, failed = self.run_setup_request(request)
        self.assertFalse(failed[-1]["ok"])
        self.assertEqual(config_path.read_bytes(), before_config)
        self.assertEqual(self.keyring(), before_keyring)
        clear_commands = [
            json.loads(line.removeprefix("secret-tool "))
            for line in Path(self.env["COMMAND_LOG"]).read_text().splitlines()
            if line.startswith('secret-tool ["clear"')
        ]
        self.assertEqual(len(clear_commands), 1)
        self.assertNotEqual(clear_commands[0][1:], json.loads(next(iter(before_keyring))))

    def test_separate_xdg_installations_do_not_share_credentials(self) -> None:
        request = {
            "requestId": "root-one",
            "provider": "icloud",
            "username": "same@icloud.test",
            "secret": "root-one-value",
        }
        _, first = self.run_setup_request(request)
        self.assertTrue(first[-1]["ok"])
        second_env = self.env.copy()
        root = Path(self.temporary.name) / "second-root"
        for variable, name in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
            second_env[variable] = str(root / name)
        request["requestId"] = "root-two"
        request["secret"] = "root-two-value"
        result = subprocess.run(
            [str(SETUP_REQUEST)],
            input=json.dumps(request).encode() + b"\n",
            capture_output=True,
            env=second_env,
            timeout=10,
            check=True,
        )
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])["ok"])
        keyring = self.keyring()
        self.assertEqual(set(keyring.values()), {"root-one-value", "root-two-value"})
        attributes = [json.loads(key) for key in keyring]
        installation_values = [value[value.index("installation") + 1] for value in attributes]
        self.assertEqual(len(set(installation_values)), 2)

    def test_google_browser_event_is_validated_and_unrelated_output_is_not_forwarded(self) -> None:
        self.env["EMIT_GOOGLE_URL"] = "1"
        result, lines = self.run_setup_request({
            "requestId": "google-browser",
            "provider": "google",
            "clientId": "desktop.apps.googleusercontent.com",
            "secret": "oauth-secret",
        })
        browser = [line for line in lines if line.get("type") == "browser"]
        self.assertEqual(len(browser), 1)
        self.assertEqual(browser[0]["requestId"], "google-browser")
        self.assertTrue(browser[0]["url"].startswith("https://accounts.google.com/"))
        self.assertNotIn(b"diagnostic that must not be forwarded", result.stdout)

    def test_oversize_candidate_fails_before_commit_and_clears_credential(self) -> None:
        self.env["OVERSIZE_CANDIDATE"] = "1"
        _, lines = self.run_setup_request({
            "requestId": "oversize-candidate",
            "provider": "caldav",
            "username": "person@example.test",
            "url": "https://calendar.example.test/dav",
            "secret": "candidate-value",
        })
        self.assertFalse(lines[-1]["ok"])
        self.assertEqual(lines[-1]["error"]["code"], "candidate_too_large")
        self.assertEqual(self.keyring(), {})
        config = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf"
        self.assertFalse(config.exists())

    def test_cleanup_failure_is_reported_without_reverting_commit(self) -> None:
        base = {
            "requestId": "cleanup-one",
            "provider": "icloud",
            "username": "first@icloud.test",
            "secret": "first-value",
        }
        _, first = self.run_setup_request(base)
        self.assertTrue(first[-1]["ok"])
        self.env["FAIL_SECRET_CLEAR"] = "1"
        replacement = {
            "requestId": "cleanup-two",
            "provider": "icloud",
            "username": "second@icloud.test",
            "secret": "second-value",
        }
        _, second = self.run_setup_request(replacement)
        self.assertTrue(second[-1]["ok"])
        self.assertFalse(second[-1]["cleanupComplete"])
        profile = json.loads(
            (Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "remote-profile.json").read_text()
        )
        self.assertEqual(profile["displayName"], "Icloud")

    def test_cancellation_during_commit_rolls_back_active_state(self) -> None:
        base = {
            "requestId": "commit-base",
            "provider": "caldav",
            "username": "base@example.test",
            "url": "https://base.example.test/dav",
            "secret": "base-value",
        }
        _, first = self.run_setup_request(base)
        self.assertTrue(first[-1]["ok"])
        config_root = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        before = {
            name: (config_root / name).read_bytes()
            for name in ("vdirsyncer.conf", "khal.conf", "remote-profile.json")
        }
        before_keyring = self.keyring()

        from backend import remote_setup

        replacement = {
            "requestId": "commit-cancel",
            "provider": "caldav",
            "username": "replacement@example.test",
            "url": "https://replacement.example.test/dav",
            "secret": "replacement-value",
        }
        validated, secret_buffer = remote_setup.validate_request(replacement)
        real_atomic_write = remote_setup.atomic_write
        cancellation_raised = False

        def cancel_profile_write(path: Path, content: bytes, mode: int = 0o600) -> None:
            nonlocal cancellation_raised
            if path.name == "remote-profile.json" and not cancellation_raised:
                cancellation_raised = True
                raise remote_setup.SetupCancelled()
            real_atomic_write(path, content, mode)

        with (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(remote_setup, "atomic_write", cancel_profile_write),
            self.assertRaises(remote_setup.SetupCancelled),
        ):
            remote_setup.setup_remote(validated, secret_buffer, lambda *_args: None, lambda _url: None)
        self.assertTrue(cancellation_raised)
        for name, content in before.items():
            self.assertEqual((config_root / name).read_bytes(), content)
        self.assertEqual(self.keyring(), before_keyring)

    def test_signal_after_commit_returns_success_and_marks_cleanup_incomplete(self) -> None:
        _, first = self.run_setup_request({
            "requestId": "signal-base",
            "provider": "icloud",
            "username": "base@icloud.test",
            "secret": "base-value",
        })
        self.assertTrue(first[-1]["ok"])
        Path(self.env["CLEAR_MARKER"]).unlink(missing_ok=True)
        self.env["BLOCK_CLEAR"] = "1"
        process = self.start_setup_request({
            "requestId": "signal-replacement",
            "provider": "icloud",
            "username": "replacement@icloud.test",
            "secret": "replacement-value",
        })
        self.wait_for_file(Path(self.env["CLEAR_MARKER"]))
        process.send_signal(signal.SIGTERM)
        lines = self.finish_setup_process(process)
        self.assertTrue(lines[-1]["ok"])
        self.assertFalse(lines[-1]["cleanupComplete"])
        profile = json.loads(
            (Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "remote-profile.json").read_text()
        )
        self.assertEqual(profile["accountId"], hashlib.sha256(b"icloud\nreplacement@icloud.test").hexdigest()[:32])

    def test_real_sigterm_cleans_entire_process_group_and_rolls_back(self) -> None:
        self.env["BLOCK_SETUP"] = "discover"
        self.env["SPAWN_TERM_IGNORING_CHILD"] = "1"
        process = self.start_setup_request({
            "requestId": "process-tree",
            "provider": "caldav",
            "username": "person@example.test",
            "url": "https://calendar.example.test/dav",
            "secret": "candidate-value",
        })
        self.wait_for_file(Path(self.env["SETUP_MARKER"]))
        self.wait_for_file(Path(self.env["CHILD_MARKER"]))
        child_pid = int(Path(self.env["CHILD_MARKER"]).read_text())
        process.send_signal(signal.SIGTERM)
        lines = self.finish_setup_process(process, timeout=20)
        self.assertFalse(lines[-1]["ok"])
        self.assertEqual(lines[-1]["error"]["code"], "cancelled")
        deadline = time.monotonic() + 5
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())
        self.assertEqual(self.keyring(), {})

    def test_setup_lock_excludes_scheduled_sync(self) -> None:
        config_root = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        config_root.mkdir(parents=True)
        (config_root / "vdirsyncer.conf").write_text("active")
        self.env["BLOCK_SETUP"] = "discover"
        process = self.start_setup_request({
            "requestId": "lock-holder",
            "provider": "caldav",
            "username": "person@example.test",
            "url": "https://calendar.example.test/dav",
            "secret": "candidate-value",
        })
        self.wait_for_file(Path(self.env["SETUP_MARKER"]))
        sync = subprocess.run([str(SYNC)], env=self.env, capture_output=True, timeout=10, check=False)
        self.assertEqual(sync.returncode, 1)
        self.assertIn(b"another account setup or synchronization", sync.stderr)
        process.send_signal(signal.SIGTERM)
        self.finish_setup_process(process, timeout=20)

    def test_failed_or_cancelled_setup_cleans_candidate_and_preserves_active_state(self) -> None:
        _, first_lines = self.run_setup_request({
            "requestId": "first",
            "provider": "caldav",
            "username": "first@example.test",
            "url": "https://first.example.test/",
            "secret": "first-secret",
        })
        self.assertTrue(first_lines[-1]["ok"])
        config_dir = Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar"
        data_dir = Path(self.env["XDG_DATA_HOME"]) / "omarchy-calendar" / "calendars" / "synced"
        before_config = (config_dir / "vdirsyncer.conf").read_bytes()
        before_khal = (config_dir / "khal.conf").read_bytes()
        before_profile = (config_dir / "remote-profile.json").read_bytes()
        before_data = (data_dir / "remote.ics").read_bytes()

        self.env["FAIL_SETUP"] = "discover"
        self.env["CANCEL_SETUP"] = "1"
        result, failed_lines = self.run_setup_request({
            "requestId": "cancelled",
            "provider": "google",
            "displayName": "Personal",
            "clientId": "client.apps.googleusercontent.com",
            "secret": "super-secret-value",
        })
        final = failed_lines[-1]
        self.assertFalse(final["ok"])
        self.assertEqual(final["error"]["code"], "cancelled")
        self.assertNotIn(b"super-secret-value", result.stdout + result.stderr)
        self.assertEqual((config_dir / "vdirsyncer.conf").read_bytes(), before_config)
        self.assertEqual((config_dir / "khal.conf").read_bytes(), before_khal)
        self.assertEqual((config_dir / "remote-profile.json").read_bytes(), before_profile)
        self.assertEqual((data_dir / "remote.ics").read_bytes(), before_data)
        command_lines = Path(self.env["COMMAND_LOG"]).read_text().splitlines()
        self.assertTrue(command_lines[-1].startswith('secret-tool ["clear"'))
        tokens = list((Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar").glob("google-token-*.json"))
        self.assertEqual(tokens, [])

    def test_successful_google_replacement_uses_isolated_token_and_clears_only_old_state(self) -> None:
        state = Path(self.env["XDG_STATE_HOME"]) / "omarchy-calendar"
        _, first_lines = self.run_setup_request({
            "requestId": "google-one",
            "provider": "google",
            "clientId": "one.apps.googleusercontent.com",
            "secret": "client-one-secret",
        })
        self.assertTrue(first_lines[-1]["ok"])
        first_tokens = list(state.glob("google-token-*.json"))
        self.assertEqual(len(first_tokens), 1)
        first_token = first_tokens[0]
        first_token_content = first_token.read_bytes()

        self.env["FAIL_SETUP"] = "sync"
        _, failed_lines = self.run_setup_request({
            "requestId": "google-two-failed",
            "provider": "google",
            "clientId": "two.apps.googleusercontent.com",
            "secret": "client-two-secret",
        })
        self.assertFalse(failed_lines[-1]["ok"])
        self.assertTrue(first_token.is_file())
        self.assertEqual(first_token.read_bytes(), first_token_content)
        self.assertEqual(len(list(state.glob("google-token-*.json"))), 1)

        self.env.pop("FAIL_SETUP")
        _, replacement_lines = self.run_setup_request({
            "requestId": "google-two-success",
            "provider": "google",
            "displayName": "Second Google",
            "clientId": "two.apps.googleusercontent.com",
            "secret": "client-two-secret",
        })
        final = replacement_lines[-1]
        self.assertTrue(final["ok"])
        self.assertTrue(final["replacesExisting"])
        self.assertFalse(first_token.exists())
        new_tokens = list(state.glob("google-token-*.json"))
        self.assertEqual(len(new_tokens), 1)
        config = (Path(self.env["XDG_CONFIG_HOME"]) / "omarchy-calendar" / "vdirsyncer.conf").read_text()
        self.assertIn(str(new_tokens[0]), config)
        self.assertNotIn(str(first_token), config)
        clear_lines = [
            line for line in Path(self.env["COMMAND_LOG"]).read_text().splitlines()
            if line.startswith('secret-tool ["clear"')
        ]
        self.assertGreaterEqual(len(clear_lines), 2)

    def test_caldav_requires_safe_https_but_allows_private_hosts(self) -> None:
        invalid_urls = (
            "http://calendar.example.test/dav",
            "https://person:password@calendar.example.test/dav",
            "https://calendar.example.test/dav#fragment",
            "https://calendar.example.test:bad/dav",
            "https://calendar.example.test/%zz",
            " https://calendar.example.test/dav",
            "https://calendar.example.test\\@evil.test/dav",
        )
        for index, url in enumerate(invalid_urls):
            with self.subTest(url=url):
                _, lines = self.run_setup_request({
                    "requestId": f"invalid-url-{index}",
                    "provider": "caldav",
                    "username": "person@example.test",
                    "url": url,
                    "secret": "candidate-value",
                })
                self.assertFalse(lines[-1]["ok"])
                self.assertEqual(lines[-1]["error"]["code"], "invalid_request")
        self.assertFalse(Path(self.env["COMMAND_LOG"]).exists())

        _, private = self.run_setup_request({
            "requestId": "private-url",
            "provider": "caldav",
            "username": "person@example.test",
            "url": "https://calendar.lan:8443/dav",
            "secret": "candidate-value",
        })
        self.assertTrue(private[-1]["ok"])

    def test_request_is_newline_delimited_bounded_and_provider_specific(self) -> None:
        _, no_newline = self.run_setup_request(raw=b'{"requestId":"x"}')
        self.assertEqual(no_newline[-1]["error"]["code"], "invalid_json")
        _, oversized = self.run_setup_request(raw=b"{" + b" " * (64 * 1024) + b"\n")
        self.assertEqual(oversized[-1]["error"]["code"], "request_too_large")
        _, invalid = self.run_setup_request({
            "requestId": "bad-fields",
            "provider": "icloud",
            "username": "person@icloud.test",
            "url": "https://must-not-be-accepted.test/",
            "secret": "app-password",
        })
        self.assertEqual(invalid[-1]["error"]["code"], "invalid_request")
        self.assertFalse(Path(self.env["COMMAND_LOG"]).exists())


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
