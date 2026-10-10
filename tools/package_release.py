#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build reproducible, source-inclusive ZIP and tar.gz releases in dist/.

Only explicit source/resource directories and named root files are included.
There is no device access, network access, or dependency installation. The
privacy audit catches common accidental disclosures; it is not a substitute
for reviewing the source and visible artwork before publishing.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = "0.1.0-alpha.6"
ROOT_FILES = {"README.md", "START-HERE.html", "LICENSE", "NOTICE", ".gitignore"}
LAUNCHERS = {"ice-circuit.py", "Install Ice Circuit.desktop"}
WORKFLOW_FILES = {".github/workflows/test.yml", ".github/workflows/release.yml"}
FRONTEND_FILES = {"src/ice_circuit/cli.py", "src/ice_circuit/gui.py", "src/ice_circuit/image_utils.py", "tests/test_frontend.py"}
DIRECTORIES = {
    "src": {".py"},
    "assets": {".png", ".pf2", ".svg", ".txt", ".md", ".json"},
    "licenses": {".txt", ".md"},
    "docs": {".md", ".txt", ".svg", ".png"},
    "tests": {".py"},
    "tools": {".py", ".sh"},
}
EXCLUDED_DIRECTORIES = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".venv", "venv", "dist", "build", "node_modules", "backup", "backups", "snapshots", "secrets", ".ssh", ".aws"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".img", ".qcow", ".qcow2", ".vmdk", ".vhd", ".vhdx", ".iso", ".efi", ".mod", ".fd", ".pem", ".key", ".p12", ".pfx", ".der", ".tar", ".gz", ".zip"}
EXECUTABLES = {"tools/build_theme.py", "tools/package_release.py"}
# Deliberately synthetic values used in the isolated transaction fixtures.
FAT_EXAMPLES = {"0000-0000", "1111-AAAA", "2222-BBBB", "3333-CCCC", "ABCD-1234"}
HOME_EXAMPLES = {"deck", "user", "username", "your-user", "example", "testuser"}


