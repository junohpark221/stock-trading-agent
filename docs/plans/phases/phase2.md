# Phase 2: 분석 엔진 — 구현 계획

> **상태**: 구현 대기
> **작성일**: 2026-03-06
> **완료 기준**: 외부 데이터 4개 수집 + 기술적 분석 + 펀더멘털 분석 + Analysis API + 테스트 전체 통과

---

## Context

Phase 1(데이터 수집 레이어)이 완료되어 KIS 브로커 연동, 종목 마스터/OHLCV 수집, Redis 캐싱, Data API가 동작하는 상태다. Phase 2는 이 데이터 위에 **분석 엔진**을 구축한다: 외부 데이터 소스 4개(DART, ECOS, FRED, Naver) 수집 + 기술적 분석 + 펀더멘털 분석. 감성분석은 LLM이 필요하므로 Phase 2에서는 **뉴스 수집만** 하고, 감성 점수 산출은 Phase 3으로 보류한다.

### DESIGN.md 대비 변경/보완 사항

| 항목 | DESIGN.md 현재 | 수정 방향 |
|------|---------------|----------|
| 감성분석 | Phase 2에서 "네이버 검색 API + LLM" 감성분석 | Phase 2는 뉴스 **수집만**, 감성 점수는 Phase 3 |
| `analysis/sentiment/` | Phase 2 생성 파일 목록에 포함 | Phase 3으로 이동. Phase 2에서는 `naver_provider.py`만 |
| Phase 2 DB 테이블 | 미정의 | `financial_statement`, `economic_indicator`, `news_article`, `disclosure` 4개 추가 |
| Phase 2 완료 기준 | "감성 분석" 포함 | "뉴스 수집" 으로 변경 |
| `config.py` Phase 2 | 외부 API 키는 DESIGN.md에 있으나 실제 config.py 미반영 | 외부 API 키 5개 + cache TTL 4개 추가 |
| DataProvider ABC | stock_master/OHLCV 전용 optional 메서드 | 수정 없음. 새 provider는 lifecycle만 상속, 도메인별 자체 메서드 |
| pykrx/FDR | "보조 시장 데이터" 로만 기술 | 유틸리티 함수 (`src/data/utils/`) |
| 외부 API 클라이언트 | opendartreader, fredapi 언급 | aiohttp 직접 호출 (async 일관성) |

---

## 핵심 설계 결정

1. **DataProvider ABC 유지** — lifecycle(initialize/shutdown/health_check) + provider_name만 상속. 기존 optional 메서드(stock_master/OHLCV)는 그대로 두고, 새 provider는 도메인별 자체 메서드 정의
2. **감성분석 Phase 3 보류** — `src/analysis/sentiment/`는 Phase 3에서 LLM과 함께 생성
3. **aiohttp 직접 호출** — DART/ECOS/FRED/Naver 모두 aiohttp(이미 의존성)로 직접 호출. fredapi/opendartreader 사용 안 함
4. **pykrx = 유틸리티 함수** — `src/data/utils/market_helpers.py`에 헬퍼로 구현
5. **기술지표 on-the-fly 계산** — DB 저장 없이 OHLCV에서 pandas-ta로 실시간 계산

---

## 재사용할 기존 코드

| 패턴/파일 | 재사용 위치 |
|----------|-----------|
| `src/data/providers/base.py` DataProvider ABC | 모든 새 provider가 상속 |
| `src/data/providers/kis_provider.py` DB upsert + Redis cache-through | DART/ECOS/FRED/Naver provider 동일 패턴 |
| `src/data/cache.py` RedisCache 싱글톤 | 모든 provider에서 캐싱 |
| `src/db/session.py:get_session_factory()` | 모든 provider/analyzer에서 DB 접근 |
| `src/db/base.py` TimestampMixin | 새 ORM 모델에서 상속 |
| `src/data/collector.py` 오케스트레이션 패턴 | collect_financials/macro/news에서 동일 패턴 |
| `src/api/routes/data.py` API 엔드포인트 패턴 | analysis.py 라우터에서 동일 패턴 |
| `tests/conftest.py` mock_aiohttp_response + AsyncContextManagerMock | 모든 provider 테스트에서 재사용 |

---

## Step 별 구현 계획

### Step 1: 기반 업데이트 (Config + Enums + Exceptions + Dependencies)

