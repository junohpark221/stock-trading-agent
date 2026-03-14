# Phase 3: LLM 에이전트 시스템 — 구현 계획

> **상태**: 대기
> **작성일**: 2026-03-14
> **완료 기준**: 4단계 에이전트 파이프라인 실행 → decision_log 체인 기록 → Mock 환경 E2E 동작

---

## Context

Phase 2 완료 상태에서 Multi-LLM 에이전트 시스템을 구축한다. 기존 분석 엔진(기술/패턴/펀더멘털/매크로/뉴스수집) 위에 LLM 프로바이더 추상화, DB 기반 모델 라우팅(에스컬레이션 포함), 키워드+LLM 하이브리드 감성분석, 4단계 에이전트 파이프라인, 의사결정 감사 추적을 구현한다.

### DESIGN.md 대비 변경 사항

| 항목 | 기존 설계 | 변경 |
|------|----------|------|
| DB 테이블 | 6개 (decision_log, agent_analyses, agent_decisions, agent_memory, agent_model_config, llm_usage) | **3개만** (decision_log, agent_model_config, llm_usage). agent_analyses/decisions는 decision_log에 통합, agent_memory는 Phase 4로 |
| 감성분석 | LLM 전수 분석 | **하이브리드**: 키워드 1차 분류 + Stock Analyst가 중요 뉴스만 LLM 심층 분석 |
| agent_memory | Phase 3 구현 | **Phase 4로 이동** (포지션 관리와 통합) |
| LLM Web Search | Phase 3 | **Phase 4 이후로 이동** |
| 모델 라인업 | o3, GPT-5, Gemini 2.5, Claude Sonnet 4.6 | **업데이트**: o3-deep-research, GPT-5.4, Gemini 3.1 Pro, Claude Sonnet 4.5 등 |

### 모델 라인업 (2026년 3월 확정)

**OpenAI** (주력):

| 모델 | ID | Input $/MTok | Output $/MTok | Context | 용도 |
|------|----|-------------|---------------|---------|------|
| o3 Deep Research | `o3-deep-research` | $2.00 | $8.00 | 200K | Trader, Risk Manager (고정) |
| o4-mini Deep Research | `o4-mini-deep-research` | ~$1.00 | ~$4.00 | 200K | Risk Manager 대안 |
| GPT-5.4 | `gpt-5.4` | $1.25 | $10.00 | 1.05M | 에스컬레이션 대상 |
| GPT-5.4 Pro | `gpt-5.4-pro` | $5.00 | $25.00 | 1.05M | 최고 품질 필요 시 |
| GPT-5 Mini | `gpt-5-mini` | $0.25 | $2.00 | 400K | 범용 분석 |
| GPT-5 Nano | `gpt-5-nano` | $0.05 | $0.40 | 400K | 리포트 생성, 저비용 |

**Google** (보조):

| 모델 | ID | Input $/MTok | Output $/MTok | Context | 용도 |
|------|----|-------------|---------------|---------|------|
| Gemini 3.1 Pro | `gemini-3.1-pro` | $2.00 | $12~18 | 1M | 심층 분석 |
| Gemini 3 Flash | `gemini-3-flash` | $0.50 | $3.00 | 1M | Stock Analyst 1차 |
| Gemini 3.1 Flash-Lite | `gemini-3.1-flash-lite` | $0.25 | $1.50 | 1M | Market Analyst 1차, 저비용 |

**Anthropic** (백업):

| 모델 | ID | Input $/MTok | Output $/MTok | Context | 용도 |
|------|----|-------------|---------------|---------|------|
| Claude Opus 4.6 | `claude-opus-4-6` | $5.00 | $25.00 | 1M | 최고 품질 백업 |
| Claude Sonnet 4.5 | `claude-sonnet-4-5-20250929` | $3.00 | $15.00 | 1M | 범용 백업 |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | $1.00 | $5.00 | 200K | 저비용 백업 |

**공통**: 모든 모델 function calling + structured output 지원. Batch API 50% 할인 가능.

### 기본 모델 할당

```yaml
trader:           fixed    → openai/o3-deep-research
risk_manager:     fixed    → openai/o4-mini-deep-research
stock_analyst:    escalation → google/gemini-3-flash → openai/o3-deep-research (threshold: 0.60)
market_analyst:   escalation → google/gemini-3.1-flash-lite → openai/gpt-5-mini (threshold: 0.60)
report_generator: fixed    → openai/gpt-5-nano
```

---

## 재사용할 기존 코드

