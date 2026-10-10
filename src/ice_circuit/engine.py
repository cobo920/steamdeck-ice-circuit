"""SteamOS-native menu installation with reviewed plans and reversible writes.

No partitioning, EFI binary replacement or NVRAM mutation is performed here.
All privileged destinations are fixed by this module, never supplied by a plan.
The injectable root/runner are for isolated tests, not a CLI installation option.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
import uuid
import zlib

from . import grub

SCHEMA = 1
GRUB = '/efi/EFI/steamos/grub.cfg'
ASSETS = '/esp/EFI/ice-circuit/theme'
STATE_DIR = '/home/.ice-circuit'
STATE = STATE_DIR + '/state.json'
BACKUPS = STATE_DIR + '/backups'
CONFIG = '/etc/ice-circuit/config.json'
HELPER = '/etc/ice-circuit/grub.py'
HOOKS = {
    '/etc/grub.d/08_ice_circuit_theme': 'theme',
    '/etc/grub.d/09_ice_circuit_menu': 'begin',
    '/etc/grub.d/zz_ice_circuit_tools': 'end',
}
DEFAULTS = '/etc/default/grub.d/99-ice-circuit.cfg'
KEEP = '/etc/atomic-update.conf.d/ice-circuit.conf'
OWNED_ETC = (CONFIG, HELPER, *HOOKS, DEFAULTS, KEEP)
REQUIRED_ASSETS = {'theme.txt', 'theme-tools.txt', 'boot-background.png', 'sans-32.pf2', 'sans-24.pf2', 'mono-18.pf2', 'icons/steamos.png', 'icons/windows.png', 'icons/tools.png'}
KNOWN_GENERATORS = {'00_header', '10_linux', '15_ostree', '20_linux_xen', '25_bli', '30_efi-prober', '30_os-prober', '30_uefi-firmware', '35_fwupd', '40_custom', '41_custom', '99_reboot', 'README'}
READONLY_LINKS = {'/etc/grub.d/15_ostree': '/usr/lib/libostree/grub2-15_ostree', '/etc/os-release': '/usr/lib/os-release'}


class EngineError(RuntimeError):
    """A safe refusal or failed transaction, with a message suitable for a UI."""


def require(condition, message):
    if not condition:
        raise EngineError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_release(text):
    result = {}
    for line in text.splitlines():
        if re.match(r'^[A-Z_]+=.*$', line):
            key, value = line.split('=', 1)
            result[key] = value.strip().strip('"')
    return result


def png_info(data):
    """Validate a bounded PNG's complete chunk stream before copying to the ESP."""
    require(len(data) <= 8 * 1024 * 1024 and data.startswith(b'\x89PNG\r\n\x1a\n'), 'Expected a PNG image smaller than 8 MiB')
    pos, dimensions, ended = 8, None, False
    while pos + 12 <= len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        require(end <= len(data), 'Truncated PNG')
        body = data[pos + 8:pos + 8 + length]
        require(zlib.crc32(kind + body) & 0xffffffff == struct.unpack('>I', data[end - 4:end])[0], 'PNG checksum mismatch')
        if dimensions is None:
            require(kind == b'IHDR' and length == 13, 'PNG header missing')
            width, height = struct.unpack('>II', body[:8])
            require(1 <= width <= 4096 and 1 <= height <= 4096, 'PNG dimensions exceed supported limits')
            dimensions = (width, height)
        if kind == b'IEND':
            require(length == 0 and end == len(data), 'Unexpected data after PNG')
            ended = True
            break
        pos = end
    require(ended, 'PNG end marker missing')
    return dimensions


def config_sections(text):
    """Separate grub-mkconfig fragments and reject unexplained outside content."""
    pattern = re.compile(r'^### BEGIN (/etc/grub\.d/[^\n]+) ###\n(.*?)^### END \1 ###\n?', re.M | re.S)
    result, cursor = {}, 0
    for match in pattern.finditer(text):
        outside = text[cursor:match.start()]
        require(all(not line.strip() or line.startswith('#') for line in outside.splitlines()), 'Unmanaged commands outside generated GRUB sections')
        require(match[1] not in result, 'Duplicate GRUB generator section')
        result[match[1]] = match[2]
        cursor = match.end()
    require(result and all(not line.strip() or line.startswith('#') for line in text[cursor:].splitlines()), 'Unrecognized GRUB configuration format')
    return result


