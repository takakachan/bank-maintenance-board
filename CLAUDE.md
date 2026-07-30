# bank-maintenance-board

銀行のメンテナンス・サービス停止告知を自動収集して表示する静的サイト。AI不使用の純粋なスクレイピング + GitHub Actions + GitHub Pages構成。

## 構成

- `scripts/build.py` — 収集とHTML生成のすべて。銀行リストは `BANKS`(group: mega/net/tokai)
- `scripts/template.html` — ページテンプレート(`{{UPDATED}}` `{{UPCOMING}}` `{{SECTIONS}}` を置換)
- `docs/` — 生成物(index.html / data.json)。GitHub Pagesのデプロイ対象
- `.github/workflows/update.yml` — 毎日 6:00 / 18:00 JST に自動実行 + push時 + 手動実行

## 開発メモ

- ローカル実行: `python scripts/build.py`(要 `pip install -r requirements.txt`)
- 銀行の追加はREADME参照。`BANKS` に1エントリ足すだけ
- 取得失敗はエラーにせず「取得失敗」表示で継続する設計(銀行サイトのブロックや構造変更に耐えるため)
- 定例メンテナンス欄(`regular`)は手動メンテの固定文字列。変更があったら手で直す

## 収集の仕組み(デバッグ時に読む)

実行ログが銀行ごとに `status / raw / 表示 / parser` を出すので、まずここを見る。

- **status**: `ok`(表示あり) / `all_past`(拾えたが全て終了済み) / `no_notice`(告知が見つからない) / `fetch_failed`
- **取得方式**: 既定はHTML。`pdf_probe` を持つ行は日付規則のPDFを直接探す(岡崎信金)
- **抽出方式**: `link_list`(aタグ) → `text_block`(地の文) の順に自動フォールバック
- **日付**: `posted_date`(掲載日) と `event_date`(停止日) を分けて保持。行頭の日付=掲載日、文中の日付=停止日として切り分ける
- **除外の判断**: リンク先本文の期間表記が全て過去なら終了済みとして落とす。日付が全く取れない告知は出さない
- `docs/data.json` の `diag` に上記の内訳と参照URLが全部入っているので、原因追跡はそこを見れば足りる

### 文字コードの落とし穴

三井住友・PayPay銀行は **Shift-JIS**。`curl_cffi` の `.text` はこれをUTF-8として読むため本文が全滅する。
必ず `decode_html()` で meta charset を見てデコードすること(この対応前は両行とも0件だった)。
