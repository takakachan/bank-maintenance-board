# -*- coding: utf-8 -*-
"""銀行の公式お知らせページからメンテナンス関連の告知を収集し、
静的HTML (docs/index.html) と docs/data.json を生成する。"""

import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
EXCLUDE = re.compile(
    r"復旧|再開しました|営業再開|【再開】|解除|平成|完了|販売停止|受付停止|取り次ぎ停止|取扱停止"
    r"|お問い合わせ|お客さまセンター|メンテナンス工業"
    # 「有限会社○○メンテナンス」など社名に含まれるケース(融資先の紹介記事など)
    r"|(?:株式会社|有限会社|合同会社|合資会社)[^\s。、]{0,12}メンテナンス")

# 銀行ごとの定義。必須キーは id / name / group / official / news_urls / regular。
# 例外的なサイトには以下の任意キーを足す(取得方式と抽出方式の2軸だけで足りる):
#   取得方式: pdf_probe     … 日付規則で命名されたPDFを総当たりで探す(岡崎信金)
#             json_source   … お知らせ一覧をJSONで配信しているサイト(静岡)
#             sitemap_probe … サイトマップのURLに日付が入っているサイト(千葉)
#             ※いずれもお知らせ一覧がJS描画でHTMLから読めない場合の代替手段
#   抽出方式: 指定不要。aタグ(link_list)→地の文(text_block)の順に自動で試す
#   pref … 県名表示 / note … 自動取得できない情報の手動補足
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
    dict(id="saitamaresona", name="埼玉りそな銀行", group="mega", pref="埼玉",
         official="https://www.saitamaresona.co.jp/kojin/oshirase/",
         news_urls=["https://www.saitamaresona.co.jp/kojin/oshirase/"],
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
    dict(id="hekishin", name="碧海信用金庫", group="tokai", pref="愛知",
         official="https://www.hekishin.jp/news/",
         news_urls=["https://www.hekishin.jp/news/",
                    "https://www.hekishin.jp/news/important/"],
         regular=""),
    # --- その他の地銀 ---
    dict(id="shizuoka", name="静岡銀行", group="other", pref="静岡",
         official="https://www.shizuokabank.co.jp/notice/",
         news_urls=[],
         # お知らせ一覧はJS描画。同じ内容を配信しているJSONを直接読む
         json_source={"url": "https://www.shizuokabank.co.jp/common/components/info.json",
                      "id": "id", "title": "displaytitle", "date": "releasedate",
                      "link": "https://www.shizuokabank.co.jp/notice/detail/{id}/"},
         regular="しずぎんダイレクト・アプリ：毎月第1・第3月曜 2:00–6:00"),
    dict(id="chiba", name="千葉銀行", group="other", pref="千葉",
         official="https://www.chibabank.co.jp/notices/",
         news_urls=[],
         # お知らせ一覧はJS描画。サイトマップのURLに日付が入っているのでそこから辿る
         sitemap_probe={"url": "https://www.chibabank.co.jp/sitemap/www.chibabank.co.jp/sitemap_notices_0.xml",
                        "pattern": r"/notices(\d{8})_\d+", "days": 60, "limit": 15},
         regular="インターネット支店：毎月第2・第3日曜 23:00–翌7:00"),
    # mainte.htmlは年間スケジュール表(個別告知ではない)ので定例欄に回し、告知は/news/から拾う
    dict(id="boy", name="横浜銀行", group="other", pref="神奈川",
         official="https://www.boy.co.jp/kojin/myd/mainte.html",
         news_urls=["https://www.boy.co.jp/news/"],
         regular="はまぎん365・マイダイレクト：毎月第1・第3月曜 2:00–6:00／1月1日 0:00–3日 24:00／年数回の全面メンテあり"),
    dict(id="gunma", name="群馬銀行", group="other", pref="群馬",
         official="https://www.gunmabank.co.jp/info/gbnotice/",
         news_urls=["https://www.gunmabank.co.jp/info/gbnotice/"],
         regular=""),
    dict(id="82", name="八十二長野銀行", group="other", pref="長野",
         official="https://bank.82group.jp/news/",
         news_urls=["https://bank.82group.jp/news/{year}/index.html"],
         regular="インターネットバンキング：毎週日曜 0:00–6:00"),
    dict(id="dhbk", name="第四北越銀行", group="other", pref="新潟",
         official="https://www.dhbk.co.jp/information/",
         news_urls=["https://www.dhbk.co.jp/information/"],
         regular=""),
    dict(id="hokugin", name="北陸銀行", group="other", pref="富山",
         official="https://www.hokugin.co.jp/info/",
         news_urls=["https://www.hokugin.co.jp/info/"],
         regular=""),
    dict(id="hokkoku", name="北國銀行", group="other", pref="石川",
         official="https://www.hokkokubank.co.jp/other/news/",
         news_urls=["https://www.hokkokubank.co.jp/other/news/"],
         regular=""),
    dict(id="fukui", name="福井銀行", group="other", pref="福井",
         official="https://www.fukuibank.co.jp/info/",
         news_urls=["https://www.fukuibank.co.jp/info/"],
         regular=""),
    dict(id="fukuoka", name="福岡銀行", group="other", pref="福岡",
         official="https://www.fukuokabank.co.jp/announcement/important/",
         news_urls=["https://www.fukuokabank.co.jp/announcement/important/"],
         regular=""),
    dict(id="ncbank", name="西日本シティ銀行", group="other", pref="福岡",
         official="https://www.ncbank.co.jp/benri/direct/internet/news.html",
         news_urls=["https://www.ncbank.co.jp/benri/direct/internet/news.html"],
         regular=""),
]

