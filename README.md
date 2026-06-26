# 잠실 우성아파트 네이버부동산 매매 매물 수집기

서울 송파구 잠실동 우성아파트(1~3차)의 네이버부동산 매매(A1) 매물 정보를 매일
자동으로 수집해 SQLite DB와 엑셀 파일로 누적 저장하는 스크립트입니다.

## 무엇을 수집하나

- 단지: 잠실우성1차 / 2차 / 3차 (검색으로 자동 매칭, `scraper.py`의
  `COMPLEX_NAME_QUERIES` 참고)
- 거래유형: 매매(A1)만 수집 (전세/월세 제외)
- 항목: 평형(공급/전용), 매매가, 층 정보, 방향, 등록/확인일자, 단지코드, 매물번호

## 파일 구조

```
naverland/
  scraper.py          # 수집 스크립트 (실행: python scraper.py)
  requirements.txt     # playwright, pandas, openpyxl
  data/
    naverland.db        # SQLite DB (listings 테이블, 원본 데이터 누적)
    naverland.xlsx       # 엑셀 내보내기 (보기용, 매번 전체 재생성)
```

## 실행 방법

```bash
pip install -r requirements.txt
python -m playwright install chromium
python scraper.py
```

이 PC에서는 Windows 스토어의 `python` 스텁이 아니라 Anaconda Python을 써야 합니다:

```powershell
C:\Users\kimhw\anaconda3\python.exe -m pip install -r requirements.txt
C:\Users\kimhw\anaconda3\python.exe -m playwright install chromium
C:\Users\kimhw\anaconda3\python.exe scraper.py
```

## 동작 방식

1. `m.land.naver.com/search/result/{검색어}`로 접속하면 단지명이 정확히 매칭될 때
   `fin.land.naver.com/complexes/{complexNo}`로 리다이렉트된다 — 이 리다이렉트를
   이용해 단지코드(complexNo)를 자동으로 알아낸다 (`find_complex_no`).
2. 해당 단지 페이지에 접속해 매매 탭의 매물 목록 API 응답을 네트워크 레벨에서
   가로채 수집한다 (`collect_sale_articles`). 정확한 API 필드명이 바뀌어도
   어느정도 대응하도록 "가격/면적/매물번호 필드를 가진 JSON 리스트"를 휴리스틱하게
   찾는 방식을 쓴다.
3. 수집한 매물을 `data/naverland.db`의 `listings` 테이블에 적재한다. 같은 매물을
   같은 시각에 중복 적재하지 않도록 `(article_no, collected_at)` 유니크 제약을 둔다.
4. 마지막에 DB 전체 내용을 `data/naverland.xlsx`로 다시 내보낸다(`export_to_excel`).

## 자동화 스케줄

Claude 앱의 "Scheduled" 작업으로 매일 낮 12시경 자동 실행되도록 등록되어 있다
(작업 ID: `naverland-jamsil-useong-scrape`). 매 실행마다:
- 이 저장소를 pull
- `python scraper.py` 실행
- 변경된 `data/naverland.db`, `data/naverland.xlsx`를 commit & push

**주의:** 이 스케줄은 Claude 앱이 켜져 있을 때만 정확한 시각에 실행된다. 앱이
꺼져 있으면 다음 실행 시점에 대신 실행된다.

## 알려진 제약/이슈

- 네이버부동산 사이트 구조(특히 `fin.land.naver.com`의 API 경로/필드명)가 바뀌면
  수집이 실패할 수 있다. 이 경우 스케줄 작업 프롬프트에 "실패 시 직접 페이지를
  확인해 스스로 수정 후 재시도"하도록 지시되어 있다.
- 짧은 시간에 너무 많은 요청을 보내면 네이버 쪽에서 429(rate limit)를 반환한다.
  실패가 429 때문이라면 몇 분 후 재시도하면 보통 풀린다.
