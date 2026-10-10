#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rebuild the public Ice Circuit theme from repository-owned SVG artwork.

Build dependencies: Python 3, librsvg, Cairo, DejaVu Sans/Mono, grub-mkfont.
These dependencies are not needed to install or configure the built theme.
No network access, device access, or boot configuration changes are performed.
"""
from __future__ import annotations

import argparse
import copy
import ctypes as C
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)


class Renderer:
    """Render vector assets without a Python imaging dependency."""

    def __init__(self):
        self.rsvg = C.CDLL("librsvg-2.so.2")
        self.cairo = C.CDLL("libcairo.so.2")
        self.gobject = C.CDLL("libgobject-2.0.so.0")
        self.rsvg.rsvg_handle_new_from_file.argtypes = [C.c_char_p, C.POINTER(C.c_void_p)]
        self.rsvg.rsvg_handle_new_from_file.restype = C.c_void_p
        self.rsvg.rsvg_handle_render_cairo.argtypes = [C.c_void_p, C.c_void_p]
        self.rsvg.rsvg_handle_render_cairo.restype = C.c_int
        self.cairo.cairo_image_surface_create.argtypes = [C.c_int, C.c_int, C.c_int]
        self.cairo.cairo_image_surface_create.restype = C.c_void_p
        self.cairo.cairo_create.argtypes = [C.c_void_p]
        self.cairo.cairo_create.restype = C.c_void_p
        self.cairo.cairo_surface_write_to_png.argtypes = [C.c_void_p, C.c_char_p]
        self.cairo.cairo_surface_write_to_png.restype = C.c_int
        self.cairo.cairo_destroy.argtypes = [C.c_void_p]
        self.cairo.cairo_surface_destroy.argtypes = [C.c_void_p]
        self.gobject.g_object_unref.argtypes = [C.c_void_p]

    def render(self, source: Path, target: Path, width: int, height: int):
        error = C.c_void_p()
        handle = self.rsvg.rsvg_handle_new_from_file(str(source).encode(), C.byref(error))
        if not handle:
            raise RuntimeError(f"Unable to open SVG: {source.name}")
        surface = self.cairo.cairo_image_surface_create(0, width, height)
        context = self.cairo.cairo_create(surface)
        try:
            if not self.rsvg.rsvg_handle_render_cairo(handle, context):
                raise RuntimeError(f"Unable to render SVG: {source.name}")
            if self.cairo.cairo_surface_write_to_png(surface, str(target).encode()):
                raise RuntimeError(f"Unable to write PNG: {target.name}")
        finally:
            self.cairo.cairo_destroy(context)
            self.cairo.cairo_surface_destroy(surface)
            self.gobject.g_object_unref(handle)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def element(fragment: str) -> ET.Element:
    return ET.fromstring(f'<g xmlns="{NS}">{fragment}</g>')


def group(art: ET.Element, identifier: str) -> ET.Element:
    result = art.find(f"{{{NS}}}g[@id='{identifier}']")
    if result is None:
        raise ValueError(f"Missing artwork group: {identifier}")
    return result


PANEL_PATH = "M13 .5H781.5V98.5L768.5 111.5H.5V13.5Z"
PANELS = {
    "select": f'''<defs><linearGradient id="panelFill"><stop stop-color="#173248"/>
      <stop offset=".8" stop-color="#142331"/><stop offset="1" stop-color="#101a25"/>
      </linearGradient></defs>
      <path d="{PANEL_PATH}" fill="url(#panelFill)" stroke="#527a95"/>
      <path d="M1.5 21V91" stroke="#b4e5ff" stroke-width="3"/>
      <path d="M674 1.5H768L780.5 14" fill="none" stroke="#86aec7" stroke-width="2"/>
      <path d="M751 48L759 56L751 64" fill="none" stroke="#c5eaff" stroke-width="2"/>''',
    "item": f'''<path d="{PANEL_PATH}" fill="#0d151e" fill-opacity=".94" stroke="#243441"/>
      <path d="M1 28V84" stroke="#4c697d" stroke-width="2" opacity=".7"/>''',
}
SLICES = {
    "nw": (0, 0, 36, 14), "n": (36, 0, 710, 14), "ne": (746, 0, 36, 14),
    "w": (0, 14, 36, 84), "c": (36, 14, 710, 84), "e": (746, 14, 36, 84),
    "sw": (0, 98, 36, 14), "s": (36, 98, 710, 14), "se": (746, 98, 36, 14),
}

# These are original generic device symbols, not redraws of OS/company marks.
GAMEPAD = '''<title>Generic gamepad</title><g fill="none" stroke="#def2ff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">
 <path d="M23 22H49C56 22 60 28 62 36L65 49C67 59 59 63 53 55L47 48H25L19 55C13 63 5 59 7 49L10 36C12 28 16 22 23 22Z" fill="#172c3c"/>
 <path d="M23 30V42M17 36H29" stroke-width="3.5"/>
 <circle cx="48" cy="32" r="2.5" fill="#def2ff" stroke="none"/>
 <circle cx="55" cy="39" r="2.5" fill="#def2ff" stroke="none"/>
 <path d="M33 30H39" stroke="#91b4ca" stroke-width="2"/>
 </g>'''
MONITOR = '''<title>Generic computer monitor</title><g fill="none" stroke="#a2bfd5" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round">
 <rect x="9" y="13" width="54" height="36" rx="3" fill="#101c27"/>
 <path d="M9 43H63M36 49V59M24 60H48"/>
 <path d="M23 24L29 29L23 34M35 34H46" stroke="#91b4ca" stroke-width="2.3"/>
 </g>'''
GEAR = '<g transform="translate(36 36)" fill="none" stroke="#637888" stroke-width="3"><circle r="11"/><circle r="4"/>' + ''.join(
    f'<path d="M0 -11V-17" transform="rotate({angle})"/>' for angle in range(0, 360, 45)) + '</g>'
FIRMWARE = '<g transform="translate(36 36)" fill="none" stroke="#819fb3" stroke-width="2.5"><rect x="-17" y="-17" width="34" height="34" rx="3"/><rect x="-8" y="-8" width="16" height="16"/>' + ''.join(
    f'<path d="M-10 -17V-25M0 -17V-25M10 -17V-25" transform="rotate({angle})"/>' for angle in (0, 90, 180, 270)) + '</g>'
POWER = '<g transform="translate(36 36)" fill="none" stroke="#819fb3" stroke-width="3.5" stroke-linecap="round"><path d="M-12 -13A18 18 0 1 0 12 -13M0 -23V-3"/></g>'
RESTART = '<g transform="translate(36 36)" fill="none" stroke="#819fb3" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M17 -8A19 19 0 1 0 18 8M17 -22V-8H3"/></g>'
RECOVERY = '<g transform="translate(36 36)" fill="none" stroke="#819fb3" stroke-width="3"><path d="M0 -24L22 -14V4Q19 18 0 26Q-19 18 -22 4V-14Z"/><path d="M0 -10V12M-11 1H11"/></g>'
ICONS = {
    "steamos": GAMEPAD, "windows": MONITOR, "tools": GEAR, "settings": GEAR,
    "uefi": FIRMWARE, "efi": FIRMWARE, "shutdown": POWER, "halt": POWER,
    "reboot": RESTART, "restart": RESTART, "recovery": RECOVERY,
    "linux": RECOVERY, "gnu-linux": RECOVERY,
}

THEME_TEMPLATE = '''# Ice Circuit: native GRUB theme for 1280x800.
# All resource paths are relative. Load the supplied PF2 fonts first.
title-text: ""
desktop-image: "{background}.png"
desktop-image-scale-method: "stretch"
desktop-color: "#080b10"
terminal-font: "Ice Circuit Mono Regular 18"
terminal-left: "0"
terminal-top: "0"
terminal-width: "100%"
terminal-height: "100%"
terminal-border: "0"

+ boot_menu {{
  left = {left}
  top = 238
  width = {width}
  height = {height}
  item_font = "Ice Circuit Sans Regular {font}"
  selected_item_font = "Ice Circuit Sans Regular {font}"
  item_color = "#b6c7d5"
  selected_item_color = "#f0f7ff"
  icon_width = 72
  icon_height = 72
  item_height = {item_height}
  item_padding = 0
  item_icon_space = 30
  item_spacing = {spacing}
  item_pixmap_style = "item_*.png"
  selected_item_pixmap_style = "select_*.png"
  scrollbar = true
  scrollbar_frame = "scroll_*.png"
  scrollbar_thumb = "thumb_*.png"
  scrollbar_thumb_overlay = true
  scrollbar_slice = "center"
  scrollbar_width = 6
  scrollbar_left_pad = 12
  scrollbar_right_pad = 0
  scrollbar_top_pad = 14
  scrollbar_bottom_pad = 14
}}
'''
TIMEOUT = '''
+ label {
  id = "__timeout__"
  left = 48
  top = 700
  width = 480
  height = 26
  font = "Ice Circuit Mono Regular 18"
  color = "#c0dff2"
  text = "AUTOMATIC BOOT IN %ds"
}

+ progress_bar {
  id = "__timeout__"
  left = 48
  top = 738
  width = 264
  height = 3
  bar_style = "progress_track_*.png"
  highlight_style = "progress_fill_*.png"
  highlight_overlay = true
  text = ""
}
'''


def build(output: Path, font_directory: Path, font_tool: str):
    theme, vectors = output / "theme", output / "vectors"
    for directory in (theme / "icons", vectors):
        directory.mkdir(parents=True, exist_ok=True)
    renderer = Renderer()

    def save_svg(name, content, width, height, viewbox=None, destination=theme):
        box = viewbox or f"0 0 {width} {height}"
        source = vectors / f"{name}.svg"
        source.write_text(f'<svg xmlns="{NS}" width="{width}" height="{height}" viewBox="{box}">{content}</svg>\n')
        renderer.render(source, destination / f"{name}.png", width, height)

    source = ROOT / "assets/source/background.svg"
    art = ET.parse(source).getroot()
    if list(art.iter(f"{{{NS}}}image")):
        raise ValueError("Background source must contain vector artwork only")
    for name in ("background", "background-tools", "boot-background"):
        drawing = copy.deepcopy(art)
        if name == "background-tools":
            drawing.remove(group(drawing, "circuit"))
            heading = list(group(drawing, "menu-heading"))
            heading[0].text = "TOOLS & RECOVERY"
            heading[1].text = "More ways to boot. Always within reach."
            for text in heading:
                text.set("x", "48")
            for text, y in zip(group(drawing, "slogan"), (716, 744)):
                text.attrib.update(x="48", y=str(y), **{"font-size": "17", "text-anchor": "start"})
        elif name == "boot-background":
            for parent in drawing.iter():
                for child in list(parent):
                    if child.tag == f"{{{NS}}}text":
                        parent.remove(child)
            for child in drawing.findall(f"{{{NS}}}g"):
                if child.get("id") != "background":
                    child.set("opacity", "0.28")
        path = vectors / f"{name}.svg"
        ET.ElementTree(drawing).write(path, encoding="unicode")
        renderer.render(path, theme / f"{name}.png", 1280, 800)

    for style, content in PANELS.items():
        for part, (x, y, width, height) in SLICES.items():
            save_svg(f"{style}_{part}", content, width, height, f"{x} {y} {width} {height}")
    for style, color in (("scroll", "#233544"), ("thumb", "#88acbf")):
        for part in SLICES:
            save_svg(f"{style}_{part}", f'<rect width="2" height="2" fill="{color}"/>', 2, 2)
    # GRUB enforces a minimum progress-widget height. Paint only its 3 px north
    # slice and leave the remaining area transparent to retain a thin line.
    for style, color in (("progress_track", "#293947"), ("progress_fill", "#a2c8e0")):
        for part in SLICES:
            top = part in ("nw", "n", "ne")
            content = f'<rect width="1" height="3" fill="{color}"/>' if top else ""
            save_svg(f"{style}_{part}", content, 1, 3 if top else 1)
    for name, icon in ICONS.items():
        save_svg(name, icon, 72, 72, destination=theme / "icons")

    font_inputs = {}
    for family, face, size in (("Sans", "DejaVuSans.ttf", 32), ("Sans", "DejaVuSans.ttf", 24), ("Mono", "DejaVuSansMono.ttf", 18)):
        font = font_directory / face
        font_inputs[face] = digest(font)
        subprocess.run([font_tool, "-n", f"Ice Circuit {family}", "-s", str(size),
                        "-r", "0x20-0x7e,0xa0-0xff,0x2190-0x2193", "-o",
                        str(theme / f"{family.lower()}-{size}.pf2"), str(font)], check=True)
    shutil.copyfile(ROOT / "licenses/DejaVu-Fonts-LICENSE.txt", theme / "FONT-LICENSE.txt")
    (theme / "theme.txt").write_text(THEME_TEMPLATE.format(background="background", left=450, width=782, height=360, font=32, item_height=84, spacing=38) + TIMEOUT)
    (theme / "theme-tools.txt").write_text(THEME_TEMPLATE.format(background="background-tools", left=48, width=1184, height=424, font=24, item_height=76, spacing=30))

    # This is a reproducible design preview, not a captured GRUB screenshot.
    preview = copy.deepcopy(art)
    for index, (label, icon) in enumerate((("SteamOS", "steamos"), ("Windows 11", "windows"), ("Tools & Recovery", "tools"))):
        panel = element(PANELS["select" if index == 0 else "item"])
        panel.set("transform", f"translate(450 {238 + index * 122})")
        preview.append(panel)
        symbol = element(ICONS[icon])
        symbol.set("transform", f"translate(486 {258 + index * 122})")
        preview.append(symbol)
        text = ET.SubElement(preview, f"{{{NS}}}text", {"x": "588", "y": str(306 + index * 122), "font-family": "DejaVu Sans", "font-size": "32", "fill": "#f0f7ff" if index == 0 else "#b6c7d5"})
        text.text = label
    preview.append(element('<text x="48" y="718" fill="#c0dff2" font-family="DejaVu Sans Mono" font-size="18">AUTOMATIC BOOT IN 5s</text><rect x="48" y="738" width="264" height="3" fill="#293947"/>'))
    preview_source = vectors / "preview.svg"
    ET.ElementTree(preview).write(preview_source, encoding="unicode")
    renderer.render(preview_source, output / "preview.png", 1280, 800)

    files = {path.relative_to(output).as_posix(): {"sha256": digest(path), "size": path.stat().st_size}
             for path in sorted(theme.rglob("*")) if path.is_file()}
    manifest = {"schema_version": 1, "theme_directory": "theme", "resolution": [1280, 800],
                "icon_size": [72, 72], "files": files,
                "build_inputs": {"build_script_sha256": digest(Path(__file__)),
                                 "background_source_sha256": digest(source), "font_sha256": font_inputs,
                                 "font_license_sha256": digest(ROOT / "licenses/DejaVu-Fonts-LICENSE.txt")}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Built {len(files)} theme files in {theme}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets")
    parser.add_argument("--font-dir", type=Path, default=Path("/usr/share/fonts/truetype/dejavu"))
    parser.add_argument("--font-tool", default=shutil.which("grub-mkfont") or shutil.which("grub2-mkfont"))
    args = parser.parse_args()
    if not args.font_tool:
        parser.error("Install grub-mkfont (or grub2-mkfont) to rebuild the PF2 fonts")
    build(args.output_dir.resolve(), args.font_dir.resolve(), args.font_tool)


if __name__ == "__main__":
    main()