GROUPS = [  # (id, 一覧の区分タグ, 絞り込みボタンの短い名前)
    ("mega", "メガバンク・大手", "メガバンク・大手"),
    ("net", "ネット銀行", "ネット銀行"),
    ("tokai", "東海3県", "東海3県"),
    ("other", "その他の地銀", "その他の地銀"),
]

DATE_RE = re.compile(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_RE2 = re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})")

# 信用金庫の告知は和暦のことがあるため西暦に直してから日付解析する
WAREKI_RE = re.compile(r"令和\s*(元|\d{1,2})\s*年")


def to_seireki(text: str) -> str:
    """「令和8年」→「2026年」(令和元年=2019年)"""
    return WAREKI_RE.sub(
        lambda m: f"{2018 + (1 if m.group(1) == '元' else int(m.group(1)))}年", text)

# 「2026年8月8日（土曜日）22時00分～2026年8月9日（日曜日）11時00分」等の期間表記
# 曜日は括弧付き（土）だけでなく、みずほのような括弧なし「土曜日」表記も許容する
_WD = r"(?:\s*[（(][^）)]{1,8}[）)]|\s*[月火水木金土日]曜日?)?"
# 時刻は「午前9時」「9:00」「AM 2：00」に対応
_TIME = r"(?:午前|午後|[AaPp][Mm])?\s*\d{1,2}\s*[:：時]\s*(?:\d{1,2})?\s*分?\s*(?:頃|ごろ)?"
# 開始側は年を省略することがある(「8月22日（土）21:00～」)
_D_START = rf"(?:\d{{4}}\s*年\s*)?\d{{1,2}}\s*月\s*\d{{1,2}}\s*日{_WD}(?:\s*{_TIME})?"
# 終了側は月まで省略することがある(「～21日（金）10:00」)
_D_END = rf"(?:(?:\d{{4}}\s*年\s*)?\d{{1,2}}\s*月\s*)?\d{{1,2}}\s*日{_WD}(?:\s*{_TIME})?"
RANGE_RE = re.compile(rf"{_D_START}\s*[〜～~－\-から]{{1,4}}\s*(?:{_D_END}|{_TIME})")


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
    text = to_seireki(text)
    dates, last_year = [], None
    for m in DATE_RE.finditer(text):
        y = m.group(1) or last_year
        if y:
            last_year = y
            d = _mk(y, m.group(2), m.group(3))
            # 年なし日付が前の日付より過去に見える場合は年跨ぎとみなす
            if d and not m.group(1) and dates and d < dates[-1]:
                d = _shift_year(d, 1) or d
        else:
            # 文中に年が一度も出てこない場合(「8月22日～8月23日」など)は
            # 今日を基準に補う。ここで捨てると期間表記ごと無効になってしまう
            d = _fix_year(_mk(NOW.year, m.group(2), m.group(3)), NOW)
        if d:
            dates.append(d)
    for m in DATE_RE2.finditer(text):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=JST))
        except ValueError:
            continue
    return dates


