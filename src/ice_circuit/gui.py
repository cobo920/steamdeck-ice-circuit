"""Small Desktop Mode setup using the dialogs already supplied by SteamOS."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile

from .cli import validate_options
from .browser import open_html
from .image_utils import ImageError, SUPPORTED_SUFFIXES, normalize_image

ROOT = Path(__file__).resolve().parents[2]
TITLE = "Ice Circuit"
BRAND_LABEL = "各社のロゴ・利用条件を確認"
BRAND_LINKS = (
    ("Steam のブランド案内", "https://partner.steamgames.com/doc/marketing/branding"),
    ("Steam Deck のブランド案内", "https://partner.steamgames.com/doc/steamhardware/steamdeck/brandguidelines"),
    ("Microsoft の商標・ブランド案内", "https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks"),
)


class Cancelled(Exception):
    pass


class Dialogs:
    def run(self, *args, cancel=True):
        result = subprocess.run(["kdialog", "--title", TITLE, *args], capture_output=True, text=True)
        if result.returncode == 1 and cancel:
            raise Cancelled()
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "画面を開けませんでした。")
        return result.stdout.strip()

    def menu(self, text, choices):
        return self.run("--menu", text, *(word for pair in choices for word in pair))

    def info(self, text):
        self.run("--msgbox", text)

    def error(self, text):
        self.run("--error", text)

    def confirm(self, text):
        result = subprocess.run(["kdialog", "--title", TITLE, "--yes-label", "適用する", "--no-label", "戻る", "--yesno", text])
        return result.returncode == 0

    def notice(self, text):
        subprocess.run(["kdialog", "--title", TITLE, "--passivepopup", text, "6"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class Settings:
    windows_label: str = "Windows"
    timeout: int = 5
    default_os: str = "steamos"
    icons: dict = field(default_factory=dict)
    reset_icons: set = field(default_factory=set)
    existing: bool = False

    def options(self, operation):
        return {"operation": operation, "windows_label": self.windows_label, "timeout": self.timeout,
                "default_os": self.default_os, "custom_icons": sorted(self.icons), "reset_icons": sorted(self.reset_icons)}


def initial_settings(config=Path("/etc/ice-circuit/config.json")):
    settings = Settings()
    try:
        if config.stat().st_size > 8192:
            return settings
        value = json.loads(config.read_text())
        candidate = {"operation": "update", **{key: value[key] for key in ("windows_label", "timeout", "default_os")}}
        checked = validate_options(candidate)
        settings.windows_label, settings.timeout, settings.default_os = checked["windows_label"], checked["timeout"], checked["default_os"]
        settings.existing = True
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return settings


def png_uri(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def preview_html(settings, assets):
    """All images are embedded; labels are escaped and no network resources load."""
    assets = Path(assets)
    icon = lambda name: png_uri(settings.icons.get(name, (assets / "icons" / (name + ".png")).read_bytes()))
    labels = {"steamos": "SteamOS", "windows": settings.windows_label}
    rows = []
    for name in ("steamos", "windows"):
        selected = " selected" if settings.default_os == name else ""
        rows.append(f'<div class="row{selected}"><img src="{icon(name)}" alt=""><span>{html.escape(labels[name])}</span></div>')
    tools = png_uri((assets / "icons/tools.png").read_bytes())
    rows.append(f'<div class="row tools"><img src="{tools}" alt=""><span>Tools &amp; Recovery</span></div>')
    note = "既存の画像は保持します。変更していない画像は、このプレビューでは標準画像で表示しています。" if settings.existing else ""
    return f'''<!doctype html><html lang="ja"><meta charset="utf-8"><title>Ice Circuit — プレビュー</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<style>body{{background:#080b10;color:#d3e1ed;font:16px sans-serif;margin:24px}}h1{{font-size:22px}}.screen{{position:relative;width:min(100%,1280px);aspect-ratio:8/5;background:url('{png_uri((assets/'background.png').read_bytes())}') center/100% 100% no-repeat}}.menu{{position:absolute;left:35.15625%;top:29.75%;width:61.09375%;display:flex;flex-direction:column;gap:10px}}.row{{height:clamp(40px,8.75vw,112px);box-sizing:border-box;border:1px solid #293e4c;background:#0d141c;display:flex;align-items:center;padding:0 4.6%;gap:4%;font-size:clamp(13px,2.5vw,32px)}}.row.selected{{border-color:#b0d9ed;background:#173040;color:white;box-shadow:inset 3px 0 #c0e1ef}}.row img{{width:9.2%;height:auto;max-height:72px;object-fit:contain}}.tools img{{opacity:.65}}.summary{{line-height:1.8}}.note{{color:#9fb2c1;max-width:1000px}}</style>
<h1>起動メニューのプレビュー</h1><p class="summary">自動起動：{html.escape(labels[settings.default_os])} ／ 待ち時間：{settings.timeout}秒</p>
<div class="screen"><div class="menu">{''.join(rows)}</div></div><p class="note">実際の起動画面とは文字の形や余白が多少異なります。{html.escape(note)}</p><p>この画面を閉じて、セットアップへ戻ってください。</p></html>'''


def write_brand_page(path):
    links = "".join(f'<li><a href="{html.escape(url, quote=True)}" rel="noreferrer">{html.escape(label)}</a></li>' for label, url in BRAND_LINKS)
    Path(path).write_text(f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>{BRAND_LABEL}</title><style>body{{max-width:760px;margin:50px auto;padding:20px;background:#0b1118;color:#d9e6ef;font:18px/1.8 sans-serif}}a{{color:#aad9f2}}</style><h1>{BRAND_LABEL}</h1><p>ロゴや名称の扱いについては、それぞれの公式案内を確認してください。リンク先を確認するだけで利用許諾が付与されるものではありません。</p><ul>{links}</ul><p>セットアップには汎用のゲームパッドとモニターのアイコンを同梱しています。</p></html>')


class AdminHelper:
    def __init__(self, stage):
        self.log = tempfile.TemporaryFile(mode="w+t")
        self.process = subprocess.Popen(["pkexec", "/usr/bin/python3", "-I", str(ROOT / "ice-circuit.py"), "helper", "--stage", str(Path(stage).resolve())],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)

    def request(self, action, **fields):
        request = json.dumps({"action": action, **fields}, ensure_ascii=False)
        self.process.stdin.write(request + "\n")
        self.process.stdin.flush()
        # Once a change starts, the backend must finish or restore its backup.
        # Keep its staged files alive throughout that transaction.
        wait = None if action in {"apply", "rollback"} else 180
        if not select.select([self.process.stdout], [], [], wait)[0]:
            raise RuntimeError("確認に時間がかかっています。画面を閉じて、しばらくしてから状態を確認してください。")
        response = self.process.stdout.readline(1024 * 1024)
        if not response:
            raise RuntimeError("管理者認証が取り消されたか、設定を確認できませんでした。")
        message = json.loads(response)
        if not message.get("ok"):
            raise RuntimeError(message.get("error", "処理を完了できませんでした。"))
        return message["result"]

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.write('{"action":"close"}\n')
                self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        finally:
            # communicate closes stdin (EOF), drains a pending response, and
            # waits for the helper. A timeout here could delete staging while
            # a transaction was still using it.
            try:
                self.process.communicate()
            finally:
                self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def stage_assets(directory, settings):
    target = Path(directory) / "theme"
    shutil.copytree(ROOT / "assets/theme", target)
    for name, data in settings.icons.items():
        (target / "icons" / (name + ".png")).write_bytes(data)
    return target


def boot_route_notice(info):
    """Explain the backend's routing warning without treating it as a blocker."""
    if any(isinstance(warning, str) and warning.startswith("No named SteamOS firmware entry")
           for warning in info.get("warnings", [])):
        return ("本体が SteamOS を自動で起動する設定が見つかりません。\n"
                "テーマ設定だけでは、電源を入れてもメニューへ進めない場合があります。\n"
                "本体の起動設定は別の修復が必要です。Ice Circuit はこの設定や起動順を変更しません。")
    return ""


