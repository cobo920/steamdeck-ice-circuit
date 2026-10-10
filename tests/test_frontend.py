"""Desktop flow and privilege-boundary tests without root or a live device."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from ice_circuit import cli, gui
from ice_circuit.image_utils import encode_png

ROOT = Path(__file__).resolve().parents[1]


class Backend:
    def __init__(self):
        self.options = None
        self.applied = []
        self.restored = []

    def discover(self):
        return {"identity": {"test": "identity"}, "backup_ids": ["backup-test"], "installed": True}

    def build_plan(self, options):
        self.options = options
        return {"plan_id": "example", "options": options}

    def apply(self, plan):
        self.applied.append(plan)
        return {"ok": True, "backup_id": "backup-new"}

    def rollback(self, backup):
        self.restored.append(backup)
        return {"ok": True}


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.helper = cli.HelperSession(self.backend, Path("/test/stage"))

    def plan(self, **options):
        return self.helper.handle({"action": "plan", "options": {"operation": "update", **options}})

    def test_unselected_icons_are_preserved(self):
        self.plan()
        self.assertEqual(self.backend.options["icon_overrides"], {})
        self.assertEqual(self.backend.options["expected_identity"], {"test": "identity"})

    def test_explicit_replace_and_reset(self):
        self.plan(custom_icons=["windows"], reset_icons=["steamos"])
        self.assertEqual(self.backend.options["icon_overrides"], {"windows": "/test/stage/theme/icons/windows.png", "steamos": None})
        self.assertNotIn("reset_icons", self.backend.options)
        self.assertNotIn("custom_icons", self.backend.options)

    def test_apply_accepts_only_held_plan_once(self):
        answer = self.plan()
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "apply", "token": answer["token"], "plan": {}})
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "apply", "token": "0" * 48})
        self.helper.handle({"action": "apply", "token": answer["token"]})
        self.assertEqual(len(self.backend.applied), 1)
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "apply", "token": answer["token"]})

    def test_new_plan_or_close_invalidates_old_token(self):
        first = self.plan()
        second = self.plan(timeout=10)
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "apply", "token": first["token"]})
        self.helper.handle({"action": "close"})
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "apply", "token": second["token"]})

    def test_no_arbitrary_options_paths_or_operations(self):
        invalid = [{"asset_source": "/etc"}, {"icon_overrides": {"windows": "/etc/shadow"}},
                   {"custom_icons": ["../../etc"]}, {"custom_icons": ["windows", "windows"]},
                   {"custom_icons": ["windows"], "reset_icons": ["windows"]},
                   {"timeout": True}, {"timeout": 0}, {"timeout": 61},
                   {"windows_label": "x" * 33}, {"windows_label": "line\nbreak"},
                   {"operation": "format"}, {"default_os": "other"}]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(**options)
        for request in ({"action": "execute", "command": "id"}, {"action": "discover", "path": "/"}):
            with self.assertRaises(ValueError):
                self.helper.handle(request)

    def test_rollback_only_listed_backup(self):
        with self.assertRaises(ValueError):
            self.helper.handle({"action": "rollback", "backup_id": "../../etc"})
        self.helper.handle({"action": "rollback", "backup_id": "backup-test"})
        self.assertEqual(self.backend.restored, ["backup-test"])

    def test_protocol_remains_json_and_close_stops_processing(self):
        source = io.BytesIO(b'{"action":"discover"}\n{"action":"close"}\n{"action":"discover"}\n')
        output, errors = io.StringIO(), io.StringIO()
        original = self.backend.discover
        def noisy():
            print("diagnostic")
            return original()
        self.backend.discover = noisy
        with contextlib.redirect_stderr(errors):
            cli.serve_helper(self.helper, source, output)
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(x["ok"] for x in results))
        self.assertEqual(errors.getvalue(), "diagnostic\n")

    def test_malformed_and_oversized_messages(self):
        for data in (b'{"action":NaN}\n', b'not JSON\n', b'{"action":"discover"}', b' ' * (cli.MAX_MESSAGE + 1) + b'\n'):
            with self.subTest(length=len(data)):
                output = io.StringIO()
                cli.serve_helper(self.helper, io.BytesIO(data), output)
                self.assertFalse(json.loads(output.getvalue().splitlines()[0])["ok"])

    def test_partial_pipe_line_cannot_outlive_deadline(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{')
            started = time.monotonic()
            with os.fdopen(read_fd, "rb", buffering=0) as source:
                cli.serve_helper(self.helper, source, io.StringIO(), timeout=0.05)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            os.close(write_fd)

    def test_pipe_eof_and_multiple_messages(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"action":"discover"}\n{"action":"close"}\n')
        os.close(write_fd)
        output = io.StringIO()
        with os.fdopen(read_fd, "rb", buffering=0) as source:
            cli.serve_helper(self.helper, source, output, timeout=0.2)
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_staging_must_be_private_and_contain_no_links(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            (path / "theme").mkdir()
            self.assertEqual(cli.validate_stage(path, os.getuid()), path.resolve())
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                cli.validate_stage(path, os.getuid())
            path.chmod(0o700)
            (path / "theme/linked").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                cli.validate_stage(path, os.getuid())


class FakeDialogs:
    def __init__(self, menus=(), accept=True):
        self.menus = iter(menus)
        self.accept = accept
        self.messages = []
        self.confirmations = []
        self.menu_calls = []

    def menu(self, text, choices):
        self.menu_calls.append((text, choices))
        result = next(self.menus)
        if isinstance(result, Exception):
            raise result
        return result

    def info(self, text):
        self.messages.append(text)

    error = info
    notice = info

    def confirm(self, text):
        self.confirmations.append(text)
        return self.accept


class GuiTests(unittest.TestCase):
    MISSING_ROUTE = "No named SteamOS firmware entry was found. Existing fallback boot will be retained; this installer does not change firmware boot order."

    def test_supported_environment_still_shows_missing_route_diagnosis(self):
        info = {"supported": True, "installed": True, "warnings": [self.MISSING_ROUTE], "os": {"name": "SteamOS"}}
        message = gui.diagnostic_text(info)
        self.assertIn("本体が SteamOS を自動で起動する設定が見つかりません", message)
        self.assertIn("テーマ設定だけでは", message)
        self.assertIn("別の修復が必要", message)
        self.assertIn("起動順を変更しません", message)
        self.assertIn("テーマの設定を進められます", message)
        self.assertTrue(info["supported"])

    def test_missing_route_is_visible_before_apply_and_after_success(self):
        dialogs = self.run_administrative(accept=True, warnings=[self.MISSING_ROUTE])
        self.assertEqual(len(dialogs.confirmations), 1)
        self.assertTrue(dialogs.confirmations[0].startswith("本体が SteamOS を自動で起動する設定が見つかりません"))
        self.assertIn("テーマ設定だけでは", dialogs.messages[-1])
        self.assertIn("起動経路を確認してから", dialogs.messages[-1])

    def test_plan_routing_warning_also_reaches_existing_confirmation(self):
        dialogs = self.run_administrative(accept=False, plan_warnings=[self.MISSING_ROUTE])
        self.assertTrue(dialogs.confirmations[0].startswith("本体が SteamOS を自動で起動する設定が見つかりません"))

    def test_failed_recovery_is_not_reported_as_success_and_raw_details_survive(self):
        cases = (
            ("Operation failed (Permission denied); recovery needs attention (disk failure). Backup: abc", "自動復元を完了できません"),
            ("Operation failed and previous files were restored: Native generator failed. Backup: abc", "変更前の状態へ戻しました"),
            ("Native files changed since this backup; use a current uninstall plan", "このバックアップへは戻せません"),
            ("Incomplete or unsupported installation state; use its recorded rollback first", "バックアップから戻す"),
            ("Another boot manager or unknown EFI folder is present: rEFInd", "別の起動メニュー"),
            ("GRUB cannot read the installed asset from media: /EFI/ice-circuit/icons/windows.png", "処理を完了できません"),
        )
        for raw, summary in cases:
            with self.subTest(raw=raw):
                message = gui.error_text(RuntimeError(raw))
                self.assertIn(summary, message)
                self.assertTrue(message.endswith("詳細（問い合わせ用・原文）：\n" + raw))
        self.assertEqual(gui.error_text(ValueError("1〜60の数字を入力してください。")), "1〜60の数字を入力してください。")

    def test_help_opens_packaged_guide_without_admin_and_failure_names_file(self):
        with tempfile.TemporaryDirectory() as name:
            guide = Path(name) / "START-HERE.html"
            guide.write_text("<!doctype html><title>Guide</title>")
            dialogs = FakeDialogs(["help", "quit"])
            app = gui.App(dialogs)
            with patch.object(gui, "ROOT", Path(name)), patch.object(gui, "open_html") as browser, patch.object(gui, "AdminHelper") as helper:
                self.assertEqual(app.run(), 0)
                browser.assert_called_once_with(guide)
                helper.assert_not_called()
            app = gui.App()
            try:
                with patch.object(gui, "ROOT", Path(name)), patch.object(gui, "open_html", side_effect=RuntimeError("No browser: test detail")):
                    with self.assertRaises(RuntimeError) as raised:
                        app.help_page()
                self.assertIn(str(guide), str(raised.exception))
                self.assertIn("Chrome や Firefox", str(raised.exception))
                self.assertIn("No browser: test detail", str(raised.exception))
                guide.unlink()
                with patch.object(gui, "ROOT", Path(name)), patch.object(gui, "open_html") as browser:
                    with self.assertRaisesRegex(RuntimeError, "ZIP をすべて展開"):
                        app.help_page()
                    browser.assert_not_called()
            finally:
                app.session.cleanup()

    def test_canceling_password_explanation_never_starts_helper(self):
        class CancelDialogs(FakeDialogs):
            def info(self, text):
                self.messages.append(text)
                raise gui.Cancelled()
        dialogs = CancelDialogs(["uninstall", "quit"])
        app = gui.App(dialogs)
        with patch.object(gui, "stage_assets", wraps=gui.stage_assets) as staged, patch.object(gui, "AdminHelper") as helper:
            self.assertEqual(app.run(), 0)
            helper.assert_not_called()
            self.assertFalse(Path(staged.call_args.args[0]).exists())
        self.assertIn("Steam Deck 本体のユーザーパスワード", dialogs.messages[0])
        self.assertIn("Windows のパスワード・PIN ではありません", dialogs.messages[0])

    def test_uninstall_and_rollback_review_exact_operation_before_applying(self):
        backups = ["20261009T120000-012345abcdef", "20261010T120000-abcdef012345"]
        for action in ("uninstall", "rollback"):
            for accept in (False, True):
                with self.subTest(action=action, accept=accept):
                    calls, stages, exits = [], [], []
                    class Helper:
                        def __init__(self, stage):
                            stages.append(Path(stage))
                        def __enter__(self):
                            return self
                        def __exit__(self, *_):
                            exits.append(True)
                        def request(self, operation, **fields):
                            calls.append((operation, fields))
                            return {"discover": {"installed": True, "backup_ids": backups},
                                    "plan": {"token": "reviewed", "plan": {}},
                                    "apply": {"backup_id": backups[-1]},
                                    "rollback": {"backup_id": backups[0]}}[operation]
                    dialogs = FakeDialogs([backups[0]], accept=accept)
                    app = gui.App(dialogs)
                    try:
                        with patch.object(gui, "AdminHelper", Helper):
                            app.administrative(action)
                    finally:
                        app.session.cleanup()
                    self.assertEqual(exits, [True])
                    self.assertFalse(stages[0].exists())
                    self.assertIn("SteamOS・Windows・ゲーム・個人データは削除しません", dialogs.confirmations[0])
                    if action == "uninstall":
                        self.assertEqual(calls[1][1]["options"]["operation"], "uninstall")
                        self.assertIn("Ice Circuit が追加した Windows", dialogs.confirmations[0])
                        expected = ["discover", "plan", "apply"] if accept else ["discover", "plan"]
                    else:
                        expected = ["discover", "rollback"] if accept else ["discover"]
                        choices = dialogs.menu_calls[0][1]
                        self.assertEqual([key for key, _ in choices], list(reversed(backups)))
                        self.assertRegex(choices[0][1], r"\d{4}年\d{2}月\d{2}日 \d{2}:\d{2}:\d{2} の変更前")
                        self.assertIn("改めて設定できます", dialogs.confirmations[0])
                        if accept:
                            self.assertEqual(calls[-1][1], {"backup_id": backups[0]})
                            self.assertIn("設定をやり直す場合", dialogs.messages[-1])
                    self.assertEqual([call[0] for call in calls], expected)

    def test_helper_close_drains_response_before_staging_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            stage = Path(name) / "stage"
            stage.mkdir()
            marker = Path(name) / "observed.txt"
            code = "import pathlib,sys,time; sys.stdin.readline(); time.sleep(0.08); pathlib.Path(sys.argv[2]).write_text(str(pathlib.Path(sys.argv[1]).is_dir())); print('x'*200000)"
            helper = gui.AdminHelper.__new__(gui.AdminHelper)
            helper.log = tempfile.TemporaryFile(mode="w+t")
            helper.process = subprocess.Popen(["/usr/bin/python3", "-I", "-c", code, str(stage), str(marker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=helper.log, text=True)
            helper.close()
            self.assertEqual(helper.process.returncode, 0)
            self.assertEqual(marker.read_text(), "True")

    def test_applying_waits_for_transaction_response(self):
        helper = gui.AdminHelper.__new__(gui.AdminHelper)
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO('{"ok":true,"result":{"backup_id":"test"}}\n')
        helper.process = Process()
        with patch.object(gui.select, "select", return_value=([Process.stdout], [], [])) as selected:
            self.assertEqual(helper.request("apply", token="reviewed"), {"backup_id": "test"})
        self.assertIsNone(selected.call_args.args[3])

    def test_preview_escapes_labels_and_embeds_chosen_image(self):
        icon = encode_png(72, 72, bytes((201, 110, 51, 255)) * (72 * 72))
        settings = gui.Settings(windows_label='<img src="http://x">', timeout=8, default_os="windows", icons={"windows": icon}, existing=True)
        page = gui.preview_html(settings, ROOT / "assets/theme")
        self.assertIn('&lt;img src=&quot;http://x&quot;&gt;', page)
        self.assertNotIn('<img src="http://x">', page)
        self.assertIn(base64.b64encode(icon).decode(), page)
        self.assertIn("待ち時間：8秒", page)
        self.assertIn("既存の画像は保持", page)
        self.assertEqual(page.count('class="row selected"'), 1)
        self.assertIn("default-src 'none'", page)

    def test_config_initializes_settings_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as name:
            config = Path(name) / "config.json"
            config.write_text(json.dumps({"windows_label": "Windows 10", "timeout": 9, "default_os": "windows"}))
            actual = gui.initial_settings(config)
            self.assertEqual((actual.windows_label, actual.timeout, actual.default_os, actual.existing), ("Windows 10", 9, "windows", True))
            config.write_text('{"windows_label":"Windows","timeout":false,"default_os":"windows"}')
            self.assertEqual(gui.initial_settings(config), gui.Settings())

    def test_brand_page_only_informational_official_links(self):
        with tempfile.TemporaryDirectory() as name:
            page = Path(name) / "brands.html"
            gui.write_brand_page(page)
            text = page.read_text()
            self.assertIn("各社のロゴ・利用条件を確認", text)
            self.assertIn("利用許諾が付与されるものではありません", text)
            self.assertEqual(text.count('href="'), 3)
            self.assertNotIn("<script", text)
            self.assertNotIn("<img", text)
            for _, link in gui.BRAND_LINKS:
                self.assertTrue(link.startswith(("https://partner.steamgames.com/doc/", "https://www.microsoft.com/en-us/legal/")))

    def test_closing_main_window_exits_and_cleans_temporary_files(self):
        app = gui.App(FakeDialogs([gui.Cancelled()]))
        directory = app.directory
        self.assertEqual(app.run(), 0)
        self.assertFalse(directory.exists())

    def test_canceling_configuration_returns(self):
        app = gui.App(FakeDialogs([gui.Cancelled()]))
        try:
            app.configure()
        finally:
            app.session.cleanup()

    def test_review_decline_never_applies_and_stage_is_private(self):
        self.run_administrative(accept=False)

    def test_review_accept_uses_same_helper_and_held_token(self):
        self.run_administrative(accept=True)

    def run_administrative(self, accept, warnings=(), plan_warnings=()):
        requests, stages = [], []
        testcase = self
        class FakeHelper:
            def __init__(self, stage):
                stages.append(Path(stage))
                testcase.assertEqual(Path(stage).stat().st_mode & 0o077, 0)
                testcase.assertTrue((Path(stage) / "theme/theme.txt").is_file())
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def request(self, action, **fields):
                requests.append((action, fields))
                return {"discover": {"installed": True, "supported": True, "warnings": list(warnings)}, "plan": {"token": "review-token", "plan": {"warnings": list(plan_warnings)}}, "apply": {"backup_id": "backup-new"}}[action]
        dialogs = FakeDialogs(accept=accept)
        app = gui.App(dialogs)
        app.settings = gui.Settings(windows_label="Windows 10", timeout=7, reset_icons={"windows"}, existing=True)
        try:
            with patch.object(gui, "AdminHelper", FakeHelper):
                app.administrative("configure")
        finally:
            app.session.cleanup()
        self.assertEqual(len(stages), 1)
        self.assertFalse(stages[0].exists())
        self.assertIn("Windows 10 の画像：標準画像に戻す", dialogs.confirmations[0])
        self.assertEqual([x[0] for x in requests], ["discover", "plan", "apply"] if accept else ["discover", "plan"])
        self.assertEqual(requests[1][1]["options"]["reset_icons"], ["windows"])
        if accept:
            self.assertEqual(requests[-1][1], {"token": "review-token"})
            self.assertIn("起動メニューが読み込まれると反映されます", dialogs.messages[-1])
            self.assertNotIn("次回の起動から有効", dialogs.messages[-1])
        return dialogs


class LauncherTests(unittest.TestCase):
    def test_desktop_launcher_handles_spaces_quotes_and_file_uri(self):
        command = next(line[5:] for line in (ROOT / "Install Ice Circuit.desktop").read_text().splitlines() if line.startswith("Exec="))
        arguments = shlex.split(command)
        self.assertNotIn("sh", arguments)
        self.assertIn("-I", arguments)
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name) / "space 'quote' $(touch INJECTION) `touch INJECTION2`"
            directory.mkdir()
            desktop = directory / "Install Ice Circuit.desktop"
            desktop.write_text("placeholder")
            (directory / "ice-circuit.py").write_text("import json,sys; print(json.dumps(sys.argv))")
            for value in (str(desktop), desktop.as_uri()):
                result = subprocess.run([value if arg == "%k" else arg for arg in arguments], cwd=name, capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(result.stdout), [str(directory / "ice-circuit.py"), "gui"])
            self.assertFalse((Path(name) / "INJECTION").exists())
            self.assertFalse((Path(name) / "INJECTION2").exists())

    def test_isolated_launcher_help_works_outside_project(self):
        result = subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "ice-circuit.py"), "--help"], cwd="/tmp", capture_output=True, text=True, check=True)
        self.assertIn("diagnose", result.stdout)
        self.assertIn("rollback", result.stdout)


if __name__ == "__main__":
    unittest.main()