| 패턴/파일 | 재사용 위치 |
|----------|-----------|
| `src/data/providers/base.py` DataProvider ABC | LLMProvider ABC 설계 참조 |
| `src/data/cache.py` RedisCache 싱글톤 | LLM Router 설정 캐싱, 응답 캐싱 |
| `src/db/session.py:get_session_factory()` | DecisionRecorder, CostTracker DB 접근 |
| `src/db/base.py` TimestampMixin | 새 ORM 모델에서 상속 |
| `src/analysis/technical/` 기술 지표/패턴 | 에이전트 도구 함수에서 래핑 |
| `src/analysis/fundamental/` 펀더멘털 분석 | 에이전트 도구 함수에서 래핑 |
| `src/data/providers/naver_provider.py` 뉴스 수집 | 감성분석에서 뉴스 조회 |
| `src/data/providers/ecos_provider.py`, `fred_provider.py` | Market Analyst 도구에서 사용 |
| `tests/conftest.py` mock 패턴 | 모든 provider 테스트에서 재사용 |

---

## Step 별 구현 계획

### Step 1: 기반 업데이트 (Config + Pydantic 모델 + 의존성)

**파일 수정:**
- `pyproject.toml` — `openai>=1.60.0`, `anthropic>=0.40.0`, `google-genai>=1.0.0`, `pyyaml>=6.0.0` 추가
- `src/config.py` — LLM 세부 설정 추가 (escalation threshold, response cache TTL, config cache TTL, budget warning %)
- `src/core/models.py` — Phase 3 Pydantic 모델 추가: `LLMMessage`, `LLMResponse`, `AgentModelConfig`, `MarketCondition`, `StockAnalysis`, `RiskAssessment`, `TradeDecision`, `SentimentResult`, `PipelineResult`
- `src/core/enums.py` — `SentimentMethod` enum 추가 (keyword/llm)
- `.env.example` — Phase 3 환경변수

**테스트:** 새 모델 직렬화/역직렬화, Settings 기본값

**커밋:** `"Phase 3 Step 1: config + pydantic models + dependencies"`

---

### Step 2: DB ORM + 마이그레이션 (3개 테이블)

**새 파일:**
- `src/db/models/llm.py` — `DecisionLog`, `AgentModelConfigDB`, `LLMUsage` ORM
- `alembic/versions/004_add_llm_agent_tables.py`

**수정:** `src/db/models/__init__.py`

**핵심 테이블:**

#### 1. `decision_log` — 통합 의사결정 감사 추적
```python
class DecisionLog(TimestampMixin, Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    decision_id: Mapped[UUID] = mapped_column(unique=True)
    parent_id: Mapped[UUID | None]   # 연관 의사결정 체인
    session_id: Mapped[UUID]         # 분석 세션 (한 사이클 묶음)

    # 분류
    stage: Mapped[str]               # market_analysis, stock_analysis, risk_check, trade_decision
    agent_type: Mapped[str | None]   # market_analyst, stock_analyst, risk_manager, trader
    symbol: Mapped[str | None]

    # LLM 정보
    llm_provider: Mapped[str | None]
    llm_model: Mapped[str | None]
    llm_prompt: Mapped[str | None]   # 요약 또는 전문
    llm_response: Mapped[str | None]
    llm_tokens_in: Mapped[int | None]
    llm_tokens_out: Mapped[int | None]
    llm_cost_usd: Mapped[Decimal | None]

    # 의사결정 내용
    decision: Mapped[str]            # buy, sell, hold, approve, reject 등
    confidence: Mapped[Decimal | None]
    reasoning: Mapped[str]           # 판단 근거 요약
    data_snapshot: Mapped[dict | None]  # JSONB

    # 결과 (사후 기록)
    outcome: Mapped[str | None]
    outcome_pnl: Mapped[Decimal | None]
    outcome_note: Mapped[str | None]
```

#### 2. `agent_model_config` — 에이전트별 모델 할당
```python
class AgentModelConfigDB(TimestampMixin, Base):
    __tablename__ = "agent_model_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_type: Mapped[str] = mapped_column(unique=True)  # trader, risk_manager, ...
    routing_mode: Mapped[str]        # fixed, escalation
    primary_model: Mapped[str]       # openai/o3-deep-research, ...
    escalation_model: Mapped[str | None]
    confidence_threshold: Mapped[Decimal | None]
    is_active: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str] = mapped_column(default="system")
```

#### 3. `llm_usage` — 일별 LLM 사용량 집계
```python
class LLMUsage(TimestampMixin, Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    date: Mapped[date]
    provider: Mapped[str]            # openai, google, anthropic
    model: Mapped[str]               # gpt-5.4, gemini-3-flash, ...
    agent_type: Mapped[str | None]   # trader, stock_analyst, ...
    tokens_in: Mapped[int] = mapped_column(default=0)
    tokens_out: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(default=0)
    call_count: Mapped[int] = mapped_column(default=0)
    escalation_count: Mapped[int] = mapped_column(default=0)
```

