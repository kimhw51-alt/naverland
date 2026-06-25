"""
잠실 우성아파트(1~3차) 네이버부동산 매매 매물 수집기.

매일 1회 실행되어 fin.land.naver.com(신 네이버부동산)에서 단지의 매매(A1) 매물
목록을 가져와 SQLite DB(data/naverland.db)에 누적 저장한다.

사이트 구조 변경 이력: 과거 new.land.naver.com 기반 구조는 폐기되었고(2026년 기준),
현재는 m.land.naver.com 검색 -> fin.land.naver.com/complexes/{complexNo} 로
리다이렉트되는 구조다. complexNo는 모바일 검색 리다이렉트를 통해 알아낸다.

실행: python scraper.py
"""
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# 검색에 사용할 단지명 후보들. 네이버부동산은 "1차/2차/3차"처럼 정확한 차수가
# 붙어야 단지를 매칭해주는 경우가 많아 후보를 여러 개 둔다.
COMPLEX_NAME_QUERIES = ["잠실우성1차", "잠실우성2차", "잠실우성3차", "잠실우성아파트"]
DB_PATH = Path(__file__).parent / "data" / "naverland.db"

DDL = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,      -- 수집 실행 시각 (ISO)
    complex_no TEXT NOT NULL,
    complex_name TEXT NOT NULL,
    article_no TEXT NOT NULL,        -- 네이버 매물 번호
    trade_type TEXT NOT NULL,        -- A1 = 매매
    pyeong_supply REAL,              -- 공급 평형
    pyeong_exclusive REAL,           -- 전용 평형
    area_supply_m2 REAL,
    area_exclusive_m2 REAL,
    price_won INTEGER,               -- 매매가 (원)
    price_text TEXT,                 -- 네이버 표시 그대로 (예: "25억 5,000")
    floor_info TEXT,
    direction TEXT,
    confirm_date TEXT,                -- 네이버 "확인" 날짜 (매물 등록/갱신 기준일)
    raw_json TEXT,
    UNIQUE(article_no, collected_at)
);
"""


def m2_to_pyeong(m2: float) -> float:
    return round(m2 / 3.3058, 1)


def parse_price_to_won(price_text) -> int | None:
    """'25억 5,000' / '9억' 같은 네이버 가격 표기를 원 단위 정수로 변환."""
    if not price_text or not isinstance(price_text, str):
        return None
    text = price_text.replace(",", "").strip()
    eok = 0
    man = 0
    m = re.search(r"(\d+)억", text)
    if m:
        eok = int(m.group(1))
    rest = re.sub(r"\d+억", "", text).strip()
    if rest:
        m2_ = re.search(r"(\d+)", rest)
        if m2_:
            man = int(m2_.group(1))
    if eok == 0 and man == 0:
        return None
    return eok * 100_000_000 + man * 10_000


def find_complex_no(page, query: str) -> tuple[str, str] | None:
    """모바일 검색결과 리다이렉트를 이용해 (complexNo, complexName)을 알아낸다.
    매칭되는 단지가 없으면 None.
    """
    kw = urllib.parse.quote(query)
    try:
        page.goto(
            f"https://m.land.naver.com/search/result/{kw}",
            wait_until="load",
            timeout=20000,
        )
    except Exception:
        return None
    page.wait_for_timeout(2500)

    m = re.search(r"/complexes/(\d+)", page.url) or re.search(r"complexNumber=(\d+)", page.url)
    if not m:
        # 검색결과 없음 (그대로 search/result 페이지에 머무름) 등
        return None
    complex_no = m.group(1)
    return complex_no, query


def fetch_complex_name(page, complex_no: str) -> str:
    try:
        resp = page.request.get(
            f"https://fin.land.naver.com/front-api/v1/complex?complexNumber={complex_no}"
        )
        if resp.status == 200:
            data = resp.json()
            name = (
                data.get("complexName")
                or data.get("name")
                or data.get("data", {}).get("complexName")
            )
            if name:
                return name
    except Exception:
        pass
    return complex_no


def collect_sale_articles(page, complex_no: str) -> list[dict]:
    """단지 페이지에서 매매(A1) 매물 목록 API 응답을 가로채 모은다.

    fin.land.naver.com의 article 목록 API 정확한 경로/파라미터가 바뀔 수 있으므로,
    "front-api" 응답 중 매물처럼 보이는 리스트(JSON 배열 안에 가격/면적 필드가 있는
    dict들)를 휴리스틱하게 수집한다.
    """
    articles: dict[str, dict] = {}

    def looks_like_article(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        keys = set(d.keys())
        price_keys = {"dealOrWarrantPrc", "price", "dealPrice", "priceText"}
        area_keys = {"area1", "area2", "exclusiveArea", "supplyArea"}
        id_keys = {"articleNo", "articleId", "id"}
        return bool(keys & price_keys) and bool(keys & area_keys) and bool(keys & id_keys)

    def extract_lists(obj):
        """JSON 응답 어디에 있든 매물처럼 보이는 리스트를 재귀적으로 찾는다."""
        found = []
        if isinstance(obj, dict):
            for v in obj.values():
                found.extend(extract_lists(v))
        elif isinstance(obj, list):
            if obj and all(looks_like_article(x) for x in obj):
                found.append(obj)
            else:
                for x in obj:
                    found.extend(extract_lists(x))
        return found

    def on_response(response):
        url = response.url
        if "fin.land.naver.com/front-api" not in url:
            return
        if "article" not in url.lower() and "complex" not in url.lower():
            return
        try:
            data = response.json()
        except Exception:
            return
        for lst in extract_lists(data):
            for art in lst:
                key = str(art.get("articleNo") or art.get("articleId") or art.get("id"))
                articles[key] = art

    page.on("response", on_response)

    page.goto(
        f"https://fin.land.naver.com/complexes/{complex_no}",
        wait_until="networkidle",
        timeout=30000,
    )
    page.wait_for_timeout(2500)

    # 매매 탭이 기본 선택이 아닐 수 있으므로 "매매" 텍스트가 있는 탭을 한 번 클릭해본다.
    try:
        sale_tab = page.get_by_text("매매", exact=False).first
        sale_tab.click(timeout=3000)
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # 매물 목록 스크롤 영역을 찾아 끝까지 스크롤하며 추가 로딩을 유도한다.
    try:
        list_panel = page.locator("[class*='list'], [class*='List']").first
        last_count = -1
        stable_rounds = 0
        for _ in range(40):
            list_panel.evaluate("el => el.scrollTo(0, el.scrollHeight)")
            page.wait_for_timeout(700)
            if len(articles) == last_count:
                stable_rounds += 1
                if stable_rounds >= 3:
                    break
            else:
                stable_rounds = 0
            last_count = len(articles)
    except Exception:
        pass

    page.remove_listener("response", on_response)
    return list(articles.values())


def upsert_articles(conn, complex_no: str, complex_name: str, articles: list[dict]) -> int:
    collected_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for art in articles:
        area_supply = art.get("area1") or art.get("supplyArea")
        area_exclusive = art.get("area2") or art.get("exclusiveArea")
        price_text = art.get("dealOrWarrantPrc") or art.get("priceText") or art.get("price")
        rows.append(
            (
                collected_at,
                complex_no,
                complex_name,
                str(art.get("articleNo") or art.get("articleId") or art.get("id")),
                "A1",
                m2_to_pyeong(area_supply) if area_supply else None,
                m2_to_pyeong(area_exclusive) if area_exclusive else None,
                area_supply,
                area_exclusive,
                parse_price_to_won(price_text),
                price_text,
                art.get("floorInfo") or art.get("floor"),
                art.get("direction"),
                art.get("articleConfirmYmd") or art.get("confirmDate") or art.get("registDate"),
                json.dumps(art, ensure_ascii=False),
            )
        )
    conn.executemany(
        """
        INSERT OR IGNORE INTO listings (
            collected_at, complex_no, complex_name, article_no, trade_type,
            pyeong_supply, pyeong_exclusive, area_supply_m2, area_exclusive_m2,
            price_won, price_text, floor_info, direction, confirm_date, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(DDL)

    total_new = 0
    total_articles = 0
    seen_complex_no = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )

        for query in COMPLEX_NAME_QUERIES:
            found = find_complex_no(page, query)
            time.sleep(2)  # 과도한 요청으로 인한 차단(429) 방지
            if not found:
                print(f"'{query}' 검색 결과 없음, 스킵", file=sys.stderr)
                continue
            complex_no, _ = found
            if complex_no in seen_complex_no:
                continue
            seen_complex_no.add(complex_no)

            complex_name = fetch_complex_name(page, complex_no)
            print(f"complexNo={complex_no} complexName={complex_name}", file=sys.stderr)

            articles = collect_sale_articles(page, complex_no)
            print(f"  매물 {len(articles)}건 수집", file=sys.stderr)
            total_articles += len(articles)

            n = upsert_articles(conn, complex_no, complex_name, articles)
            total_new += n
            time.sleep(2)

        browser.close()

    print(f"수집 완료: 총 매물 {total_articles}건 확인, 신규 적재 {total_new}건")
    conn.close()


if __name__ == "__main__":
    main()
