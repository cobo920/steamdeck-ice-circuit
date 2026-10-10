"""Isolated transaction tests. No real devices, firmware or sudo are used."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ice_circuit import grub
from ice_circuit.engine import ASSETS, BACKUPS, CONFIG, GRUB, HOOKS, OWNED_ETC, STATE, Engine, EngineError, canonical, digest, validate_candidate


def png(width=72, height=72, color=(12, 34, 56, 255)):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = b''.join(b'\x00' + bytes(color) * width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def native_entry(kernel='fixture-kernel'):
    # Purpose-written miniature generator output, not a copied vendor script.
    return f'''set linux_gfx_mode=${{steamenv_kernel_mode}}
export linux_gfx_mode
menuentry 'SteamOS' --class steamos --class os $menuentry_id_option 'gnulinux-simple-fixture' {{
    steamenv_boot linux /boot/{kernel} root=UUID=fixture
    initrd /boot/fixture-initrd
}}
submenu 'Native recovery' {{
    menuentry 'Fallback' {{
        steamenv_boot linux /boot/{kernel} recovery
        initrd /boot/fixture-fallback
    }}
}}
'''


def section(name, text):
    return f'### BEGIN /etc/grub.d/{name} ###\n{text}### END /etc/grub.d/{name} ###\n\n'


class FakeRunner:
    def __init__(self, root):
        self.root = root
        self.kernel = 'fixture-kernel'
        self.identity = {'/dev/testroot': {'UUID': '1111-AAAA', 'PARTUUID': '1111-AAAA-1111'}, '/dev/testesp': {'UUID': '2222-BBBB', 'PARTUUID': '2222-BBBB-2222'}, '/dev/testefi': {'UUID': '3333-CCCC', 'PARTUUID': '3333-CCCC-3333'}}
        self.nvram = 'BootOrder: 0002,0001\nBoot0002* SteamOS\nBoot0001* Windows Boot Manager\n'
        self.fail = None
        self.corrupt_media = False
        self.calls = []
        self.snapshots = {}

    def generated(self):
        installed = (self.root / CONFIG.lstrip('/')).exists()
        mode = '1280x800x32' if installed else 'auto'
        cfg = '# Fixture configuration\n' + section('00_header', f'steamenv_loader_mode={mode}\ntimeout=0\nsteamenv_init\n')
        if installed:
            settings = json.loads((self.root / CONFIG.lstrip('/')).read_text())
            cfg += section('08_ice_circuit_theme', grub.theme_fragment(settings))
            cfg += section('09_ice_circuit_menu', grub.main_fragment(settings, native_entry(self.kernel)))
        cfg += section('10_linux', native_entry(self.kernel))
        cfg += section('40_custom', '# Fixture custom entries, intentionally empty.\n')
        if installed:
            cfg += section('zz_ice_circuit_tools', grub.end_fragment())
        return cfg

    def __call__(self, args, capture_output=True, text=True, timeout=120, **kwargs):
        self.calls.append(args)
        command, output, error, rc = args[0], '', '', 0
        if self.fail == command:
            return subprocess.CompletedProcess(args, 1, '' if text else b'', 'injected failure' if text else b'injected failure')
        if command == 'findmnt':
            target = args[args.index('-T') + 1]
            device, kind, mount = {'/': ('/dev/testroot', 'btrfs', '/'), '/esp/EFI': ('/dev/testesp', 'vfat', '/esp'), '/efi/EFI': ('/dev/testefi', 'vfat', '/efi')}[target]
            output = json.dumps({'filesystems': [{'source': device, 'fstype': kind, 'target': mount, 'options': 'rw,relatime'}]})
        elif command == 'blkid':
            output = self.identity[args[-1]][args[2]] + '\n'
        elif command == 'btrfs':
            output = 'ro=true\n'
        elif command == 'efibootmgr':
            self.assert_readonly_efi_args(args)
            output = self.nvram
        elif command == 'grub-mkconfig':
            Path(args[-1]).write_text(self.generated())
        elif command == 'grub-script-check':
            # Production uses the actual GRUB parser. This runner can inject its
            # failure and verifies that a concrete candidate was supplied.
            assert Path(args[-1]).is_file()
        elif command == 'blockdev':
            output = '8\n'
        elif command == 'dd':
            output_path = Path(next(a[3:] for a in args if a.startswith('of=')))
            assert 'iflag=direct' in args and 'conv=fsync' in args
            output_path.write_bytes(b'SNAPSHOT')
            self.snapshots[str(output_path)] = {p.relative_to(self.root / 'esp').as_posix(): p.read_bytes() for p in (self.root / 'esp').rglob('*') if p.is_file()}
        elif command == 'grub-fstest':
            output = self.snapshots[args[1]][args[-1].lstrip('/')]
            if self.corrupt_media:
                output = b'CORRUPTED MEDIA'
        elif command == 'sync':
            pass
        else:
            raise AssertionError('Unexpected external command: ' + repr(args))
        if not text and isinstance(output, str):
            output = output.encode()
        return subprocess.CompletedProcess(args, rc, output, error if text else error.encode())

    @staticmethod
    def assert_readonly_efi_args(args):
        assert args == ['efibootmgr', '-v'], 'No NVRAM mutations allowed'


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'root'
        self.root.mkdir()
        for name in ('etc/grub.d', 'etc/default', 'etc/atomic-update.conf.d', 'esp/EFI/boot', 'esp/EFI/steamos', 'esp/EFI/Microsoft/Boot', 'efi/EFI/steamos', 'home', 'run'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.put('/etc/os-release', 'ID=steamos\nVARIANT_ID=steamdeck\nNAME=SteamOS\nVERSION_ID=fixture\nBUILD_ID=fixture\n')
        self.put('/etc/machine-id', '0123456789abcdef0123456789abcdef\n')
        self.put('/etc/default/grub', 'GRUB_TIMEOUT=0\n')
        for name in ('00_header', '10_linux', '40_custom'):
            self.put('/etc/grub.d/' + name, '#!/bin/sh\n# Fixture generator\n', 0o755)
        self.put('/esp/EFI/boot/bootx64.efi', b'STEAM CHAINLOADER FIXTURE')
        self.put('/esp/EFI/steamos/steamcl.efi', b'STEAM CHAINLOADER FIXTURE')
        self.put('/esp/EFI/Microsoft/Boot/bootmgfw.efi', b'WINDOWS FIXTURE')
        self.put('/efi/EFI/steamos/grubx64.efi', b'GRUB FIXTURE')
        self.runner = FakeRunner(self.root)
        self.put(GRUB, self.runner.generated())
        self.engine = Engine(self.root, self.runner)
        self.assets = Path(self.temporary.name) / 'assets'
        (self.assets / 'icons').mkdir(parents=True)
        for name in ('theme.txt', 'theme-tools.txt'):
            (self.assets / name).write_text('desktop-color: "#000000"\n')
        (self.assets / 'boot-background.png').write_bytes(png())
        for name in ('steamos', 'windows', 'tools'):
            (self.assets / 'icons' / (name + '.png')).write_bytes(png())
        for name in ('sans-32.pf2', 'sans-24.pf2', 'mono-18.pf2'):
            (self.assets / name).write_bytes(b'FILE\x00\x00\x00\x04PFF2fixture')

    def tearDown(self):
        self.temporary.cleanup()

    def put(self, name, data, mode=0o644):
        path = self.root / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(mode)

    def options(self, operation='install', **extra):
        return {'operation': operation, 'asset_source': str(self.assets), 'expected_identity': self.engine.discover()['identity'], **extra}

    def install(self, **extra):
        return self.engine.apply(self.engine.build_plan(self.options(**extra)))

    def test_discovery_is_readonly_and_partition_ids_are_discovered(self):
        found = self.engine.discover()
        self.assertTrue(found['supported'], found)
        self.assertEqual(found['identity']['esp_uuid'], '2222-BBBB')
        self.assertFalse((self.root / STATE.lstrip('/')).exists())

    def mount_rows(self, transform):
        original = self.engine.runner
        def runner(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == 'findmnt' and args[args.index('-T') + 1] != '/':
                value = json.loads(result.stdout)
                value['filesystems'] = transform(value['filesystems'][0])
                result.stdout = json.dumps(value)
            return result
        self.engine.runner = runner

    def test_native_stacked_automount_supports_install_and_rollback(self):
        def stacked(row):
            return [{'source': 'systemd-1', 'fstype': 'autofs', 'target': row['target'], 'options': 'rw,timeout=60'}, row]
        self.mount_rows(stacked)
        found = self.engine.discover()
        self.assertTrue(found['supported'], found)
        self.assertEqual(found['devices']['esp'], '/dev/testesp')
        before = self.engine.read(GRUB)
        installed = self.install()
        self.engine.rollback(installed['backup_id'])
        self.assertEqual(before, self.engine.read(GRUB))

    def test_multiple_real_mounts_remain_ambiguous(self):
        self.mount_rows(lambda row: [row, dict(row, source='/dev/another-device')])
        found = self.engine.discover()
        self.assertFalse(found['supported'])
        self.assertIn('A unique mount', found['blockers'][0])
        self.assertFalse(self.engine.path(CONFIG).exists())

    def test_unactivated_automount_is_not_treated_as_a_disk(self):
        self.mount_rows(lambda row: [{'source': 'systemd-1', 'fstype': 'autofs', 'target': row['target'], 'options': 'rw,timeout=60'}])
        found = self.engine.discover()
        self.assertFalse(found['supported'])
        self.assertIn('A unique mount', found['blockers'][0])
        self.assertFalse(self.engine.path(CONFIG).exists())

    def test_install_retains_native_files_nvram_and_boot_commands(self):
        original = self.engine._critical()
        result = self.install(windows_label='Windows 10')
        self.assertTrue(result['ok'])
        self.assertEqual(original, self.engine._critical())
        self.assertTrue(self.engine.discover()['installed'])
        cfg = self.engine.read(GRUB).decode()
        self.assertIn('menuentry "Windows 10"', cfg)
        self.assertIn('submenu \'Tools & Recovery\'', cfg)
        self.assertIn('root=UUID=fixture', cfg)
        self.assertTrue(any(call[0] == 'grub-fstest' for call in self.runner.calls))

    def test_restrictive_umask_keeps_settings_public_and_backups_private(self):
        previous = os.umask(0o077)
        try:
            installed = self.install()
        finally:
            os.umask(previous)
        mode = lambda logical: stat.S_IMODE(self.engine.path(logical).stat().st_mode)
        self.assertEqual(mode('/etc/ice-circuit'), 0o755)
        self.assertEqual(mode('/etc/default/grub.d'), 0o755)
        self.assertEqual(mode(CONFIG), 0o644)
        for logical in ('/home/.ice-circuit', BACKUPS, BACKUPS + '/' + installed['backup_id']):
            self.assertEqual(mode(logical), 0o700)
        self.assertEqual(mode(STATE), 0o600)

    def test_directory_creation_does_not_chmod_fat_or_existing_directories(self):
        existing = self.engine.path('/etc/private-existing')
        existing.mkdir(mode=0o700)
        with patch('ice_circuit.engine.os.fchmod', side_effect=AssertionError('Unexpected chmod')):
            self.engine._mkdir(existing, mode=0o755)
            self.engine._mkdir(self.engine.path('/esp/EFI/ice-circuit/theme'), mode=0o755)
            self.engine._mkdir(self.engine.path('/efi/EFI/fixture-directory'), mode=0o755)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o700)

    def test_stale_plan_refuses_before_mutation(self):
        plan = self.engine.build_plan(self.options())
        self.put('/etc/grub.d/10_linux', '#!/bin/sh\n# Changed upstream\n', 0o755)
        with self.assertRaisesRegex(EngineError, 'changed since review'):
            self.engine.apply(plan)
        self.assertFalse((self.root / CONFIG.lstrip('/')).exists())

    def test_native_vendor_symlink_is_readonly_fingerprinted(self):
        target = '/usr/lib/libostree/grub2-15_ostree'
        self.put(target, '#!/bin/sh\n# Vendor fixture\n', 0o755)
        self.engine.path('/etc/grub.d/15_ostree').symlink_to(target)
        self.assertTrue(self.engine.discover()['supported'])
        plan = self.engine.build_plan(self.options())
        self.assertEqual(plan['current']['/etc/grub.d/15_ostree']['link'], target)
        self.put(target, '#!/bin/sh\n# Changed vendor target\n', 0o755)
        with self.assertRaisesRegex(EngineError, 'changed since review'):
            self.engine.apply(plan)
        self.install()
        self.assertTrue(self.engine.path('/etc/grub.d/15_ostree').is_symlink())

    def test_unexpected_vendor_symlink_target_is_refused(self):
        self.engine.path('/etc/grub.d/15_ostree').symlink_to('/tmp/other')
        self.assertFalse(self.engine.discover()['supported'])
        self.assertIn('Unexpected vendor symbolic link', self.engine.discover()['blockers'][0])

    def test_standard_relative_os_release_link_is_readable(self):
        release = self.engine.read('/etc/os-release')
        self.put('/usr/lib/os-release', release)
        self.engine.path('/etc/os-release').unlink()
        self.engine.path('/etc/os-release').symlink_to('../usr/lib/os-release')
        self.assertTrue(self.engine.discover()['supported'])

    def test_modified_plan_cannot_add_arbitrary_destination(self):
        plan = self.engine.build_plan(self.options())
        plan['mutations'].append({'path': '/etc/passwd', 'action': 'write'})
        plan.pop('plan_id')
        plan['plan_id'] = digest(canonical(plan))
        with self.assertRaisesRegex(EngineError, 'changed since review'):
            self.engine.apply(plan)
        self.assertFalse((self.root / CONFIG.lstrip('/')).exists())

    def test_wrong_machine_identity_refuses(self):
        options = self.options()
        options['expected_identity']['machine_id_hash'] = '0' * 64
        with self.assertRaisesRegex(EngineError, 'target identity'):
            self.engine.build_plan(options)

    def test_script_check_failure_restores_every_owned_file(self):
        before = self.engine.read(GRUB)
        self.runner.fail = 'grub-script-check'
        with self.assertRaisesRegex(EngineError, 'previous files were restored'):
            self.install()
        self.assertEqual(before, self.engine.read(GRUB))
        self.assertTrue(all(not self.engine.path(p).exists() for p in OWNED_ETC))
        self.assertFalse(self.engine.path('/esp/EFI/ice-circuit').exists())
        self.assertFalse(self.engine.path(STATE).exists())

    def test_raw_media_mismatch_rolls_back(self):
        self.runner.corrupt_media = True
        before = self.engine.read(GRUB)
        with self.assertRaisesRegex(EngineError, 'cannot read the installed asset'):
            self.install()
        self.assertEqual(before, self.engine.read(GRUB))
        self.assertFalse(self.engine.path(CONFIG).exists())

    def test_atomic_config_commit_failure_restores_original(self):
        atomic = self.engine._atomic
        before = self.engine.read(GRUB)
        failed = False
        def fail_once(path, data, *args, **kwargs):
            nonlocal failed
            if path == self.engine.path(GRUB) and not failed:
                failed = True
                raise OSError('injected EFI rename failure')
            return atomic(path, data, *args, **kwargs)
        with patch.object(self.engine, '_atomic', fail_once):
            with self.assertRaisesRegex(EngineError, 'previous files were restored'):
                self.install()
        self.assertEqual(before, self.engine.read(GRUB))
        self.assertFalse(self.engine.path(CONFIG).exists())

    def test_install_rollback_and_uninstall_rollback(self):
        before = self.engine.read(GRUB)
        first = self.install()
        self.engine.rollback(first['backup_id'])
        self.assertEqual(before, self.engine.read(GRUB))
        self.assertFalse(self.engine.path(CONFIG).exists())
        self.install()
        themed = self.engine.read(GRUB)
        removed = self.engine.apply(self.engine.build_plan(self.options('uninstall')))
        self.assertFalse(self.engine.path(CONFIG).exists())
        self.assertNotIn(b'Tools & Recovery', self.engine.read(GRUB))
        self.engine.rollback(removed['backup_id'])
        self.assertEqual(themed, self.engine.read(GRUB))
        self.assertTrue(self.engine.discover()['installed'])

    def test_old_rolled_back_backup_cannot_remove_a_new_install(self):
        first = self.install()
        self.engine.rollback(first['backup_id'])
        second = self.install(windows_label='New installation')
        before = self.engine._fingerprints()
        with self.assertRaisesRegex(EngineError, 'already been restored'):
            self.engine.rollback(first['backup_id'])
        self.assertEqual(before, self.engine._fingerprints())
        self.assertEqual(self.engine.discover()['installation']['backup_id'], second['backup_id'])

    def test_interrupted_recovery_accepts_fat_mount_derived_modes(self):
        before = self.engine.read(GRUB)
        result = self.install()
        folder = self.engine.path(BACKUPS + '/' + result['backup_id'])
        journal = json.loads((folder / 'journal.json').read_text())
        journal['phase'] = 'applying'
        self.engine._journal(folder, journal)
        for name in journal['originals']:
            if name.startswith(('/esp/', '/efi/')) and self.engine.path(name).exists():
                self.engine.path(name).chmod(0o700)
        self.engine.rollback(result['backup_id'])
        self.assertEqual(before, self.engine.read(GRUB))
        self.assertFalse(self.engine.path(CONFIG).exists())

    def test_corrupt_backup_refuses_before_any_destination_changes(self):
        self.install()
        result = self.engine.apply(self.engine.build_plan(self.options('update', windows_label='Updated')))
        folder = self.engine.path(BACKUPS + '/' + result['backup_id'])
        journal = json.loads((folder / 'journal.json').read_text())
        (folder / journal['originals'][STATE]['backup']).write_bytes(b'corrupt')
        before = self.engine._fingerprints()
        with self.assertRaisesRegex(EngineError, 'Recovery file checksum mismatch'):
            self.engine.rollback(result['backup_id'])
        self.assertEqual(before, self.engine._fingerprints())
        self.assertEqual(json.loads((folder / 'journal.json').read_text())['phase'], 'applied')

    def test_automatic_recovery_preserves_concurrent_external_changes(self):
        def concurrent_change(*args, **kwargs):
            self.put(CONFIG, 'EXTERNAL CHANGE\n')
            raise EngineError('Injected external edit')
        with patch.object(self.engine, '_media_verify', concurrent_change):
            with self.assertRaisesRegex(EngineError, 'recovery needs attention.*Unrecognized partial'):
                self.install()
        self.assertEqual(self.engine.read(CONFIG), b'EXTERNAL CHANGE\n')

    def test_uninstall_after_slot_upgrade_regenerates_current_kernel(self):
        installed = self.install()
        self.runner.identity['/dev/testroot'] = {'UUID': '4444-DDDD', 'PARTUUID': '4444-DDDD-4444'}
        self.runner.identity['/dev/testefi'] = {'UUID': '5555-EEEE', 'PARTUUID': '5555-EEEE-5555'}
        self.runner.kernel = 'new-fixture-kernel'
        self.put(GRUB, self.runner.generated())
        with self.assertRaisesRegex(EngineError, 'Slot or partition changed'):
            self.engine.rollback(installed['backup_id'])
        self.engine.apply(self.engine.build_plan(self.options('uninstall')))
        cfg = self.engine.read(GRUB).decode()
        self.assertIn('/boot/new-fixture-kernel', cfg)
        self.assertNotIn('Tools & Recovery', cfg)

    def test_custom_icons_survive_theme_update_and_can_reset(self):
        custom = Path(self.temporary.name) / 'user.png'
        custom.write_bytes(png(color=(150, 151, 152, 255)))
        self.install(icon_overrides={'windows': str(custom)})
        custom.unlink()
        (self.assets / 'icons/windows.png').write_bytes(png(color=(90, 91, 92, 255)))
        self.engine.apply(self.engine.build_plan(self.options('update')))
        self.assertEqual(self.engine.read(ASSETS + '/icons/windows.png'), png(color=(150, 151, 152, 255)))
        self.engine.apply(self.engine.build_plan(self.options('update', icon_overrides={'windows': None})))
        self.assertEqual(self.engine.read(ASSETS + '/icons/windows.png'), png(color=(90, 91, 92, 255)))

    def test_external_icon_change_blocks_update_and_rollback(self):
        result = self.install()
        self.put(ASSETS + '/icons/windows.png', png(color=(1, 1, 1, 255)))
        with self.assertRaisesRegex(EngineError, 'modified outside'):
            self.engine.build_plan(self.options('update'))
        with self.assertRaisesRegex(EngineError, 'Later modifications'):
            self.engine.rollback(result['backup_id'])

    def test_other_bootmanager_is_not_deleted(self):
        self.put('/esp/EFI/refind/refind_x64.efi', b'OTHER BOOT MANAGER')
        found = self.engine.discover()
        self.assertFalse(found['supported'])
        self.assertIn('Another boot manager', found['blockers'][0])
        self.assertTrue(self.engine.path('/esp/EFI/refind/refind_x64.efi').exists())

    def test_symlink_destination_and_source_are_refused(self):
        external = Path(self.temporary.name) / 'external'
        external.write_bytes(png())
        (self.assets / 'icons/windows.png').unlink()
        (self.assets / 'icons/windows.png').symlink_to(external)
        with self.assertRaisesRegex(EngineError, 'Symbolic link'):
            self.engine.build_plan(self.options())
        self.assertEqual(external.read_bytes(), png())

    def test_malformed_image_and_option_injection_rejected(self):
        with self.assertRaises((EngineError, ValueError)):
            self.engine.build_plan(self.options(windows_label='Windows\n}\nmalicious'))
        for value in (-1, 0, 61, True, '5'):
            with self.assertRaises((EngineError, ValueError)):
                self.engine.build_plan(self.options(timeout=value))
        (self.assets / 'icons/windows.png').write_bytes(b'not a PNG')
        with self.assertRaisesRegex(EngineError, 'Expected a PNG'):
            self.engine.build_plan(self.options())

    def test_native_boot_body_change_is_rejected(self):
        before = self.runner.generated()
        after = before.replace('root=UUID=fixture', 'root=UUID=unexpected')
        with self.assertRaisesRegex(EngineError, 'Native boot commands changed'):
            validate_candidate(before, after, 'uninstall')


class GeneratorTests(unittest.TestCase):
    def settings(self):
        return {'version': 1, 'esp_uuid': '2222-BBBB', 'windows_uuid': '2222-BBBB', 'windows_label': 'Windows 10', 'timeout': 5, 'default_os': 'steamos'}

    def test_primary_keeps_native_kernel_and_preamble(self):
        selected = grub.primary_entry(native_entry())
        self.assertIn('set linux_gfx_mode=', selected)
        self.assertIn('steamenv_boot linux /boot/fixture-kernel', selected)
        self.assertIn("'ice-circuit-steamos'", selected)
        self.assertNotIn('Native recovery', selected)

    def test_unknown_vendor_output_rejects_promotion(self):
        for altered in (native_entry().replace("'SteamOS'", "'Other OS'"), native_entry().replace('steamenv_boot linux', 'linux'), native_entry().replace('    initrd ', 'initrd ')):
            with self.assertRaises(ValueError):
                grub.primary_entry(altered)

    def test_flat_fallback_does_not_shift_native_indices(self):
        result = grub.main_fragment(self.settings())
        self.assertEqual(result, '')

    def test_one_shot_runs_native_menu_at_root_without_promotion(self):
        result = grub.main_fragment(self.settings(), native_entry())
        self.assertTrue(result.startswith('if [ "${boot_once}" != true ]; then'))
        self.assertTrue(result.endswith('function ice_circuit_native_menu {\n'))
        ending = grub.end_fragment()
        self.assertIn('if [ "${boot_once}" = true ]; then\n    ice_circuit_native_menu\nelse\nsubmenu', ending)

    def test_label_is_quoted_and_escaped(self):
        config = self.settings()
        config['windows_label'] = 'Windows "test" $root; \\ example'
        output = grub.windows_entry(config)
        self.assertIn('"Windows \\"test\\" \\$root; \\\\ example"', output)
        self.assertEqual(output.count('menuentry '), 1)

    def test_generator_pair_closes_even_if_settings_change_mid_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, begin, end, record = [base / name for name in ('config.json', '09_menu', 'zz_tools', 'record.json')]
            config.write_text(json.dumps(self.settings()))
            for path in (begin, end):
                path.write_text('#!/bin/sh\n')
                path.chmod(0o755)
            runner = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, native_entry(), '')
            output = io.StringIO()
            with patch.object(grub, 'CONFIG', config), patch.object(grub, 'BEGIN', begin), patch.object(grub, 'END', end), patch.object(grub, 'GENERATOR', base / '10_linux'), patch.object(grub, 'context_path', return_value=record), patch.object(grub.subprocess, 'run', runner), contextlib.redirect_stdout(output):
                grub.run('begin')
                self.assertTrue(record.exists())
                config.write_text('invalid settings introduced by a concurrent update')
                grub.run('end')
            self.assertFalse(record.exists())
            self.assertIn('function ice_circuit_native_menu {', output.getvalue())
            self.assertTrue(output.getvalue().endswith(grub.end_fragment()))

    def test_unsupported_generator_clears_stale_pair_and_emits_no_group(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, end, record = [base / name for name in ('config.json', 'zz_tools', 'record.json')]
            config.write_text(json.dumps(self.settings()))
            end.write_text('#!/bin/sh\n')
            end.chmod(0o755)
            record.write_text('{"open": true}')
            runner = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, 'Unknown future native generator layout\n', '')
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(grub, 'CONFIG', config), patch.object(grub, 'END', end), patch.object(grub, 'GENERATOR', base / '10_linux'), patch.object(grub, 'context_path', return_value=record), patch.object(grub.subprocess, 'run', runner), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                grub.run('begin')
                grub.run('end')
            self.assertEqual(output.getvalue(), '')
            self.assertFalse(record.exists())
            self.assertIn('retaining the flat native menu', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