def validate_candidate(before, after, operation):
    old, new = config_sections(before), config_sections(after)
    for mapping in (old, new):
        for name in HOOKS:
            mapping.pop(name, None)
    require(set(old) == set(new), 'Native generator list changed; review the OS configuration before continuing')
    for name in old:
        left, right = old[name], new[name]
        if name.endswith('/00_header'):
            # The only vendor header changes requested by our drop-in are the
            # display mode; locale may differ between a GUI and the OS updater.
            for expression in (r'^steamenv_loader_mode=.*$', r'^  set lang=.*$'):
                left = re.sub(expression, '<display-setting>', left, flags=re.M)
                right = re.sub(expression, '<display-setting>', right, flags=re.M)
        require(left == right, f'Native boot commands changed in {name}; refusing to replace the working configuration')
    if operation != 'uninstall':
        sections = config_sections(after)
        require(all(name in sections for name in HOOKS), 'Ice Circuit generator output missing')
        begin, end = sections['/etc/grub.d/09_ice_circuit_menu'], sections['/etc/grub.d/zz_ice_circuit_tools']
        require("--id ice-circuit-windows" in begin, 'Windows menu entry missing')
        opened = 'function ice_circuit_native_menu {' in begin
        require((end == grub.end_fragment()) if opened else (not end.strip()), 'Unbalanced optional Tools submenu')
        require(opened and "'ice-circuit-steamos'" in begin, 'Native generator is not supported for initial installation; configuration left unchanged')


