"""Open private, self-contained HTML using an installed desktop browser."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading

FLATPAK_BROWSERS = (
    ("com.google.Chrome", "Google Chrome"),
    ("org.mozilla.firefox", "Firefox"),
    ("org.chromium.Chromium", "Chromium"),
)
NATIVE_BROWSERS = (("google-chrome", "Google Chrome"), ("chromium", "Chromium"), ("firefox", "Firefox"))
LAUNCH_SECONDS = 2


def query(command):
    if not shutil.which(command[0]):
        return ""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def application_dirs():
    roots = [Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))]
    roots += [Path(x) for x in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if x]
    roots += [Path.home() / ".local/share/flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    return [root / "applications" for root in dict.fromkeys(roots)]


def desktop_entry(identifier):
    if not identifier.endswith(".desktop") or Path(identifier).name != identifier:
        return None, {}
    for directory in application_dirs():
        path = directory / identifier
        try:
            if path.stat().st_size > 65536:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            parser.read_string(path.read_text())
            return path, dict(parser["Desktop Entry"])
        except (OSError, UnicodeError, configparser.Error, KeyError):
            continue
    return None, {}


def installed_entry(entry):
    """SteamOS ships an Install Firefox shortcut even without Firefox itself."""
    command = entry.get("Exec", "")
    if entry.get("Hidden", "false").lower() == "true" or entry.get("Type", "") != "Application":
        return False
    if any(word in command.lower() for word in ("appstream:", "plasma-discover", "gnome-software")):
        return False
    try:
        words = shlex.split(command)
        if not words:
            return False
        required = entry.get("TryExec", "") or words[0]
        return bool(required and shutil.which(required))
    except ValueError:
        return False


def launch_candidates(path):
    uri = Path(path).resolve().as_uri()
    default = query(["xdg-settings", "get", "default-web-browser"])
    installed = set(query(["flatpak", "list", "--app", "--columns=application"]).splitlines())
    desktop, entry = desktop_entry(default)
    known_ids = dict(FLATPAK_BROWSERS)
    default_app = entry.get("X-Flatpak", "")
    entry_installed = installed_entry(entry)
    entry_is_flatpak = entry_installed and Path(shlex.split(entry["Exec"])[0]).name == "flatpak"
    if not default_app and default.removesuffix(".desktop") in known_ids and (not entry_installed or entry_is_flatpak):
        default_app = default.removesuffix(".desktop")
    used = set()

    def flatpak_command(app):
        exported, details = desktop_entry(app + ".desktop")
        # Keep the exported launcher's browser options when it forwards files.
        if exported and installed_entry(details) and "--file-forwarding" in shlex.split(details.get("Exec", "")) and shutil.which("gio"):
            return ["gio", "launch", str(exported), uri]
        # @@u asks the document portal to grant access to this file only.
        return ["flatpak", "run", "--file-forwarding", app, "@@u", uri, "@@"]

    if default_app:
        if default_app in installed:
            used.add(default_app)
            yield flatpak_command(default_app), entry.get("Name", known_ids.get(default_app, "ブラウザー"))
    elif desktop and entry_installed:
        if shutil.which("gio"):
            yield ["gio", "launch", str(desktop), uri], entry.get("Name", "ブラウザー")
        elif shutil.which("xdg-open"):
            yield ["xdg-open", uri], entry.get("Name", "ブラウザー")
    for app, label in FLATPAK_BROWSERS:
        if app in installed and app not in used:
            yield flatpak_command(app), label
    for executable, label in NATIVE_BROWSERS:
        found = shutil.which(executable)
        if found:
            yield [found, uri], label


def open_html(path):
    """A running browser is left alone; immediate failures try another choice."""
    errors = []
    for command, label in launch_candidates(path):
        with tempfile.TemporaryFile(mode="w+b") as error_log:
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=error_log)
                try:
                    status = process.wait(timeout=LAUNCH_SECONDS)
                except subprocess.TimeoutExpired:
                    # Do not kill a browser just because it keeps running.
                    threading.Thread(target=process.wait, daemon=True).start()
                    return label
                if status == 0:
                    return label
                error_log.seek(0)
                detail = error_log.read(2048).decode("utf-8", errors="replace").strip()
                errors.append(label + (": " + detail if detail else f" (終了コード {status})"))
            except OSError as error:
                errors.append(label + ": " + str(error))
    if errors:
        raise RuntimeError("ブラウザーを起動できませんでした。\n\n" + "\n".join(errors)[:4000])
    raise RuntimeError("利用できるブラウザーが見つかりません。Chrome または Firefox などをインストールしてから、もう一度プレビューを開いてください。既定のブラウザー設定は変更していません。")
