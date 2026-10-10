"""Command line and the narrow, short-lived privileged GUI protocol."""
from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import stat
import sys
import time

MAX_MESSAGE = 65536
IDLE_SECONDS = 600


def engine():
    from .engine import Engine
    return Engine()


def validate_options(value):
    if not isinstance(value, dict):
        raise ValueError("Options must be an object")
    allowed = {"operation", "windows_label", "timeout", "default_os", "custom_icons", "reset_icons"}
    if set(value) - allowed:
        raise ValueError("Unknown option")
    result = dict(value)
    if result.get("operation") not in {"install", "update", "uninstall"}:
        raise ValueError("Unknown operation")
    label = result.get("windows_label", "Windows")
    if not isinstance(label, str) or not 1 <= len(label) <= 32 or not label.isprintable():
        raise ValueError("Invalid Windows label")
    timeout = result.get("timeout", 5)
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("Timeout must be between 1 and 60 seconds")
    if result.get("default_os", "steamos") not in {"steamos", "windows"}:
        raise ValueError("Invalid default OS")
    for key in ("custom_icons", "reset_icons"):
        icons = result.get(key, [])
        if not isinstance(icons, list) or len(icons) > 2 or any(x not in ("steamos", "windows") for x in icons) or len(set(icons)) != len(icons):
            raise ValueError("Invalid icon selection")
    if set(result.get("custom_icons", [])) & set(result.get("reset_icons", [])):
        raise ValueError("An icon cannot be replaced and reset together")
    result.update(windows_label=label, timeout=timeout, default_os=result.get("default_os", "steamos"))
    return result


def validate_stage(stage, uid):
    """Only use the private, user-prepared directory fixed at helper startup."""
    path = Path(stage)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Invalid staging directory")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError("Staging directory must be private and owned by the requesting user")
    theme = path / "theme"
    if theme.is_symlink() or not theme.is_dir():
        raise ValueError("Missing prepared theme")
    # The engine also validates its explicit asset set before writing anything.
    for item in theme.rglob("*"):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError("Unsupported staged file")
    return path.resolve()


class HelperSession:
    """The caller may accept a held plan, but cannot replace its contents."""
    def __init__(self, backend, stage):
        self.backend = backend
        self.stage = Path(stage)
        self.plan = None
        self.token = None

    def handle(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
            raise ValueError("Invalid request")
        action = request["action"]
        permitted = {
            "discover": {"action"}, "plan": {"action", "options"},
            "apply": {"action", "token"}, "rollback": {"action", "backup_id"},
            "close": {"action"},
        }
        if action not in permitted or set(request) != permitted[action]:
            raise ValueError("Unknown request or fields")
        if action == "discover":
            return self.backend.discover()
        if action == "close":
            self.plan = self.token = None
            return {"closed": True}
        if action == "plan":
            self.plan = self.token = None
            options = validate_options(request["options"])
            icons = options.pop("custom_icons", [])
            reset_icons = options.pop("reset_icons", [])
            options["asset_source"] = str(self.stage / "theme")
            options["icon_overrides"] = {
                name: str(self.stage / "theme/icons" / (name + ".png")) for name in icons
            }
            options["icon_overrides"].update({name: None for name in reset_icons})
            options["expected_identity"] = self.backend.discover()["identity"]
            self.plan = self.backend.build_plan(options)
            self.token = secrets.token_hex(24)
            return {"plan": self.plan, "token": self.token}
        if action == "apply":
            token = request["token"]
            if not isinstance(token, str) or len(token) != 48 or self.plan is None or not hmac.compare_digest(token, self.token):
                raise ValueError("There is no matching reviewed plan")
            plan = self.plan
            self.plan = self.token = None  # A plan is consumed even when apply fails.
            return self.backend.apply(plan)
        backup = request["backup_id"]
        if not isinstance(backup, str) or not 1 <= len(backup) <= 128:
            raise ValueError("Invalid backup")
        if backup not in self.backend.discover().get("backup_ids", []):
            raise ValueError("Backup is not in the available list")
        return self.backend.rollback(backup)


def request_lines(source, timeout):
    """Bound both message size and time, even when a peer never sends a newline."""
    try:
        descriptor = source.fileno()
    except (AttributeError, OSError, ValueError):
        descriptor = None
    pending = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        if descriptor is None:
            line = source.readline(MAX_MESSAGE + 1)
            if not line:
                return
        else:
            while b"\n" not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                    return
                data = os.read(descriptor, min(4096, MAX_MESSAGE + 1 - len(pending)))
                if not data:
                    if pending:
                        raise ValueError("Request is incomplete")
                    return
                pending.extend(data)
                if len(pending) > MAX_MESSAGE and b"\n" not in pending:
                    raise ValueError("Request is too large")
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            line += b"\n"
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            raise ValueError("Request is too large or incomplete")
        yield line
        deadline = time.monotonic() + timeout


def serve_helper(session, source=None, output=None, timeout=IDLE_SECONDS):
    source = source if source is not None else sys.stdin.buffer
    output = output if output is not None else sys.stdout
    lines = iter(request_lines(source, timeout))
    while True:
        try:
            line = next(lines)
        except StopIteration:
            return
        except (OSError, ValueError) as error:
            output.write(json.dumps({"ok": False, "error": str(error)}) + "\n")
            output.flush()
            return
        close = False
        try:
            request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
            # Backend diagnostics must never corrupt the protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                result = session.handle(request)
            response = {"ok": True, "result": result}
            close = request.get("action") == "close"
        except Exception as error:
            response = {"ok": False, "error": str(error)[:4000]}
        output.write(json.dumps(response, ensure_ascii=False) + "\n")
        output.flush()
        if close:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ice-circuit", description="Steam Deck boot menu setup")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("gui", help="Open the Desktop Mode setup")
    commands.add_parser("diagnose", help="Print a read-only diagnosis as JSON")
    plan = commands.add_parser("plan", help="Build a read-only plan from an options JSON file")
    plan.add_argument("--options", type=Path, required=True)
    apply = commands.add_parser("apply", help="Apply a previously reviewed plan JSON")
    apply.add_argument("--plan", type=Path, required=True)
    rollback = commands.add_parser("rollback", help="Restore a listed backup")
    rollback.add_argument("backup_id")
    helper = commands.add_parser("helper", help="Internal GUI helper (not a standalone command)")
    helper.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from .gui import main as gui_main
            return gui_main()
        if args.command == "helper":
            if os.geteuid() != 0 or not os.environ.get("PKEXEC_UID", "").isdigit():
                raise ValueError("The GUI helper must be started through pkexec")
            stage = validate_stage(args.stage, int(os.environ["PKEXEC_UID"]))
            serve_helper(HelperSession(engine(), stage))
            return 0
        backend = engine()
        if args.command == "diagnose":
            result = backend.discover()
        elif args.command == "plan":
            result = backend.build_plan(json.loads(args.options.read_text()))
        elif args.command == "apply":
            result = backend.apply(json.loads(args.plan.read_text()))
        else:
            result = backend.rollback(args.backup_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
