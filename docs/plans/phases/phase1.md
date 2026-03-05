# Phase 1: 데이터 수집 레이어 — 구현 계획

> **상태**: Step 1-9 구현 완료 (2026-03-05). develop 머지 대기.
> **작성일**: 2026-02-28
> **완료 기준**: KIS 모의투자 API 연결 성공 → 종목 마스터 수집 → 일봉 데이터 3년치 수집/저장 → Redis 캐시 동작

---

## Context

Phase 0(FastAPI + DB/Redis 인프라)이 완료되어 Phase 1을 시작한다. Phase 1은 KIS Open Trading API 연동을 통한 시장 데이터 수집 레이어를 구축하는 단계로, 2계층 아키텍처를 채택한다:

- **BrokerInterface** (하위 레이어): KIS API 직접 호출 래퍼 (인증, 시세, 주문)
- **DataProvider** (상위 레이어): BrokerInterface 위에 Redis 캐싱 + DB 영구 저장 + 배치 수집을 래핑

Phase 2에서 4개 추가 provider(DART, ECOS, FRED, Naver)가 DataProvider ABC를 상속하므로 범용적으로 설계한다.

---

## 파일 현황

**신규 생성: 19개** (12 소스 + 6 `__init__.py` + 1 migration)
**수정: 4개** (`src/core/exceptions.py`, `src/config.py`, `alembic/env.py`, `src/main.py`)
**테스트 신규: 3개**, **테스트 수정: 1개** (`tests/conftest.py`)

---

## Step별 구현 계획

### Step 1: 예외 + 설정 업데이트 → commit #1
> `"Phase 1: config + exception updates for KIS broker"`

**수정 파일:**

| 파일 | 변경 내용 |
|------|----------|
| `src/core/exceptions.py` | `TokenExpiredError(AuthError)`, `RateLimitError(APIError)`, `KISResponseError(APIError)` 추가 |
| `src/config.py` | `KIS_RATE_LIMIT_INTERVAL`, `KIS_TOKEN_REDIS_TTL`, `KIS_OHLCV_CACHE_TTL`, `KIS_PRICE_CACHE_TTL` 추가 |

**KISResponseError 설계:**
```python
class KISResponseError(APIError):
    """KIS API 응답 오류 (rt_cd != "0")."""
    def __init__(self, msg_cd: str, msg1: str, tr_id: str = ""):
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.tr_id = tr_id
        super().__init__(f"KIS API error [{msg_cd}]: {msg1} (tr_id={tr_id})")
```

**Settings 추가 필드:**
```python
# KIS_ACCOUNT_PROD는 src/config.py L32에 이미 존재 — 추가 불필요
KIS_RATE_LIMIT_INTERVAL: float = 0.5   # paper=0.5s, prod=0.05s
KIS_TOKEN_REDIS_TTL: int = 82800       # 23시간 (토큰 유효 24시간, 1시간 여유)
KIS_OHLCV_CACHE_TTL: int = 300         # 5분
KIS_PRICE_CACHE_TTL: int = 10          # 10초
KIS_BASE_URL: str = ""                 # 빈 값이면 KIS_IS_PAPER로 자동 결정
```

---

### Step 2: DB ORM 모델 + Migration → commit #2
> `"Phase 1: StockMaster + DailyOHLCV ORM models and migration"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/db/models/__init__.py` | ORM 모델 패키지 (Alembic auto-detection용 import) |
| `src/db/models/market_data.py` | `StockMaster`, `DailyOHLCV` ORM 모델 |

**수정 파일:** `alembic/env.py` — `from src.db.models import market_data  # noqa: F401` 추가

**StockMaster 스키마:**
- `symbol` (PK, String(20)), `name`, `market_type`, `standard_code`, `sector`
- `listed_shares` (BigInteger), `market_cap_krw` (Numeric(20,0)), `face_value`
- `listing_date` (Date, nullable), `is_active` (Boolean, default True)
- `TimestampMixin` (created_at, updated_at)

**DailyOHLCV 스키마:**
- `id` (PK, BigInteger, autoincrement), `symbol` (String(20), index)
- `date` (Date), `open/high/low/close` (Numeric(15,2)), `volume` (BigInteger)
- `trading_value` (Numeric(20,0)), `change_rate` (Numeric(10,4))
- `UniqueConstraint("symbol", "date")`, `TimestampMixin`