def find_detail_pdf(page_url: str) -> str | None:
    """告知ページ本文に日付が無い場合、同じ名前のPDFに本文が載っていることが多い。
    無関係な資料PDFを拾わないよう、ページと同名のものだけを対象にする。"""
    html = fetch(page_url)
    if not html:
        return None
    stem = re.sub(r"\.\w+$", "", page_url.rsplit("/", 1)[-1].split("?")[0])
    if not stem:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if ".pdf" in href.lower() and stem in href:
            return href
    return None


def _judge_text(item: dict, text: str) -> bool | None:
    """本文から停止期間を読み取る。True=残す / False=終了済み / None=日付が読めない"""
    today = NOW.strftime("%Y-%m-%d")
    # 期間表記(○月○日○時〜○月○日○時)のうち、終了が未来のものを探す
    ranges = list(RANGE_RE.finditer(text))
    # 告知本体は年まで明記するのに対し、末尾の年末年始・GW案内は年を省く。
    # 年つきの期間があるページでは年なしの記載を無視して誤検出を防ぐ
    dated = [m for m in ranges if re.search(r"\d{4}\s*年", m.group(0))]
    ranges = dated or ranges
    future_ranges = []
    for m in ranges:
        snippet = " ".join(m.group(0).split())
        dates = _dates_in(snippet)
        if not dates:
            continue
        # 終了を時刻まで見て判定する(日付単位だと終わった告知が半日以上残る)
        _, end, _allday = period_range(snippet, min(dates).strftime("%Y-%m-%d"))
        if end > NOW:
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
        return None  # 日付が読み取れない
    # 本文の日付は全て過去。ただしタイトル側の日付が未来なら信じて残す
    return bool(item.get("event_date")) and item["event_date"] >= today


def deep_check(item: dict) -> bool:
    """リンク先本文から停止期間を読み取り、item に period / date を設定する。
    終了日時が過去と判定できた告知は False (=除外) を返す。"""
    text = linked_page_text(item["url"])
    if not text:
        return True  # 読めない場合は除外しない(リンクは生きているため)
    item["services"] = detect_services(item["title"], text)
    verdict = _judge_text(item, text)
    if verdict is None:
        # 本文がPDFに分かれている告知(大垣共立など)は日程がHTML側に無い
        pdf = find_detail_pdf(item["url"])
        pdf_text = linked_page_text(pdf) if pdf else None
        if pdf_text:
            if not item.get("services"):
                item["services"] = detect_services(item["title"], pdf_text)
            verdict = _judge_text(item, pdf_text)
    return True if verdict is None else verdict


def _mk(year, month, day):
    try:
        return datetime(int(year), int(month), int(day), tzinfo=JST)
    except ValueError:
        return None


def _shift_year(d: datetime, years: int):
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return None  # 2月29日など存在しない日付になる場合