**수정 파일:**
- `src/config.py` — Phase 2 외부 API 키 + cache TTL 추가
- `src/core/enums.py` — 새 enum 추가
- `src/core/exceptions.py` — 데이터 프로바이더 예외 추가
- `pyproject.toml` — Phase 2 의존성 추가

**config.py 추가 필드 (Phase 2: Market Data 섹션 아래):**
```python
# Phase 2: External Data Sources
DART_API_KEY: str = ""
ECOS_API_KEY: str = ""
FRED_API_KEY: str = ""
NAVER_CLIENT_ID: str = ""
NAVER_CLIENT_SECRET: str = ""

# Phase 2: Cache TTLs
DART_CACHE_TTL: int = 86400       # 24시간 (재무제표는 자주 안 바뀜)
ECOS_CACHE_TTL: int = 86400       # 24시간
FRED_CACHE_TTL: int = 86400       # 24시간
NEWS_CACHE_TTL: int = 3600        # 1시간
```

**새 Enum:**
```python
class ReportType(StrEnum):
    """DART 재무제표 보고서 종류."""
    ANNUAL = "annual"              # 사업보고서
    SEMI_ANNUAL = "semi_annual"    # 반기보고서
    QUARTERLY = "quarterly"        # 분기보고서

class SentimentLabel(StrEnum):
    """뉴스 감성 분류 (Phase 3에서 사용, 미리 정의)."""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"

class DataSourceType(StrEnum):
    """외부 데이터 소스 식별자."""
    KIS = "kis"
    DART = "dart"
    ECOS = "ecos"
    FRED = "fred"
    NAVER = "naver"
    PYKRX = "pykrx"
```

**새 Exception:**
```python
class DataProviderError(TradingAgentError):
    """외부 데이터 소스 공통 에러."""

class ExternalAPIError(DataProviderError):
    """외부 API 호출 실패 (네트워크, 인증, 응답 오류)."""
```

**예외 트리 업데이트:**
```
TradingAgentError (base)
├── ...기존...
├── DataProviderError
│   └── ExternalAPIError
└── ...기존...
```

**pyproject.toml 추가 의존성:**
```
pandas>=2.2.0
pandas-ta>=0.3.14b
pykrx>=1.0.0
```

**커밋:** `"Phase 2 Step 1: config + enums + exceptions + dependencies"`

---

### Step 2: DB ORM 모델 + 마이그레이션 + Pydantic 모델

**새 파일:**
- `src/db/models/analysis.py` — Phase 2 ORM 모델 4개
- `alembic/versions/003_add_analysis_tables.py` — 마이그레이션

**수정 파일:**
- `src/db/models/__init__.py` — import 추가
- `src/core/models.py` — Pydantic 도메인 모델 추가

**DB 테이블 설계:**