> **설계 노트**: `symbol`은 `stock_master`에 대한 FK를 설정하지 않음. OHLCV를 종목 마스터 없이도 삽입 가능하도록 하여 수집 순서 의존성을 제거.

**마이그레이션 생성:** `uv run alembic revision --autogenerate -m "Phase 1: stock_master + daily_ohlcv"` → `uv run alembic upgrade head`

---

### Step 3: Redis 캐시 래퍼 → commit #3
> `"Phase 1: Redis cache wrapper with namespace support"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/data/__init__.py` | 빈 패키지 |
| `src/data/cache.py` | `RedisCache` 클래스 — 네임스페이스 기반 키 관리 |

**RedisCache 메서드:**
- `get/set/delete/exists(namespace, key)` — 기본 CRUD
- `get_json/set_json(namespace, key)` — JSON 직렬화/역직렬화
- `clear_namespace(namespace)` — `SCAN` + `DEL`

**캐시 네임스페이스 패턴:**

| 네임스페이스 | 키 포맷 | TTL | 용도 |
|-------------|---------|-----|------|
| `kis:token` | (단일 키) | 23h | OAuth 토큰 |
| `kis:price` | `{symbol}` | 10s | 현재가 |
| `kis:ohlcv` | `{symbol}` | 5min | 일봉 캐시 |
| `kis:master` | (단일 키) | 24h | 종목 마스터 |
| `data:{provider}` | `{key}` | varies | Phase 2+ 용 |

---

### Step 4: BrokerInterface ABC + KIS Auth → commit #4
> `"Phase 1: BrokerInterface ABC + KIS OAuth token management"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/broker/__init__.py` | 빈 패키지 |
| `src/broker/base.py` | `BrokerInterface` ABC |
| `src/broker/kis/__init__.py` | 빈 패키지 |
| `src/broker/kis/auth.py` | `KISAuth` — Redis 기반 토큰 관리 |

**BrokerInterface ABC 메서드:**
```python
class BrokerInterface(ABC):
    async def get_price(self, symbol: str) -> PriceInfo
    async def get_daily_ohlcv(self, symbol: str, *, period_days: int = 100) -> list[OHLCV]
    async def place_order(self, order: OrderRequest) -> OrderResult
    async def cancel_order(self, order_id: str) -> bool
    async def get_balance(self) -> AccountBalance
    async def get_positions(self) -> list[Position]
    async def get_stock_master(self) -> list[StockInfo]
    async def connect(self) -> None
    async def disconnect(self) -> None
```

**KISAuth 토큰 전략:**
1. `get_token()`: Redis 캐시 조회 → 캐시 히트면 즉시 반환
2. 캐시 미스 (TTL 23시간 만료): POST `/oauth2/tokenP`로 새 토큰 발급
3. API 호출 중 토큰 만료 에러 (`EGW00123`, `EGW00121`): `refresh_token()`으로 강제 재발급 + 1회 재시도
4. 백그라운드 갱신 불필요 — 1시간 여유(23h TTL vs 24h 유효)로 lazy refresh 보장

**build_headers() — TR ID 자동 변환:**
- 모의투자(`KIS_IS_PAPER=true`): T/J/C 접두사 → V 접두사 (예: TTTC0012U → VTTC0012U)
- F 접두사(FHKST...)는 조회용으로 모의/실전 동일 — 변환하지 않음
- 실전투자: TR ID 변경 없음

**build_headers() — 필수 헤더:**
```python
headers = {
    "Content-Type": "application/json",
    "Accept": "text/plain",
    "charset": "UTF-8",
    "User-Agent": "stock-trading-agent/0.2.0",
    "authorization": f"Bearer {token}",
    "appkey": settings.KIS_APP_KEY,
    "appsecret": settings.KIS_APP_SECRET,
    "tr_id": tr_id,
    "custtype": "P",       # 개인고객
    "tr_cont": tr_cont,    # 페이지네이션용
}
```

**토큰 재요청 참고:**
> KIS는 6시간 이내 토큰 재요청 시 기존 토큰을 그대로 반환 (신규 발급 안 됨).
> Redis TTL 23시간 전략에 영향 없으나 디버깅 시 참고.