**커밋:** `"Phase 3 Step 2: DB ORM 3 tables + migration"`

---

### Step 3: LLMProvider ABC + MockLLMProvider

**새 파일:**
- `src/llm/__init__.py`
- `src/llm/base.py` — LLMProvider ABC
- `src/llm/providers/__init__.py`
- `src/llm/providers/mock.py` — MockLLMProvider
- `tests/test_llm_base.py`

**ABC 메서드:**
```python
class LLMProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[LLMMessage], tools: list[Tool] | None = None) -> LLMResponse: ...

    @abstractmethod
    async def structured_output(self, messages: list[LLMMessage], schema: type[BaseModel]) -> BaseModel: ...

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
```

**MockLLMProvider:** deterministic 응답, cost=0, 테스트에서 응답 주입 가능. 이후 모든 Step에서 사용.

**커밋:** `"Phase 3 Step 3: LLMProvider ABC + MockLLMProvider"`

---

### Step 4: OpenAI + Google + Anthropic 프로바이더

**새 파일:**
- `src/llm/providers/openai.py` — AsyncOpenAI SDK, `response_format=json_schema`
- `src/llm/providers/google.py` — google.genai SDK, `response_schema` + `response_mime_type`
- `src/llm/providers/anthropic.py` — AsyncAnthropic SDK, tool_use 패턴으로 structured output, prompt caching
- `tests/test_llm_providers.py` — SDK mock 단위 테스트

**각 프로바이더 내 `_PRICING` dict로 모델별 비용 계산.**

**커밋:** `"Phase 3 Step 4: OpenAI + Google + Anthropic providers"`

---

### Step 5: LLM Router + Cost Tracker + Admin API

**새 파일:**
- `src/llm/router.py` — DB config 조회(Redis 캐시 5분) → fixed/escalation 라우팅 → CostTracker 기록
- `src/llm/cost_tracker.py` — llm_usage 일별 upsert, 월간 예산 체크, 80% 경고 + 100% 시 에스컬레이션 비활성화
- `config/default_model_assignments.yaml` — 위 기본 모델 할당
- `src/api/admin/__init__.py`
- `src/api/admin/llm_config.py` — CRUD + reset + 사용량 조회 (6개 엔드포인트)
- `tests/test_llm_router.py`
- `tests/test_cost_tracker.py`

**Admin API 엔드포인트:**
```
GET    /api/admin/llm/config              — 전체 에이전트 모델 설정 조회
GET    /api/admin/llm/config/{agent_type} — 특정 에이전트 설정 조회
PUT    /api/admin/llm/config/{agent_type} — 에이전트 모델 변경 (Redis 캐시 즉시 무효화)
POST   /api/admin/llm/config/reset        — 기본값으로 초기화
GET    /api/admin/llm/models              — 사용 가능한 모델 목록 + 가격 정보
GET    /api/admin/llm/usage               — LLM 사용량/비용 통계 조회
```

**에스컬레이션 흐름:** 1차 모델 → confidence < threshold → 프리미엄 모델 재분석 → 결과 비교 후 높은 confidence 채택

**커밋:** `"Phase 3 Step 5: LLM router + cost tracker + admin API"`

---

### Step 6: 키워드 기반 감성분석 (하이브리드 1차)

**새 파일:**
- `src/analysis/sentiment/__init__.py`
- `src/analysis/sentiment/analyzer.py` — KeywordSentimentAnalyzer
- `src/analysis/sentiment/keywords.py` — 한국어 긍정/부정/중요 키워드 사전
- `tests/test_sentiment.py`

**KeywordSentimentAnalyzer:**
- 한국어 긍정/부정/중요 키워드 사전으로 1차 분류 (비용 $0)
- `needs_llm_analysis()`: low confidence(0.3~0.5) 또는 중요 키워드(공시, 인수합병 등) 매칭 시 True → Stock Analyst LLM 심층 분석 트리거
- `news_article` 테이블의 sentiment_score/label/method 업데이트 함수

**커밋:** `"Phase 3 Step 6: keyword sentiment analyzer (hybrid phase 1)"`

---

### Step 7: Decision Recorder + 에이전트 도구 함수

**새 파일:**
- `src/agent/__init__.py`
- `src/agent/decision_recorder.py`
- `src/agent/tools/__init__.py`
- `src/agent/tools/technical.py` — 기술 지표 조회 (indicators.py, patterns.py 래핑)
- `src/agent/tools/fundamental.py` — 재무 데이터 조회 (analyzer.py 래핑)
- `src/agent/tools/market_data.py` — 시세/호가 조회 (KIS provider 래핑)
- `src/agent/tools/news.py` — 뉴스/공시 조회 (naver/dart provider 래핑)
- `tests/test_decision_recorder.py`
- `tests/test_agent_tools.py`

