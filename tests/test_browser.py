"""Installed-browser selection and launch errors, without opening a browser."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ice_circuit import browser


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.page = self.directory / "space 'quote' $(command) 日本語.html"
        self.page.write_text("<!doctype html><title>Preview</title>")
        self.default = "org.mozilla.firefox.desktop"
        self.apps = "com.google.Chrome"
        self.available = {"sh", "flatpak", "gio", "xdg-settings", "xdg-open"}
        self.addCleanup(patch.stopall)
        patch.object(browser, "application_dirs", return_value=[self.directory]).start()
        patch.object(browser.shutil, "which", side_effect=lambda value: "/usr/bin/" + Path(value).name if Path(value).name in self.available else None).start()
        self.query = patch.object(browser, "query", side_effect=lambda command: self.default if command[0] == "xdg-settings" else self.apps).start()

    def write_entry(self, name, command, extra=""):
        path = self.directory / name
        path.write_text("[Desktop Entry]\nType=Application\nName=Browser\nExec=" + command + "\n" + extra)
        return path

    def test_steam_deck_install_firefox_stub_is_skipped_for_installed_chrome(self):
        self.write_entry(self.default, "sh -c 'xdg-open appstream://org.mozilla.firefox' %U")
        candidates = list(browser.launch_candidates(self.page))
        self.assertEqual(candidates, [(["flatpak", "run", "--file-forwarding", "com.google.Chrome", "@@u", self.page.as_uri(), "@@"], "Google Chrome")])
        self.assertFalse(any("appstream:" in part for command, _ in candidates for part in command))
        self.assertEqual([call.args[0] for call in self.query.call_args_list], [["xdg-settings", "get", "default-web-browser"], ["flatpak", "list", "--app", "--columns=application"]])

    def test_normal_native_default_stays_first_and_uses_its_desktop_entry(self):
        self.default = "custom-browser.desktop"
        self.available.add("my-browser")
        desktop = self.write_entry(self.default, '/usr/bin/my-browser --profile "personal profile" %U')
        candidates = list(browser.launch_candidates(self.page))
        self.assertEqual(candidates[0], (["gio", "launch", str(desktop), self.page.as_uri()], "Browser"))
        self.assertEqual(candidates[1][1], "Google Chrome")

    def test_installed_default_flatpak_precedes_other_installed_browsers(self):
        self.apps = "com.google.Chrome\norg.mozilla.firefox"
        desktop = self.write_entry(self.default, "/usr/bin/flatpak run --file-forwarding org.mozilla.firefox @@u %U @@", "X-Flatpak=org.mozilla.firefox\n")
        candidates = list(browser.launch_candidates(self.page))
        self.assertEqual(candidates[0][0], ["gio", "launch", str(desktop), self.page.as_uri()])
        self.assertEqual(candidates[1][1], "Google Chrome")

    def test_native_firefox_with_flatpak_style_desktop_name_remains_default(self):
        self.available.add("firefox")
        desktop = self.write_entry("org.mozilla.firefox.desktop", "/usr/bin/firefox --new-tab %U")
        candidates = list(browser.launch_candidates(self.page))
        self.assertEqual(candidates[0][0], ["gio", "launch", str(desktop), self.page.as_uri()])
        self.assertEqual(candidates[1][1], "Google Chrome")

    def test_exported_chrome_launcher_retains_file_forwarding_and_options(self):
        desktop = self.write_entry("com.google.Chrome.desktop", "/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=/app/bin/chrome --file-forwarding com.google.Chrome @@u %U @@", "X-Flatpak=com.google.Chrome\n")
        candidates = list(browser.launch_candidates(self.page))
        self.assertEqual(candidates[0][0], ["gio", "launch", str(desktop), self.page.as_uri()])

    def test_uninstalled_flatpak_desktop_entry_is_not_treated_as_browser(self):
        self.apps = ""
        self.write_entry(self.default, "/usr/bin/flatpak run org.mozilla.firefox %U", "X-Flatpak=org.mozilla.firefox\n")
        with self.assertRaisesRegex(RuntimeError, "利用できるブラウザーが見つかりません"):
            browser.open_html(self.page)

    def test_desktop_identifier_cannot_escape_application_directory(self):
        self.default = "../outside.desktop"
        self.apps = ""
        self.assertEqual(list(browser.launch_candidates(self.page)), [])

    def test_malformed_default_without_exec_does_not_break_fallback(self):
        self.write_entry(self.default, "", "TryExec=sh\n")
        self.assertEqual(list(browser.launch_candidates(self.page))[0][1], "Google Chrome")

    def test_immediate_launch_failure_tries_next_browser_without_shell(self):
        candidates = [(["broken-browser", self.page.as_uri()], "Broken"), (["working-browser", self.page.as_uri()], "Working")]
        broken, working = Mock(), Mock()
        broken.wait.return_value = 1
        working.wait.return_value = 0
        with patch.object(browser, "launch_candidates", return_value=candidates), patch.object(browser.subprocess, "Popen", side_effect=[broken, working]) as launch:
            self.assertEqual(browser.open_html(self.page), "Working")
        self.assertEqual(launch.call_count, 2)
        self.assertNotIn("shell", launch.call_args.kwargs)
        self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertNotEqual(launch.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_browser_that_remains_running_is_not_terminated(self):
        running = Mock()
        running.wait.side_effect = subprocess.TimeoutExpired("browser", 2)
        with patch.object(browser, "launch_candidates", return_value=[(["browser", self.page.as_uri()], "Browser")]), patch.object(browser.subprocess, "Popen", return_value=running), patch.object(browser.threading, "Thread") as reaper:
            self.assertEqual(browser.open_html(self.page), "Browser")
        running.terminate.assert_not_called()
        running.kill.assert_not_called()
        reaper.assert_called_once_with(target=running.wait, daemon=True)
        reaper.return_value.start.assert_called_once()

    def test_all_immediate_failures_are_reported_with_captured_stderr(self):
        def fail(command, **options):
            options["stderr"].write(b"Cannot connect to display")
            process = Mock()
            process.wait.return_value = 1
            return process
        with patch.object(browser, "launch_candidates", return_value=[(["browser", self.page.as_uri()], "Browser")]), patch.object(browser.subprocess, "Popen", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "Cannot connect to display"):
                browser.open_html(self.page)


if __name__ == "__main__":
    unittest.main()