**connect()/disconnect() 패턴:**
- `connect()`: `aiohttp.ClientSession` 생성 + 초기 토큰 발급
- `disconnect()`: `ClientSession.close()` + 리소스 정리

**KIS API Base URL:**
- 모의투자: `https://openapivts.koreainvestment.com:29443`
- 실전투자: `https://openapi.koreainvestment.com:9443`

---

### Step 5: KIS 응답 모델 + REST 클라이언트 → commit #5
> `"Phase 1: KIS response models + REST client with rate limiting"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/broker/kis/models.py` | KIS 응답 파싱 Pydantic 모델 (raw str → Decimal/int 변환) |
| `src/broker/kis/client.py` | `KISClient(BrokerInterface)` — KIS REST 클라이언트 |

**KIS 응답 모델:**
- `KISBaseResponse` — 공통 래퍼 (`rt_cd`, `msg_cd`, `msg1`, `is_ok` property)
- `KISPriceOutput` — 현재가 (stck_prpr, stck_oprc, stck_hgpr, stck_lwpr 등)
- `KISDailyChartOutput` — 일봉 (stck_bsop_date, stck_clpr, acml_vol 등)
- `KISOrderOutput` — 주문 응답 (ODNO, ORD_TMD 등)
- `KISBalanceOutput1` — 보유 종목 (pdno, hldg_qty, pchs_avg_pric 등)
- `KISBalanceOutput2` — 계좌 요약 (dnca_tot_amt, tot_evlu_amt 등)

**KISClient 핵심 설계:**

| 기능 | KIS API | TR ID (실전/모의) | 메서드 |
|------|---------|----------|--------|
| 현재가 | GET `/uapi/domestic-stock/v1/quotations/inquire-price` | FHKST01010100 | `get_price()` |
| 일봉 | GET `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | FHKST03010100 | `get_daily_ohlcv()` |
| 주문 | POST `/uapi/domestic-stock/v1/trading/order-cash` | Buy: TTTC0012U/VTTC0012U, Sell: TTTC0011U/VTTC0011U | `place_order()` |
| 잔고 | GET `/uapi/domestic-stock/v1/trading/inquire-balance` | TTTC8434R/VTTC8434R | `get_balance()`, `get_positions()` |
| 종목 마스터 | .mst.zip 다운로드 | — | `get_stock_master()` |

**API 필수 파라미터:**

현재가/일봉 공통:
```python
"FID_COND_MRKT_DIV_CODE": "J"  # J:KRX(기본), NX:NXT, UN:통합
```

일봉 추가:
```python
"FID_ORG_ADJ_PRC": "0"  # 0:수정주가(기본, 분석용), 1:원주가
```

주문 추가:
```python
"EXCG_ID_DVSN_CD": "KRX"  # 거래소 구분 (KRX 또는 SOR)
"SLL_TYPE": "01"           # 매도 시 매도구분 (01:일반)
```

**Rate Limiter:**
- `asyncio.Semaphore(1)` + `asyncio.sleep(KIS_RATE_LIMIT_INTERVAL)` — 동시 1건 + 호출 간격 보장
- paper=0.5s, prod=0.05s

**일봉 페이지네이션 (get_daily_ohlcv):**
- 1회 최대 100건 → 날짜 윈도우 이동으로 순차 수집
- 3년치(~750일) = 8+ 페이지 필요
- 중복 제거: `(symbol, date)` set으로 dedup + 날짜순 정렬

**잔고 페이지네이션 (get_positions):**
- `tr_cont` M/F → 다음 페이지 존재, `CTX_AREA_FK100/NK100` 전달
- 실전: 최대 50건/페이지, 모의: 최대 20건/페이지
- 최대 10 페이지 안전 제한 (실전 500건, 모의 200건까지)

**종목 마스터 (get_stock_master):**
- KOSPI/KOSDAQ .mst.zip 바이너리 다운로드 (인증 불필요)
- cp949 디코딩 → 고정폭 파싱 (KOSPI part2=228바이트, KOSDAQ part2=222바이트)
- 6자리 숫자 종목코드만 필터

**에러 코드 매핑:**

| KIS msg_cd | 의미 | 변환 예외 |
|------------|------|----------|
| `EGW00123`, `EGW00121` | 토큰 만료 | `TokenExpiredError` (자동 재시도) |
| `EGW00201` | Rate limit | `RateLimitError` |
| `APBK0013` | 잔고 부족 | `InsufficientFundsError` |
| 기타 `rt_cd != "0"` | 일반 에러 | `KISResponseError` |

---

### Step 6: DataProvider ABC + KIS Provider → commit #6
> `"Phase 1: DataProvider ABC + KIS data provider with DB persistence"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/data/providers/__init__.py` | 빈 패키지 |
| `src/data/providers/base.py` | `DataProvider` ABC |
| `src/data/providers/kis_provider.py` | `KISDataProvider` — 캐싱 + DB 저장 |

**DataProvider ABC 설계 (Phase 2 확장 고려):**
```python
class DataProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str                    # "kis", "dart", "ecos" 등

    @abstractmethod
    async def initialize(self) -> None                # 연결/인증
    @abstractmethod
    async def shutdown(self) -> None                  # 리소스 정리
    @abstractmethod
    async def health_check(self) -> bool              # 연결 상태

    # 선택 구현 (NotImplementedError 기본)
    async def fetch_stock_master(self) -> list[StockInfo]
    async def fetch_daily_ohlcv(self, symbol, *, start_date, end_date) -> list[OHLCV]
    async def sync_stock_master(self) -> int           # DB upsert, 건수 반환
    async def sync_daily_ohlcv(self, symbol, *, period_days) -> int