#### 1. `financial_statement` — DART 재무제표
```python
class FinancialStatement(TimestampMixin, Base):
    __tablename__ = "financial_statement"
    __table_args__ = (
        UniqueConstraint("symbol", "fiscal_year", "fiscal_quarter", "report_type",
                         name="uq_financial_statement"),
        Index("ix_financial_statement_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    corp_code: Mapped[str] = mapped_column(String(20), nullable=False)  # DART 고유 기업코드
    report_type: Mapped[str] = mapped_column(String(20), nullable=False)  # ReportType enum
    fiscal_year: Mapped[int] = mapped_column(nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(nullable=True)  # annual은 null

    # 재무 데이터 (Numeric 정밀도)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    operating_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # 투자 지표
    per: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    eps: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    bps: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    # 원본 데이터
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

#### 2. `economic_indicator` — ECOS/FRED 매크로 지표
```python
class EconomicIndicator(TimestampMixin, Base):
    __tablename__ = "economic_indicator"
    __table_args__ = (
        UniqueConstraint("source", "indicator_code", "date",
                         name="uq_economic_indicator"),
        Index("ix_economic_indicator_code", "source", "indicator_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # DataSourceType
    indicator_code: Mapped[str] = mapped_column(String(50), nullable=False)
    indicator_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

#### 3. `news_article` — 뉴스 기사
```python
class NewsArticle(TimestampMixin, Base):
    __tablename__ = "news_article"
    __table_args__ = (
        UniqueConstraint("link", name="uq_news_article_link"),
        Index("ix_news_article_symbol", "symbol"),
        Index("ix_news_article_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # DataSourceType
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)  # null = 시장 전체 뉴스
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str] = mapped_column(String(500), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # 감성분석 (Phase 3에서 채움)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 3), nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
```

#### 4. `disclosure` — DART 공시
```python
class Disclosure(TimestampMixin, Base):
    __tablename__ = "disclosure"
    __table_args__ = (
        UniqueConstraint("receipt_no", name="uq_disclosure_receipt_no"),
        Index("ix_disclosure_symbol", "symbol"),
        Index("ix_disclosure_date", "receipt_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    corp_code: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    report_name: Mapped[str] = mapped_column(String(300), nullable=False)
    receipt_no: Mapped[str] = mapped_column(String(20), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    filer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**새 Pydantic 모델 (core/models.py 추가):**
```python
class FinancialStatementInfo(BaseModel):
    """DART 재무제표 조회 응답."""
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    corp_code: str
    report_type: ReportType
    fiscal_year: int
    fiscal_quarter: int | None = None
    revenue: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    total_assets: Decimal | None = None
    total_equity: Decimal | None = None
    total_liabilities: Decimal | None = None
    per: Decimal | None = None
    pbr: Decimal | None = None
    roe: Decimal | None = None
    eps: Decimal | None = None
    bps: Decimal | None = None

class EconomicIndicatorInfo(BaseModel):
    """경제지표 조회 응답."""
    model_config = ConfigDict(from_attributes=True)

    source: DataSourceType
    indicator_code: str
    indicator_name: str
    date: date
    value: Decimal
    unit: str | None = None

class NewsArticleInfo(BaseModel):
    """뉴스 기사 응답."""
    model_config = ConfigDict(from_attributes=True)

    source: DataSourceType
    symbol: str | None = None
    title: str
    description: str | None = None
    link: str
    published_at: datetime
    sentiment_score: Decimal | None = None
    sentiment_label: SentimentLabel | None = None

class DisclosureInfo(BaseModel):
    """DART 공시 정보 응답."""
    model_config = ConfigDict(from_attributes=True)

    corp_code: str
    symbol: str
    report_name: str
    receipt_no: str
    receipt_date: date
    filer_name: str | None = None

class TechnicalIndicators(BaseModel):
    """기술지표 계산 결과 (DB 미저장, in-memory)."""
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    # 추세
    sma_20: list[float | None] = []
    sma_60: list[float | None] = []
    sma_120: list[float | None] = []
    ema_12: list[float | None] = []
    ema_26: list[float | None] = []
    macd: list[float | None] = []
    macd_signal: list[float | None] = []
    macd_histogram: list[float | None] = []
    # 모멘텀
    rsi_14: list[float | None] = []
    stoch_k: list[float | None] = []
    stoch_d: list[float | None] = []
    # 변동성
    bb_upper: list[float | None] = []
    bb_middle: list[float | None] = []
    bb_lower: list[float | None] = []
    atr_14: list[float | None] = []
    # 거래량
    obv: list[float | None] = []
    # 최신값 스냅샷 (API 응답용)
    latest_rsi: float | None = None
    latest_macd: float | None = None
    latest_bb_position: float | None = None  # (close - bb_lower) / (bb_upper - bb_lower)
    latest_atr: float | None = None

class FundamentalScore(BaseModel):
    """펀더멘털 점수 계산 결과."""
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    overall_score: Decimal        # 0~100 종합 점수
    valuation_score: Decimal      # PER/PBR/ROE 기반
    growth_score: Decimal         # 매출/영업이익 성장률
    profitability_score: Decimal  # 영업이익률, 순이익률
    reasoning: str                # 점수 산출 근거 설명

class PatternSignal(BaseModel):
    """차트 패턴 감지 결과."""
    model_config = ConfigDict(from_attributes=True)

    pattern_name: str             # e.g. "golden_cross", "double_bottom"
    signal_type: str              # "bullish" | "bearish"
    confidence: Decimal           # 0~1
    description: str              # 패턴 설명 (한국어)
```

**커밋:** `"Phase 2 Step 2: analysis DB models + migration + Pydantic models"`

---

### Step 3: DART 데이터 프로바이더

**새 파일:**
- `src/data/providers/dart_provider.py`
- `tests/test_dart_provider.py`

**DartProvider(DataProvider) 구현:**
```python
class DartProvider(DataProvider):
    """DART 공시/재무제표 데이터 제공자.

    - aiohttp으로 DART Open API 직접 호출
    - symbol → corp_code 매핑을 Redis에 캐싱
    - 재무제표/공시를 DB에 upsert

    DART API 엔드포인트:
    - https://opendart.fss.or.kr/api/list.json — 공시 검색
    - https://opendart.fss.or.kr/api/fnlttSinglAcnt.json — 단일 기업 재무제표
    - https://opendart.fss.or.kr/api/company.json — 기업 개황 (symbol→corp_code)
    """

    provider_name = "dart"

    def __init__(self, *, cache, session_factory, settings): ...
    async def initialize(self): ...    # aiohttp.ClientSession 생성
    async def shutdown(self): ...      # 세션 닫기
    async def health_check(self): ...  # API 키 존재 여부

    # corp_code 매핑
    async def _resolve_corp_code(self, symbol: str) -> str | None:
        """symbol → corp_code 변환. Redis 캐시 우선, miss시 API 호출."""

    # 데이터 조회
    async def fetch_disclosures(self, corp_code, start_date, end_date) -> list[DisclosureInfo]: ...
    async def fetch_financial_statement(self, corp_code, fiscal_year, report_type) -> FinancialStatementInfo | None: ...

    # DB sync
    async def sync_disclosures(self, symbol: str) -> int: ...
    async def sync_financials(self, symbol: str, fiscal_year: int) -> int: ...
```

**패턴:** KISDataProvider의 DB upsert + Redis cache-through 패턴 재사용

**DART report_type → reprt_code 매핑:**
- `annual` → `11011` (사업보고서)
- `semi_annual` → `11012` (반기보고서)
- `quarterly` → `11013` (1분기), `11014` (3분기)

**corp_code 변환 주의사항:**
- DART는 자체 `corp_code` (8자리) 사용. 종목코드(`symbol`)와 다름
- `company.json?stock_code={symbol}` API로 매핑 조회
- 매핑 결과를 `dart:corp_code:{symbol}` 키로 Redis 캐싱 (TTL 7일)

**커밋:** `"Phase 2 Step 3: DART data provider"`

---

### Step 4: ECOS + FRED 데이터 프로바이더

**새 파일:**
- `src/data/providers/ecos_provider.py`
- `src/data/providers/fred_provider.py`
- `tests/test_macro_providers.py`

**EcosProvider(DataProvider):**
```python
class EcosProvider(DataProvider):
    """한국은행 ECOS 경제통계 데이터 제공자.

    API: https://ecos.bok.or.kr/api/StatisticSearch/{API_KEY}/json/kr/1/100/{통계표코드}/{주기}/{시작일}/{종료일}/{항목코드}
    """
    provider_name = "ecos"

    async def fetch_indicator(self, indicator_code, start_date, end_date) -> list[EconomicIndicatorInfo]: ...
    async def sync_indicators(self, indicator_codes: list[str]) -> int: ...
```

**ECOS 주요 지표 코드:**
| 지표 | 통계표코드 | 항목코드 | 주기 |
|------|-----------|---------|------|
| 기준금리 | 722Y001 | 0101000 | D |
| 원/달러 환율 | 731Y003 | 0000001 | D |
| 소비자물가지수 | 901Y009 | 0 | M |
| GDP 성장률 | 200Y002 | 10111 | Q |
| M2 통화량 | 101Y003 | BBGA00 | M |

**FredProvider(DataProvider):**
```python
class FredProvider(DataProvider):
    """FRED (미국 연준) 경제 데이터 제공자.

    API: https://api.stlouisfed.org/fred/series/observations?series_id={ID}&api_key={KEY}&file_type=json
    """
    provider_name = "fred"

    async def fetch_series(self, series_id, start_date, end_date) -> list[EconomicIndicatorInfo]: ...
    async def sync_series(self, series_ids: list[str]) -> int: ...
```

**FRED 주요 시리즈:**
| 지표 | series_id | 설명 |
|------|-----------|------|
| Fed 기금금리 | FEDFUNDS | 미국 기준금리 |
| CPI | CPIAUCSL | 소비자물가지수 |
| 실업률 | UNRATE | 미국 실업률 |
| 10년물 국채 | GS10 | 장기 금리 |
| VIX | VIXCLS | 변동성 지수 |

**공통 패턴:** 두 provider 모두 "코드 + 날짜 → 값" 구조로 `economic_indicator` 테이블에 저장

**커밋:** `"Phase 2 Step 4: ECOS + FRED macro data providers"`

---

### Step 5: Naver 뉴스 데이터 프로바이더

**새 파일:**
- `src/data/providers/naver_provider.py`
- `tests/test_naver_provider.py`

**NaverProvider(DataProvider):**
```python
class NaverProvider(DataProvider):
    """네이버 뉴스 검색 API 데이터 제공자.

    API: https://openapi.naver.com/v1/search/news.json
    인증: X-Naver-Client-Id, X-Naver-Client-Secret 헤더
    일일 한도: 25,000회

    sentiment_score/label 필드는 null로 저장 (Phase 3에서 LLM으로 채움)
    """
    provider_name = "naver"

    async def fetch_news(self, query, display=100, sort="date") -> list[NewsArticleInfo]: ...
    async def sync_news(self, symbol: str, query: str) -> int: ...  # DB upsert, 중복 link 제외
```

**네이버 뉴스 API 파라미터:**
- `query`: 검색어 (종목명 또는 "삼성전자 주가")
- `display`: 결과 개수 (최대 100)
- `start`: 시작 위치 (1~1000)
- `sort`: `date` (최신순) | `sim` (정확도순)

**HTML 태그 제거:** 네이버 API 응답의 `title`/`description`에 `<b>` 태그 포함. 저장 전 strip 처리.

**커밋:** `"Phase 2 Step 5: Naver news data provider"`

---

### Step 6: 기술적 분석 — 지표 계산

**새 파일:**
- `src/analysis/__init__.py`
- `src/analysis/technical/__init__.py`
- `src/analysis/technical/indicators.py`
- `tests/test_technical_indicators.py`

**주요 함수 (모두 상세 한국어 주석 필수 — DESIGN.md RSI 예시 형식 준수):**

| 카테고리 | 함수 | pandas-ta 활용 | 설명 |
|---------|------|---------------|------|
| 추세 | `calculate_sma(prices, periods=[20,60,120])` | `ta.sma()` | 단순이동평균 |
| 추세 | `calculate_ema(prices, periods=[12,26])` | `ta.ema()` | 지수이동평균 |
| 추세 | `calculate_macd(prices)` | `ta.macd()` | MACD + signal + histogram |
| 모멘텀 | `calculate_rsi(prices, period=14)` | `ta.rsi()` | 상대강도지수 |
| 모멘텀 | `calculate_stochastic(high, low, close)` | `ta.stoch()` | 스토캐스틱 K/D |
| 변동성 | `calculate_bollinger_bands(prices, period=20)` | `ta.bbands()` | 볼린저밴드 |
| 변동성 | `calculate_atr(high, low, close, period=14)` | `ta.atr()` | 평균 진폭 |
| 거래량 | `calculate_obv(close, volume)` | `ta.obv()` | 누적 거래량 |

**통합 함수:**
```python
def compute_all_indicators(df: pd.DataFrame) -> TechnicalIndicators:
    """OHLCV DataFrame에서 모든 기술지표를 한번에 계산하여 TechnicalIndicators 반환.

    Args:
        df: columns=['open','high','low','close','volume'], index=date

    Returns:
        TechnicalIndicators with all computed series + latest snapshot values
    """
```

**주석 형식 (DESIGN.md 예시 준수):**
```python
def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI (Relative Strength Index) — 상대강도지수

    [매매 로직에서의 역할]
    - 과매수/과매도 판단의 핵심 지표
    - RSI > 70: 과매수 → 매도 시그널 고려
    - RSI < 30: 과매도 → 매수 시그널 고려

    [계산 방식]
    1. 가격 변화량(delta) = 현재가 - 전일가
    ...

    [우리 전략에서의 활용]
    - 포지션 트레이딩: RSI < 35 + 이동평균선 지지 → 매수 후보
    - 스윙 트레이딩: RSI < 25 → 단기 반등 매수, RSI > 75 → 단기 매도
    """
```

**커밋:** `"Phase 2 Step 6: technical indicators (pandas-ta)"`

---

### Step 7: 기술적 분석 — 차트 패턴 감지

**새 파일:**
- `src/analysis/technical/patterns.py`
- `tests/test_technical_patterns.py`

**주요 함수 (상세 주석 필수):**
```python
def detect_golden_cross(sma_short, sma_long) -> PatternSignal | None:
    """골든크로스 — 단기 MA가 장기 MA를 상향 돌파"""

def detect_death_cross(sma_short, sma_long) -> PatternSignal | None:
    """데드크로스 — 단기 MA가 장기 MA를 하향 돌파"""

def detect_support_resistance(prices, window=20) -> dict:
    """지지/저항선 탐지 (로컬 최저/최고 기반)"""

def detect_double_top(prices) -> PatternSignal | None:
    """이중 천장 — M자 패턴, 하락 반전 시그널"""

def detect_double_bottom(prices) -> PatternSignal | None:
    """이중 바닥 — W자 패턴, 상승 반전 시그널"""

def detect_macd_divergence(prices, macd) -> PatternSignal | None:
    """MACD 다이버전스 — 가격/지표 괴리, 추세 반전 경고"""
```

**통합 함수:**
```python
def scan_patterns(df: pd.DataFrame, indicators: TechnicalIndicators) -> list[PatternSignal]:
    """모든 패턴을 스캔하여 감지된 패턴 목록 반환."""
```

**커밋:** `"Phase 2 Step 7: chart pattern detection"`

---

### Step 8: 펀더멘털 분석

**새 파일:**
- `src/analysis/fundamental/__init__.py`
- `src/analysis/fundamental/analyzer.py`
- `tests/test_fundamental.py`

**FundamentalAnalyzer 클래스:**
```python
class FundamentalAnalyzer:
    """펀더멘털 분석기 — DART 재무제표 기반 종합 점수 산출.

    점수 구성 (0~100):
    - valuation_score (40%): PER/PBR/ROE 기반 밸류에이션
    - growth_score (30%): 매출/영업이익 YoY 성장률
    - profitability_score (30%): 영업이익률, 순이익률
    """

    def __init__(self, session_factory): ...

    async def analyze(self, symbol: str) -> FundamentalScore:
        """종합 펀더멘털 점수 산출."""

    def _score_valuation(self, fs: FinancialStatementInfo) -> Decimal:
        """PER/PBR/ROE 기반 밸류에이션 점수 (0~100)."""

    def _score_growth(self, statements: list[FinancialStatementInfo]) -> Decimal:
        """매출/영업이익 성장률 점수 (0~100)."""

    def _score_profitability(self, fs: FinancialStatementInfo) -> Decimal:
        """영업이익률, 순이익률 점수 (0~100)."""

    async def compare_sector(self, symbol: str, sector: str) -> dict:
        """섹터 대비 비교 (향후 확장)."""
```

**입력:** `financial_statement` 테이블 데이터 (DartProvider가 수집)
**출력:** `FundamentalScore` (overall_score, valuation_score, growth_score, profitability_score, reasoning)

**커밋:** `"Phase 2 Step 8: fundamental analyzer"`

---

### Step 9: 데이터 수집 오케스트레이션 + pykrx 유틸리티

**새 파일:**
- `src/data/utils/__init__.py`
- `src/data/utils/market_helpers.py`

**수정 파일:**
- `src/data/collector.py` — 새 수집 함수 추가

**collector.py 추가 함수:**
```python
async def collect_financials(
    provider: DartProvider,
    symbols: list[str],
    fiscal_year: int,
) -> CollectionSummary:
    """DartProvider로 재무제표 수집 오케스트레이션. 실패 허용."""

async def collect_macro_indicators(
    ecos: EcosProvider,
    fred: FredProvider,
) -> dict[str, int]:
    """ECOS + FRED 동시 수집. {"ecos": n, "fred": m} 반환."""

async def collect_news(
    provider: NaverProvider,
    symbols: list[str],
) -> CollectionSummary:
    """NaverProvider로 뉴스 수집 오케스트레이션. 실패 허용."""
```

**market_helpers.py:**
```python
async def fetch_pykrx_ohlcv(symbol: str, start: str, end: str) -> pd.DataFrame:
    """pykrx로 히스토리컬 OHLCV 조회 (sync → asyncio.to_thread 래핑)."""

async def validate_ohlcv_with_pykrx(symbol: str, date_range: tuple[str, str]) -> dict:
    """KIS 데이터와 pykrx 데이터 교차 검증. 차이 통계 반환."""
```

**커밋:** `"Phase 2 Step 9: collection orchestration + pykrx helpers"`

---

### Step 10: Analysis API + 통합 테스트 + 문서 업데이트

**새 파일:**
- `src/api/routes/analysis.py`
- `tests/test_analysis_api.py`
- `tests/test_analysis_integration.py`

**수정 파일:**
- `src/main.py` — analysis 라우터 등록
- `docs/TRADING_LOGIC.md` — 기술지표 + 펀더멘털 분석 섹션 작성
- `docs/DESIGN.md` — Phase 2 완료 상태 반영

**API 엔드포인트:**
```
GET /api/analysis/technical/{symbol}     — 기술지표 계산 결과 (TechnicalIndicators)
GET /api/analysis/fundamental/{symbol}   — 펀더멘털 점수 (FundamentalScore)
GET /api/analysis/macro                  — 최근 매크로 지표 조회
GET /api/data/financials/{symbol}        — DART 재무제표 raw 데이터
GET /api/data/news/{symbol}              — 수집된 뉴스 목록
GET /api/data/disclosures/{symbol}       — DART 공시 목록
```

**analysis.py 라우터:**
- `/api/analysis` prefix, `["analysis"]` tag
- 기술지표: OHLCV DB 조회 → pandas-ta 실시간 계산 → TechnicalIndicators 반환
- 펀더멘털: FundamentalAnalyzer.analyze(symbol) → FundamentalScore 반환
- 매크로: economic_indicator 테이블 최근 데이터 조회

**data.py 라우터 추가 엔드포인트 (기존 라우터에 추가):**
- financials, news, disclosures는 데이터 조회이므로 `/api/data` 프리픽스

**통합 테스트:**
- Mock OHLCV → 기술지표 계산 → 패턴 스캔 전체 파이프라인
- Mock DART 응답 → DB 저장 → 펀더멘털 분석
- Mock Naver 응답 → DB 저장 → API 조회
- 모든 API 엔드포인트 응답 스키마 검증

**커밋:** `"Phase 2 Step 10: analysis API + integration tests + docs"`

---

## 파일 생성/수정 요약

### 새 파일 (약 22개)
```
src/analysis/__init__.py
src/analysis/technical/__init__.py
src/analysis/technical/indicators.py
src/analysis/technical/patterns.py
src/analysis/fundamental/__init__.py
src/analysis/fundamental/analyzer.py
src/data/providers/dart_provider.py
src/data/providers/ecos_provider.py
src/data/providers/fred_provider.py
src/data/providers/naver_provider.py
src/data/utils/__init__.py
src/data/utils/market_helpers.py
src/api/routes/analysis.py
src/db/models/analysis.py
alembic/versions/003_add_analysis_tables.py
tests/test_dart_provider.py
tests/test_macro_providers.py
tests/test_naver_provider.py
tests/test_technical_indicators.py
tests/test_technical_patterns.py
tests/test_fundamental.py
tests/test_analysis_api.py
tests/test_analysis_integration.py
```

### 수정 파일 (약 8개)
```
src/config.py                  — 외부 API 키 + cache TTL 추가
src/core/enums.py              — ReportType, SentimentLabel, DataSourceType 추가
src/core/exceptions.py         — DataProviderError, ExternalAPIError 추가
src/core/models.py             — 7개 Pydantic 모델 추가
src/db/models/__init__.py      — analysis 모델 import
src/data/collector.py          — 수집 오케스트레이션 함수 추가
src/main.py                    — analysis 라우터 등록
pyproject.toml                 — pandas, pandas-ta, pykrx 추가
docs/DESIGN.md                 — Phase 2 완료 반영
docs/TRADING_LOGIC.md          — 기술지표 + 펀더멘털 섹션 작성
```

---

## 검증 방법

1. **단위 테스트**: `uv run pytest tests/ -v` — 모든 테스트 통과 (기존 217개 + Phase 2 신규)
2. **기술지표 검증**: 삼성전자(005930) mock OHLCV → RSI, MACD, BB 등 계산 → 값 범위 검증
3. **펀더멘털 검증**: Mock DART 재무제표 → FundamentalScore 산출 → 점수 합리성 확인
4. **Provider 검증**: 각 provider의 HTTP 호출을 mock → DB upsert → 조회 일치 확인
5. **API 검증**: httpx AsyncClient로 analysis 엔드포인트 응답 스키마 검증
6. **통합 파이프라인**: OHLCV → 기술지표 → 패턴스캔 전체 흐름 mock 테스트
7. **문서 확인**: TRADING_LOGIC.md에 기술지표/펀더멘털 섹션 작성 완료 확인
