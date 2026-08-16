"""Secure, transactional remote account setup for the widget protocol."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

APP = "omarchy-calendar"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_SECRET_BYTES = 16 * 1024
MAX_ACTIVE_FILE_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 300
SECRET_TIMEOUT_SECONDS = 30
PROVIDERS = frozenset(("google", "caldav", "icloud"))
active_process: subprocess.Popen[bytes] | None = None


class SetupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SetupCancelled(SetupError):
    def __init__(self) -> None:
        super().__init__("cancelled", "account setup was cancelled")


def xdg_path(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else Path.home() / fallback


def app_paths() -> dict[str, Path]:
    config = xdg_path("XDG_CONFIG_HOME", ".config") / APP
    data = xdg_path("XDG_DATA_HOME", ".local/share") / APP
    state = xdg_path("XDG_STATE_HOME", ".local/state") / APP
    return {
        "config": config,
        "data": data,
        "state": state,
        "khal_config": config / "khal.conf",
        "vdirsyncer_config": config / "vdirsyncer.conf",
        "profile": config / "remote-profile.json",
        "lock": state / "setup.lock",
    }


def source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def assert_outside_source(current_paths: dict[str, Path]) -> None:
    root = source_root()
    for name in ("config", "data", "state"):
        try:
            current_paths[name].resolve().relative_to(root)
        except ValueError:
            continue
        raise SetupError("unsafe_path", f"refusing to write {name} inside the plugin source tree")


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def quoted(value: str) -> str:
    assert value
    assert "\x00" not in value
    assert "\n" not in value
    assert "\r" not in value
    return json.dumps(value, ensure_ascii=False)


def khal_config(current_paths: dict[str, Path]) -> str:
    local = current_paths["data"] / "calendars" / "local"
    synced = current_paths["data"] / "calendars" / "synced" / "*"
    return f"""[calendars]
[[local]]
path = {local}
type = calendar

[[synced]]
path = {synced}
type = discover

[locale]
timeformat = %H:%M
dateformat = %Y-%m-%d
longdateformat = %Y-%m-%d
datetimeformat = %Y-%m-%d %H:%M
longdatetimeformat = %Y-%m-%d %H:%M

[default]
default_calendar = local
highlight_event_days = true
print_new = event
"""


def secret_attributes(provider: str, account_id: str, slot: int) -> list[str]:
    assert provider in PROVIDERS
    assert len(account_id) == 32
    assert slot in (0, 1)
    return [
        "service", APP,
        "purpose", "remote-credential",
        "provider", provider,
        "account", account_id,
        "slot", str(slot),
    ]


def vdirsyncer_config(
    current_paths: dict[str, Path],
    request: dict[str, str],
    account_id: str,
    slot: int,
    *,
    status_path: Path,
    synced_path: Path,
    token_path: Path | None,
) -> str:
    fetch = json.dumps(["command", "secret-tool", "lookup", *secret_attributes(request["provider"], account_id, slot)])
    provider = request["provider"]
    if provider == "google":
        assert token_path is not None
        remote_lines = (
            'type = "google_calendar"',
            f"token_file = {quoted(str(token_path))}",
            f"client_id = {quoted(request['clientId'])}",
            f"client_secret.fetch = {fetch}",
        )
    else:
        assert token_path is None
        url = request["url"] if provider == "caldav" else "https://caldav.icloud.com/"
        remote_lines = (
            'type = "caldav"',
            f"url = {quoted(url)}",
            f"username = {quoted(request['username'])}",
            f"password.fetch = {fetch}",
        )
    remote = "\n".join(remote_lines)
    return f"""[general]
status_path = {quoted(str(status_path))}

[pair omarchy_calendar]
a = "omarchy_calendar_local"
b = "omarchy_calendar_remote"
collections = ["from a", "from b"]
metadata = ["displayname", "color"]

[storage omarchy_calendar_local]
type = "filesystem"
path = {quoted(str(synced_path))}
fileext = ".ics"

