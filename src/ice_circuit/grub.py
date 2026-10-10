#!/usr/bin/env python3
"""Small, self-contained GRUB generator installed in /etc/ice-circuit.

The native generators remain installed and run normally. Only a validated copy
of their primary Linux entry is promoted; the original output stays in Tools.
A per-grub-mkconfig record pairs the opening and closing fragments. Unsupported
native output produces a flat native menu, never half of a submenu.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/ice-circuit/config.json')
GENERATOR = Path('/etc/grub.d/10_linux')
BEGIN = Path('/etc/grub.d/09_ice_circuit_menu')
END = Path('/etc/grub.d/zz_ice_circuit_tools')


def quote(value):
    """A GRUB double-quoted word, without executable expansion or line breaks."""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid GRUB string')
    return '"' + ''.join('\\' + c if c in '\\"$`' else c for c in value) + '"'


def settings(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Unknown configuration version')
    for name in ('esp_uuid', 'windows_uuid'):
        if not re.fullmatch(r'[A-Fa-f0-9-]{4,64}', value.get(name, '')):
            raise ValueError('Invalid filesystem UUID')
    label = value.get('windows_label', '')
    if not isinstance(label, str) or not 1 <= len(label) <= 64:
        raise ValueError('Invalid Windows label')
    quote(label)
    if type(value.get('timeout')) is not int or not 1 <= value['timeout'] <= 60:
        raise ValueError('Timeout must be between 1 and 60 seconds')
    if value.get('default_os') not in ('steamos', 'windows'):
        raise ValueError('Invalid default OS')
    return value


def primary_entry(native):
    """Recognize the supported SteamOS generator, without guessing at braces."""
    lines = native.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith('menuentry ')), None)
    if start is None:
        raise ValueError('Native generator has no primary entry')
    header = lines[start]
    pattern = r"^menuentry 'SteamOS' .* --class steamos .*\$menuentry_id_option 'gnulinux-simple-[A-Za-z0-9_-]+' \{\n?$"
    if not re.fullmatch(pattern, header):
        # --class steamos may immediately follow the title.
        pattern = r"^menuentry 'SteamOS' --class steamos .*\$menuentry_id_option 'gnulinux-simple-[A-Za-z0-9_-]+' \{\n?$"
        if not re.fullmatch(pattern, header):
            raise ValueError('Unsupported native primary entry header')
    if any(re.match(r'\s*(?:menuentry|submenu)\s', line) for line in lines[:start]):
        raise ValueError('Unexpected native preamble')
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip('\r\n') == '}'), None)
    if end is None or any(line.strip() and not line[0].isspace() for line in lines[start + 1:end]):
        raise ValueError('Unsupported native primary entry body')
    body = ''.join(lines[start + 1:end])
    if len(re.findall(r'^\s*steamenv_boot\s+linux\s', body, re.M)) != 1 or len(re.findall(r'^\s*initrd\s', body, re.M)) != 1:
        raise ValueError('Native kernel/initrd commands were not recognized')
    lines[start] = re.sub(r"'gnulinux-simple-[A-Za-z0-9_-]+'", "'ice-circuit-steamos'", header, count=1)
    return ''.join(lines[:end + 1])


def windows_entry(config):
    return f'''menuentry {quote(config['windows_label'])} --class windows --class os --id ice-circuit-windows {{
    insmod part_gpt
    insmod fat
    insmod chain
    search --no-floppy --fs-uuid --set=root {config['windows_uuid']}
    chainloader /EFI/Microsoft/Boot/bootmgfw.efi
}}
'''


def main_fragment(config, native=None, bli=''):
    config = settings(config)
    if native is None:
        return ''
    promoted = primary_entry(native)
    default = 'ice-circuit-windows' if config['default_os'] == 'windows' else 'ice-circuit-steamos'
    result = bli + 'if [ "${boot_once}" != true ]; then\n' + promoted + '\n' + windows_entry(config)
    result += f'''# Preserve one-shot selections and steamenv's indefinite recovery timeout.
set default={default}
set timeout_style=menu
if [ "${{timeout}}" = 0 ]; then
    set timeout={config['timeout']}
fi
fi
function ice_circuit_native_menu {{
'''
    return result


def end_fragment():
    # One-shot entries keep the complete native layout, including numeric
    # indices, titles, IDs and nested paths. No lossy default remapping is needed.
    return '''}
if [ "${boot_once}" = true ]; then
    ice_circuit_native_menu
else
submenu 'Tools & Recovery' --class tools --id ice-circuit-tools {
    if [ "${ice_circuit_theme_ready}" = true ]; then
        if [ -f "$ice_circuit_theme_dir/theme-tools.txt" ]; then
            set theme=$ice_circuit_theme_dir/theme-tools.txt
            export theme
        fi
    fi
    ice_circuit_native_menu
}
fi
'''


def theme_fragment(config):
    settings(config)
    return f'''# Locate shared-ESP artwork without changing the kernel's root variable.
if [ "${{boot_once}}" != true ]; then
insmod part_gpt
insmod fat
if search --no-floppy --fs-uuid --set=ice_circuit_theme_device {config['esp_uuid']}; then
    set ice_circuit_theme_dir=($ice_circuit_theme_device)/EFI/ice-circuit/theme
    if [ -f "$ice_circuit_theme_dir/theme.txt" ]; then
        if loadfont "$ice_circuit_theme_dir/sans-32.pf2" "$ice_circuit_theme_dir/sans-24.pf2" "$ice_circuit_theme_dir/mono-18.pf2"; then
            insmod png
            insmod gfxmenu
            if [ -f "$ice_circuit_theme_dir/boot-background.png" ]; then
                if insmod gfxterm_background; then
                    background_image "$ice_circuit_theme_dir/boot-background.png"
                fi
            fi
            set theme=$ice_circuit_theme_dir/theme.txt
            set ice_circuit_theme_ready=true
            export theme
            export ice_circuit_theme_dir
            export ice_circuit_theme_ready
        fi
    fi
fi
fi
'''


def context_path():
    # All grub.d scripts are direct children of the same grub-mkconfig shell.
    # Include its start time so an abandoned record cannot match a reused PID.
    parent = os.getppid()
    stat = Path(f'/proc/{parent}/stat').read_text()
    start = stat[stat.rfind(')') + 2:].split()[19]
    directory = Path('/run/ice-circuit')
    if directory.is_symlink():
        raise ValueError('Unsafe generation state directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Unsafe generation state permissions')
    return directory / f'grub-{parent}-{start}.json'


def run(mode):
    if os.environ.get('ICE_CIRCUIT_DISABLE') == '1':
        return
    # Closing a previously opened group does not depend on current config or
    # reparsing the vendor output; an update cannot change the decision midway.
    if mode == 'end':
        record = context_path()
        if record.exists() and not record.is_symlink():
            data = json.loads(record.read_text())
            record.unlink()
            if data == {'open': True}:
                print(end_fragment(), end='')
        return
    config = settings(json.loads(CONFIG.read_text()))
    if mode == 'theme':
        print(theme_fragment(config), end='')
        return
    if mode != 'begin':
        raise ValueError('Unknown generator mode')
    record = context_path()
    record.unlink(missing_ok=True)
    native = None
    bli = ''
    try:
        if not END.is_file() or not os.access(END, os.X_OK):
            raise ValueError('Closing generator missing')
        if any(p.is_file() and os.access(p, os.X_OK) and p.name > END.name for p in END.parent.iterdir()):
            raise ValueError('Unknown generator after Tools')
        candidate = subprocess.run([str(GENERATOR)], text=True, capture_output=True, check=True, timeout=60).stdout
        primary_entry(candidate)
        # The BLI module previously loaded at the root menu level.
        bli_path = GENERATOR.with_name('25_bli')
        if bli_path.is_file() and os.access(bli_path, os.X_OK):
            bli = subprocess.run([str(bli_path)], text=True, capture_output=True, check=True, timeout=10).stdout
        native = candidate
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Ice Circuit: native layout unsupported; retaining the flat native menu: {error}', file=sys.stderr)
    fragment = main_fragment(config, native, bli)
    if native is not None:
        descriptor, temporary = tempfile.mkstemp(prefix='.grub-', dir=record.parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                json.dump({'open': True}, output)
            os.replace(temporary, record)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Do not emit the opening until all generation and state recording succeeds.
    print(fragment, end='')


if __name__ == '__main__':
    try:
        run(sys.argv[1])
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        print(f'Ice Circuit: optional menu customization disabled: {error}', file=sys.stderr)
    # Styling must not prevent the OS updater from generating its native menu.
