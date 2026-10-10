# 開発者向けガイド

一般の利用者は [GitHub Releases の導入用 ZIP](https://github.com/cobo920/steamdeck-ice-circuit/releases)と[導入手順](GETTING-STARTED.md)を使ってください。このページはコード確認・テスト・配布物作成のためのものです。

## 構成

| 場所 | 内容 |
| --- | --- |
| `src/ice_circuit/gui.py` | SteamOS の kdialog を使うデスクトップ設定画面。 |
| `src/ice_circuit/cli.py` | CLI と、短時間だけ動く管理者ヘルパー。 |
| `src/ice_circuit/engine.py` | 環境診断、計画、バックアップ、適用・復元。 |
| `src/ice_circuit/grub.py` | SteamOS 標準の起動処理を保持するメニュー生成。 |
| `src/ice_circuit/browser.py` | 導入済みブラウザーでローカル HTML を開く処理。 |
| `src/ice_circuit/image_utils.py` | 一般ユーザー権限で行う画像検証・アイコン変換。 |
| `assets/` | 配布テーマ、元アート、フォント、リソースのハッシュ。 |
| `tests/` | 一時ディレクトリや模擬コマンドを使った検証。 |
| `tools/` | アセット生成・配布パッケージ生成。 |

## ローカルで確認する

Python 3 を使い、プロジェクトのルートで実行します。自動テストに実機への書き込みや管理者権限は必要ありません。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 ice-circuit.py --help
python3 ice-circuit.py diagnose
```

`diagnose` は読み取り専用です。対応していない環境では `supported: false` と理由を返します。テスト結果と実機確認の範囲は [VALIDATION.md](VALIDATION.md) で区別しています。

SteamOS のデスクトップ上で GUI を直接起動する場合は、一般ユーザーで次を実行します。

```sh
python3 ice-circuit.py gui
```

GUI は Python 標準ライブラリ、kdialog、pkexec、SteamOS 標準の GRUB 関連ツールを使います。JPEG / SVG 変換には導入済みの ffmpeg / rsvg-convert を使います。実行時に Python パッケージをダウンロードする処理はありません。

## テーマの再生成

```sh
python3 tools/build_theme.py --help
python3 tools/build_theme.py
```

必要なフォント・ライブラリと元データは [assets/README.md](../assets/README.md) を参照してください。配布用テーマは生成済みのため、通常の導入でこの作業は不要です。

## 配布パッケージを作る

```sh
python3 tools/package_release.py --help
python3 tools/package_release.py
```

配布物と SHA256 は `dist/` に出力します。パッケージ作成時に配布対象を限定し、テーマの整合性や端末固有情報の混入を確認します。利用者向けの入口は GitHub Releases の **`Ice-Circuit-SteamDeck-<version>.zip`** です。

リリース時には ZIP 内のランチャー・同梱ガイド・ソースが揃っていること、README のバージョンとダウンロード名が一致すること、検証記録が実際の確認範囲を示していることを確認します。

### alpha.7 の配布内容

0.1.0-alpha.7 は、alpha.6 と同じインストーラ・テーマを同梱する記録更新版です。導入案内、検証記録、リリースノートと配布バージョンを更新します。検証に使った alpha.6 のタグ・配布ファイルは保持し、alpha.7 を別のリリースとして作成します。

## 実機を扱う変更

通常の計画・適用は既存の SteamOS 構成を検証し、変更前の設定をバックアップします。EFI 実行ファイルと UEFI 起動登録は変更しません。GUI は一般ユーザーで画像を変換します。管理者ヘルパーが計画を作成・保持し、利用者の確認後にその計画を適用します。

実機での確認が必要な場合は、作業用の検証機と復旧手段を用意し、隔離テストと結果を分けて記録してください。端末のバックアップ、ディスクイメージ、認証情報を公開ソースへ入れないでください。

[README に戻る](../README.md)