def _fix_year(d, base):
    """年の記載がない日付の年を補正する。
    年末年始をまたぐ告知(12月に見る「1月5日」など)を正しく解釈しつつ、
    送った先が遠すぎる場合は単なる過去のアーカイブとみなして動かさない。"""
    if d is None:
        return None
    near = timedelta(days=120)
    if d < base - timedelta(days=180):          # 大きく過去 → 翌年の告知か
        rolled = _shift_year(d, 1)
        if rolled and rolled < NOW + near:
            return rolled
    elif d > base + timedelta(days=180):        # 大きく未来 → 前年の告知か
        rolled = _shift_year(d, -1)
        if rolled and rolled > NOW - near:
            return rolled
    return d


def read_dates(title: str) -> tuple[datetime | None, datetime | None]:
    """タイトルから (掲載日, 停止日) を切り分ける。

    一覧ページは行頭に掲載日を置く慣習なので、行頭の日付は掲載日、
    文中の日付は停止日とみなす。年の記載がない停止日は掲載日の年を借りる。
      例) 「2026.07.21 …サービス停止のお知らせ（7月27日…）」
          → 掲載日 2026-07-21 / 停止日 2026-07-27
    """
    title = to_seireki(title)
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
            d = _fix_year(d, base)
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
    def exists(day: datetime):
        """PDFの有無だけHEADで確認する(本文は取得しない)"""
        url = pattern.replace("{date}", day.strftime("%Y%m%d"))
        try:
            if chrome_requests is not None:
                r = chrome_requests.head(url, impersonate="chrome", timeout=10)
            else:
                r = requests.head(url, headers=HEADERS, timeout=10)
            return (day, url) if r.status_code == 200 else None
        except Exception:
            return None

    # 日付総当たりは大半が空振りなので並列で叩く(逐次だと9秒、16並列で0.5秒)
    days = [NOW - timedelta(days=i) for i in range(bank.get("probe_days", 60))]
    with ThreadPoolExecutor(max_workers=16) as pool:
        hits = [h for h in pool.map(exists, days) if h]

    items, found_any = [], bool(hits)
    for d, url in hits:
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
    return items, found_any


def probe_json_items(bank: dict) -> tuple[list[dict], bool]:
    """お知らせ一覧をJSONで配信しているサイト向け(静岡)"""
    cfg = bank.get("json_source")
    if not cfg:
        return [], False
    raw = fetch(cfg["url"])
    if not raw:
        return [], False
    try:
        data = json.loads(raw)
    except ValueError as e:
        print(f"  json NG: {cfg['url']} ({e})", file=sys.stderr)
        return [], False
    rows = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get(cfg["title"], "")).strip()
        if not title or not KEYWORD.search(title) or EXCLUDE.search(title):
            continue
        # 「配信日 + タイトル」の順に並べると掲載日と停止日を切り分けられる
        posted, event = read_dates(f"{row.get(cfg['date'], '')} {title}")
        d = event or posted
        if d and d.year < NOW.year:
            continue
        items.append(make_item(title, cfg["link"].format(id=row.get(cfg["id"], "")),
                               posted, event))
    return items, True