class PackageError(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def excluded_name(path):
    lower = path.name.lower()
    return (lower in EXCLUDED_DIRECTORIES or path.suffix.lower() in EXCLUDED_SUFFIXES
            or (lower.startswith(".") and lower != ".gitignore")
            or path.stem.lower() in {"secrets", "credentials", "token", "tokens", "private-key"}
            or lower in {"id_rsa", "id_ed25519", "known_hosts", "authorized_keys"}
            or lower.endswith(("~", ".swp", ".bak")))


def collect_sources(root):
    """Read a bounded allowlist once; do not traverse symlinks or special files."""
    root = Path(root)
    result = {}

    def add(path):
        if excluded_name(path) or any(item.is_symlink() for item in (path, *path.parents)):
            return
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return
        relative = path.relative_to(root).as_posix()
        if not re.fullmatch(r"[A-Za-z0-9_. -]+(?:/[A-Za-z0-9_. -]+)*", relative):
            raise PackageError("Unsupported source filename")
        if info.st_size > 16 * 1024 * 1024:
            raise PackageError(f"Source file exceeds size limit: {relative}")
        result[relative] = path.read_bytes()

    for name in sorted(ROOT_FILES | LAUNCHERS | WORKFLOW_FILES):
        path = root / name
        if path.exists() or path.is_symlink():
            add(path)
    for name, suffixes in DIRECTORIES.items():
        base = root / name
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, folders, filenames in os.walk(base, followlinks=False):
            folders[:] = sorted(folder for folder in folders
                                if not excluded_name(Path(directory) / folder)
                                and not (Path(directory) / folder).is_symlink())
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.suffix.lower() in suffixes:
                    add(path)
    return dict(sorted(result.items()))


def audit_text(relative, text):
    """Reject concrete identifiers, not references to UUID/SSH APIs or paths."""
    checks = (
        (r"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----", "private key"),
        (r"\bssh-(?:rsa|ed25519)\s+[A-Za-z0-9+/]{32,}={0,3}", "SSH public key"),
        (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "access key"),
        (r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}\b", "access token"),
        (r"\bsteamdeck-recovery-[0-9]{8}\b", "private recovery workspace"),
    )
    for expression, label in checks:
        if re.search(expression, text):
            raise PackageError(f"Privacy audit: possible {label} in {relative}")
    for match in re.finditer(r"/home/([A-Za-z_][A-Za-z0-9_.-]*)(?=[/\s\"':),]|$)", text):
        if match[1] not in HOME_EXAMPLES:
            raise PackageError(f"Privacy audit: personal home path in {relative}")
    for match in re.finditer(r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}(?![A-Fa-f0-9])", text):
        # Repeated characters are recognizable synthetic examples.
        if len(set(match[0].replace("-", "").lower())) != 1:
            raise PackageError(f"Privacy audit: concrete UUID in {relative}")
    for match in re.finditer(r"(?<![A-Za-z0-9-])[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}(?![A-Za-z0-9-])", text):
        if match[0].upper() not in FAT_EXAMPLES and not all(len(set(part.upper())) == 1 for part in match[0].split("-")):
            raise PackageError(f"Privacy audit: concrete FAT UUID in {relative}")
    private_networks = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    for match in re.finditer(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", text):
        try:
            address = ipaddress.ip_address(match[0])
        except ValueError:
            continue
        # Network specifications in this audit are definitions, not hosts.
        if any(address in network and address != network.network_address for network in private_networks):
            raise PackageError(f"Privacy audit: private IP address in {relative}")


def validate_sources(files):
    required = ROOT_FILES | LAUNCHERS | WORKFLOW_FILES | FRONTEND_FILES | {"tools/build_theme.py", "tools/package_release.py", "src/ice_circuit/engine.py", "src/ice_circuit/grub.py", "assets/manifest.json", "assets/source/background.svg", "assets/preview.png", "licenses/ARTWORK-NOTICE.txt", "licenses/FONT-PROVENANCE.txt", "licenses/DejaVu-Fonts-LICENSE.txt", "docs/RELEASE-NOTES.md"}
    if required - files.keys():
        raise PackageError("Required release files missing: " + ", ".join(sorted(required - files.keys())))
    if b"GNU GENERAL PUBLIC LICENSE" not in files["LICENSE"] or b"Version 3, 29 June 2007" not in files["LICENSE"]:
        raise PackageError("Project GPLv3 license is missing")
    font_notice = files["licenses/DejaVu-Fonts-LICENSE.txt"]
    for required_notice in (b"Bitstream", b"Vera", b"Arev", b"Tavmjong Bah", b"Permission is hereby granted"):
        if required_notice not in font_notice:
            raise PackageError("Upstream font license notice is incomplete")
    if files.get("assets/theme/FONT-LICENSE.txt") != font_notice:
        raise PackageError("Installed font notice differs from the checked-in license")
    manifest = json.loads(files["assets/manifest.json"])
    if manifest.get("schema_version") != 1 or manifest.get("theme_directory") != "theme":
        raise PackageError("Unsupported asset manifest")
    actual_assets = {name.removeprefix("assets/") for name in files if name.startswith("assets/theme/")}
    if set(manifest["files"]) != actual_assets:
        raise PackageError("Asset manifest does not cover exactly the installed theme files")
    for name, record in manifest["files"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or not name.startswith("theme/"):
            raise PackageError("Unsafe asset manifest path")
        content = files["assets/" + name]
        if record != {"sha256": sha256(content), "size": len(content)}:
            raise PackageError(f"Asset SHA256/size mismatch: {name}")
    for key, path in (("build_script_sha256", "tools/build_theme.py"),
                      ("background_source_sha256", "assets/source/background.svg"),
                      ("font_license_sha256", "licenses/DejaVu-Fonts-LICENSE.txt")):
        if manifest["build_inputs"].get(key) != sha256(files[path]):
            raise PackageError(f"Asset build input changed without rebuilding: {path}")
    for relative, data in files.items():
        suffix = PurePosixPath(relative).suffix.lower()
        if suffix == ".png":
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PackageError(f"Invalid PNG resource: {relative}")
            # Binary artwork pixels are not claimed to be text-auditable.
            continue
        if suffix == ".pf2":
            if not data.startswith(b"FILE\x00\x00\x00\x04PFF2"):
                raise PackageError(f"Invalid font resource: {relative}")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PackageError(f"Unexpected binary source file: {relative}") from error
        if "\0" in text:
            raise PackageError(f"Unexpected binary source file: {relative}")
        audit_text(relative, text)


def file_mode(relative):
    return 0o755 if relative in EXECUTABLES | LAUNCHERS else 0o644


def archive_files(files, manifest):
    manifest_data = json_bytes(manifest)
    archived = dict(files, **{"RELEASE-MANIFEST.json": manifest_data})
    archived["SHA256SUMS"] = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(archived.items())).encode()
    return archived, manifest_data


def create_archive(files, release, manifest):
    archived, manifest_data = archive_files(files, manifest)
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, data in sorted(archived.items()):
                entry = tarfile.TarInfo(f"{release}/{name}")
                entry.size = len(data)
                entry.mode = file_mode(name)
                entry.mtime = 0
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(data))
    return output.getvalue(), manifest_data


def create_zip(files, release, manifest):
    """Create a relocatable ZIP with fixed dates and Unix executable modes."""
    archived, _ = archive_files(files, manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(archived.items()):
            # ZIP's earliest representable date is 1980-01-01, unlike tar.
            entry = zipfile.ZipInfo(f"{release}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3  # Unix: Ark/unzip can restore launcher mode.
            entry.external_attr = (stat.S_IFREG | file_mode(name)) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=9)
    return output.getvalue()


def atomic_write(path, data):
    if path.is_symlink():
        raise PackageError("Refusing a symlink output")
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def package(root, version):
    if len(version) > 64 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?", version):
        raise PackageError("Version must be a simple semantic version, for example 0.1.0-alpha.6")
    files = collect_sources(root)
    validate_sources(files)
    release = "ice-circuit-" + version
    archive_name, zip_name = release + ".tar.gz", "Ice-Circuit-SteamDeck-" + version + ".zip"
    manifest = {"schema_version": 2, "name": "ice-circuit", "version": version,
                "project_license": "GPL-3.0-or-later", "font_license": "licenses/DejaVu-Fonts-LICENSE.txt",
                "source_included": True, "archive_root": release, "source_date_epoch": 0,
                "distribution_files": {"recommended": zip_name, "source_tar_gz": archive_name},
                "audit": {"allowlist": True, "asset_sha256_verified": True,
                          "text_privacy_patterns_checked": True, "binary_artwork_requires_visual_review": True},
                "files": {name: {"sha256": sha256(data), "size": len(data), "mode": format(file_mode(name), "04o")}
                          for name, data in sorted(files.items())}}
    archive_data, _ = create_archive(files, release, manifest)
    zip_data = create_zip(files, release, manifest)
    archives = {archive_name: {"format": "tar.gz", "sha256": sha256(archive_data), "size": len(archive_data)},
                zip_name: {"format": "zip", "sha256": sha256(zip_data), "size": len(zip_data)}}
    # Archive hashes belong outside the archives to avoid a circular checksum.
    manifest_data = json_bytes(dict(manifest, archives=archives))
    destination = Path(root) / "dist"
    if destination.is_symlink():
        raise PackageError("Refusing a symlink dist directory")
    destination.mkdir(exist_ok=True)
    manifest_name = release + ".manifest.json"
    atomic_write(destination / archive_name, archive_data)
    atomic_write(destination / zip_name, zip_data)
    atomic_write(destination / manifest_name, manifest_data)
    sums = ("".join(f"{info['sha256']}  {name}\n" for name, info in sorted(archives.items()))
            + f"{sha256(manifest_data)}  {manifest_name}\n").encode()
    atomic_write(destination / "SHA256SUMS", sums)
    return {"recommended_download": zip_name, "archives": archives, "manifest": manifest_name, "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args()
    try:
        result = package(ROOT, args.version)
    except (PackageError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Release not created: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
