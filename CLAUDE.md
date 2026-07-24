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
