# -*- coding: utf-8 -*-
"""銀行の公式お知らせページからメンテナンス関連の告知を収集し、
静的HTML (docs/index.html) と docs/data.json を生成する。"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    # ボット対策(TLSフィンガープリント判定)のあるサイト向けにChromeを擬装
    from curl_cffi import requests as chrome_requests
except ImportError:
    chrome_requests = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

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
EXCLUDE = re.compile(r"復旧|再開しました|解除|平成|完了|販売停止|受付停止|取り次ぎ停止|取扱停止|お問い合わせ|お客さまセンター|メンテナンス工業")

# 銀行ごとの定義。必須キーは id / name / group / official / news_urls / regular。
# 例外的なサイトには以下の任意キーを足す(取得方式と抽出方式の2軸だけで足りる):
#   取得方式: pdf_probe … お知らせ一覧がJS描画で読めないサイト向け。
#             日付規則で命名されたPDFを直接探す(probe_days で遡る日数)
#   抽出方式: 指定不要。aタグ(link_list)→地の文(text_block)の順に自動で試す
#   pref … 東海3県の県名表示 / note … 自動取得できない情報の手動補足
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
         official="https://www.sevenbank.co.jp/support/important.html",
         news_urls=["https://www.sevenbank.co.jp/support/important.html",
                    "https://www.sevenbank.co.jp/"],
         regular=""),
    # --- 東海3県の地銀・信金 ---
    dict(id="okashin", name="岡崎信用金庫", group="tokai", pref="愛知",
         official="https://www.okashin.co.jp/",
         news_urls=[],
         pdf_probe="https://www.okashin.co.jp/system/data/{date}_info.pdf",
         probe_days=90,
         regular="臨時休止はPDFで随時告知"),
    dict(id="okb", name="大垣共立銀行", group="tokai", pref="岐阜",
         official="https://www.okb.co.jp/",
         news_urls=["https://www.okb.co.jp/"],
         regular=""),
    dict(id="juroku", name="十六銀行", group="tokai", pref="岐阜",
         official="https://www.juroku.co.jp/",
         news_urls=["https://www.juroku.co.jp/oshirase_personal/",
                    "https://www.16fg.co.jp/news/16bank/"],
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

# 「2026年8月8日（土曜日）22時00分～2026年8月9日（日曜日）11時00分」等の期間表記
# 曜日は括弧付き（土）だけでなく、みずほのような括弧なし「土曜日」表記も許容する
_WD = r"(?:\s*[（(][^）)]{1,8}[）)]|\s*[月火水木金土日]曜日?)?"
_TIME = r"(?:午前|午後)?\s*\d{1,2}\s*[:：時]\s*(?:\d{1,2})?\s*分?\s*(?:頃|ごろ)?"
_D_FULL = rf"\d{{4}}\s*年\s*\d{{1,2}}\s*月\s*\d{{1,2}}\s*日{_WD}(?:\s*{_TIME})?"
_D_PART = rf"(?:\d{{4}}\s*年\s*)?\d{{1,2}}\s*月\s*\d{{1,2}}\s*日{_WD}(?:\s*{_TIME})?"
RANGE_RE = re.compile(rf"{_D_FULL}\s*[〜～~－\-から]{{1,4}}\s*(?:{_D_PART}|{_TIME})")


def resolve_url(url: str) -> str:
    return url.replace("{year}", str(NOW.year))


CHARSET_RE = re.compile(rb'charset=["\']?([\w-]+)', re.I)


def decode_html(raw: bytes) -> str:
    """meta charsetを尊重してデコードする。
    Shift-JISのページをUTF-8として読むと本文が全て文字化けするため必須。"""
    m = CHARSET_RE.search(raw[:4096])
    declared = m.group(1).decode("ascii", "ignore").lower() if m else None
    candidates = [declared] if declared else []
    candidates += ["utf-8", "cp932", "euc-jp"]
    for enc in candidates:
        if not enc:
            continue
        if enc in ("shift_jis", "shift-jis", "sjis", "x-sjis"):
            enc = "cp932"  # cp932はShift_JISの上位互換
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def fetch(url: str) -> str | None:
    if chrome_requests is not None:
        try:
            r = chrome_requests.get(url, impersonate="chrome", timeout=25)
            if r.status_code == 200:
                return decode_html(r.content)
        except Exception:
            pass  # 通常のrequestsで再試行
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return decode_html(r.content)
    except Exception as e:
        print(f"  fetch NG: {url} ({e})", file=sys.stderr)
        return None


_page_cache: dict[str, str | None] = {}


def linked_page_text(url: str) -> str | None:
    """リンク先(HTML/PDF)の本文テキストを取得する。失敗時はNone。"""
    if url in _page_cache:
        return _page_cache[url]
    text = None
    try:
        if url.lower().split("?")[0].endswith(".pdf"):
            if PdfReader is not None:
                getter = chrome_requests if chrome_requests is not None else requests
                kw = {"impersonate": "chrome"} if chrome_requests is not None else {"headers": HEADERS}
                r = getter.get(url, timeout=25, **kw)
                if r.status_code == 200 and len(r.content) < 5_000_000:
                    reader = PdfReader(BytesIO(r.content))
                    text = " ".join(p.extract_text() or "" for p in reader.pages[:5])
        else:
            html = fetch(url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                text = " ".join(soup.get_text(" ", strip=True).split())
    except Exception as e:
        print(f"  deep NG: {url} ({e})", file=sys.stderr)
    _page_cache[url] = text
    return text


def _dates_in(text: str) -> list[datetime]:
    """テキスト中の年付き日付をすべて返す(年なしは直前の年を引き継ぐ)"""
    dates, last_year = [], None
    for m in DATE_RE.finditer(text):
        y = m.group(1) or last_year
        if not y:
            continue
        last_year = y
        try:
            d = datetime(int(y), int(m.group(2)), int(m.group(3)), tzinfo=JST)
        except ValueError:
            continue
        # 年なし日付が前の日付より過去に見える場合は年跨ぎとみなす
        if not m.group(1) and dates and d < dates[-1]:
            d = d.replace(year=d.year + 1)
        dates.append(d)
    for m in DATE_RE2.finditer(text):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=JST))
        except ValueError:
            continue
    return dates


def deep_check(item: dict) -> bool:
    """リンク先本文から停止期間を読み取り、item に period / date を設定する。
    終了日時が過去と判定できた告知は False (=除外) を返す。"""
    text = linked_page_text(item["url"])
    if not text:
        return True  # 読めない場合は除外しない(リンクは生きているため)
    today = NOW.strftime("%Y-%m-%d")
    # 期間表記(○月○日○時〜○月○日○時)のうち、終了が未来のものを探す
    ranges = list(RANGE_RE.finditer(text))
    future_ranges = []
    for m in ranges:
        snippet = " ".join(m.group(0).split())
        dates = _dates_in(snippet)
        if dates and max(dates) + timedelta(days=1) > NOW:
            future_ranges.append((snippet, min(dates)))
    if future_ranges:
        # 同じ停止でも「8月8日～8月9日」と「8月8日22時～8月9日11時」の両方が
        # 載っていることがあるため、時刻を含む具体的な表記を優先する
        snippet, start = max(future_ranges,
                             key=lambda x: bool(re.search(r"[:：]|\d\s*時", x[0])))
        item["period"] = snippet[:110]
        # 本文の期間表記は最も確実な停止日なのでタイトル推定より優先する
        item["event_date"] = item["date"] = start.strftime("%Y-%m-%d")
        return True
    if ranges:
        # 期間表記はあったが全て過去 = 終了済み。
        # ここで打ち切らないと年末年始案内などの無関係な未来日を拾ってしまう
        return False
    dates = [d for d in _dates_in(text) if abs((d - NOW).days) < 400]
    future = [d for d in dates if d + timedelta(days=1) > NOW]
    if future:
        if not item.get("event_date"):
            item["event_date"] = min(future).strftime("%Y-%m-%d")
        if not item.get("date"):
            item["date"] = item["event_date"]
        return True
    if not dates:
        return True  # 日付が読み取れないページは除外しない
    # 本文の日付は全て過去。ただしタイトル側の日付が未来なら信じて残す
    return bool(item.get("date")) and item["date"] >= today


def _mk(year, month, day):
    try:
        return datetime(int(year), int(month), int(day), tzinfo=JST)
    except ValueError:
        return None


def read_dates(title: str) -> tuple[datetime | None, datetime | None]:
    """タイトルから (掲載日, 停止日) を切り分ける。

    一覧ページは行頭に掲載日を置く慣習なので、行頭の日付は掲載日、
    文中の日付は停止日とみなす。年の記載がない停止日は掲載日の年を借りる。
      例) 「2026.07.21 …サービス停止のお知らせ（7月27日…）」
          → 掲載日 2026-07-21 / 停止日 2026-07-27
    """
    posted = event = None

    m = DATE_RE2.search(title)  # 2026.07.21 / 2026/07/13 形式
    if m:
        d = _mk(m.group(1), m.group(2), m.group(3))
        if d and m.start() <= 2:
            posted = d
        else:
            event = event or d

    for m in DATE_RE.finditer(title):  # 2026年7月27日 / 7月27日 形式
        if m.group(1):
            d = _mk(m.group(1), m.group(2), m.group(3))
        else:
            base = posted or NOW
            d = _mk(base.year, m.group(2), m.group(3))
            if d and d < base - timedelta(days=180):
                d = d.replace(year=d.year + 1)  # 年跨ぎの告知
        if d is None:
            continue
        if m.start() <= 2 and m.group(1) and posted is None:
            posted = d
        elif event is None:
            event = d

    return posted, event


def make_item(title: str, url: str, posted, event) -> dict:
    """表示用の date は停止日を優先し、無ければ掲載日で代用する"""
    shown = event or posted
    return {
        "title": title[:120],
        "url": url,
        "date": shown.strftime("%Y-%m-%d") if shown else None,
        "posted_date": posted.strftime("%Y-%m-%d") if posted else None,
        "event_date": event.strftime("%Y-%m-%d") if event else None,
    }


def extract_items(html: str, base_url: str) -> tuple[list[dict], str]:
    """告知を抽出する。返り値は (告知リスト, 使用したパーサ名)。
    link_list(aタグ) を優先し、取れなければ text_block(リスト/表の地の文) に落とす。"""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if len(text) < 10:
            continue
        if text.startswith(("Q ", "Q.", "Q&")) or text.endswith("こちら"):
            continue  # FAQ・リンクラベルはノイズ
        if not KEYWORD.search(text) or EXCLUDE.search(text):
            continue
        href = urljoin(base_url, a["href"])
        if not href.startswith("http") or href in seen:
            continue
        posted, event = read_dates(text)
        d = event or posted
        if d and d.year < NOW.year:  # 過去年の古い告知は除外
            continue
        seen.add(href)
        items.append(make_item(text, href, posted, event))
    if items:
        return items, "link_list"
    # リンク化されていない告知(リスト・表のテキスト)へのフォールバック
    seen_text = set()
    for el in soup.find_all(["li", "dt", "dd", "tr", "td", "p", "div"]):
        text = " ".join(el.get_text(" ", strip=True).split())
        if not (10 <= len(text) <= 200) or text in seen_text:
            continue
        if not KEYWORD.search(text) or EXCLUDE.search(text):
            continue
        if re.search(r"毎日|毎週|毎月|毎年", text):
            continue  # 定例スケジュール表は告知ではない(定例メンテ欄で扱う)
        posted, event = read_dates(text)
        d = event or posted
        if d is None or d.year < NOW.year:
            continue
        seen_text.add(text)
        items.append(make_item(text, base_url, posted, event))
        if len(items) >= 3:
            break
    return items, ("text_block" if items else "none")


def probe_pdf_items(bank: dict) -> tuple[list[dict], bool]:
    """お知らせ一覧がJS描画のサイト向け: 規則的な命名のPDFを日付総当たりで探す。
    返り値は (メンテ関連の告知リスト, PDFが1件でも見つかったか)"""
    pattern = bank.get("pdf_probe")
    if not pattern or PdfReader is None:
        return [], False
    items, found_any = [], False
    for i in range(bank.get("probe_days", 60)):
        d = NOW - timedelta(days=i)
        url = pattern.replace("{date}", d.strftime("%Y%m%d"))
        try:
            if chrome_requests is not None:
                r = chrome_requests.head(url, impersonate="chrome", timeout=10)
            else:
                r = requests.head(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        found_any = True
        text = linked_page_text(url)
        if not text:
            continue
        flat = re.sub(r"\s+", "", text)  # PDF抽出で混入する空白を除去
        if not KEYWORD.search(flat) or EXCLUDE.search(flat[:120]):
            continue
        # 冒頭の日付・宛名・金庫名を除いてタイトルを切り出す
        head = re.sub(r"^.{0,40}?各位", "", flat[:200])
        head = re.sub(r"^.{0,20}?(?:信用金庫|銀行)", "", head, count=1)
        m = re.search(r"(.{0,25}?(?:臨時休止|休止|停止|メンテナンス|システム更改).{0,25}?お知らせ(?:（[^）]{1,15}）)?)", head)
        title = m.group(1) if m else head[:60]
        items.append(make_item(title, url, posted=d, event=None))
        time.sleep(0.1)
    return items, found_any


def collect_bank(bank: dict) -> dict:
    """1行分を収集する。診断情報(sources / raw_count / parser / drop理由)も併せて返す。"""
    items, ok, sources, parsers = [], False, [], []

    for url in bank["news_urls"]:
        u = resolve_url(url)
        html = fetch(u)
        if html is None:
            sources.append({"url": u, "ok": False, "raw": 0, "parser": "fetch_failed"})
            continue
        ok = True
        found, parser = extract_items(html, u)
        items.extend(found)
        parsers.append(parser)
        sources.append({"url": u, "ok": True, "raw": len(found), "parser": parser})

    if bank.get("pdf_probe"):
        pdf_items, pdf_found = probe_pdf_items(bank)
        ok = ok or pdf_found
        items.extend(pdf_items)
        if pdf_items:
            parsers.append("pdf_probe")
        sources.append({"url": bank["pdf_probe"], "ok": pdf_found,
                        "raw": len(pdf_items), "parser": "pdf_probe"})

    raw_count = len(items)

    # URL重複を除き、日付の新しい順に並べる
    uniq, seen = [], set()
    for it in sorted(items, key=lambda x: x["date"] or "", reverse=True):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)

    # リンク先の本文を読み、終了済みを除外して停止期間を抽出
    # (タイトルの日付が未来の告知は、リンク先の判定に関わらず残す)
    today = NOW.strftime("%Y-%m-%d")
    kept, dropped_past = [], 0
    for it in uniq[:5]:
        time.sleep(0.2)
        title_future = bool(it.get("date")) and it["date"] >= today
        if deep_check(it) or title_future:
            kept.append(it)
        else:
            dropped_past += 1

    # 期間を特定できず、日付が30日以上前の告知は古いものとして落とす
    cutoff = (NOW - timedelta(days=30)).strftime("%Y-%m-%d")
    fresh = [it for it in kept
             if it.get("period") or not it.get("date") or it["date"] >= cutoff]
    dropped_past += len(kept) - len(fresh)

    # タイトルにも本文にも日付が無い告知は「今後の予定」と断定できないため出さない
    # (公式ページのリンクは常に残るので取りこぼしにはならない)
    dated = [it for it in fresh if it.get("date")]
    dropped_undated = len(fresh) - len(dated)
    fresh = dated

    # 0件の理由を区別する(UIで「取得失敗」と「終了済み」を出し分けるため)
    if fresh:
        status = "ok"
    elif not ok:
        status = "fetch_failed"
    elif raw_count == 0:
        status = "no_notice"      # 告知自体が見つからない
    else:
        status = "all_past"       # 拾えたが全て終了済み
    latest_past = max((it["date"] for it in uniq if it.get("date")), default=None)

    return {
        "id": bank["id"], "name": bank["name"], "group": bank["group"],
        "pref": bank.get("pref"), "official": resolve_url(bank["official"]),
        "regular": bank["regular"], "note": bank.get("note"),
        "fetch_ok": ok, "items": fresh[:5],
        "diag": {
            "status": status,
            "raw_count": raw_count,
            "kept_count": len(fresh),
            "dropped_past": dropped_past,
            "dropped_undated": dropped_undated,
            "parsers": sorted(set(parsers)),
            "latest_seen": latest_past,
            "sources": sources,
        },
    }


def collect() -> list[dict]:
    results = []
    for bank in BANKS:
        r = collect_bank(bank)
        d = r["diag"]
        print(f"* {bank['name']:<12} {d['status']:<12} "
              f"raw={d['raw_count']:<3} 表示={d['kept_count']:<3} "
              f"parser={','.join(d['parsers']) or '-'}")
        results.append(r)
    return results


def upcoming_items(results: list[dict]) -> list[dict]:
    today = NOW.strftime("%Y-%m-%d")
    ups = []
    for bank in results:
        for it in bank["items"]:
            if it["date"] and it["date"] >= today:
                ups.append({**it, "bank": bank["name"]})
    return sorted(ups, key=lambda x: x["date"])[:12]


def git_history(limit: int = 40) -> list[dict]:
    """サイト自体の更新履歴をgitのコミットログから作る。
    git が使えない環境では空リストを返し、履歴ボタンを出さない。"""
    try:
        out = subprocess.run(
            ["git", "log", f"-{limit}", "--date=short", "--pretty=format:%ad\t%s"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        if out.returncode != 0:
            return []
    except Exception as e:
        print(f"  git history NG: {e}", file=sys.stderr)
        return []
    rows = []
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        date, subject = line.split("\t", 1)
        if not subject.strip():
            continue
        rows.append({"date": date, "subject": subject.strip()})
    return rows


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def render(results: list[dict]) -> str:
    ups = upcoming_items(results)
    up_html = "".join(
        f'<div class="up-card"><span class="up-date">{esc(u["date"])}</span>'
        f'<span class="up-bank">{esc(u["bank"])}</span>'
        f'<span class="up-desc"><a href="{esc(u["url"])}" target="_blank" rel="noopener">{esc(u["title"])}</a>'
        + (f'<span class="when">【停止期間】{esc(u["period"])}</span>' if u.get("period") else "")
        + "</span></div>"
        for u in ups
    ) or '<p class="section-note">日付を特定できる今後の告知は現在ありません。各行の告知一覧をご確認ください。</p>'

    group_names = dict(GROUPS)
    rows = []
    for b in results:
        tag = group_names[b["group"]] + (f"・{b['pref']}" if b.get("pref") else "")
        note = (f'<p class="note">⚠ {esc(b["note"])}</p>' if b.get("note") else "")
        if b["items"]:
            lis = "".join(
                f'<li>{("<b>" + esc(it["date"]) + "</b> ") if it["date"] else ""}'
                f'<a href="{esc(it["url"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
                + (f'<span class="when">【停止期間】{esc(it["period"])}</span>' if it.get("period") else "")
                + "</li>"
                for it in b["items"]
            )
            notice = f'<ul class="notice-list">{lis}</ul>'
        else:
            status = b.get("diag", {}).get("status", "no_notice")
            latest = b.get("diag", {}).get("latest_seen")
            if status == "all_past":
                seen = f"（直近の告知は {esc(latest)}）" if latest else ""
                notice = f'<span class="muted">今後予定されている停止の告知はありません{seen}</span>'
            elif status == "fetch_failed":
                notice = '<span class="muted">自動取得に失敗しました。公式ページをご確認ください</span>'
            else:
                notice = '<span class="muted">メンテナンス関連の告知は見つかりませんでした</span>'
        focused = '1' if (b["items"] or not b["fetch_ok"]) else '0'
        rows.append(
            f'<tr class="bank-row" data-focused="{focused}" data-group="{b["group"]}">'
            f'<td class="bank" data-label="銀行名">{esc(b["name"])}<span class="pref">{esc(tag)}</span></td>'
            f'<td class="period" data-label="メンテナンス関連の告知">{note}{notice}</td>'
            f'<td class="regular" data-label="定例メンテナンス">{esc(b["regular"]) or "—"}</td>'
            f'<td class="src" data-label="ソース"><a href="{esc(b["official"])}" target="_blank" rel="noopener">公式ページ</a></td></tr>'
        )
    sections = [
        f'<section><h2>銀行一覧<span class="count">{len(results)}行</span></h2>'
        f'<div class="table-scroll"><table data-group><thead><tr>'
        f'<th>銀行名</th><th>メンテナンス関連の告知（自動取得）</th><th>定例メンテナンス</th><th>ソース</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
    ]

    # 更新履歴(同じ日の変更はまとめて1ブロックにする)
    history, last_date = [], None
    for row in git_history():
        if row["date"] != last_date:
            if last_date is not None:
                history.append("</ul>")
            history.append(f'<h3 class="hist-date">{esc(row["date"])}</h3><ul class="hist-list">')
            last_date = row["date"]
        history.append(f'<li>{esc(row["subject"])}</li>')
    if history:
        history.append("</ul>")
    hist_html = "".join(history)
    hist_btn = ('<button class="hist-btn" id="hist-open" type="button">更新履歴</button>'
                if hist_html else "")

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return (template
            .replace("{{UPDATED}}", NOW.strftime("%Y年%m月%d日 %H:%M"))
            .replace("{{UPCOMING}}", up_html)
            .replace("{{SECTIONS}}", "".join(sections))
            .replace("{{HISTORY_BUTTON}}", hist_btn)
            .replace("{{HISTORY}}", hist_html))


def main():
    results = collect()
    docs = Path(__file__).resolve().parent.parent / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(
        json.dumps({"updated": NOW.isoformat(), "banks": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    (docs / "index.html").write_text(render(results), encoding="utf-8")
    by_status = {}
    for r in results:
        s = r["diag"]["status"]
        by_status[s] = by_status.get(s, 0) + 1
    print("内訳:", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    ok = sum(1 for r in results if r["fetch_ok"])
    print(f"done: {ok}/{len(results)} 行の取得に成功")


if __name__ == "__main__":
    main()
