"""
잠실 우성아파트(1~3차) 네이버부동산 매매 매물 수집기.

매일 1회 실행되어 fin.land.naver.com(신 네이버부동산)에서 단지의 매매(A1) 매물
목록을 가져와 SQLite DB(data/naverland.db)에 누적 저장하고,
보기 편한 엑셀 파일(data/naverland.xlsx)로도 함께 내보낸다.

사이트 구조 변경 이력(2026-06-26 기준):
- new.land.naver.com, m.land.naver.com 은 완전히 폐기되어 전부 404.
- fin.land.naver.com 자체도 기본 navigator.webdriver=true 인 헤드리스
  브라우저는 financial.pstatic.net의 404 페이지로 강제 리다이렉트한다(봇 차단).
  Playwright context에서 navigator.webdriver를 숨기면(stealth) 정상 동작한다.
- fin.land.naver.com 내부에는 자유 텍스트 검색 API가 없어, complexNo는
  search.naver.com 통합검색 결과에 노출되는 "fin.land.naver.com/complexes/{no}"
  링크를 파싱해서 알아낸다.
- 매물 목록은 단지 페이지의 "매매" 탭 클릭 시 호출되는
  POST https://fin.land.naver.com/front-api/v1/complex/article/list 로 가져온다.
  (사이트 최상단 GNB의 "매물" 링크는 로그인이 필요한 별도 페이지이므로 사용하지 않음)

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

import pandas as pd
from playwright.sync_api import sync_playwright

# 검색에 사용할 단지명 후보들.
COMPLEX_NAME_QUERIES = ["잠실우성아파트", "잠실우성1차", "잠실우성2차", "잠실우성3차"]
DB_PATH = Path(__file__).parent / "data" / "naverland.db"
XLSX_PATH = Path(__file__).parent / "data" / "naverland.xlsx"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

# 엑셀에 보여줄 컬럼명(한글) 매핑
EXCEL_COLUMNS = {
    "collected_at": "수집일시",
    "complex_no": "단지코드",
    "complex_name": "단지명",
    "article_no": "매물번호",
    "pyeong_supply": "공급평형",
    "pyeong_exclusive": "전용평형",
    "area_supply_m2": "공급면적(m2)",
    "area_exclusive_m2": "전용면적(m2)",
    "price_text": "매매가",
    "price_won": "매매가(원)",
    "floor_info": "층정보",
    "direction": "방향",
    "confirm_date": "등록/확인일",
}

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
    price_text TEXT,                 -- 표시용 가격 문자열 (예: "25억 5,000")
    floor_info TEXT,
    direction TEXT,
    confirm_date TEXT,                -- 매물 등록/갱신 확인 날짜
    raw_json TEXT,
    UNIQUE(article_no, collected_at)
);
"""


def m2_to_pyeong(m2) -> float | None:
    if not m2:
        return None
    return round(m2 / 3.3058, 1)


def won_to_price_text(won: int) -> str:
    """원 단위 정수를 '25억 5,000' 같은 네이버 표기 스타일로 변환."""
    eok, man = divmod(won, 100_000_000)
    man = man // 10_000
    if eok and man:
        return f"{eok}억 {man:,}"
    if eok:
        return f"{eok}억"
    return f"{man:,}"


def new_stealth_context(browser):
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
    )
    ctx.add_init_script(STEALTH_INIT_SCRIPT)
    return ctx


def find_complex_no(page, query: str) -> str | None:
    """search.naver.com 통합검색 결과에서 fin.land.naver.com/complexes/{no} 링크를 찾는다."""
    kw = urllib.parse.quote(query)
    try:
        page.goto(
            f"https://search.naver.com/search.naver?query={kw}",
            wait_until="load",
            timeout=20000,
        )
    except Exception:
        return None
    page.wait_for_timeout(1500)
    html = page.content()
    m = re.search(r"fin\.land\.naver\.com/complexes/(\d+)", html)
    if not m:
        return None
    return m.group(1)


def fetch_complex_name(page, complex_no: str) -> str:
    try:
        resp = page.request.get(
            f"https://fin.land.naver.com/front-api/v1/complex?complexNumber={complex_no}"
        )
        if resp.status == 200:
            data = resp.json()
            name = data.get("result", {}).get("name")
            if name:
                return name
    except Exception:
        pass
    return complex_no