class Engine:
    def __init__(self, root=Path('/'), runner=None):
        self.root = Path(root).absolute()
        self.runner = runner or subprocess.run

    def path(self, logical):
        require(isinstance(logical, str) and logical.startswith('/') and '..' not in PurePosixPath(logical).parts, 'Unsafe system path')
        return self.root / logical.lstrip('/')

    def safe(self, path, *, file=False):
        path = Path(path)
        for item in (path, *path.parents):
            require(not item.is_symlink(), f'Symbolic link is not an installation destination: {item}')
        if path.exists():
            require(path.is_file() if file else (path.is_file() or path.is_dir()), f'Unsupported filesystem object: {path}')
        elif file:
            raise EngineError(f'Required file missing: {path}')
        return path

    def run(self, *args, check=True, **kwargs):
        result = self.runner(list(args), capture_output=True, text=kwargs.pop('text', True), timeout=kwargs.pop('timeout', 120), **kwargs)
        if check and result.returncode:
            message = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
            raise EngineError(f'{args[0]} failed: {message.strip()}')
        return result

    def read(self, logical):
        return self._input_path(logical).read_bytes()

    def _input_path(self, logical):
        """Allow only known vendor links for reading, never for mutation."""
        path = self.path(logical)
        if path.is_symlink() and logical in READONLY_LINKS:
            self.safe(path.parent)
            link = os.readlink(path)
            resolved = os.path.normpath(str(PurePosixPath(logical).parent / link))
            require(resolved == READONLY_LINKS[logical], 'Unexpected vendor symbolic link: ' + logical)
            return self.safe(self.path(READONLY_LINKS[logical]), file=True)
        return self.safe(path, file=True)

    def _input_record(self, logical):
        path = self.path(logical)
        if path.is_symlink() and logical in READONLY_LINKS:
            target = self._input_path(logical)
            return {'link': os.readlink(path), 'sha256': digest(target.read_bytes()), 'mode': stat.S_IMODE(target.stat().st_mode)}
        return self.record(logical)

    def record(self, logical):
        path = self.safe(self.path(logical))
        if not path.exists():
            return None
        require(path.is_file(), f'Expected regular file: {logical}')
        info = path.stat()
        return {'sha256': digest(path.read_bytes()), 'mode': stat.S_IMODE(info.st_mode)}

    def tree(self, logical):
        base = self.safe(self.path(logical))
        if not base.exists():
            return {}
        require(base.is_dir(), f'Expected directory: {logical}')
        result = {}
        for path in sorted(base.rglob('*')):
            self.safe(path)
            if path.is_file():
                result[str(PurePosixPath(logical) / path.relative_to(base).as_posix())] = self.record(str(PurePosixPath(logical) / path.relative_to(base).as_posix()))
        return result

    def _state(self):
        path = self.path(STATE)
        if not path.exists():
            return None
        self.safe(path, file=True)
        value = json.loads(path.read_text())
        require(value.get('schema') == SCHEMA and value.get('phase') == 'installed', 'Incomplete or unsupported installation state; use its recorded rollback first')
        require(isinstance(value.get('files'), dict), 'Installation manifest missing')
        for name in value['files']:
            self._destination(name)
        return value

    def _mount(self, target):
        result = self.run('findmnt', '-J', '-T', target, '-o', 'SOURCE,FSTYPE,TARGET,OPTIONS')
        rows = json.loads(result.stdout).get('filesystems', [])
        # Some SteamOS findmnt versions report both the systemd automount
        # trigger and its mounted filesystem. Only the latter contains data.
        rows = [row for row in rows if row.get('fstype') != 'autofs']
        require(len(rows) == 1, f'A unique mount for {target} was not found')
        row = rows[0]
        require(str(row.get('source', '')).startswith('/dev/'), f'{target} is not a mounted disk filesystem')
        return row

    def discover(self):
        result = {'schema': SCHEMA, 'supported': False, 'blockers': [], 'warnings': [], 'requires_privilege': False,
                  'identity': {}, 'os': {}, 'devices': {}, 'installed': False, 'installation': None, 'backup_ids': [], 'readonly': False, 'nvram': {'text': ''}}
        try:
            release = parse_release(self.read('/etc/os-release').decode())
            result['os'] = {'name': release.get('NAME', ''), 'version': release.get('VERSION_ID', ''), 'build': release.get('BUILD_ID', '')}
            require(release.get('ID') == 'steamos' and release.get('VARIANT_ID') == 'steamdeck', 'This initial release supports SteamOS on Steam Deck only')
            machine = self.read('/etc/machine-id').decode().strip()
            require(re.fullmatch(r'[a-f0-9]{32}', machine), 'Machine identity is missing')
            result['identity']['machine_id_hash'] = digest(machine.encode())
            for folder in ('/esp/EFI', '/efi/EFI'):
                self.path(folder).stat()  # Trigger the existing automount.
            root, esp, efi = self._mount('/'), self._mount('/esp/EFI'), self._mount('/efi/EFI')
            require(root['fstype'] == 'btrfs' and esp['fstype'] == efi['fstype'] == 'vfat', 'Unsupported SteamOS/ESP filesystem layout')
            require(esp['source'] != efi['source'], 'Shared ESP and slot EFI must be separate filesystems')
            for name, row in (('root', root), ('esp', esp), ('efi', efi)):
                result['devices'][name] = row['source']
                for field in ('UUID', 'PARTUUID'):
                    value = self.run('blkid', '-s', field, '-o', 'value', row['source']).stdout.strip()
                    require(re.fullmatch(r'[A-Fa-f0-9-]{4,64}', value), f'{name} {field} unavailable')
                    result['identity'][name + '_' + field.lower()] = value
            for row in (esp, efi):
                require('rw' in row.get('options', '').split(','), 'EFI filesystem is not writable')
            result['readonly'] = self.run('btrfs', 'property', 'get', '/', 'ro').stdout.strip() == 'ro=true'
            require(result['readonly'], 'Restore the normal SteamOS readonly state before installation')
            result['nvram']['text'] = self.run('efibootmgr', '-v').stdout
            self.safe(self.path(GRUB), file=True)
            self.safe(self.path('/efi/EFI/steamos/grubx64.efi'), file=True)
            windows = '/esp/EFI/Microsoft/Boot/bootmgfw.efi'
            self.safe(self.path(windows), file=True)
            result['devices']['windows_efi'] = windows
            # Recovery remains selectable even if an interrupted transaction
            # makes the current owned-file manifest fail its normal checks.
            backups = self.path(BACKUPS)
            if backups.exists():
                self.safe(backups)
                result['backup_ids'] = sorted(p.name for p in backups.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch(r'[0-9]{8}T[0-9]{6}-[a-f0-9]{12}', p.name))
            result['installation'] = self._state()
            result['installed'] = result['installation'] is not None
            if result['installed']:
                previous = result['installation']['identity']
                require(previous['machine_id_hash'] == result['identity']['machine_id_hash'] and previous['esp_partuuid'] == result['identity']['esp_partuuid'], 'Installation belongs to a different machine or shared ESP')
            self._check_conflicts(result['installation'])
            if 'SteamOS' not in result['nvram']['text']:
                result['warnings'].append('No named SteamOS firmware entry was found. Existing fallback boot will be retained; this installer does not change firmware boot order.')
            result['warnings'].append('Windows EFI was detected; its Windows version is not inferred from the EFI filename.')
            result['supported'] = True
        except PermissionError:
            result['requires_privilege'] = True
            result['blockers'].append('Administrator read access is required to inspect the EFI partitions and build a complete plan.')
        except (EngineError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            result['blockers'].append(str(error))
        return result

    def _check_conflicts(self, state):
        own = {Path(p).name for p in HOOKS}
        for p in self.path('/etc/grub.d').iterdir():
            if p.is_symlink():
                self._input_path('/etc/grub.d/' + p.name)
            else:
                self.safe(p)
            if p.is_file() and os.access(p, os.X_OK):
                require(p.name in KNOWN_GENERATORS | own, f'Unrecognized GRUB customization: {p.name}')
        custom = self.read('/etc/grub.d/40_custom').decode()
        require(all(not line.strip() or line.lstrip().startswith('#') for line in custom.splitlines()[2:]), 'Existing custom GRUB menu requires manual migration')
        for location in ('/efi/EFI/steamos/custom.cfg', '/boot/grub/custom.cfg'):
            require(not self.path(location).exists(), 'Existing custom.cfg requires manual migration')
        allowed_esp = {'boot', 'steamos', 'microsoft', 'ice-circuit'}
        for p in self.path('/esp/EFI').iterdir():
            require(p.name.lower() in allowed_esp, f'Another boot manager or unknown EFI folder is present: {p.name}')
        # A fallback binary different from SteamOS's chainloader may be rEFInd,
        # Clover or a user-installed manager; never overwrite or claim it.
        efi_files = list(self.path('/esp/EFI').rglob('*'))
        lower = {p.relative_to(self.path('/esp/EFI')).as_posix().lower(): p for p in efi_files if p.is_file()}
        fallback, steamcl = lower.get('boot/bootx64.efi'), lower.get('steamos/steamcl.efi')
        require(fallback is not None and steamcl is not None and fallback.read_bytes() == steamcl.read_bytes(), 'The shared-ESP fallback is not the native SteamOS chainloader')
        if state is None:
            require(not self.path('/etc/ice-circuit').exists() and not self.path('/esp/EFI/ice-circuit').exists(), 'Unowned Ice Circuit files already exist')
            require(all(not self.path(name).exists() for name in OWNED_ETC), 'Unowned integration files already exist')
        else:
            current = {p: self.record(p) for p in OWNED_ETC}
            current.update(self.tree(ASSETS))
            expected = {p: value for p, value in state['files'].items() if p != GRUB}
            require(current == expected, 'Installed files were modified outside Ice Circuit; preserve or review those changes first')

    def _fingerprints(self):
        records = {GRUB: self.record(GRUB), STATE: self.record(STATE)}
        for folder in ('/etc/grub.d', '/etc/default/grub.d', '/etc/atomic-update.conf.d'):
            base = self.path(folder)
            if base.exists():
                for path in base.iterdir():
                    if path.is_file() or path.is_symlink():
                        records[folder + '/' + path.name] = self._input_record(folder + '/' + path.name)
        records['/etc/default/grub'] = self.record('/etc/default/grub')
        for name in ('/etc/default/grub-steamos', '/usr/bin/grub-mkconfig', '/usr/bin/grub-script-check', '/usr/share/grub/grub-mkconfig_lib'):
            records[name] = self.record(name)
        for name in OWNED_ETC:
            records[name] = self.record(name)
        records.update(self.tree(ASSETS))
        for base in ('/esp/EFI', '/efi/EFI'):
            for path in self.path(base).rglob('*'):
                if path.suffix.lower() == '.efi':
                    logical = base + '/' + path.relative_to(self.path(base)).as_posix()
                    records[logical] = self.record(logical)
        return records

    def _assets(self, options, state):
        directory = Path(options['asset_source']).absolute()
        self.safe(directory)
        require(directory.is_dir(), 'Theme source directory is missing')
        result = {}
        for path in sorted(directory.rglob('*')):
            self.safe(path)
            if not path.is_file():
                continue
            name = path.relative_to(directory).as_posix()
            require(re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', name) and '..' not in PurePosixPath(name).parts, 'Unsupported asset filename')
            require(path.suffix.lower() in ('.png', '.pf2', '.txt'), 'Only theme text, PNG images and PF2 fonts may be installed')
            data = path.read_bytes()
            require(len(data) <= 8 * 1024 * 1024, 'Theme asset exceeds size limit')
            if path.suffix.lower() == '.png':
                png_info(data)
            elif path.suffix.lower() == '.pf2':
                require(data.startswith(b'FILE\x00\x00\x00\x04PFF2'), 'Invalid GRUB PF2 font')
            else:
                data.decode('utf-8')
            result[name] = data
        require(REQUIRED_ASSETS <= result.keys(), 'Theme package is missing required files')
        require(len(result) <= 256 and sum(map(len, result.values())) <= 24 * 1024 * 1024, 'Theme package exceeds size limit')
        overrides = copy.deepcopy(state.get('overrides', {})) if state else {}
        for name, info in overrides.items():
            require(name in ('steamos', 'windows'), 'Unknown stored icon override')
            result['icons/' + name + '.png'] = self.read(ASSETS + '/icons/' + name + '.png')
        for name, source in options.get('icon_overrides', {}).items():
            require(name in ('steamos', 'windows'), 'Unknown icon target')
            if source is None:
                overrides.pop(name, None)
                result['icons/' + name + '.png'] = (directory / 'icons' / (name + '.png')).read_bytes()
                continue
            path = self.safe(Path(source).absolute(), file=True)
            data = path.read_bytes()
            require(png_info(data) == (72, 72), 'Choose or prepare a 72 × 72 PNG icon')
            result['icons/' + name + '.png'] = data
            overrides[name] = {'sha256': digest(data), 'source_name': path.name}
        return result, overrides

    def _options(self, options):
        require(isinstance(options, dict), 'Plan options must be an object')
        allowed = {'operation', 'asset_source', 'windows_label', 'timeout', 'default_os', 'icon_overrides', 'expected_identity', 'repair_nvram'}
        require(not (options.keys() - allowed), 'Unknown installer option')
        require(not options.get('repair_nvram'), 'Firmware boot-order repair is a separate reviewed operation and is not implemented here')
        operation = options.get('operation', 'install')
        require(operation in ('install', 'update', 'uninstall'), 'Unknown operation')
        value = {'operation': operation, 'windows_label': options.get('windows_label', 'Windows'), 'timeout': options.get('timeout', 5), 'default_os': options.get('default_os', 'steamos'),
                 'icon_overrides': copy.deepcopy(options.get('icon_overrides', {})), 'expected_identity': copy.deepcopy(options.get('expected_identity'))}
        require(isinstance(value['icon_overrides'], dict), 'Icon overrides must be an object')
        if operation != 'uninstall':
            require(isinstance(options.get('asset_source'), str), 'A theme source directory is required')
            value['asset_source'] = str(Path(options['asset_source']).absolute())
        try:
            grub.settings(dict(version=1, esp_uuid='0000-0000', windows_uuid='0000-0000', **{k: value[k] for k in ('windows_label', 'timeout', 'default_os')}))
        except ValueError as error:
            raise EngineError(str(error)) from error
        return value

    def build_plan(self, options):
        options = self._options(options)
        discovery = self.discover()
        require(discovery['supported'], '; '.join(discovery['blockers']))
        require(options['expected_identity'] == discovery['identity'], 'Explicitly confirm the discovered target identity before planning')
        state = discovery['installation']
        operation = options['operation']
        require(bool(state) == (operation != 'install'), 'Use install for a new installation, or update/uninstall for an owned installation')
        files, overrides = ({}, {}) if operation == 'uninstall' else self._assets(options, state)
        if operation != 'uninstall':
            # 10_linux relies on variables exported by grub-mkconfig. Its
            # existing generated section is the safe read-only planning probe;
            # apply validates a freshly generated candidate before committing.
            source = config_sections(self.read(GRUB).decode()).get('/etc/grub.d/10_linux', '')
            try:
                grub.primary_entry(source)
            except ValueError as error:
                raise EngineError('Unsupported native SteamOS generator: ' + str(error)) from error
        plan = {'schema': SCHEMA, 'operation': operation, 'options': options, 'identity': discovery['identity'],
                'current': self._fingerprints(), 'nvram': discovery['nvram']['text'], 'overrides': overrides,
                'assets': {name: {'sha256': digest(data), 'size': len(data)} for name, data in files.items()},
                'helper_sha256': digest(Path(grub.__file__).read_bytes()),
                'mutations': [{'path': p, 'action': 'remove' if operation == 'uninstall' else 'write'} for p in OWNED_ETC] + [{'path': ASSETS, 'action': 'remove' if operation == 'uninstall' else 'replace-owned-assets'}, {'path': GRUB, 'action': 'regenerate-and-validate'}],
                'warnings': discovery['warnings']}
        plan['plan_id'] = digest(canonical(plan))
        return plan

    def _destination(self, logical):
        require(logical in OWNED_ETC or logical in (GRUB, STATE) or logical.startswith(ASSETS + '/'), 'A plan or journal contains an unowned destination')
        if logical.startswith(ASSETS + '/'):
            relative = logical[len(ASSETS) + 1:]
            require(re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', relative) and '..' not in PurePosixPath(relative).parts, 'Invalid installed asset path')
        return self.safe(self.path(logical))

    def _mkdir(self, path, mode=0o755):
        path = Path(path)
        self.safe(path)
        if path.exists():
            require(path.is_dir(), f'Not a directory: {path}')
            return
        self._mkdir(path.parent, mode)
        path.mkdir(mode=mode)
        # mkdir's mode is filtered by the caller's umask. Newly created public
        # settings directories must remain traversable by the desktop GUI;
        # recovery directories retain their explicitly requested private mode.
        # FAT permissions are mount-derived and must not be chmod'ed here.
        fat_roots = (self.path('/esp'), self.path('/efi'))
        if not any(path == base or base in path.parents for base in fat_roots):
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._fsync(path.parent)

    @staticmethod
    def _fsync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic(self, path, data, mode=0o644, efi=False):
        path = self.safe(Path(path))
        self._mkdir(path.parent)
        fd, name = tempfile.mkstemp(prefix='.ice-circuit-', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(data)
                if not efi:
                    os.fchmod(output.fileno(), mode)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            self._fsync(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _write(self, logical, data, mode=0o644):
        path = self._destination(logical)
        self._atomic(path, data, mode, logical.startswith(('/esp/', '/efi/')))

    def _remove(self, logical):
        path = self._destination(logical)
        if path.exists():
            require(path.is_file(), 'Refusing to remove a non-file')
            path.unlink()
            self._fsync(path.parent)

    def _journal(self, folder, value):
        self._atomic(folder / 'journal.json', canonical(value) + b'\n', 0o600)

    def _payload(self, plan, assets):
        settings = {'version': 1, 'esp_uuid': plan['identity']['esp_uuid'], 'windows_uuid': plan['identity']['esp_uuid'],
                    **{name: plan['options'][name] for name in ('windows_label', 'timeout', 'default_os')}}
        payload = {CONFIG: (canonical(settings) + b'\n', 0o644), HELPER: (Path(grub.__file__).read_bytes(), 0o644)}
        for path, mode in HOOKS.items():
            payload[path] = (f'#!/bin/sh\n# Ice Circuit optional generator; native generators remain unchanged.\nexec /usr/bin/python3 -B /etc/ice-circuit/grub.py {mode}\n'.encode(), 0o755)
        payload[DEFAULTS] = (b'# Display mode required by the Steam Deck firmware for a visible menu.\nGRUB_GFXMODE=1280x800x32\n', 0o644)
        payload[KEEP] = (('# Preserve only Ice Circuit-owned configuration across SteamOS slot updates.\n' + '\n'.join((CONFIG, HELPER, *HOOKS, DEFAULTS, KEEP)) + '\n').encode(), 0o644)
        for name, data in assets.items():
            payload[ASSETS + '/' + name] = (data, 0o644)
        return payload

    def _critical(self):
        records = self._fingerprints()
        return {p: value for p, value in records.items() if p not in OWNED_ETC and p not in (GRUB, STATE) and not p.startswith(ASSETS + '/')}

    def _media_verify(self, backup, expected, device):
        # Reading a mounted FAT block device through buffered I/O can expose
        # stale block-cache contents. Verify GRUB against a fresh direct read.
        self.run('sync', '-f', str(self.path(ASSETS)))
        snapshot = backup / 'esp-direct.img'
        size = int(self.run('blockdev', '--getsize64', device).stdout)
        require(shutil.disk_usage(backup).free >= size + 16 * 1024 * 1024, 'Insufficient backup space for ESP verification')
        self.run('dd', 'if=' + device, 'of=' + str(snapshot), 'bs=1M', 'iflag=direct', 'conv=fsync', 'status=none')
        os.chmod(snapshot, 0o600)
        require(snapshot.stat().st_size == size, 'Incomplete direct ESP snapshot')
        for logical, info in expected.items():
            if logical.startswith(ASSETS + '/'):
                image_path = '/EFI/ice-circuit/theme/' + logical[len(ASSETS) + 1:]
                result = self.run('grub-fstest', str(snapshot), 'cat', image_path, text=False)
                require(digest(result.stdout) == info['sha256'], 'GRUB cannot read the installed asset from media: ' + image_path)
        return {'image': snapshot.name, 'sha256': digest(snapshot.read_bytes()), 'bytes': size}

    def _empty_owned_directories(self):
        for logical in ('/esp/EFI/ice-circuit', '/etc/ice-circuit'):
            base = self.path(logical)
            if base.exists():
                for path in sorted((p for p in base.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    self.safe(path)
                    if not any(path.iterdir()):
                        path.rmdir()
                if not any(base.iterdir()):
                    base.rmdir()

    def _backup(self, paths, discovery, before_state):
        folder = self.path(BACKUPS + '/' + time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:12])
        self._mkdir(folder, 0o700)
        originals = {}
        for i, name in enumerate(sorted(paths)):
            record = self.record(name)
            originals[name] = record
            if record is not None:
                backup_name = f'file-{i:04d}'
                self._atomic(folder / backup_name, self.read(name), 0o600)
                originals[name] = dict(record, backup=backup_name)
        journal = {'schema': SCHEMA, 'phase': 'prepared', 'identity': discovery['identity'], 'originals': originals,
                   'intended': {}, 'after': {}, 'before_state': before_state, 'nvram': discovery['nvram']['text'], 'critical': self._critical()}
        self._journal(folder, journal)
        return folder, journal

    def _recovery_guard(self, journal):
        require(journal.get('phase') in ('applied', 'prepared', 'applying', 'rolling-back'), 'This backup has already been restored or is not recoverable')
        for name, original in journal['originals'].items():
            self._destination(name)
            actual = self.record(name)
            if journal['phase'] == 'applied':
                require(name in journal['after'] and actual == journal['after'][name], f'Later modifications prevent rollback: {name}')
            else:
                old = {k: v for k, v in original.items() if k != 'backup'} if original else None
                intended = journal['intended'].get(name)
                # VFAT modes are supplied by mount masks, not requested chmod.
                if name.startswith(('/esp/', '/efi/')):
                    actual, old, intended = [item.get('sha256') if item else None for item in (actual, old, intended)]
                require(actual in (old, intended), f'Unrecognized partial transaction data: {name}')
        require(set(self.tree(ASSETS)) <= journal['originals'].keys(), 'Additional assets prevent rollback')

    def _restore(self, folder, journal):
        # Read and verify every backup before the first destination changes.
        self._recovery_guard(journal)
        restored = {}
        for name, original in journal['originals'].items():
            if original is None:
                restored[name] = None
                continue
            require(re.fullmatch(r'file-[0-9]{4}', original['backup']), 'Unsafe recovery filename')
            data = self.safe(folder / original['backup'], file=True).read_bytes()
            require(digest(data) == original['sha256'], 'Recovery file checksum mismatch')
            restored[name] = data
        journal['phase'] = 'rolling-back'
        self._journal(folder, journal)
        # Restore boot config first, while all former assets are still available.
        names = sorted(restored, key=lambda p: (p != GRUB, p == STATE, p))
        for name in names:
            if restored[name] is None:
                self._remove(name)
            else:
                self._write(name, restored[name], journal['originals'][name]['mode'])
        self._empty_owned_directories()
        journal['phase'] = 'rolled-back'
        self._journal(folder, journal)

    def _lock(self):
        require(self.root != Path('/') or os.geteuid() == 0, 'Administrator privileges are required')
        self._mkdir(self.path(STATE_DIR), 0o700)
        if self.root == Path('/'):
            info = self.path(STATE_DIR).stat()
            require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o077, 'Recovery directory must be private and owned by root')
        lock_path = self.safe(self.path(STATE_DIR + '/lock'))
        lock = open(lock_path, 'a+b')
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise EngineError('Another Ice Circuit operation is in progress')
        return lock

    def apply(self, plan):
        require(isinstance(plan, dict) and plan.get('schema') == SCHEMA, 'Invalid plan')
        value = copy.deepcopy(plan)
        supplied = value.pop('plan_id', None)
        require(supplied == digest(canonical(value)), 'Plan checksum mismatch')
        # Rebuild from bounded options; never trust mutation paths, bytes or
        # manifests supplied by a caller, even with a recomputed plan checksum.
        current = self.build_plan(plan['options'])
        require(current == plan, 'The device, package, assets or settings changed since review; create a new plan')
        with self._lock():
            require(self.build_plan(plan['options']) == plan, 'State changed while acquiring the installation lock')
            discovery = self.discover()
            before_state = discovery['installation']
            assets, overrides = ({}, {}) if plan['operation'] == 'uninstall' else self._assets(plan['options'], before_state)
            require({name: {'sha256': digest(data), 'size': len(data)} for name, data in assets.items()} == plan['assets'] and overrides == plan['overrides'], 'Source assets changed while preparing the transaction')
            payload = {} if plan['operation'] == 'uninstall' else self._payload(plan, assets)
            if payload:
                require(digest(payload[HELPER][0]) == plan['helper_sha256'], 'Generator helper changed while preparing the transaction')
            old_assets = self.tree(ASSETS)
            paths = set(OWNED_ETC) | set(old_assets) | set(payload) | {GRUB, STATE}
            space = sum(len(data) for path, (data, _) in payload.items() if path.startswith(ASSETS + '/'))
            require(shutil.disk_usage(self.path('/esp')).free >= space + 2 * 1024 * 1024, 'Insufficient free ESP space for safe theme staging')
            folder, journal = self._backup(paths, discovery, before_state)
            before_cfg = self.read(GRUB).decode()
            journal.update(phase='applying', operation=plan['operation'], plan_id=plan['plan_id'])
            journal['intended'] = {p: {'sha256': digest(data), 'mode': mode} for p, (data, mode) in payload.items()}
            self._journal(folder, journal)
            try:
                if plan['operation'] == 'uninstall':
                    # Remove the hooks before regenerating this slot's current
                    # native configuration. Never restore an obsolete kernel cfg.
                    for name in OWNED_ETC:
                        self._remove(name)
                else:
                    for name, (data, mode) in payload.items():
                        self._write(name, data, mode)
                with tempfile.TemporaryDirectory(prefix='ice-circuit-', dir=self.path('/run')) as temp:
                    candidate = Path(temp) / 'grub.cfg'
                    self.run('grub-mkconfig', '-o', str(candidate))
                    self.run('grub-script-check', str(candidate))
                    data = self.safe(candidate, file=True).read_bytes()
                    validate_candidate(before_cfg, data.decode(), plan['operation'])
                    intended_cfg = {'sha256': digest(data), 'mode': 0o644}
                    journal['intended'][GRUB] = intended_cfg
                    self._journal(folder, journal)
                    if plan['operation'] != 'uninstall':
                        current_assets = self.tree(ASSETS)
                        # Obsolete files are removed only once the new cfg is ready.
                        for obsolete in old_assets.keys() - payload.keys():
                            self._remove(obsolete)
                        journal['media'] = self._media_verify(folder, self.tree(ASSETS), discovery['devices']['esp'])
                    require(self._critical() == journal['critical'], 'Native boot files changed during installation')
                    require(self.run('efibootmgr', '-v').stdout == journal['nvram'], 'Firmware boot settings changed during installation')
                    require(self.run('btrfs', 'property', 'get', '/', 'ro').stdout.strip() == 'ro=true', 'SteamOS readonly state changed')
                    self._write(GRUB, data)
                if plan['operation'] == 'uninstall':
                    for name in old_assets:
                        self._remove(name)
                    self._remove(STATE)
                    self._empty_owned_directories()
                else:
                    files = {p: self.record(p) for p in OWNED_ETC}
                    files.update(self.tree(ASSETS))
                    files[GRUB] = self.record(GRUB)
                    state = {'schema': SCHEMA, 'phase': 'installed', 'identity': discovery['identity'], 'files': files,
                             'options': {k: plan['options'][k] for k in ('windows_label', 'timeout', 'default_os')},
                             'overrides': plan['overrides'], 'backup_id': folder.name}
                    journal['intended'][STATE] = {'sha256': digest(canonical(state) + b'\n'), 'mode': 0o600}
                    self._journal(folder, journal)
                    self._write(STATE, canonical(state) + b'\n', 0o600)
                journal['after'] = {name: self.record(name) for name in paths}
                journal['phase'] = 'applied'
                self._journal(folder, journal)
                return {'ok': True, 'operation': plan['operation'], 'backup_id': folder.name, 'plan_id': plan['plan_id'], 'reboot_required': True, 'nvram_changed': False}
            except BaseException as error:
                try:
                    self._restore(folder, journal)
                except BaseException as recovery:
                    raise EngineError(f'Operation failed ({error}); recovery needs attention ({recovery}). Backup: {folder.name}') from error
                raise EngineError(f'Operation failed and previous files were restored: {error}. Backup: {folder.name}') from error

    def rollback(self, backup_id):
        require(isinstance(backup_id, str) and re.fullmatch(r'[0-9]{8}T[0-9]{6}-[a-f0-9]{12}', backup_id), 'Invalid backup identifier')
        with self._lock():
            folder = self.safe(self.path(BACKUPS + '/' + backup_id))
            journal = json.loads(self.safe(folder / 'journal.json', file=True).read_text())
            require(journal.get('schema') == SCHEMA and journal.get('phase') in ('applied', 'prepared', 'applying', 'rolling-back'), 'This backup has already been restored or is not recoverable')
            # Avoid discover's installed-state requirement while recovering an
            # interrupted transaction. Read only the identity and invariant data.
            machine = self.read('/etc/machine-id').decode().strip()
            require(digest(machine.encode()) == journal['identity']['machine_id_hash'], 'Recovery belongs to another machine')
            for target, key in (('/', 'root'), ('/esp/EFI', 'esp'), ('/efi/EFI', 'efi')):
                self.path(target).stat()
                row = self._mount(target)
                partuuid = self.run('blkid', '-s', 'PARTUUID', '-o', 'value', row['source']).stdout.strip()
                require(partuuid == journal['identity'][key + '_partuuid'], 'Slot or partition changed; use a new uninstall plan instead of restoring an old boot configuration')
            require(self._critical() == journal['critical'], 'Native files changed since this backup; use a current uninstall plan')
            nvram = self.run('efibootmgr', '-v').stdout
            self._restore(folder, journal)
            require(self.run('efibootmgr', '-v').stdout == nvram, 'Firmware settings changed during rollback')
            return {'ok': True, 'operation': 'rollback', 'backup_id': backup_id, 'reboot_required': True, 'nvram_changed': False}
