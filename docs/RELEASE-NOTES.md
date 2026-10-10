Steam Deck の起動メニューを、黒とアイスブルーの回路デザインに。SteamOS と Windows を選びやすくし、詳細起動・復旧項目は「Tools & Recovery」にまとめます。

**ダウンロードするのは、下の Assets にある `Ice-Circuit-SteamDeck-0.1.0-alpha.6.zip` です。** GitHub が自動表示する「Source code (zip)」「Source code (tar.gz)」は選ばなくて大丈夫です。

1. Steam Deck をデスクトップモードにして、ZIP をダウンロードします。
2. ZIP を展開し、`ice-circuit-0.1.0-alpha.6` フォルダーを開きます。
3. `START-HERE.html` の案内を読み、`Install Ice Circuit.desktop` から設定を始めます。Git の操作は不要です。管理者パスワードが未設定の場合の準備は、導入ガイドをご覧ください。

[導入ガイド](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/GETTING-STARTED.md) · [困ったときは](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/RECOVERY.md) · [検証済みの範囲](https://github.com/cobo920/steamdeck-ice-circuit/blob/main/docs/VALIDATION.md)

この版は開発中のプレリリースです。SteamOS と Windows が既に起動できる、標準の SteamOS 起動構成を対象にしています。OS のインストールやパーティション変更は行いません。

- LCD モデルの SteamOS 3.8.16 と Windows 10 で導入・復元・起動を確認しています。Windows 11、OLED、SteamOS の実際の A/B 更新後の維持は未検証です。
- 設定画面からの既存設定の更新は実機で確認済みです。新規導入・取り外し・バックアップ復元は処理部分を実機確認済みですが、各操作の画面全体の試験は未完了です。完全に電源を切った状態からの起動も未確認です。
- rEFInd・Clover・独自の起動設定がある環境は対象外です。UEFI の起動登録が失われている場合、その復旧は別途必要です。
- 公式ロゴは同梱していません。標準の汎用アイコンを使うか、ご自身が利用できる画像に差し替えられます。

`SHA256SUMS` と `*.manifest.json` は整合性確認用、`ice-circuit-0.1.0-alpha.6.tar.gz` は同じソース・アセット一式の別形式です。通常は ZIP だけで始められます。