```

> **설계 노트**: 데이터 유형이 provider마다 다르므로(시세 vs 공시 vs 매크로 지표 vs 뉴스), 데이터 수집 메서드는 `@abstractmethod`가 아닌 `NotImplementedError` 기본값. `initialize/shutdown/health_check`만 진정한 추상 메서드.

**KISDataProvider:**
- `__init__(client: KISClient, cache: RedisCache, session_factory, settings)` — 의존성 주입
- `sync_stock_master()`: API 수집 → PostgreSQL `ON CONFLICT DO UPDATE` upsert
- `sync_daily_ohlcv()`: API 수집 → `uq_daily_ohlcv_symbol_date` constraint 기반 upsert
- 캐시: `fetch_stock_master/fetch_daily_ohlcv`에서 Redis 캐시 확인 후 API 호출

---

### Step 7: Data Collector + Mock Broker → commit #7
> `"Phase 1: data collector functions + InMemoryBroker"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/data/collector.py` | `collect_stock_master()`, `collect_daily_ohlcv()` — 수동 트리거 함수 |
| `src/broker/mock/__init__.py` | 빈 패키지 |
| `src/broker/mock/client.py` | `InMemoryBroker(BrokerInterface)` — 테스트용 |

**collector.py:**
- `collect_stock_master(provider)` → `provider.sync_stock_master()` 호출
- `collect_daily_ohlcv(provider, symbols, period_days)` → 다중 종목 순차 수집, 실패 건 로그 후 계속

**InMemoryBroker:**
- 사전 주입 가능한 `prices`, `ohlcv_data`, `positions`, `stocks` 딕셔너리/리스트
- `place_order()`: 자동 체결, 순차 주문번호 생성 (`MOCK-000001`)
- 기본 테스트 종목 3개 (삼성전자, SK하이닉스, NAVER)

---

### Step 8: API 라우트 + main.py 등록 → commit #8
> `"Phase 1: data API routes + app registration"`

**신규 파일:**

| 파일 | 역할 |
|------|------|
| `src/api/__init__.py` | 빈 패키지 |
| `src/api/routes/__init__.py` | 빈 패키지 |
| `src/api/routes/data.py` | 데이터 확인용 API 엔드포인트 |

**수정 파일:** `src/main.py` — `app.include_router(data_router)` 추가, version `"0.2.0"`

**엔드포인트:**

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/api/data/stocks` | 종목 마스터 목록 (market 필터, pagination) |
| GET | `/api/data/ohlcv/{symbol}` | 종목 일봉 조회 (날짜 범위 필터) |
| GET | `/api/data/stats` | 수집 데이터 통계 (종목 수, OHLCV 건수 등) |

---

### Step 9: 테스트 → commit #9
> `"Phase 1: broker + data layer tests"`

**신규 파일:**