def error_text(error):
    """Explain common refusals without losing the exact diagnostic message."""
    raw = str(error)
    if "recovery needs attention" in raw:
        summary = ("設定変更と自動復元を完了できませんでした。\n"
                   "再起動する前に、この詳細を控えてください。記載のバックアップを使う復旧が必要です。")
    elif "previous files were restored" in raw:
        summary = "設定を適用できなかったため、変更前の状態へ戻しました。\n詳細を控え、メイン画面の「状態を確認」で状況を確認してください。"
    elif "Slot or partition changed" in raw or "Native files changed since this backup" in raw:
        summary = ("OS の更新などで起動設定が変わっているため、このバックアップへは戻せません。\n"
                   "SteamOS 標準の画面へ戻したい場合は、メイン画面の「Ice Circuit を取り外す」を選んでください。")
    elif "already been restored" in raw:
        summary = "このバックアップは復元済み、または復元できない状態です。\nメイン画面の「状態を確認」で現在の設定を確認してください。"
    elif "Incomplete or unsupported installation state" in raw:
        summary = "前回の設定が途中で止まっているか、現在の版では扱えない状態です。\nメイン画面の「バックアップから戻す」で、前回の変更前へ戻せるか確認してください。"
    elif any(value in raw for value in ("modified outside Ice Circuit", "Later modifications prevent rollback", "Unrecognized partial transaction", "Additional assets prevent rollback")):
        summary = "保存後に別の操作で設定や画像が変更されているため、上書きを停止しました。\n詳細を控えて、変更内容を確認してください。"
    elif any(value in raw for value in ("Another boot manager", "Unrecognized GRUB customization", "requires manual migration", "not the native SteamOS chainloader")):
        summary = "別の起動メニューや独自の起動設定が見つかりました。\nこの環境にはそのまま導入できません。既存の設定を確認してください。"
    elif "changed since review" in raw or "changed while" in raw:
        summary = "確認後に設定や画像が変わったため、適用を停止しました。\nセットアップを開き直して、変更内容をもう一度確認してください。"
    elif "Another Ice Circuit operation" in raw:
        summary = "別の Ice Circuit の処理が進んでいます。\n先に開いたセットアップの完了を待ってから、やり直してください。"
    elif "Insufficient" in raw and "space" in raw:
        summary = "設定の保存やバックアップに必要な空き容量が足りません。\n詳細を控えて、端末の空き容量を確認してください。"
    elif any(value in raw for value in ("Unsupported native", "Native generator", "Native boot commands", "supports SteamOS on Steam Deck only", "Unsupported SteamOS/ESP")):
        summary = "この SteamOS の起動設定は、現在の Ice Circuit では扱えません。\nSteamOS と Ice Circuit のバージョンを添えて、詳細を確認してください。"
    elif "readonly" in raw:
        summary = "SteamOS のシステム保護設定を確認できませんでした。\n独自の変更をしている場合は、標準の保護状態に戻してからやり直してください。"
    elif isinstance(error, ImageError):
        summary = "この画像を取り込めませんでした。\n別の画像を選ぶか、画像編集アプリで通常の PNG として保存してから選んでください。"
    elif "Permission denied" in raw or "Administrator" in raw:
        summary = "必要なファイルを確認する権限がありません。\n管理者認証の状況と、詳細に表示されたファイルを確認してください。"
    elif any("\u3040" <= character <= "\u9fff" for character in raw):
        return raw
    else:
        summary = "処理を完了できませんでした。\nこの詳細を控え、メイン画面の「状態を確認」で状況を確認してください。"
    return summary + "\n\n詳細（問い合わせ用・原文）：\n" + raw