[storage omarchy_calendar_remote]
{remote}
"""


def bounded_string(
    request: dict[str, Any], key: str, *, maximum: int, required: bool = False
) -> str | None:
    value = request.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SetupError("invalid_request", f"{key} must be a string")
    if required and not value.strip():
        raise SetupError("invalid_request", f"{key} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise SetupError("invalid_request", f"{key} exceeds {maximum} bytes")
    if any(character in value for character in "\x00\r\n"):
        raise SetupError("invalid_request", f"{key} contains a forbidden control character")
    return value


def validate_request(raw_request: Any) -> tuple[dict[str, str], str]:
    if not isinstance(raw_request, dict):
        raise SetupError("invalid_request", "request must be a JSON object")
    provider = bounded_string(raw_request, "provider", maximum=16, required=True)
    request_id = bounded_string(raw_request, "requestId", maximum=128, required=True)
    secret = bounded_string(raw_request, "secret", maximum=MAX_SECRET_BYTES, required=True)
    display_name = bounded_string(raw_request, "displayName", maximum=256)
    assert provider is not None
    assert request_id is not None
    assert secret is not None
    if provider not in PROVIDERS:
        raise SetupError("invalid_request", "provider must be google, caldav, or icloud")

    base = {"requestId", "provider", "displayName", "secret"}
    required = set(base) - {"displayName"}
    if provider == "google":
        allowed = base | {"clientId"}
        required.add("clientId")
    elif provider == "caldav":
        allowed = base | {"username", "url"}
        required.update(("username", "url"))
    else:
        allowed = base | {"username"}
        required.add("username")
    unknown = sorted(set(raw_request) - allowed)
    missing = sorted(required - set(raw_request))
    if unknown:
        raise SetupError("invalid_request", f"unknown fields: {', '.join(unknown)}")
    if missing:
        raise SetupError("invalid_request", f"missing fields: {', '.join(missing)}")

    validated = {"requestId": request_id, "provider": provider}
    if display_name is not None:
        validated["displayName"] = display_name
    if provider == "google":
        client_id = bounded_string(raw_request, "clientId", maximum=512, required=True)
        assert client_id is not None
        validated["clientId"] = client_id
    else:
        username = bounded_string(raw_request, "username", maximum=512, required=True)
        assert username is not None
        validated["username"] = username
        if provider == "caldav":
            url = bounded_string(raw_request, "url", maximum=4096, required=True)
            assert url is not None
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                raise SetupError("invalid_request", "url must be an HTTP(S) URL without embedded credentials")
            validated["url"] = url
    return validated, secret


def account_id_for(request: dict[str, str]) -> str:
    provider = request["provider"]
    if provider == "google":
        identity = request["clientId"]
    elif provider == "caldav":
        identity = f"{request['username']}\n{request['url']}"
    else:
        identity = request["username"]
    return hashlib.sha256(f"{provider}\n{identity}".encode()).hexdigest()[:32]


def read_profile(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        profile = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(profile, dict):
        return None
    provider = profile.get("provider")
    account_id = profile.get("accountId")
    slot = profile.get("credentialSlot")
    if provider not in PROVIDERS or not isinstance(account_id, str) or len(account_id) != 32 or slot not in (0, 1):
        return None
    if any(character not in "0123456789abcdef" for character in account_id):
        return None
    return profile


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=1)


def handle_termination(_signum: int, _frame: Any) -> None:
    process = active_process
    if process is not None:
        terminate_process_group(process)
    raise SetupCancelled()


def run_process(argv: list[str], timeout: int, *, input_buffer: bytearray | None = None) -> None:
    global active_process
    assert argv
    assert timeout > 0
    executable = shutil.which(argv[0])
    if executable is None:
        raise SetupError("missing_command", f"required command is not installed: {argv[0]}")
    resolved_argv = [executable, *argv[1:]]
    try:
        process = subprocess.Popen(
            resolved_argv,
            stdin=subprocess.PIPE if input_buffer is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError as error:
        raise SetupError("command_failed", f"failed to start {Path(executable).name}") from error
    active_process = process
    try:
        if input_buffer is not None:
            assert process.stdin is not None
            process.stdin.write(input_buffer)
            process.stdin.close()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            terminate_process_group(process)
            raise SetupError("command_timeout", f"{Path(executable).name} timed out") from error
    finally:
        if input_buffer is not None:
            input_buffer[:] = b"\x00" * len(input_buffer)
        if process.poll() is None:
            terminate_process_group(process)
        active_process = None
    if returncode in (128 + signal.SIGINT, 128 + signal.SIGTERM, -signal.SIGINT, -signal.SIGTERM):
        raise SetupCancelled()
    if returncode != 0:
        raise SetupError("command_failed", f"{Path(executable).name} exited unsuccessfully")


def clear_secret(attributes: list[str]) -> bool:
    try:
        run_process(["secret-tool", "clear", *attributes], SECRET_TIMEOUT_SECONDS)
    except SetupError:
        return False
    return True


def read_active_file(path: Path) -> bytes | None:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_ACTIVE_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > MAX_ACTIVE_FILE_BYTES:
        raise SetupError("active_state_too_large", f"active {path.name} exceeds its safety limit")
    return content


def restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, previous)


def google_token_path(state_path: Path, account_id: str, slot: int) -> Path:
    return state_path / f"google-token-{account_id}-{slot}.json"


def profile_bytes(request: dict[str, str], account_id: str, slot: int) -> bytes:
    profile: dict[str, Any] = {
        "version": 1,
        "provider": request["provider"],
        "displayName": request.get("displayName") or request["provider"].capitalize(),
        "accountId": account_id,
        "credentialSlot": slot,
    }
    return (json.dumps(profile, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def setup_remote(
    request: dict[str, str],
    secret: str,
    emit_progress: Callable[[str, str, bool], None],
) -> dict[str, Any]:
    current_paths = app_paths()
    assert_outside_source(current_paths)
    for directory in (current_paths["config"], current_paths["data"], current_paths["state"]):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    lock_stream = current_paths["lock"].open("a+b")
    os.chmod(current_paths["lock"], 0o600)
    try:
        try:
            fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("setup_busy", "another account setup is already running") from error

        config_path = current_paths["vdirsyncer_config"]
        khal_path = current_paths["khal_config"]
        profile_path = current_paths["profile"]
        replaces_existing = config_path.is_file()
        old_profile = read_profile(profile_path) if replaces_existing else None
        old_config = read_active_file(config_path)
        old_khal = read_active_file(khal_path)
        old_profile_bytes = read_active_file(profile_path)
        old_was_legacy_google = old_profile is None and old_config is not None and b'type = "google_calendar"' in old_config

        account_id = account_id_for(request)
        slot = 1 - int(old_profile["credentialSlot"]) if old_profile is not None else 0
        attributes = secret_attributes(request["provider"], account_id, slot)
        active_synced = current_paths["data"] / "calendars" / "synced"
        active_synced_existed = active_synced.exists()
        if active_synced_existed and not active_synced.is_dir():
            raise SetupError("invalid_active_state", "active synchronized calendar path is not a directory")
        active_status = current_paths["state"] / "vdirsyncer-status"
        active_status_existed = active_status.exists()
        if active_status_existed and not active_status.is_dir():
            raise SetupError("invalid_active_state", "active synchronization status path is not a directory")

        emit_progress("storing_secret", "Saving credential", replaces_existing)
        secret_buffer = bytearray(secret.encode("utf-8"))
        secret = ""
        credential_attempted = True
        setup_committed = False
        cleanup_complete = True
        candidate_root: Path | None = None
        final_token: Path | None = None
        final_token_previous: bytes | None = None
        final_token_moved = False
        backup_synced: Path | None = None
        backup_status: Path | None = None
        data_activated = False
        status_activated = False
        try:
            run_process(
                ["secret-tool", "store", "--label=Omarchy Calendar remote credential", *attributes],
                SECRET_TIMEOUT_SECONDS,
                input_buffer=secret_buffer,
            )
            candidate_root = Path(tempfile.mkdtemp(prefix=".remote-candidate-", dir=current_paths["data"]))
            os.chmod(candidate_root, 0o700)
            candidate_synced = candidate_root / "synced"
            candidate_synced.mkdir(mode=0o700)
            candidate_status = candidate_root / "status"
            candidate_status.mkdir(mode=0o700)
            candidate_token = candidate_root / "google-token.json" if request["provider"] == "google" else None
            candidate_config = candidate_root / "vdirsyncer.conf"
            candidate_content = vdirsyncer_config(
                current_paths,
                request,
                account_id,
                slot,
                status_path=candidate_status,
                synced_path=candidate_synced,
                token_path=candidate_token,
            )
            atomic_write(candidate_config, candidate_content.encode())

            stage_message = "Authorizing Google in your browser" if request["provider"] == "google" else "Discovering calendars"
            emit_progress("authorizing" if request["provider"] == "google" else "discovering", stage_message, replaces_existing)
            run_process(["vdirsyncer", "-c", str(candidate_config), "discover"], COMMAND_TIMEOUT_SECONDS)
            emit_progress("syncing", "Performing initial calendar sync", replaces_existing)
            run_process(["vdirsyncer", "-c", str(candidate_config), "sync"], COMMAND_TIMEOUT_SECONDS)
            if candidate_token is not None and not candidate_token.is_file():
                raise SetupError("oauth_failed", "Google authorization did not produce an OAuth token")

            final_content = vdirsyncer_config(
                current_paths,
                request,
                account_id,
                slot,
                status_path=current_paths["state"] / "vdirsyncer-status",
                synced_path=active_synced,
                token_path=(google_token_path(current_paths["state"], account_id, slot) if candidate_token else None),
            ).encode()
            emit_progress("committing", "Activating remote account", replaces_existing)

            try:
                if candidate_token is not None:
                    final_token = google_token_path(current_paths["state"], account_id, slot)
                    final_token_previous = read_active_file(final_token)
                    final_token.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.replace(candidate_token, final_token)
                    final_token_moved = True
                    os.chmod(final_token, 0o600)

                active_synced.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if active_synced_existed:
                    backup_synced = Path(tempfile.mkdtemp(prefix=".synced-backup-", dir=active_synced.parent))
                    backup_synced.rmdir()
                    os.replace(active_synced, backup_synced)
                os.replace(candidate_synced, active_synced)
                data_activated = True
                os.chmod(active_synced, 0o700)

                if active_status_existed:
                    backup_status = Path(tempfile.mkdtemp(prefix=".status-backup-", dir=active_status.parent))
                    backup_status.rmdir()
                    os.replace(active_status, backup_status)
                os.replace(candidate_status, active_status)
                status_activated = True
                os.chmod(active_status, 0o700)

                (current_paths["data"] / "calendars" / "local").mkdir(mode=0o700, parents=True, exist_ok=True)
                atomic_write(khal_path, khal_config(current_paths).encode())
                atomic_write(config_path, final_content)
                atomic_write(profile_path, profile_bytes(request, account_id, slot))
            except BaseException:
                restore_file(khal_path, old_khal)
                restore_file(config_path, old_config)
                restore_file(profile_path, old_profile_bytes)
                if status_activated:
                    shutil.rmtree(active_status, ignore_errors=True)
                if backup_status is not None:
                    os.replace(backup_status, active_status)
                    backup_status = None
                if data_activated:
                    shutil.rmtree(active_synced, ignore_errors=True)
                if backup_synced is not None:
                    os.replace(backup_synced, active_synced)
                    backup_synced = None
                if final_token_moved and final_token is not None:
                    restore_file(final_token, final_token_previous)
                    final_token_moved = False
                raise
            setup_committed = True
            if backup_synced is not None:
                shutil.rmtree(backup_synced, ignore_errors=True)
                backup_synced = None
            if backup_status is not None:
                shutil.rmtree(backup_status, ignore_errors=True)
                backup_status = None

            if old_profile is not None:
                old_attributes = secret_attributes(
                    str(old_profile["provider"]), str(old_profile["accountId"]), int(old_profile["credentialSlot"])
                )
                if old_attributes != attributes:
                    cleanup_complete = clear_secret(old_attributes) and cleanup_complete
                if old_profile["provider"] == "google":
                    old_token = google_token_path(
                        current_paths["state"], str(old_profile["accountId"]), int(old_profile["credentialSlot"])
                    )
                    if old_token != final_token:
                        old_token.unlink(missing_ok=True)
            elif old_was_legacy_google:
                (current_paths["state"] / "google-token.json").unlink(missing_ok=True)

            return {
                "ok": True,
                "provider": request["provider"],
                "displayName": request.get("displayName") or request["provider"].capitalize(),
                "connected": True,
                "replacesExisting": replaces_existing,
                "cleanupComplete": cleanup_complete,
            }
        finally:
            secret = ""
            if candidate_root is not None:
                shutil.rmtree(candidate_root, ignore_errors=True)
            if backup_synced is not None:
                if not active_synced.exists():
                    os.replace(backup_synced, active_synced)
                else:
                    shutil.rmtree(backup_synced, ignore_errors=True)
            if backup_status is not None:
                if not active_status.exists():
                    os.replace(backup_status, active_status)
                else:
                    shutil.rmtree(backup_status, ignore_errors=True)
            if credential_attempted and not setup_committed:
                clear_secret(attributes)
            if final_token_moved and not setup_committed and final_token is not None:
                restore_file(final_token, final_token_previous)
    finally:
        lock_stream.close()


def read_request(stream: BinaryIO) -> dict[str, Any]:
    raw = stream.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise SetupError("request_too_large", f"request exceeds {MAX_REQUEST_BYTES} bytes")
    if not raw:
        raise SetupError("invalid_json", "request is empty")
    if not raw.endswith(b"\n"):
        raise SetupError("invalid_json", "request must end with a newline")
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SetupError("invalid_json", "request is not valid UTF-8 JSON") from error
    if not isinstance(request, dict):
        raise SetupError("invalid_request", "request must be a JSON object")
    return request


def encode_line(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise SetupError("response_too_large", "setup response exceeded its protocol limit")
    return encoded + b"\n"


def run_protocol(input_stream: BinaryIO, output_stream: BinaryIO) -> int:
    os.umask(0o077)
    request: dict[str, Any] = {}
    request_id: str | None = None
    replaces_existing: bool | None = None
    final_emitted = False

    def emit(value: dict[str, Any]) -> None:
        output_stream.write(encode_line(value))
        output_stream.flush()

    try:
        request = read_request(input_stream)
        validated, secret = validate_request(request)
        request_id = validated["requestId"]
        request.pop("secret", None)
        request.clear()
        replaces_existing = app_paths()["vdirsyncer_config"].is_file()

        def progress(stage: str, message: str, replaces_existing: bool) -> None:
            emit({
                "type": "progress",
                "requestId": request_id,
                "stage": stage,
                "message": message,
                "replacesExisting": replaces_existing,
            })

        try:
            result = setup_remote(validated, secret, progress)
        finally:
            secret = ""
        response = {"type": "result", "final": True, "requestId": request_id, **result}
    except SetupError as error:
        response = {
            "type": "result",
            "final": True,
            "ok": False,
            "error": {"code": error.code, "message": error.message},
        }
    except Exception:  # noqa: BLE001 - stdout must end with one valid final response.
        response = {
            "type": "result",
            "final": True,
            "ok": False,
            "error": {"code": "internal_error", "message": "unexpected account setup failure"},
        }
    if replaces_existing is not None:
        response.setdefault("replacesExisting", replaces_existing)
    if request_id is None:
        candidate_id = request.get("requestId")
        if isinstance(candidate_id, str) and len(candidate_id.encode()) <= 128:
            request_id = candidate_id
    if request_id is not None:
        response.setdefault("requestId", request_id)
    if not final_emitted:
        emit(response)
        final_emitted = True
    return 0


def main() -> int:
    import sys

    signal.signal(signal.SIGTERM, handle_termination)
    signal.signal(signal.SIGINT, handle_termination)
    return run_protocol(sys.stdin.buffer, sys.stdout.buffer)