def probe_sitemap_items(bank: dict) -> tuple[list[dict], bool]:
    """サイトマップのURLに日付が入っているサイト向け(千葉)。
    一覧がJS描画でも、URLの日付で新しいものだけ開けば安く済む。"""
    cfg = bank.get("sitemap_probe")
    if not cfg:
        return [], False
    raw = fetch(cfg["url"])
    if not raw:
        return [], False
    pat = re.compile(cfg["pattern"])
    cutoff = NOW - timedelta(days=cfg.get("days", 60))
    targets = []
    for loc in re.findall(r"<loc>(.*?)</loc>", raw):
        m = pat.search(loc)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=JST)
        except ValueError:
            continue
        if d >= cutoff:
            targets.append((d, loc))
    targets.sort(reverse=True)

    items = []
    for d, url in targets[:cfg.get("limit", 15)]:
        text = linked_page_text(url)
        if not text:
            continue
        head = text[:300]
        if not KEYWORD.search(head) or EXCLUDE.search(head[:100]):
            continue
        title = head.split("｜")[0].strip() or head[:60]
        items.append(make_item(title, url, posted=d, event=None))
    return items, bool(targets)


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

    # HTMLから読めないサイト向けの代替手段(いずれも該当行だけ動く)
    for key, label, probe in (("pdf_probe", "pdf_probe", probe_pdf_items),
                              ("json_source", "json", probe_json_items),
                              ("sitemap_probe", "sitemap", probe_sitemap_items)):
        if not bank.get(key):
            continue
        found, reached = probe(bank)
        ok = ok or reached
        items.extend(found)
        if found:
            parsers.append(label)
        src = bank[key] if isinstance(bank[key], str) else bank[key]["url"]
        sources.append({"url": src, "ok": reached, "raw": len(found), "parser": label})

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

    # 事前告知と当日告知のように、同じ停止を指す告知が並ぶことがあるためまとめる
    # (期間が完全に一致する場合のみ。掲載が新しい方を残す)
    seen_slot, uniq_slot = set(), []
    for it in fresh:
        slot = (it.get("date"), it.get("period"))
        if it.get("period") and slot in seen_slot:
            continue
        seen_slot.add(slot)
        uniq_slot.append(it)
    fresh = uniq_slot
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
    """表示対象の告知を日付順に並べる(開始が過去でも実施中のものは含む)"""
    ups = []
    for bank in results:
        for it in bank["items"]:
            if it.get("date"):
                ups.append({**it, "bank": bank["name"]})
    return sorted(ups, key=lambda x: x["date"])


WEEKDAYS = "月火水木金土日"
# 「1:00」「午前1時」「22時00分」に対応。分は区切り直後か「分」付きのみ拾う
# (「6時 20」のように後続の数字を分と誤認しないため)
TIME_RE = re.compile(r"(午前|午後)?\s*(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|時(?:\s*(\d{1,2})\s*分)?)")


# 止まるサービスの分類。上から順に見て最大3つまで付ける
SERVICE_TAGS = [
    ("ATM", r"ATM|現金自動|キャッシュコーナー|CD機"),
    ("振込", r"振込|送金|口振|口座振替|ペイジー|Pay-?easy|即時入金"),
    ("アプリ", r"アプリ|スマホ|スマート|ログイン|インターネットバンキング|ネットバンキング"
              r"|ダイレクト|オンラインサービス|[WwＷ][EeＥ][BbＢ]サービス"),
    ("投信", r"投信|投資信託|ファンド|ラップ|NISA"),
    ("カード", r"デビット|クレジット|カードローン|キャッシュカード"),
]


def detect_services(title: str, body: str | None) -> list[str]:
    """告知タイトル(足りなければ本文冒頭)から止まるサービスを判定する"""
    found = [name for name, pat in SERVICE_TAGS if re.search(pat, title)]
    if not found and body:
        # タイトルが「サービス一時休止について」等で内容が分からない場合だけ本文を見る
        found = [name for name, pat in SERVICE_TAGS if re.search(pat, body[:900])]
    return found[:3]


def _side_info(part: str):
    """期間表記の片側から (日付, (時, 分)) を取り出す"""
    d = None
    m = DATE_RE.search(to_seireki(part))
    if m:
        d = _mk(m.group(1) or NOW.year, m.group(2), m.group(3))
    t = None
    mt = TIME_RE.search(part)
    if mt:
        hour = int(mt.group(2))
        if mt.group(1) == "午後" and hour < 12:
            hour += 12
        t = (hour, int(mt.group(3) or mt.group(4) or 0))
    return d, t


def _hhmm(t) -> str:
    return f"{t[0]}:{t[1]:02d}"