def diagnostic_text(info):
    os_info = info.get("os", {})
    lines = ["環境：" + " ".join(str(os_info.get(x, "")) for x in ("name", "version")),
             "Ice Circuit：" + ("設定済み" if info.get("installed") else "未設定、または詳細確認前")]
    notice = boot_route_notice(info)
    if notice:
        lines.append(notice)
    if info.get("requires_privilege"):
        lines.append("起動設定の詳しい確認には管理者認証が必要です。適用前にも確認します。")
    elif info.get("supported"):
        lines.append("テーマの設定を進められます。起動経路は別に確認してください。" if notice else "この環境で設定を進められます。")
    else:
        lines.append("この環境には今のまま適用できません。")
        lines.extend(error_text(blocker) for blocker in info.get("blockers", []))
    return "\n\n".join(lines)


def backup_label(identifier):
    try:
        created = datetime.strptime(identifier.split("-", 1)[0], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone()
        return created.strftime("%Y年%m月%d日 %H:%M:%S") + " の変更前  (" + identifier[-6:] + ")"
    except ValueError:
        return identifier


class App:
    def __init__(self, dialogs=None):
        self.dialogs = dialogs or Dialogs()
        self.settings = initial_settings()
        self.session = tempfile.TemporaryDirectory(prefix="ice-circuit-preview-")
        self.directory = Path(self.session.name)

    def preview(self):
        path = self.directory / "preview.html"
        path.write_text(preview_html(self.settings, ROOT / "assets/theme"))
        open_html(path)

    def brand_page(self):
        path = self.directory / "brands.html"
        write_brand_page(path)
        open_html(path)

    def help_page(self):
        path = ROOT / "START-HERE.html"
        if not path.is_file():
            raise RuntimeError("導入ガイドが見つかりません。ダウンロードした ZIP をすべて展開し、そのフォルダーからセットアップを開き直してください。\n\n見つからないファイル：" + str(path))
        try:
            open_html(path)
        except (OSError, RuntimeError) as error:
            raise RuntimeError("導入ガイドをブラウザーで開けませんでした。\n展開したフォルダーの START-HERE.html を、Chrome や Firefox などで開いてください。\n\nファイル：" + str(path) + "\n\n詳細（問い合わせ用・原文）：\n" + str(error)) from error

    def choose_icon(self, name):
        label = "SteamOS" if name == "steamos" else self.settings.windows_label
        choice = self.dialogs.menu(label + " の画像", [("choose", "利用できる画像を選ぶ"), ("reset", "標準の画像に戻す"), ("back", "戻る")])
        if choice == "choose":
            patterns = " ".join("*" + suffix for suffix in SUPPORTED_SUFFIXES)
            selected = self.dialogs.run("--getopenfilename", str(Path.home()), patterns + "|画像")
            self.settings.icons[name] = normalize_image(Path(selected))
            self.settings.reset_icons.discard(name)
            self.preview()
        elif choice == "reset":
            self.settings.icons.pop(name, None)
            self.settings.reset_icons.add(name)

    def configure(self, repair=False):
        while True:
            try:
                s = self.settings
                chosen = self.dialogs.menu("起動メニューの設定\n画像は、ご自身が利用できるものを選んでください。", [
                    ("label", "Windows の表示名：" + s.windows_label),
                    ("default", "自動起動：" + ("SteamOS" if s.default_os == "steamos" else s.windows_label)),
                    ("timeout", f"待ち時間：{s.timeout}秒"),
                    ("steamos", "SteamOS の画像を選ぶ"), ("windows", "Windows の画像を選ぶ"),
                    ("preview", "今の設定をプレビュー"), ("brands", BRAND_LABEL),
                    ("apply", "変更内容を確認して適用"), ("back", "戻る")])
            except Cancelled:
                return
            try:
                if chosen == "back":
                    return
                if chosen == "label":
                    s.windows_label = self.dialogs.menu("Windows の表示名を選んでください。", [(x, x) for x in ("Windows", "Windows 10", "Windows 11")])
                elif chosen == "default":
                    s.default_os = self.dialogs.menu("操作しなかったときに起動するOS", [("steamos", "SteamOS"), ("windows", s.windows_label)])
                elif chosen == "timeout":
                    text = self.dialogs.run("--inputbox", "メニューの待ち時間（1〜60秒）", str(s.timeout))
                    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 60:
                        raise ValueError("1〜60の数字を入力してください。")
                    s.timeout = int(text)
                elif chosen in ("steamos", "windows"):
                    self.choose_icon(chosen)
                elif chosen == "preview":
                    self.preview()
                elif chosen == "brands":
                    self.brand_page()
                elif chosen == "apply":
                    self.administrative("repair" if repair else "configure")
                    return
            except Cancelled:
                continue
            except (ValueError, OSError, RuntimeError) as error:
                self.dialogs.error(error_text(error))

    def administrative(self, action):
        with tempfile.TemporaryDirectory(prefix="ice-circuit-stage-") as stage:
            stage_assets(stage, self.settings)
            self.dialogs.info("次に、Steam Deck 本体のユーザーパスワードで管理者認証を行います。\n"
                              "Steam アカウントや Windows のパスワード・PIN ではありません。\n\n"
                              "まだ本体のパスワードを設定していない場合は、いったん戻って導入ガイドを確認してください。\n"
                              + ("この操作は状態の確認だけを行い、起動設定は変更しません。" if action == "diagnose" else
                                 "認証の後に変更内容を表示します。最後の確認で「適用する」を選ぶまで、起動設定は変更しません。"))
            with AdminHelper(stage) as helper:
                info = helper.request("discover")
                route_notice = boot_route_notice(info)
                if action == "diagnose":
                    self.dialogs.info(diagnostic_text(info))
                    return
                if action == "rollback":
                    backups = info.get("backup_ids", [])
                    if not backups:
                        self.dialogs.info("戻せるバックアップが見つかりませんでした。")
                        return
                    selected = self.dialogs.menu("取り消したい変更の日時を選んでください。\nその変更を行う直前の設定へ戻します（新しい順・本体の時刻）。", [(x, backup_label(x)) for x in reversed(backups)])
                    summary = ("次の時点の起動メニューと画像へ戻します。\n" + backup_label(selected) + "\n\n"
                               "SteamOS・Windows・ゲーム・個人データは削除しません。\n"
                               "復元後は「インストール・設定変更」から、改めて設定できます。")
                    if route_notice:
                        summary = route_notice + "\n\n" + summary
                    if not self.dialogs.confirm(summary):
                        return
                    result = helper.request("rollback", backup_id=selected)
                else:
                    if action == "repair" and not info.get("installed"):
                        raise ValueError("修復できるIce Circuitの設定がありません。インストールから進めてください。")
                    operation = "uninstall" if action == "uninstall" else ("update" if info.get("installed") else "install")
                    pending = helper.request("plan", options=self.settings.options(operation))
                    route_notice = boot_route_notice(pending.get("plan", {})) or route_notice
                    if operation == "uninstall":
                        summary = ("Ice Circuit の起動メニューとテーマを取り外し、SteamOS 標準の起動設定へ戻します。\n\n"
                                   "SteamOS・Windows・ゲーム・個人データは削除しません。\n"
                                   "Ice Circuit が追加した Windows の選択項目も取り外されます。\n"
                                   "現在の設定はバックアップに残ります。")
                    else:
                        default = "SteamOS" if self.settings.default_os == "steamos" else self.settings.windows_label
                        images = []
                        for name, label in (("steamos", "SteamOS"), ("windows", self.settings.windows_label)):
                            status = "選んだ画像に変更" if name in self.settings.icons else ("標準画像に戻す" if name in self.settings.reset_icons else ("現在の画像を保持" if info.get("installed") else "標準画像"))
                            images.append(label + " の画像：" + status)
                        summary = f"Windows の表示名：{self.settings.windows_label}\n自動起動：{default}\n待ち時間：{self.settings.timeout}秒\n" + "\n".join(images) + "\n\n通常の選択画面と、文字のない回路背景を設定します。\n現在の設定をバックアップしてから反映します。"
                    if route_notice:
                        summary = route_notice + "\n\n" + summary
                    if not self.dialogs.confirm(summary):
                        return
                    self.dialogs.notice("設定を反映しています。完了するまでお待ちください。")
                    result = helper.request("apply", token=pending["token"])
                self.settings = initial_settings()
                if action == "rollback":
                    message = "選んだ変更の前の設定へ戻しました。\n設定をやり直す場合は「インストール・設定変更」を選んでください。"
                elif action == "uninstall":
                    message = "Ice Circuit を取り外しました。SteamOS・Windows・ゲーム・個人データはそのままです。\n戻したい場合は「バックアップから戻す」を選んでください。"
                else:
                    message = "設定を保存しました。SteamOS の起動メニューが読み込まれると反映されます。"
                if route_notice:
                    message += "\n\n" + route_notice
                backup_id = result.get("backup_id", "")
                message += "\n\n" + ("復元したバックアップ：" if action == "rollback" else "今回の変更を戻すバックアップ：") + backup_label(backup_id)
                message += "\n\n起動経路を確認してから、ご自身のタイミングで再起動してください。" if route_notice else "\n\n再起動はご自身のタイミングで行ってください。"
                self.dialogs.info(message)

    def run(self):
        try:
            while True:
                try:
                    action = self.dialogs.menu("SteamOS と Windows が入った Steam Deck の起動メニューを設定します。\n"
                                               "はじめての方は「使い方・困ったとき」を確認してから\n"
                                               "「インストール・設定変更」を選んでください。", [
                        ("configure", "インストール・設定変更"), ("diagnose", "状態を確認"),
                        ("repair", "設定を修復"), ("rollback", "バックアップから戻す"),
                        ("uninstall", "Ice Circuit を取り外す"), ("help", "使い方・困ったとき"),
                        ("brands", BRAND_LABEL), ("quit", "終了")])
                except Cancelled:
                    return 0
                try:
                    if action == "quit":
                        return 0
                    if action in ("configure", "repair"):
                        self.configure(action == "repair")
                    elif action == "brands":
                        self.brand_page()
                    elif action == "help":
                        self.help_page()
                    elif action == "diagnose":
                        from .engine import Engine
                        info = Engine().discover()
                        self.dialogs.info(diagnostic_text(info))
                        if info.get("requires_privilege") and self.dialogs.menu("詳しい状態も確認できます。", [("full", "管理者認証して詳しく確認"), ("back", "戻る")]) == "full":
                            self.administrative("diagnose")
                    else:
                        self.administrative(action)
                except Cancelled:
                    continue
                except (ValueError, OSError, RuntimeError) as error:
                    self.dialogs.error(error_text(error))
        finally:
            self.session.cleanup()


def main():
    if os.geteuid() == 0:
        print("セットアップ画面は一般ユーザーで起動してください。", file=sys.stderr)
        return 1
    if not shutil.which("kdialog") or not shutil.which("pkexec"):
        print("Steam Deckのデスクトップモードから起動してください（kdialog / pkexec が必要です）。", file=sys.stderr)
        return 1
    return App().run()
