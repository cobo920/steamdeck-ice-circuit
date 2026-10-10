# SPDX-License-Identifier: GPL-3.0-or-later
"""Validate the shipped resource tree without imaging or GRUB dependencies."""
import hashlib
import json
from pathlib import Path
import re
import struct
import unittest
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
THEME = ASSETS / "theme"
SVG = "{http://www.w3.org/2000/svg}"


def png_chunks(path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"Invalid PNG signature: {path.name}")
    offset, chunks = 8, []
    while offset < len(data):
        if offset + 12 > len(data):
            raise AssertionError(f"Truncated PNG chunk: {path.name}")
        length = struct.unpack_from(">I", data, offset)[0]
        tag = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        end = offset + 12 + length
        if end > len(data):
            raise AssertionError(f"Truncated PNG data: {path.name}")
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(tag + payload) & 0xffffffff != crc:
            raise AssertionError(f"Invalid PNG CRC: {path.name}")
        chunks.append((tag, payload))
        offset = end
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1] != (b"IEND", b""):
        raise AssertionError(f"Incomplete PNG: {path.name}")
    return chunks


def pf2_name(path):
    data, offset = path.read_bytes(), 0
    if data[:12] != b"FILE\x00\x00\x00\x04PFF2":
        raise AssertionError(f"Invalid PF2 signature: {path.name}")
    while offset + 8 <= len(data):
        tag = data[offset:offset + 4]
        length = struct.unpack_from(">I", data, offset + 4)[0]
        if tag == b"NAME":
            return data[offset + 8:offset + 8 + length].rstrip(b"\0").decode()
        offset += 8 + length
    raise AssertionError(f"PF2 name missing: {path.name}")


class ShippedAssetTests(unittest.TestCase):
    def test_manifest_covers_every_installed_file_and_matches_bytes(self):
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        files = {p.relative_to(ASSETS).as_posix(): p for p in THEME.rglob("*") if p.is_file()}
        self.assertEqual(set(manifest["files"]), set(files))
        for relative, path in files.items():
            with self.subTest(resource=relative):
                self.assertFalse(path.is_symlink())
                self.assertEqual(manifest["files"][relative], {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                })
        self.assertEqual(manifest["resolution"], [1280, 800])
        self.assertEqual(manifest["icon_size"], [72, 72])
        for key, path in (("background_source_sha256", ASSETS / "source/background.svg"),
                          ("build_script_sha256", ROOT / "tools/build_theme.py"),
                          ("font_license_sha256", ROOT / "licenses/DejaVu-Fonts-LICENSE.txt")):
            self.assertEqual(manifest["build_inputs"][key], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_png_integrity_dimensions_and_no_text_metadata(self):
        paths = [*THEME.rglob("*.png"), ASSETS / "preview.png"]
        for path in paths:
            with self.subTest(resource=path.name):
                chunks = png_chunks(path)
                width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
                self.assertGreater(width, 0)
                self.assertGreater(height, 0)
                self.assertEqual((depth, compression, filtering, interlace), (8, 0, 0, 0))
                self.assertIn(color, (2, 6))
                self.assertFalse({b"tEXt", b"iTXt", b"zTXt", b"eXIf"} & {tag for tag, _ in chunks})
                if path.parent.name == "icons":
                    self.assertEqual((width, height), (72, 72))
                    self.assertEqual(color, 6)  # Transparent user-replaceable canvas.
                elif path.name in ("background.png", "background-tools.png", "boot-background.png", "preview.png"):
                    self.assertEqual((width, height), (1280, 800))

    def test_theme_resource_paths_and_font_names_resolve(self):
        fonts = {pf2_name(p) for p in THEME.glob("*.pf2")}
        for path in (THEME / "theme.txt", THEME / "theme-tools.txt"):
            theme = path.read_text()
            for resource in re.findall(r'[=:]\s*"([^"\n]+\.png)"', theme):
                self.assertNotIn("/", resource)
                self.assertNotIn("..", resource)
                if "*" in resource:
                    matches = list(THEME.glob(resource))
                    self.assertEqual(len(matches), 9, resource)
                    self.assertEqual({p.stem.rsplit("_", 1)[1] for p in matches}, {"nw", "n", "ne", "w", "c", "e", "sw", "s", "se"})
                else:
                    self.assertTrue((THEME / resource).is_file(), resource)
            referenced = re.findall(r'(?:terminal-font|item_font|selected_item_font|font)\s*[=:]\s*"([^"]+)"', theme)
            self.assertTrue(set(referenced).issubset(fonts), referenced)
            for setting, expected in (("left", "0"), ("top", "0"), ("width", "100%"), ("height", "100%"), ("border", "0")):
                self.assertIn(f'terminal-{setting}: "{expected}"', theme)

    def test_boot_background_has_no_text_and_menu_background_has_no_rows(self):
        boot = ET.parse(ASSETS / "vectors/boot-background.svg").getroot()
        self.assertFalse(list(boot.iter(SVG + "text")))
        for filename in ("background.svg", "background-tools.svg"):
            drawing = ET.parse(ASSETS / "vectors" / filename).getroot()
            text = " ".join(node.text or "" for node in drawing.iter(SVG + "text"))
            self.assertNotIn("SteamOS", text)
            self.assertNotIn("Windows", text)
            self.assertNotIn("AUTOMATIC BOOT", text)

    def test_svg_sources_are_local_vectors_with_generic_default_icons(self):
        for path in [*ASSETS.joinpath("source").glob("*.svg"), *ASSETS.joinpath("vectors").glob("*.svg")]:
            with self.subTest(source=path.name):
                drawing = ET.parse(path).getroot()
                self.assertFalse(list(drawing.iter(SVG + "image")))
                for node in drawing.iter():
                    for name, value in node.attrib.items():
                        if name.endswith("href"):
                            self.assertTrue(value.startswith("#"), (name, value))
        for name, description in (("steamos", "Generic gamepad"), ("windows", "Generic computer monitor")):
            drawing = ET.parse(ASSETS / "vectors" / f"{name}.svg").getroot()
            self.assertEqual(drawing.find(SVG + "title").text, description)

    def test_converted_fonts_retain_complete_upstream_notice(self):
        original = (ROOT / "licenses/DejaVu-Fonts-LICENSE.txt").read_bytes()
        self.assertEqual((THEME / "FONT-LICENSE.txt").read_bytes(), original)
        for required in (b"Bitstream", b"Vera", b"Arev", b"Tavmjong Bah", b"Permission is hereby granted"):
            self.assertIn(required, original)
        self.assertNotIn(b"Debian packaging", original)


if __name__ == "__main__":
    unittest.main()
