# 銀行メンテナンス情報ボード

日本の主要銀行(メガバンク・ネット銀行)と東海3県の地銀・信金のメンテナンス/サービス停止告知を自動収集して一覧表示する静的サイト。

## 仕組み

- `scripts/build.py` が各行の公式お知らせページを取得し、「メンテナンス」「休止」「停止」等のキーワードに一致する告知リンクを抽出
- `docs/index.html` と `docs/data.json` を生成
- GitHub Actions (`.github/workflows/update.yml`) が毎日 6:00 / 18:00 JST に実行し、GitHub Pagesへ自動デプロイ

AIは使用していません。すべてPythonの単純なスクレイピングです。

## ローカルで動かす

```bash
pip install -r requirements.txt
python scripts/build.py
```

生成された `docs/index.html` をブラウザで開く。

## 銀行の追加・削除

`scripts/build.py` の `BANKS` リストに1エントリ追加するだけです。

```python
dict(id="example", name="〇〇銀行", group="net",  # mega / net / tokai
     official="https://example.co.jp/maintenance/",   # 「公式ページ」リンク先
     news_urls=["https://example.co.jp/news/"],       # 告知を収集するページ(複数可)
     regular="毎週日曜 0:00–6:00"),                    # 定例メンテ(手動メモ、空でも可)
```