| 파일 | 테스트 대상 |
|------|-----------|
| `tests/test_broker_kis.py` | KISAuth 토큰, KISClient API 호출, TR ID 변환, 에러 매핑, .mst 파싱, InMemoryBroker |
| `tests/test_data_provider.py` | KISDataProvider DB upsert, 캐시 동작, collector 함수 |
| `tests/test_data_api.py` | /api/data/* 엔드포인트 (빈 DB, 필터, pagination) |

**수정 파일:** `tests/conftest.py` — `mock_broker`, `mock_redis`, `cache` fixture 추가

**테스트 방식:**
- KIS API 호출: `AsyncMock` + `aiohttp.ClientSession` mock (실제 API 호출 없음)
- DB: mock session 또는 SQLite in-memory
- Redis: `AsyncMock` 기반 `RedisCache`
- FastAPI: `httpx.AsyncClient` + `ASGITransport`

---

## 실행 순서

```
1. feature/phase1 브랜치 생성
2. Step 1: 예외 + 설정 → commit #1
3. Step 2: ORM 모델 + migration → commit #2
4. uv run alembic upgrade head (테이블 생성 확인)
5. Step 3: Redis 캐시 래퍼 → commit #3
6. Step 4: BrokerInterface ABC + KIS Auth → commit #4
7. Step 5: KIS 응답 모델 + REST 클라이언트 → commit #5
8. Step 6: DataProvider ABC + KIS Provider → commit #6
9. Step 7: Data Collector + Mock Broker → commit #7
10. Step 8: API 라우트 + main.py → commit #8
11. Step 9: 테스트 → commit #9
12. uv run pytest tests/ -v (전체 테스트 통과)
13. develop에 머지
14. git tag v0.2.0-phase1
```

---

## 검증 방법

```bash
# 1. 인프라 확인
docker compose up -d
uv run alembic upgrade head

# 2. 테이블 확인 (psql)
# \d stock_master
# \d daily_ohlcv

# 3. 전체 테스트
uv run pytest tests/ -v

# 4. 서버 시작
uv run uvicorn src.main:app --reload

# 5. API 확인
curl http://localhost:8000/api/data/stats
curl "http://localhost:8000/api/data/stocks?limit=5"

# 6. (KIS 모의투자 계정 설정 후) 실제 데이터 수집
# .env에 KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO 설정
# Python 콘솔에서:
#   await provider.sync_stock_master()
#   await provider.sync_daily_ohlcv("005930", period_days=750)

# 7. 수집 결과 확인
curl "http://localhost:8000/api/data/stocks?market=kospi&limit=5"
curl "http://localhost:8000/api/data/ohlcv/005930?limit=10"
```

---

## 주의사항

1. **주문 TR ID: KIS 레퍼런스 기준 TTTC0012U/0011U 채택** ✅ (해결됨 — DESIGN.md와 동기화 완료)
2. **F 접두사 TR ID(FHKST...)는 모의/실전 동일** — `build_headers()`에서 변환 제외
3. **.mst 바이너리 파싱**: macOS에서 cp949 디코딩 시 `errors="replace"` 필수
4. **배치 수집 소요시간**: 2000종목 × 8페이지 × 0.5s(모의) ≈ 2.2시간. 초기에는 주요 종목만 수집 권장
5. **OHLCV upsert 성능**: 종목당 ~750건. 대량 수집 시 배치 커밋 고려 (100건 단위)
6. **Alembic env.py**: `from src.db.models import market_data` import 추가 필수 (auto-detect용)

---

## 진행 추적

- [x] Step 1: 예외 + 설정 업데이트 + commit #1
- [x] Step 2: ORM 모델 + Migration + commit #2
- [x] Step 3: Redis 캐시 래퍼 + commit #3
- [x] Step 4: BrokerInterface ABC + KIS Auth + commit #4
- [x] Step 5: KIS 응답 모델 + REST 클라이언트 + commit #5
- [x] Step 6: DataProvider ABC + KIS Provider + commit #6
- [x] Step 7: Data Collector + Mock Broker + commit #7
- [x] Step 8: API 라우트 + main.py + commit #8
- [x] Step 9: 테스트 + commit #9
- [ ] Alembic migration 적용
- [x] 전체 테스트 통과 (217개)
- [ ] develop 머지 + git tag v0.2.0-phase1