def period_range(period: str, date_str: str):
    """期間表記から (開始, 終了, 終日か) を返す。読めない場合は日付だけの終日予定。
    24時表記や日をまたぐ表記(22:00～翌11:00)にも対応する。"""
    base = _mk(*date_str.split("-"))
    parts = re.split(r"[〜～~－]|から", period or "", maxsplit=1)
    if len(parts) == 2:
        d1, t1 = _side_info(parts[0])
        d2, t2 = _side_info(parts[1])
        start_d, end_d = d1 or base, d2 or d1 or base
        if t1 and t2:
            start = start_d + timedelta(hours=t1[0], minutes=t1[1])
            end = end_d + timedelta(hours=t2[0], minutes=t2[1])
            if end <= start:  # 「22:00～翌6:00」のように日付が省略されている
                end += timedelta(days=1)
            return start, end, False
        if d1 and d2 and d1 != d2:
            return start_d, end_d + timedelta(days=1), True  # 終日(終了は翌日0時)
    return base, base + timedelta(days=1), True


def compact_period(period: str) -> str:
    """カード用に期間を短くする。
      「2026年8月4日（火）1:00～6:00」          → 1:00–6:00
      「2026年8月8日 土曜日 22時00分～翌 11時」 → 22:00–翌11:00
      「2026年10月10日(土)～10月12日(月祝)」    → 10/10–10/12 終日
    """
    if not period:
        return ""
    parts = re.split(r"[〜～~－]|から", period, maxsplit=1)
    if len(parts) < 2:
        return ""
    d1, t1 = _side_info(parts[0])
    d2, t2 = _side_info(parts[1])
    if not t1 and not t2:  # 時刻がなく日付だけ = 終日の休止
        return f"{d1.month}/{d1.day}–{d2.month}/{d2.day} 終日" if d1 and d2 else ""
    if not t2:
        return _hhmm(t1) if t1 else ""
    if not t1:
        return _hhmm(t2)
    if d1 and d2 and d1.date() != d2.date():
        gap = (d2.date() - d1.date()).days
        return (f"{_hhmm(t1)}–翌{_hhmm(t2)}" if gap == 1
                else f"{_hhmm(t1)}–{d2.month}/{d2.day} {_hhmm(t2)}")
    # 同じ日付に見えても終了が開始より前なら日をまたいでいる
    if t2 <= t1:
        return f"{_hhmm(t1)}–翌{_hhmm(t2)}"
    return f"{_hhmm(t1)}–{_hhmm(t2)}"


def short_title(title: str) -> str:
    """先頭の掲載日やラベルを削ってカードで読みやすくする"""
    t = re.sub(r"^\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*", "", title)
    for _ in range(3):  # 「お知らせ」「重要」などが重なることがある
        t = re.sub(r"^(?:お知らせ|重要|個人|法人|New)\s*", "", t).strip()
    # 日時はカード右側に別途出しているので本文からは省く
    t = re.sub(r"\s*メンテナンス日時.*$", "", t).strip()
    t = re.sub(r"[（(]\s*\d{4}\s*年\s*\d{1,2}\s*月\s*[）)]$", "", t).strip()
    t = re.sub(r"(?:のお知らせ|のご案内|について)$", "", t).strip()
    return t or title


def card_date(item: dict) -> str:
    """カード左の日付ラベル。
    「実施中」と言えるのは停止期間が実際に今をまたいでいる場合だけ。
    掲載日しか分からない告知を実施中と呼ばないよう区別する。"""
    date_str = item["date"]
    d = datetime.strptime(date_str, "%Y-%m-%d")
    label = f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
    if item.get("period"):
        # 当日でも開始時刻を過ぎていれば実施中と出す
        start, end, _ = period_range(item["period"], date_str)
        return "実施中" if start <= NOW < end else label
    if date_str < NOW.strftime("%Y-%m-%d"):
        return "日時未確認"  # 掲載日しか読み取れなかった告知
    return label


