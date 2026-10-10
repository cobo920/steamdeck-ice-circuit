Ice Circuit artwork and theme resources

`theme/` is the installable theme tree. `source/background.svg` is the editable
background input. `vectors/` contains the generated vector assets, and
`preview.png` is an illustrative default menu preview using generic device
icons. The boot background contains no labels or instructions.

Rebuild from the repository root:

```sh
python3 tools/build_theme.py
```

Build dependencies are Python 3, librsvg 2, Cairo, GNU GRUB's `grub-mkfont`
(`grub2-mkfont` is also detected), and DejaVu Sans/Sans Mono. The default font
directory is `/usr/share/fonts/truetype/dejavu`; `--font-dir` changes it.
`--output-dir` creates an isolated build. These tools are not runtime
dependencies of the desktop configuration application.

The theme targets 1280×800. Load `sans-32.pf2`, `sans-24.pf2`, and `mono-18.pf2`
before selecting `theme.txt`. `theme-tools.txt` provides a wider menu for long
recovery entries. `boot-background.png` is loaded separately with GRUB's
`background_image` command for its text terminal. Entry titles and the selected
row are rendered by GRUB; the background contains no OS menu entries.

Icon class mappings are `steamos` (generic gamepad), `windows` (generic
computer monitor), and `tools` (small gear). Optional replacement icons use
the filenames `steamos.png` and `windows.png` and a 72×72 pixel canvas. Replacing
icons is a separate user action; the theme build never searches for or imports
official logos.

`manifest.json` records each installed resource by relative path, SHA256 and
size, together with the background and font input hashes. Font terms are in
`../licenses/DejaVu-Fonts-LICENSE.txt`; artwork provenance is in
`../licenses/ARTWORK-NOTICE.txt`.