**DecisionRecorder:** decision_log 삽입, session_id/parent_id 체인 관리, 사후 outcome 업데이트

**도구 함수:** 기존 analysis/data 모듈을 래핑하여 LLM tool_use 호출용 dict 반환

**커밋:** `"Phase 3 Step 7: decision recorder + agent tool functions"`

---

### Step 8: 4개 에이전트 + 프롬프트

**새 파일:**
- `src/agent/agents/__init__.py`
- `src/agent/agents/base.py` — BaseAgent ABC
- `src/agent/agents/market_analyst.py`
- `src/agent/agents/stock_analyst.py` (하이브리드 감성분석 통합)
- `src/agent/agents/risk_manager.py`
- `src/agent/agents/trader.py`
- `src/agent/prompts/__init__.py`
- `src/agent/prompts/market_analysis.py`
- `src/agent/prompts/stock_analysis.py`
- `src/agent/prompts/risk_assessment.py`
- `src/agent/prompts/trade_decision.py`
- `tests/test_agents.py`

**핵심:**
- 모든 에이전트는 `LLMRouter.route()` → structured output (Pydantic schema 강제)
- Stock Analyst에 하이브리드 감성분석 통합 (키워드 1차 → 중요 뉴스만 LLM 심층)
- 프롬프트: 한국어, 2000토큰 이내, JSON 데이터 입력

**커밋:** `"Phase 3 Step 8: 4 agents + prompt templates"`

---

### Step 9: PipelineOrchestrator

**새 파일:**
- `src/agent/orchestrator.py`
- `tests/test_orchestrator.py`

**파이프라인 흐름:**
```
Market Analyst → Stock Analyst (per symbol, 병렬 가능) → Risk Manager (buy/sell만) → Trader (approved만)
```

**에러 처리:**
- Market Analyst 실패 = 전체 중단
- 개별 종목 분석 실패 = 해당 종목 건너뜀, 나머지 계속

**커밋:** `"Phase 3 Step 9: PipelineOrchestrator"`

---

### Step 10: Pipeline API + 통합 테스트 + 문서

**새 파일:**
- `src/api/routes/pipeline.py` — `POST /api/pipeline/run`, `GET /api/pipeline/result/{session_id}`
- `src/api/routes/decisions.py` — decision_log 조회 3개 엔드포인트
- `tests/test_pipeline_api.py`
- `tests/test_phase3_integration.py`

**수정:**
- `src/main.py` — 라우터 등록 (pipeline, decisions, admin)
- `docs/DESIGN.md` — Phase 3 완료 상태 반영
- `docs/TRADING_LOGIC.md` — 감성분석 섹션 추가

**Pipeline API:**
```
POST /api/pipeline/run                    — 파이프라인 실행 (symbols, strategy_type 파라미터)
GET  /api/pipeline/result/{session_id}    — 실행 결과 조회
```

**Decision Log API:**
```
GET  /api/decisions/{session_id}          — 특정 세션의 전체 의사결정 체인
GET  /api/decisions?symbol=005930&from=.. — 종목별 의사결정 이력
GET  /api/decisions/stats                 — 의사결정 통계
```

**커밋:** `"Phase 3 Step 10: pipeline API + integration tests + docs"`

---

## 파일 요약

**새 파일 ~35개**, **수정 파일 ~8개**

```
src/llm/          — base.py, router.py, cost_tracker.py, providers/{mock,openai,google,anthropic}.py
src/agent/        — orchestrator.py, decision_recorder.py, agents/{base,market,stock,risk,trader}.py,
                    tools/{technical,fundamental,market_data,news}.py, prompts/{market,stock,risk,trade}.py
src/analysis/sentiment/ — analyzer.py, keywords.py
src/db/models/llm.py, src/api/admin/llm_config.py, src/api/routes/{pipeline,decisions}.py
config/default_model_assignments.yaml
tests/ — 11개 테스트 파일
```

---

## 검증 방법

1. `uv run pytest tests/ -v` — 기존 + Phase 3 전체 통과
2. MockLLMProvider로 모든 테스트 (실제 API 호출 0건, 비용 $0)
3. 전체 파이프라인 E2E: Mock 환경에서 4단계 실행 → decision_log 체인 검증
4. Admin API: 모델 설정 CRUD + 캐시 무효화 즉시 반영
5. 에스컬레이션: 1차 low confidence → 2차 모델 재분석 → 기록 확인
6. 감성분석: 한국어 뉴스 50건으로 키워드 분류 정확도 측정
7. CostTracker: 월간 집계 + 예산 경고/초과 시나리오