def _ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """ICSは1行75オクテット以内。日本語が途中で壊れないようバイト単位で折る"""
    raw = line.encode("utf-8")
    if len(raw) <= 73:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            out.append(cur.decode("utf-8"))
            cur = b" " + b  # 継続行は先頭に空白
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(ups: list[dict]) -> str:
    """カレンダー購読用のiCalendarを作る(時刻はUTCに変換して環境差をなくす)"""
    stamp = NOW.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//bank-maintenance-board//JP", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:銀行メンテナンス情報",
        "X-WR-TIMEZONE:Asia/Tokyo",
        "X-WR-CALDESC:銀行のシステムメンテナンス・サービス停止予定",
    ]
    for u in ups:
        start, end, allday = period_range(u.get("period", ""), u["date"])
        uid = hashlib.md5(f"{u['bank']}{u['url']}{u['date']}".encode()).hexdigest()
        tags = "・".join(u.get("services") or [])
        summary = f"{u['bank']} 停止" + (f"（{tags}）" if tags else "")
        desc = short_title(u["title"])
        if u.get("period"):
            desc += f"\n停止期間: {u['period']}"
        desc += f"\n{u['url']}"
        lines += ["BEGIN:VEVENT", f"UID:{uid}@bank-maintenance-board", f"DTSTAMP:{stamp}"]
        if allday:
            lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
        else:
            lines += [f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
                      f"DTEND:{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"]
        lines += [
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            f"URL:{u['url']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    # ヘッダー行にも日本語が入るので最後にまとめて折り返す
    return "\r\n".join(_ics_fold(l) for l in lines) + "\r\n"


def read_history(limit: int = 40) -> list[dict]:
    """サイトに表示する更新履歴。CHANGELOG.md を読む。
    見出し「## YYYY-MM-DD」とその下の「- 項目」という形式。"""
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    if not path.exists():
        return []
    rows, date = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("## "):
            date = line[3:].strip()
        elif line.startswith("- ") and date:
            rows.append({"date": date, "subject": line[2:].strip()})
            if len(rows) >= limit:
                break
    return rows


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


TAG_CLASS = {"ATM": "atm", "振込": "furikomi", "アプリ": "app", "投信": "toshin", "カード": "card"}


def render_tags(services) -> str:
    if not services:
        return ""
    chips = "".join(f'<span class="tag tag-{TAG_CLASS.get(s, "etc")}">{esc(s)}</span>'
                    for s in services)
    return f'<div class="tags">{chips}</div>'


def render_card(u: dict) -> str:
    label = card_date(u)
    # 「実施中」は日付ではなく状態なので、そこだけバッジで目立たせる
    date_cls = "up-date is-now" if label == "実施中" else "up-date"
    return (
        '<div class="up-card">'
        '<div class="up-line">'
        f'<span class="{date_cls}">{esc(label)}</span>'
        f'<span class="up-bank">{esc(u["bank"])}</span>'
        f'<span class="up-time">{esc(compact_period(u.get("period", "")) or "時間未定")}</span>'
        '</div>'
        f'<a class="up-title" href="{esc(u["url"])}" target="_blank" rel="noopener">'
        f'{esc(short_title(u["title"]))}</a>'
        f'{render_tags(u.get("services"))}'
        '</div>'
    )


def render(results: list[dict]) -> str:
    ups = upcoming_items(results)
    up_html = "".join(render_card(u) for u in ups) or (
        '<p class="section-note">日付を特定できる今後の告知は現在ありません。'
        '各行の告知一覧をご確認ください。</p>')

    group_names = {gid: label for gid, label, _ in GROUPS}
    rows = []
    for b in results:
        tag = group_names[b["group"]] + (f"・{b['pref']}" if b.get("pref") else "")
        note = (f'<p class="note">⚠ {esc(b["note"])}</p>' if b.get("note") else "")
        # 告知の中身は上部カードに出しているので、ここでは件数と状態だけ示す
        if b["items"]:
            # 同じ日に複数件ある行(ゆうちょ等)は日付を重複表示しない
            days = "・".join(dict.fromkeys(
                card_date(it) for it in b["items"] if it.get("date")))
            notice = (f'<span class="cnt">{len(b["items"])}件</span>'
                      f'<span class="cnt-days">{esc(days)}</span>')
        else:
            status = b.get("diag", {}).get("status", "no_notice")
            latest = b.get("diag", {}).get("latest_seen")
            if status == "all_past":
                seen = f'<span class="cnt-days">直近の告知 {esc(latest)}</span>' if latest else ""
                notice = f'<span class="muted">予定なし</span>{seen}'
            elif status == "fetch_failed":
                notice = '<span class="muted">取得失敗（公式ページをご確認ください）</span>'
            else:
                notice = '<span class="muted">告知なし</span>'
        focused = '1' if (b["items"] or not b["fetch_ok"]) else '0'
        rows.append(
            f'<tr class="bank-row" data-focused="{focused}" data-group="{b["group"]}">'
            f'<td class="bank" data-label="銀行名">{esc(b["name"])}<span class="pref">{esc(tag)}</span></td>'
            f'<td class="period" data-label="今後の停止予定">{note}{notice}</td>'
            f'<td class="regular" data-label="定例メンテナンス">{esc(b["regular"]) or "—"}</td>'
            f'<td class="src" data-label="ソース"><a href="{esc(b["official"])}" target="_blank" rel="noopener">公式ページ</a></td></tr>'
        )
    sections = [
        f'<section><h2>銀行一覧<span class="count">{len(results)}行</span></h2>'
        f'<div class="table-scroll"><table data-group><thead><tr>'
        f'<th>銀行名</th><th>今後の停止予定</th><th>定例メンテナンス</th><th>ソース</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
    ]

    # 更新履歴(同じ日の変更はまとめて1ブロックにする)
    history, last_date = [], None
    for row in read_history():
        if row["date"] != last_date:
            if last_date is not None:
                history.append("</ul>")
            history.append(f'<h3 class="hist-date">{esc(row["date"])}</h3><ul class="hist-list">')
            last_date = row["date"]
        history.append(f'<li>{esc(row["subject"])}</li>')
    if history:
        history.append("</ul>")
    hist_html = "".join(history)
    hist_btn = ('<button class="btn" id="hist-open" type="button">更新履歴</button>'
                if hist_html else "")

    # 区分の絞り込みボタンはGROUPSから生成する(銀行を足しても勝手に増える)
    group_btns = '<button class="group-btn is-active" data-group="all" type="button">すべて</button>'
    group_btns += "".join(
        f'<button class="group-btn" data-group="{gid}" type="button">{esc(short)}</button>'
        for gid, _, short in GROUPS)

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    return (template
            .replace("{{UPDATED}}", NOW.strftime("%Y年%m月%d日 %H:%M"))
            .replace("{{UPCOMING}}", up_html)
            .replace("{{SECTIONS}}", "".join(sections))
            .replace("{{HISTORY_BUTTON}}", hist_btn)
            .replace("{{HISTORY}}", hist_html)
            .replace("{{GROUP_BUTTONS}}", group_btns))


def main():
    results = collect()
    docs = Path(__file__).resolve().parent.parent / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "data.json").write_text(
        json.dumps({"updated": NOW.isoformat(), "banks": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    (docs / "index.html").write_text(render(results), encoding="utf-8")
    ups = upcoming_items(results)
    (docs / "calendar.ics").write_text(build_ics(ups), encoding="utf-8", newline="")
    print(f"カレンダー: {len(ups)}件を calendar.ics に出力")
    by_status = {}
    for r in results:
        s = r["diag"]["status"]
        by_status[s] = by_status.get(s, 0) + 1
    print("内訳:", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    ok = sum(1 for r in results if r["fetch_ok"])
    print(f"done: {ok}/{len(results)} 行の取得に成功")


if __name__ == "__main__":
    main()
