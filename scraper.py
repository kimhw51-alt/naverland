"""
잠실7동 우성아파트 네이버부동산 매매 매물 수집기.

매일 1회 실행되어 new.land.naver.com에서 해당 단지의 매매(A1) 매물 목록을
가져와 SQLite DB(data/naverland.db)에 누적 저장한다.

실행: python scraper.py
"""
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

COMPLEX_NAME_QUERY = "잠실 우성"   # 검색에 사용할 키워드
COMPLEX_NAME_MATCH = "우성"        # 검색 결과 중 단지명에 포함되어야 하는 문자열
DONG_MATCH = "잠실"                 # 동/지역명 필터링용
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


def parse_price_to_won(price_text: str) -> int | None:
    """'25억 5,000' / '9억' 같은 네이버 가격 표기를 원 단위 정수로 변환."""
    if not price_text:
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


def find_complex_no(page) -> tuple[str, str]:
    """검색창에서 단지를 검색해 (complexNo, complexName)을 반환."""
    page.goto("https://new.land.naver.com/complexes", wait_until="domcontentloaded")
    search_box = page.locator("input.search_input, input[placeholder*='검색']").first
    search_box.click()
    search_box.fill(COMPLEX_NAME_QUERY)
    page.wait_for_timeout(1500)

    candidates = page.locator("ul.list_search_result li, li.search_list_item, .item_result")
    count = candidates.count()
    target = None
    for i in range(count):
        text = candidates.nth(i).inner_text()
        if COMPLEX_NAME_MATCH in text and DONG_MATCH in text:
            target = candidates.nth(i)
            break
    if target is None and count > 0:
        # 동 이름까지 못 찾으면 단지명만으로 재시도
        for i in range(count):
            text = candidates.nth(i).inner_text()
            if COMPLEX_NAME_MATCH in text:
                target = candidates.nth(i)
                break
    if target is None:
        raise RuntimeError(
            f"'{COMPLEX_NAME_QUERY}' 검색 결과에서 '{COMPLEX_NAME_MATCH}' 단지를 찾지 못함"
        )

    target.click()
    page.wait_for_url(re.compile(r".*/complexes/\d+.*"), timeout=15000)
    m = re.search(r"/complexes/(\d+)", page.url)
    if not m:
        raise RuntimeError(f"complexNo를 URL에서 추출 못함: {page.url}")
    complex_no = m.group(1)
    complex_name = page.title().split("|")[0].strip() or COMPLEX_NAME_QUERY
    return complex_no, complex_name


def collect_sale_articles(page, complex_no: str) -> list[dict]:
    """매매(A1) 탭의 매물 목록 API 응답을 가로채 누적."""
    articles: dict[str, dict] = {}

    def on_response(response):
        url = response.url
        if "/api/articles/complex/" in url and complex_no in url and "tradTpCd=A1" in url:
            try:
                data = response.json()
            except Exception:
                return
            for art in data.get("articleList", []):
                articles[str(art.get("articleNo"))] = art

    page.on("response", on_response)

    sale_url = (
        f"https://new.land.naver.com/complexes/{complex_no}"
        f"?ms=37.515,127.103,16&a=APT&e=RETAIL&tradTpCd=A1"
    )
    page.goto(sale_url, wait_until="networkidle")
    page.wait_for_timeout(2000)

    list_panel = page.locator("div.list_contents, div[class*='article_list']").first
    last_count = -1
    stable_rounds = 0
    for _ in range(60):  # 무한 스크롤 안전 상한
        list_panel.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        page.wait_for_timeout(800)
        if len(articles) == last_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                break
        else:
            stable_rounds = 0
        last_count = len(articles)

    page.remove_listener("response", on_response)
    return list(articles.values())


def upsert_articles(conn, complex_no: str, complex_name: str, articles: list[dict]):
    collected_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for art in articles:
        area_supply = art.get("area1")  # m^2
        area_exclusive = art.get("area2")
        price_text = art.get("dealOrWarrantPrc")
        rows.append(
            (
                collected_at,
                complex_no,
                complex_name,
                str(art.get("articleNo")),
                "A1",
                m2_to_pyeong(area_supply) if area_supply else None,
                m2_to_pyeong(area_exclusive) if area_exclusive else None,
                area_supply,
                area_exclusive,
                parse_price_to_won(price_text),
                price_text,
                art.get("floorInfo"),
                art.get("direction"),
                art.get("articleConfirmYmd"),
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        complex_no, complex_name = find_complex_no(page)
        print(f"complexNo={complex_no} complexName={complex_name}", file=sys.stderr)
        articles = collect_sale_articles(page, complex_no)
        browser.close()

    n = upsert_articles(conn, complex_no, complex_name, articles)
    print(f"수집 완료: 매물 {len(articles)}건, 신규 적재 {n}건")
    conn.close()


if __name__ == "__main__":
    main()
