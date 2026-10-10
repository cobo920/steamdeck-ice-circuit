Steam Deck の起動メニューを、黒とアイスブルーの回路デザインに。SteamOS と Windows を選びやすくし、詳細起動・復旧項目は「Tools & Recovery」にまとめます。

**ダウンロードするのは、下の Assets にある `Ice-Circuit-SteamDeck-0.1.0-alpha.7.zip` です。** GitHub が自動表示する「Source code (zip)」「Source code (tar.gz)」は選ばなくて大丈夫です。

1. Steam Deck をデスクトップモードにして、ZIP をダウンロードします。
2. ZIP を展開し、`ice-circuit-0.1.0-alpha.7` フォルダーを開きます。
3. `START-HERE.html` の案内を読み、`Install Ice Circuit.desktop` から設定を始めます。Git の操作は不要です。管理者パスワードが未設定の場合の準備は、導入ガイドをご覧ください。

[導入ガイド](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/GETTING-STARTED.md) · [困ったときは](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/RECOVERY.md) · [検証済みの範囲](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/VALIDATION.md)

## alpha.7 について

alpha.6 と同じインストーラ・テーマに、導入案内と実機検証の記録を更新した版です。インストーラの機能変更はありません。検証に使った alpha.6 のタグと配布ファイルはそのまま保持しています。

## 確認できたこと

Steam Deck LCD・SteamOS 3.8.16・Windows 10 の検証機で確認しています。

- 利用者が Dolphin で alpha.6 の ZIP を展開し、ランチャーからセットアップと同梱ガイドを表示。
- alpha.5 でデスクトップ画面からの既存設定の更新、alpha.6 で取り外し、バックアップ復元、取り外した状態からの新規導入に成功。
- 復元後のファイルは操作前と一致。新規導入後も設定・テーマ・補助プログラムを照合し、標準 EFI ファイル・UEFI 起動登録・SteamOS の読み取り専用状態を維持。
- 実媒体コピーから全76リソースの SHA256 を照合。生成された起動設定は、通常再起動でメニュー・SteamOS・Windows 10 の起動を確認した設定と一致。
- alpha.6 の GUI 新規導入後、完全に電源を切った状態からの選択画面表示と SteamOS の起動に成功。
- alpha.6 の配布 ZIP から検証機で全102件の自動テストに成功。GitHub 上の Python 3.11・3.12・3.13 でも各102件に成功。

## 検証範囲と制限

この版は開発中のプレリリースです。SteamOS と Windows が既に起動できる、標準の SteamOS 起動構成を対象にしています。OS のインストールやパーティション変更は行いません。

- Windows 11、OLED、SteamOS の実際の A/B 更新後の維持は未検証です。
- ブラウザーで GitHub のリリースページから ZIP をダウンロードする操作は、実機では未確認です。ZIP の展開以降は確認しています。
- rEFInd・Clover・独自の起動設定がある環境は対象外です。UEFI の起動登録が失われている場合、その復旧は別途必要です。検証機の起動登録復旧もインストーラとは別に実施しています。
- 公式ロゴは同梱していません。標準の汎用アイコンを使うか、ご自身が利用できる画像に差し替えられます。

`SHA256SUMS` と `*.manifest.json` は整合性確認用、`ice-circuit-0.1.0-alpha.7.tar.gz` は同じソース・アセット一式の別形式です。通常は ZIP だけで始められます。
