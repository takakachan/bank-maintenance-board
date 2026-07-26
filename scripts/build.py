# -*- coding: utf-8 -*-
"""銀行の公式お知らせページからメンテナンス関連の告知を収集し、
静的HTML (docs/index.html) と docs/data.json を生成する。"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    # ボット対策(TLSフィンガープリント判定)のあるサイト向けにChromeを擬装
    from curl_cffi import requests as chrome_requests
except ImportError:
    chrome_requests = None

JST = timezone(timedelta(hours=9))
NOW = datetime.now(JST)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 BankMainteBoard/1.0"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

KEYWORD = re.compile(r"メンテナンス|休止|停止|システム更改|利用できません")
EXCLUDE = re.compile(r"復旧|再開しました|解除|平成|完了|販売停止|受付停止|取り次ぎ停止|取扱停止|お問い合わせ|お客さまセンター")

BANKS = [
    # --- メガバンク・大手 ---
    dict(id="mizuho", name="みずほ銀行", group="mega",
         official="https://www.mizuhobank.co.jp/oshirase/maintenance_personal.html",
         news_urls=["https://www.mizuhobank.co.jp/direct/info/index.html",
                    "https://www.mizuhobank.co.jp/oshirase/maintenance_personal.html"],
         regular="臨時メンテナンスはお知らせページで随時告知"),
    dict(id="mufg", name="三菱UFJ銀行", group="mega",
         official="https://direct.bk.mufg.jp/btm/ser_naiyo/index.html",
         news_urls=["https://www.bk.mufg.jp/info/index.html"],
         regular="三菱UFJダイレクト：毎月第2土曜 21:00–翌日曜 7:00／ペイジー：毎日 23:30–0:30"),
    dict(id="smbc", name="三井住友銀行", group="mega",
         official="https://www.smbc.co.jp/kojin/direct/jikan/",
         news_urls=["https://www.smbc.co.jp/kojin/spaplli/directapp/news/"],
         regular="SMBCダイレクト：毎週日曜 21:00–月曜 7:00 は一部機能停止"),
    dict(id="jpbank", name="ゆうちょ銀行", group="mega",
         official="https://www.jp-bank.japanpost.jp/news/{year}/news_{year}.html",
         news_urls=["https://www.jp-bank.japanpost.jp/news/{year}/news_{year}.html"],
         regular="夜間（23:55前後–翌朝）に振込・アプリの休止が入ることが多い"),
    dict(id="resona", name="りそな銀行", group="mega",
         official="https://www.resonabank.co.jp/direct/service/",
         news_urls=["https://www.resonabank.co.jp/kojin/oshirase/"],
         regular="マイゲート・アプリ：毎月第1月曜 2:00–6:00／第2土曜 23:00–翌日曜 6:00"),
    # --- ネット銀行 ---
    dict(id="netbk", name="住信SBIネット銀行", group="net",
         official="https://www.netbk.co.jp/contents/company/info/maintenance/",
         news_urls=["https://www.netbk.co.jp/contents/company/info/maintenance/",
                    "https://www.netbk.co.jp/contents/company/info/"],
         regular="毎週日曜 0:00–5:00 各種手続き停止ほか"),
    dict(id="paypay", name="PayPay銀行", group="net",
         official="https://www.paypay-bank.co.jp/news/index.html",
         news_urls=["https://www.paypay-bank.co.jp/news/index.html"],
         regular="1・4・7・10月の最終火曜 1:00–6:00（全体）／日曜未明に一部機能停止あり"),
    dict(id="rakuten", name="楽天銀行", group="net",
         official="https://www.rakuten-bank.co.jp/info/",
         news_urls=["https://www.rakuten-bank.co.jp/info/{year}/"],
         regular="毎月1回・月曜未明 1:00–7:00 が通例"),
    dict(id="jibun", name="auじぶん銀行", group="net",
         official="https://www.jibunbank.co.jp/maintenance/",
         news_urls=["https://www.jibunbank.co.jp/maintenance/"],
         regular="毎月第2土曜の翌日曜 0:00–7:00／カードローン：毎週月曜 1:00–5:00"),
    dict(id="aeon", name="イオン銀行", group="net",
         official="https://www.aeonbank.co.jp/maintenance/",
         news_urls=["https://www.aeonbank.co.jp/maintenance/",
                    "https://www.aeonbank.co.jp/news/"],
         regular=""),
    dict(id="seven", name="セブン銀行", group="net",
         official="https://www.sevenbank.co.jp/",
         news_urls=["https://www.sevenbank.co.jp/"],
         regular=""),
    # --- 東海3県の地銀・信金 ---
    dict(id="okashin", name="岡崎信用金庫", group="tokai", pref="愛知",
         official="https://www.okashin.co.jp/",
         news_urls=["https://www.okashin.co.jp/info/important/index.html",
                    "https://www.okashin.co.jp/info/{year}/index.html"],
         regular="臨時休止はPDFで随時告知",
         note="2026/10/10(土)〜10/12(月祝) システム更改のためATM等オンラインサービスを3日間臨時休止予定（公式PDF告知より・手動記載）"),
    dict(id="okb", name="大垣共立銀行", group="tokai", pref="岐阜",
         official="https://www.okb.co.jp/",
         news_urls=["https://www.okb.co.jp/"],
         regular=""),
    dict(id="juroku", name="十六銀行", group="tokai", pref="岐阜",
         official="https://www.juroku.co.jp/",
         news_urls=["https://www.16fg.co.jp/news/16bank/"],
         regular="インターネットバンキング：毎月第2・第3土曜 21:00–23:00／1月1日 21:00–23:00"),
    dict(id="gifushin", name="岐阜信用金庫", group="tokai", pref="岐阜",
         official="https://www.gifushin.co.jp/",
         news_urls=["https://www.gifushin.co.jp/info/"],
         regular=""),
    dict(id="aichibank", name="あいち銀行", group="tokai", pref="愛知",
         official="https://www.aichibank.co.jp/important_infomation/",
         news_urls=["https://www.aichibank.co.jp/important_infomation/"],
         regular="インターネットバンキング：毎日 2:00–6:00 は利用不可"),
    dict(id="meigin", name="名古屋銀行", group="tokai", pref="愛知",
         official="https://www.meigin.com/",
         news_urls=["https://www.meigin.com/kojin/bankstage/news/",
                    "https://www.meigin.com/"],
         regular=""),
    dict(id="hyakugo", name="百五銀行", group="tokai", pref="三重",
         official="https://www.hyakugo.co.jp/whats_new/",
         news_urls=["https://www.hyakugo.co.jp/whats_new/"],
         regular=""),
    dict(id="san33", name="三十三銀行", group="tokai", pref="三重",
         official="https://www.33bank.co.jp/",
         news_urls=["https://www.33bank.co.jp/"],
         regular=""),
]

GROUPS = [
    ("mega", "メガバンク・大手"),
    ("net", "ネット銀行"),
    ("tokai", "東海3県の地銀・信金"),
]

DATE_RE = re.compile(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_RE2 = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")


def resolve_url(url: str) -> str:
    return url.replace("{year}", str(NOW.year))


def fetch(url: str) -> str | None:
    if chrome_requests is not None:
        try:
            r = chrome_requests.get(url, impersonate="chrome", timeout=25)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass  # 通常のrequestsで再試行
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        if r.encoding in (None, "ISO-8859-1"):
            r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        print(f"  fetch NG: {url} ({e})", file=sys.stderr)
        return None


def guess_date(title: str):
    """タイトル中の最初の日付を推定して返す（年省略時は前後関係から補完）"""
    m1 = DATE_RE.search(title)   # 「7月27日」等(イベント日のことが多い)
    m2 = DATE_RE2.search(title)  # 「2026.07.21」等(掲載日のことが多い)
    try:
        if m1 and m1.group(1):
            return datetime(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)), tzinfo=JST)
        if m1 and m2:
            # 掲載日の年を借りてイベント日を組み立てる(掲載日より前なら翌年とみなす)
            base = datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), tzinfo=JST)
            d = datetime(base.year, int(m1.group(2)), int(m1.group(3)), tzinfo=JST)
            if d < base:
                d = d.replace(year=base.year + 1)
            return d
        if m1:
            # 年の記載がない場合、30日以上過去に見える日付は信用しない
            d = datetime(NOW.year, int(m1.group(2)), int(m1.group(3)), tzinfo=JST)
            return d if (NOW - d).days <= 30 else None
        if m2:
            return datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)), tzinfo=JST)
    except ValueError:
        return None
    return None


def extract_items(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if len(text) < 10:
            continue
        if not KEYWORD.search(text) or EXCLUDE.search(text):
            continue
        href = urljoin(base_url, a["href"])
        if not href.startswith("http") or href in seen:
            continue
        d = guess_date(text)
        if d and d.year < NOW.year:  # 過去年の古い告知は除外
            continue
        seen.add(href)
        items.append({
            "title": text[:120],
            "url": href,
            "date": d.strftime("%Y-%m-%d") if d else None,
        })
    if items:
        return items
    # リンク化されていない告知(リスト・表のテキスト)へのフォールバック
    seen_text = set()
    for el in soup.find_all(["li", "dt", "dd", "tr", "td", "p", "div"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not (10 <= len(text) <= 200) or text in seen_text:
            continue
        if not KEYWORD.search(text) or EXCLUDE.search(text):
            continue
        d = guess_date(text)
        if d is None or d.year < NOW.year:
            continue
        seen_text.add(text)
        items.append({
            "title": text[:120],
            "url": base_url,
            "date": d.strftime("%Y-%m-%d"),
        })
        if len(items) >= 3:
            break
    return items


def collect() -> list[dict]:
    results = []
    for bank in BANKS:
        print(f"* {bank['name']}")
        items, ok = [], False
        for url in bank["news_urls"]:
            html = fetch(resolve_url(url))
            if html is None:
                continue
            ok = True
            items.extend(extract_items(html, resolve_url(url)))
        # URL重複を除きつつ日付の新しい順に最大5件
        uniq, seen = [], set()
        for it in sorted(items, key=lambda x: x["date"] or "", reverse=True):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            uniq.append(it)
        results.append({
            "id": bank["id"], "name": bank["name"], "group": bank["group"],
            "pref": bank.get("pref"), "official": resolve_url(bank["official"]),
            "regular": bank["regular"], "note": bank.get("note"),
            "fetch_ok": ok, "items": uniq[:5],
        })
    return results


def upcoming_items(results: list[dict]) -> list[dict]:
    today = NOW.strftime("%Y-%m-%d")
    ups = []
    for bank in results:
        for it in bank["items"]:
            if it["date"] and it["date"] >= today:
                ups.append({**it, "bank": bank["name"]})
    return sorted(ups, key=lambda x: x["date"])[:12]


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def render(results: list[dict]) -> str:
    ups = upcoming_items(results)
    up_html = "".join(
        f'<div class="up-card"><span class="up-date">{esc(u["date"])}</span>'
        f'<span class="up-bank">{esc(u["bank"])}</span>'
        f'<span class="up-desc"><a href="{esc(u["url"])}" target="_blank" rel="noopener">{esc(u["title"])}</a></span></div>'
        for u in ups
    ) or '<p class="section-note">日付を特定できる今後の告知は現在ありません。各行の告知一覧をご確認ください。</p>'

    sections = []
    for gid, gname in GROUPS:
        group_banks = [r for r in results if r["group"] == gid]
        rows = []
        for b in group_banks:
            pref = f'<span class="pref">{esc(b["pref"])}</span>' if b.get("pref") else ""
            note = (f'<p class="note">⚠ {esc(b["note"])}</p>' if b.get("note") else "")
            if b["items"]:
                lis = "".join(
                    f'<li>{("<b>" + esc(it["date"]) + "</b> ") if it["date"] else ""}'
                    f'<a href="{esc(it["url"])}" target="_blank" rel="noopener">{esc(it["title"])}</a></li>'
                    for it in b["items"]
                )
                notice = f'<ul class="notice-list">{lis}</ul>'
            elif b["fetch_ok"]:
                notice = '<span class="muted">キーワードに一致する告知は見つかりませんでした</span>'
            else:
                notice = '<span class="muted">自動取得に失敗しました。公式ページをご確認ください</span>'
            focused = '1' if (b["items"] or not b["fetch_ok"]) else '0'
            rows.append(
                f'<tr class="bank-row" data-focused="{focused}">'
                f'<td class="bank" data-label="銀行名">{esc(b["name"])}{pref}</td>'
                f'<td class="period" data-label="メンテナンス関連の告知">{note}{notice}</td>'
                f'<td class="regular" data-label="定例メンテナンス">{esc(b["regular"]) or "—"}</td>'
                f'<td class="src" data-label="ソース"><a href="{esc(b["official"])}" target="_blank" rel="noopener">公式ページ</a></td></tr>'
            )
        sections.append(
            f'<section><h2>{gname}<span class="count">{len(group_banks)}行</span></h2>'
            f'<div class="table-scroll"><table data-group><thead><tr>'
            f'<th>銀行名</th><th>メンテナンス関連の告知（自動取得）</th><th>定例メンテナンス</th><th>ソース</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
        )

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return (template
            .replace("{{UPDATED}}", NOW.strftime("%Y年%m月%d日 %H:%M"))
            .replace("{{UPCOMING}}", up_html)
            .replace("{{SECTIONS}}", "".join(sections)))


def main():
    results = collect()
    docs = Path(__file__).resolve().parent.parent / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(
        json.dumps({"updated": NOW.isoformat(), "banks": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    (docs / "index.html").write_text(render(results), encoding="utf-8")
    ok = sum(1 for r in results if r["fetch_ok"])
    print(f"done: {ok}/{len(results)} 行の取得に成功")


if __name__ == "__main__":
    main()