def collect_sale_articles(page, complex_no: str) -> list[dict]:
    """단지의 매매(A1) 매물 목록을 front-api에서 페이지네이션하며 모두 가져온다."""
    articles: list[dict] = []
    last_info: list = []
    body = {
        "size": 30,
        "complexNumber": complex_no,
        "tradeTypes": ["A1"],
        "pyeongTypes": [],
        "dongNumbers": [],
        "userChannelType": "PC",
        "articleSortType": "RANKING_DESC",
        "lastInfo": [],
    }

    for _ in range(50):  # 안전 상한
        body["lastInfo"] = last_info
        resp = page.request.post(
            "https://fin.land.naver.com/front-api/v1/complex/article/list",
            data=json.dumps(body),
            headers={"content-type": "application/json"},
        )
        if resp.status != 200:
            break
        data = resp.json()
        result = data.get("result", {})
        page_list = result.get("list", [])
        articles.extend(page_list)
        if not result.get("hasNextPage") or not page_list:
            break
        last_info = result.get("lastInfo", [])
        if not last_info:
            break
        time.sleep(1)

    return articles


def upsert_articles(conn, complex_no: str, complex_name: str, articles: list[dict]) -> int:
    collected_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for art in articles:
        info = art.get("representativeArticleInfo", {})
        space = info.get("spaceInfo", {})
        price = info.get("priceInfo", {})
        detail = info.get("articleDetail", {})

        area_supply = space.get("supplySpace")
        area_exclusive = space.get("exclusiveSpace")
        deal_price = price.get("dealPrice")

        rows.append(
            (
                collected_at,
                complex_no,
                complex_name,
                str(info.get("articleNumber")),
                "A1",
                m2_to_pyeong(area_supply),
                m2_to_pyeong(area_exclusive),
                area_supply,
                area_exclusive,
                deal_price,
                won_to_price_text(deal_price) if deal_price else None,
                detail.get("floorInfo"),
                detail.get("direction"),
                info.get("verificationInfo", {}).get("articleConfirmDate"),
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


def export_to_excel(conn):
    """DB 전체 누적 데이터를 보기 편한 엑셀 파일로 내보낸다."""
    df = pd.read_sql_query(
        f"SELECT {', '.join(EXCEL_COLUMNS.keys())} FROM listings "
        "ORDER BY collected_at DESC, complex_name, pyeong_supply",
        conn,
    )
    df = df.rename(columns=EXCEL_COLUMNS)
    XLSX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(XLSX_PATH, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="매물목록", index=False)
        worksheet = writer.sheets["매물목록"]
        for col_cells in worksheet.columns:
            max_len = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
            worksheet.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 40)


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(DDL)

    total_new = 0
    total_articles = 0
    seen_complex_no = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = new_stealth_context(browser)
        page = ctx.new_page()

        for query in COMPLEX_NAME_QUERIES:
            complex_no = find_complex_no(page, query)
            time.sleep(2)  # 과도한 요청으로 인한 차단(429) 방지
            if not complex_no:
                print(f"'{query}' 검색 결과 없음, 스킵", file=sys.stderr)
                continue
            if complex_no in seen_complex_no:
                continue
            seen_complex_no.add(complex_no)

            # 단지 페이지를 한 번 로드해 세션/쿠키를 확보한다.
            page.goto(
                f"https://fin.land.naver.com/complexes/{complex_no}",
                wait_until="load",
                timeout=30000,
            )
            page.wait_for_timeout(1500)

            complex_name = fetch_complex_name(page, complex_no)
            print(f"complexNo={complex_no} complexName={complex_name}", file=sys.stderr)

            articles = collect_sale_articles(page, complex_no)
            print(f"  매물 {len(articles)}건 수집", file=sys.stderr)
            total_articles += len(articles)

            n = upsert_articles(conn, complex_no, complex_name, articles)
            total_new += n
            time.sleep(2)

        browser.close()

    export_to_excel(conn)
    print(f"수집 완료: 총 매물 {total_articles}건 확인, 신규 적재 {total_new}건, 엑셀 내보내기 완료({XLSX_PATH})")
    conn.close()


if __name__ == "__main__":
    main()
