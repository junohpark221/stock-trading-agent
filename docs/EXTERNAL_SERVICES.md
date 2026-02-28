# 외부 서비스 & 데이터 소스 레퍼런스

> **이 문서는 프로젝트에서 사용하는 모든 외부 서비스와 데이터 소스를 정리한 레퍼런스입니다.**
> 각 서비스의 API 정보, 인증 방식, 엔드포인트, 제한사항을 기록합니다.
>
> **최종 수정일: 2026-02-27**

---

## 카테고리 A: 시장 데이터 & 브로커 (Phase 1)

| 서비스 | 용도 | 비용 | 인증 |
|--------|------|------|------|
| **KIS Open Trading API** | 주문, 실시간 시세, OHLCV, 호가, 투자자별 매매동향 | 무료 | OAuth 2.0 |

- API 포털: https://apiportal.koreainvestment.com/
- 상세: 아래 [KIS 레퍼런스 가이드](#kis-레퍼런스-가이드) 참조

---

## 카테고리 B: 공시 & 재무제표 (Phase 2)

| 서비스 | 용도 | 비용 | 인증 |
|--------|------|------|------|
| **DART (OPEN DART API)** | 사업보고서, 재무제표, 주요 공시, XBRL | 무료 | API Key |

- 포털: https://opendart.fss.or.kr/
- Python: `opendartreader` 또는 직접 HTTP
- 일일 한도: 10,000회/일
- 환경변수: `DART_API_KEY`

### 주요 엔드포인트

| 기능 | 엔드포인트 | 설명 |
|------|-----------|------|
| 공시 검색 | `https://opendart.fss.or.kr/api/list.json` | 기업별/기간별 공시 목록 |
| 단일회사 재무제표 | `https://opendart.fss.or.kr/api/fnlttSinglAcnt.json` | 단일 기업 재무제표 |
| 다중회사 재무제표 | `https://opendart.fss.or.kr/api/fnlttMultiAcnt.json` | 복수 기업 비교 |
| 기업 개황 | `https://opendart.fss.or.kr/api/company.json` | 기업 기본 정보 |

---

## 카테고리 C: 경제 지표 (Phase 2~3)

| 서비스 | 용도 | 비용 | 인증 |
|--------|------|------|------|
| **한국은행 ECOS API** | GDP, 금리, 환율, CPI, 통화량, 외환보유고 | 무료 | API Key |
| **FRED API** | 미국 경제지표 (Fed 금리, 고용, 인플레이션 등) | 무료 | API Key |

### ECOS (한국은행 경제통계시스템)

- 포털: https://ecos.bok.or.kr/api/
- 기본 URL: `https://ecos.bok.or.kr/api/StatisticSearch/{API_KEY}/json/kr/1/100/{통계코드}`
- 일일 한도: 100,000회/일
- 환경변수: `ECOS_API_KEY`

| 주요 통계 | 통계코드 | 설명 |
|----------|---------|------|
| 기준금리 | 722Y001 | 한국은행 기준금리 |
| 원/달러 환율 | 731Y003 | 주요 환율 |
| 소비자물가지수 | 901Y009 | CPI |
| GDP | 200Y002 | 국내총생산 |
| 통화량(M2) | 101Y003 | 광의통화 |

### FRED (Federal Reserve Economic Data)

- 포털: https://fred.stlouisfed.org/docs/api/fred/
- 기본 URL: `https://api.stlouisfed.org/fred/series/observations`
- Python: `fredapi` 라이브러리
- 일일 한도: 120회/분
- 환경변수: `FRED_API_KEY`

| 주요 지표 | Series ID | 설명 |
|----------|----------|------|
| Fed 기준금리 | FEDFUNDS | Federal Funds Rate |
| 미국 CPI | CPIAUCSL | Consumer Price Index |
| 미국 실업률 | UNRATE | Unemployment Rate |
| 10년 국채금리 | GS10 | 10-Year Treasury Rate |
| VIX | VIXCLS | 변동성 지수 |

---

## 카테고리 D: 뉴스 & 감성분석 (Phase 2~3)

| 서비스 | 용도 | 비용 | 인증 |
|--------|------|------|------|
| **네이버 검색 API (뉴스)** | 한국 금융 뉴스 헤드라인 + 요약 수집 | 무료 (25,000회/일) | Client ID + Secret |
| **네이버 DataLab 트렌드 API** | 종목 검색 트렌드 (관심도 변화 추적) | 무료 | Client ID + Secret |
| **LLM 웹 검색 (내장)** | 심층 뉴스 분석 + 감성분석 | LLM 비용에 포함 | LLM API Key |
| **RSS 피드** | 한경/연합뉴스 안정적 뉴스 수신 (백업) | 무료 | 없음 |

### 네이버 검색 API

- 개발자 포털: https://developers.naver.com
- 환경변수: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`

| 기능 | 엔드포인트 | 일일 한도 |
|------|-----------|----------|
| 뉴스 검색 | `https://openapi.naver.com/v1/search/news.json` | 25,000회/일 |
| DataLab 트렌드 | `https://naveropenapi.apigw.ntruss.com/datalab/v1/search` | 1,000회/일 |

**뉴스 검색 파라미터:**

| 파라미터 | 설명 | 예시 |
|---------|------|------|
| `query` | 검색어 | `삼성전자 실적` |
| `display` | 결과 수 (최대 100) | `100` |
| `sort` | 정렬 (`sim`: 관련도, `date`: 최신) | `date` |

**응답 필드:** `title`, `originallink`, `link`, `description`, `pubDate`

### 감성분석 방식

웹 스크래핑 대신 다음 방식을 사용:

1. **네이버 검색 API**: 뉴스 헤드라인 + 요약 텍스트 수집
2. **LLM 감성분석**: Sentiment Analyzer 에이전트가 수집된 뉴스 텍스트를 읽고 감성 점수 산출
3. **LLM 웹 검색**: OpenAI/Gemini/Claude API의 내장 웹 검색으로 심층 뉴스 분석
4. **RSS 피드 (백업)**: 한경, 연합뉴스 등 주요 언론사 RSS로 안정적 수신

### RSS 피드 소스

| 언론사 | RSS URL | 카테고리 |
|--------|---------|---------|
| 한국경제 | `https://www.hankyung.com/feed/stock` | 증권 |
| 연합뉴스 | `https://www.yna.co.kr/rss/economy.xml` | 경제 |
| 매일경제 | `https://www.mk.co.kr/rss/30100041/` | 증권 |

> RSS URL은 변경될 수 있으므로 구현 시 재확인 필요.

---

## 카테고리 E: 기술적 분석 라이브러리 (Phase 2)

| 라이브러리 | 용도 | 설치 |
|-----------|------|------|
| **pandas-ta** | 150+ 기술 지표 (RSI, MACD, 볼린저밴드 등) | `uv add pandas-ta` |

- GitHub: https://github.com/twopirllc/pandas-ta
- 주요 지표: RSI, MACD, Bollinger Bands, SMA/EMA, ATR, OBV, VWAP, Stochastic
- DataFrame 확장으로 `df.ta.rsi()` 형태로 사용

---

## 카테고리 F: 보조 시장 데이터 (Phase 2)

| 라이브러리 | 용도 | 설치 |
|-----------|------|------|
| **pykrx** | KRX 직접 OHLCV, 투자자별 매매동향 (히스토리컬) | `uv add pykrx` |
| **FinanceDataReader** | 글로벌 주식 + 환율 + 암호화폐 데이터 | `uv add finance-datareader` |

> pykrx/FinanceDataReader는 KIS API의 보조/백업/히스토리컬 검증 용도.

### pykrx

- GitHub: https://github.com/sharebook-kr/pykrx
- KRX에서 직접 데이터 수집 (인증 불필요)
- 주요 함수: `stock.get_market_ohlcv_by_date()`, `stock.get_market_trading_value_by_date()`

### FinanceDataReader

- GitHub: https://github.com/financedata-org/FinanceDataReader
- 글로벌 시장 데이터 + 한국 시장 지원
- 주요 함수: `fdr.DataReader('005930', '2023')`, `fdr.StockListing('KOSPI')`

---

## 카테고리 G: 기존 확정 서비스 (변경 없음)

| 서비스 | 용도 | Phase |
|--------|------|-------|
| OpenAI API | 주력 LLM | 3 |
| Google Gemini API | 보조 LLM | 3 |
| Anthropic Claude API | 백업 LLM | 3 |
| Telegram Bot API | 알림 + 사람 승인 | 5 |
| PostgreSQL | 메인 DB | 0 (완료) |
| Redis | 캐시 + 토큰 저장 | 0 (완료) |
| Sentry | 에러 트래킹 | 8 |
| AWS (ECS, RDS, ElastiCache 등) | 프로덕션 인프라 | 8 |

---

## 선택적 서비스 (필요 시 추가)

| 서비스 | 용도 | 비고 |
|--------|------|------|
| Finnhub | 글로벌 뉴스 + 회사 프로필 | 한국 시장 실시간 미지원, 뉴스 감성분석 US-only. 무료 |
| Tavily API | AI 에이전트 전용 고속 검색 | 유료. 성능 필요 시 |
| Perplexity API | 실시간 검색 + 요약 | $5/1K. 대안적 검색 |

---

## 에이전트별 데이터 소스 매핑

| 에이전트 | 주요 데이터 소스 | 용도 |
|---------|----------------|------|
| **Market Analyst** | ECOS, FRED, LLM 웹 검색 | 매크로 지표, 시장 환경 분석 |
| **Stock Analyst** | KIS API, DART, pandas-ta, pykrx | 시세, 재무제표, 기술 지표, 히스토리컬 데이터 |
| **Sentiment Analyzer** | 네이버 검색 API, LLM 웹 검색, RSS | 뉴스 수집, 감성 점수 산출, 트렌드 추적 |
| **Risk Manager** | KIS API (포지션), ECOS (금리/환율) | 포트폴리오 리스크, 매크로 리스크 체크 |
| **Trader** | 위 에이전트 결과물 종합 | 최종 매매 결정 |
| **Report Generator** | 위 에이전트 결과물 + DB 통계 | 리포트 생성 |

---

## KIS 레퍼런스 가이드

### 로컬 저장 위치

```
/Users/oliver.p/Desktop/Personal/open-trading-api
```

- GitHub 원본: https://github.com/koreainvestment/open-trading-api
- API 포털: https://apiportal.koreainvestment.com/

### 사용 방식

- **직접 import하지 않음** — 패턴/API 호출 방식 참조 전용
- KIS API의 엔드포인트, 파라미터, 응답 형식을 확인할 때 이 repo의 코드를 읽어 참고
- 우리 프로젝트의 `src/broker/` 모듈 구현 시 API 호출 패턴을 차용

### 디렉토리 구조 & 참조 가이드

```
open-trading-api/
├── examples_llm/              <- [주 참조] LLM 최적화 코드 (기능별 분리)
│   ├── kis_auth.py            <- 인증 패턴 참조 (토큰 발급/관리)
│   └── domestic_stock/        <- 국내주식 API 156개 기능
│       ├── inquire_price/     <- 현재가 시세 조회
│       │   ├── inquire_price.py       <- 한줄 호출 함수
│       │   └── chk_inquire_price.py   <- 테스트/검증 코드
│       ├── inquire_daily_itemchartprice/  <- 일봉 OHLCV
│       ├── inquire_asking_price_exp_ccn/  <- 호가
│       ├── order_cash/                    <- 매수/매도 주문
│       ├── inquire_balance/               <- 잔고 조회
│       ├── investor/                      <- 투자자별 매매동향
│       └── ... (156개 기능)
│
├── examples_user/             <- [보조 참조] 사용자용 통합 코드
│   ├── kis_auth.py            <- 인증 (examples_llm과 동일)
│   └── domestic_stock/
│       ├── domestic_stock_functions.py     <- REST 전체 함수 모음
│       ├── domestic_stock_examples.py      <- 사용 예제
│       ├── domestic_stock_functions_ws.py  <- WebSocket 함수 모음
│       └── domestic_stock_examples_ws.py   <- WebSocket 예제
│
├── stocks_info/               <- [참조] 종목 마스터 데이터
│   ├── kis_kospi_code_mst.py  <- KOSPI 종목 코드
│   ├── kis_kosdaq_code_mst.py <- KOSDAQ 종목 코드
│   ├── kis_konex_code_mst.py  <- KONEX 종목 코드
│   ├── sector_code.py         <- 업종 코드
│   └── theme_code.py          <- 테마 코드
│
├── MCP/                       <- [참조] MCP 서버 (8개 도구)
│   └── Kis Trading MCP/       <- 시세, 계좌, 주문, 분석 도구
│
├── docs/convention.md         <- [참조] KIS SDK 코딩 컨벤션
├── kis_devlp.yaml             <- 설정 파일 형식 참조
└── README.md                  <- 전체 사용 가이드
```

### 언제 무엇을 참조하는가

| 상황 | 참조 위치 |
|------|----------|
| API 엔드포인트/파라미터 확인 | `examples_llm/domestic_stock/{기능명}/{기능명}.py` |
| API 호출 패턴 (인증, 헤더, 에러 처리) | `examples_llm/kis_auth.py` |
| 응답 형식/데이터 구조 확인 | `examples_llm/domestic_stock/{기능명}/chk_{기능명}.py` |
| WebSocket 실시간 데이터 패턴 | `examples_user/domestic_stock/domestic_stock_functions_ws.py` |
| 종목 코드 체계 이해 | `stocks_info/kis_kospi_code_mst.py`, `kis_kosdaq_code_mst.py` |
| 통합 함수 설계 참고 | `examples_user/domestic_stock/domestic_stock_functions.py` |
| MCP 도구 설계 참고 | `MCP/Kis Trading MCP/` |

### 우리 프로젝트에서 활용하는 주요 KIS API

| 기능 | 엔드포인트 | TR ID | 참조 파일 |
|------|-----------|-------|----------|
| 현재가 시세 | `/uapi/domestic-stock/v1/quotations/inquire-price` | FHKST01010100 | `inquire_price/` |
| 일봉 OHLCV | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | FHKST03010100 | `inquire_daily_itemchartprice/` |
| 매수 주문 | `/uapi/trading/order-cash` | TTTC0802U / VTTC0802U | `order_cash/` |
| 매도 주문 | `/uapi/trading/order-cash` | TTTC0801U / VTTC0801U | `order_cash/` |
| 잔고 조회 | `/uapi/trading/inquire-balance` | TTZS | `inquire_balance/` |
| 호가 | `/uapi/domestic-stock/v1/quotations/inquire-asking-price` | — | `asking_price_krx/` |
| 투자자별 매매 | `/uapi/domestic-stock/v1/quotations/inquire-investor` | — | `investor/` |
| 재무 비율 | — | — | `finance_ratio/` |

### 주의사항

- KIS repo는 **패턴 참조용**이며 직접 import하지 않음
- KIS repo 업데이트 시 `git pull`로 최신화 가능
- 우리 프로젝트의 인증/토큰 관리는 Redis 기반으로 자체 구현 (KIS repo의 파일 기반과 다름)
- 모의투자(paper) / 실전투자(prod) URL과 TR ID가 다르므로 참조 시 주의

---

## 환경변수 요약

| 환경변수 | 서비스 | Phase |
|---------|--------|-------|
| `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` | KIS API | 1 |
| `DART_API_KEY` | DART 전자공시 | 2 |
| `ECOS_API_KEY` | 한국은행 ECOS | 2 |
| `FRED_API_KEY` | FRED | 2 |
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 네이버 검색/DataLab | 2 |
| `OPENAI_API_KEY` | OpenAI | 3 |
| `GOOGLE_API_KEY` | Gemini | 3 |
| `ANTHROPIC_API_KEY` | Claude | 3 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram | 5 |
| `SENTRY_DSN` | Sentry | 8 |

---

## Phase 2 추가 의존성 (pyproject.toml)

Phase 2 구현 시 추가할 패키지:

```
pandas-ta      # 기술적 분석 지표 (150+)
pykrx          # KRX 히스토리컬 데이터 (보조)
finance-datareader  # 글로벌 시장 데이터 (보조)
fredapi        # FRED API 클라이언트
```

선택적:
```
opendartreader  # DART API Python 래퍼 (직접 HTTP 대신 사용 가능)
```
