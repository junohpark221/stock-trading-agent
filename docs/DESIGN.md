# 주식 매매 및 시장 분석 에이전트 - 구현 계획서

> **이 문서는 프로젝트의 핵심 코어 문서입니다.**
> 프로젝트에 변경사항이 생기거나 수정/최신화해야 하는 정보가 있으면 반드시 이 문서를 업데이트합니다.
>
> **최종 수정일: 2026-02-09**
> **버전: 1.3**

---

## Context

개인 프로젝트로 한국 주식시장(KOSPI/KOSDAQ) 자동매매 에이전트를 구축한다. **멀티 LLM**(Claude, OpenAI GPT, Google Gemini)이 핵심 의사결정자로서 시장 분석, 매매 판단, 리포트 생성을 주도하되, 중요 의사결정은 텔레그램을 통한 사용자 승인을 거친다. 포지션 트레이딩(주력) + 스윙 트레이딩(부) 혼합 전략을 사용하며, 향후 미국 시장으로 확장 가능한 구조를 설계한다.

**현재 상태:** Claude Code 에이전트 8개와 스킬 5개가 설정되어 있으나 소스코드는 없는 그린필드 프로젝트.

**참조 리소스:**
- KIS Open Trading API SDK: `/Users/oliver.p/Desktop/Personal/open-trading-api`
  - `examples_user/` — 개발자용 통합 예제 (REST + WebSocket)
  - `examples_llm/` — LLM 최적화 단일 함수 예제
  - `MCP/` — Model Context Protocol 서버 (8개 트레이딩 도구)
  - `stocks_info/` — 종목 마스터 데이터 (KOSPI/KOSDAQ/KONEX/업종)
  - `kis_devlp.yaml` — API 키/계좌 설정 템플릿

**관련 문서:**
- [AWS 인프라 운영 가이드](./AWS_INFRASTRUCTURE_GUIDE.md) — AWS 클라우드 배포/운영 상세 가이드
- [매매 로직 레퍼런스](./TRADING_LOGIC.md) — 매매 전략, 분석 차트 생성 로직 통합 문서 (Phase 2~4 구현 시 작성)

**Phase별 상세 계획:**
- [Phase 0: 프로젝트 기반 구축](./plans/phases/phase0.md)
- (이후 Phase는 구현 시 추가)

---

## 전체 아키텍처 개요

```
┌─────────────────────────────────────────────────────────┐
│                    사용자 인터페이스                        │
│         Telegram Bot  /  FastAPI Dashboard               │
│         (승인 요청, 알림, 리포트, 수동 제어)                  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              오케스트레이터 (Orchestrator)                  │
│   스케줄러 + 이벤트 루프 + 에이전트 간 조율                    │
└──┬──────────┬──────────┬──────────┬─────────────────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌──────┐ ┌────────┐ ┌────────┐ ┌──────────┐
│ 데이터 │ │ 분석    │ │ 전략    │ │ 실행     │
│ 수집   │ │ 엔진    │ │ 엔진    │ │ 엔진     │
│ 레이어 │ │ 레이어  │ │ 레이어  │ │ 레이어    │
└──┬───┘ └──┬─────┘ └──┬─────┘ └──┬───────┘
   │        │          │          │
   ▼        ▼          ▼          ▼
┌─────────────────────────────────────────────────────────┐
│              공통 인프라 레이어                              │
│   PostgreSQL / Redis / Logging / Config / 의사결정 기록DB  │
└─────────────────────────────────────────────────────────┘
```

### LLM 프로바이더 추상화 레이어

```
┌─────────────────────────────────────┐
│         LLM Gateway (Router)        │
│  DB 기반 설정, 폴백, 비용 추적, 로깅   │
│  Admin API로 런타임 모델 변경 가능     │
└──┬──────────┬──────────┬───────────┘
   │          │          │
   ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌──────┐
│ OpenAI │ │Gemini  │ │Claude│
│ (주력)  │ │ (보조)  │ │(백업) │
│ o3     │ │2.5 Flash│ │Opus  │
│ GPT-5  │ │Flash-  │ │Sonnet│
│ o4-mini│ │Lite    │ │Haiku │
│ 5 Mini │ │2.5 Pro │ │      │
│ 5 Nano │ │        │ │      │
└────────┘ └────────┘ └──────┘
```

---

## 핵심 설계 원칙

1. **Multi-LLM + 비용 최적화**: OpenAI(주력), Gemini(보조), Claude(백업) 복수 LLM 프로바이더를 추상화하고, DB 기반 에이전트별 모델 설정 + 혼합 라우팅(고정 배분 + 에스컬레이션)으로 성능과 비용을 최적화. Admin API로 런타임 모델 변경 가능.
2. **LLM-First**: LLM이 시장을 읽고 판단하는 "애널리스트" 역할. 기술 지표는 LLM의 도구(tool)
3. **Human-in-the-Loop**: 신규 종목 진입, 대규모 포지션 변경, 손절 등 주요 결정은 텔레그램 승인 필요
4. **Decision Audit Trail**: 모든 분석/매매 의사결정의 근거를 DB에 기록하여 추후 추적 가능
5. **Broker-Agnostic**: 브로커 인터페이스 추상화로 KIS → Alpaca/IBKR 확장 가능
6. **Async-First**: 모든 I/O를 asyncio 기반으로 구현
7. **Safety-First**: 모의투자부터 시작, 리스크 관리 모듈은 우회 불가능한 게이트로 설계
8. **Decimal Precision**: 모든 금융 데이터에 Decimal 타입 사용
9. **Environment Parity**: develop(Docker 로컬) ↔ production(AWS) 환경 설정을 일관되게 관리. Git 브랜치(develop/production)와 배포 환경을 1:1 대응
10. **Annotated Logic**: 매매 로직과 분석 차트 생성 코드에는 상세 주석을 달고, `docs/TRADING_LOGIC.md`에 통합 관리

---

## 멀티 LLM 전략

### LLM 프로바이더 추상화

```python
class LLMProvider(ABC):
    """LLM 프로바이더 공통 인터페이스"""

    @abstractmethod
    async def chat(self, messages: list[Message], tools: list[Tool] | None = None) -> LLMResponse:
        """메시지 기반 대화 (tool_use 지원)"""

    @abstractmethod
    async def structured_output(self, messages: list[Message], schema: type[BaseModel]) -> BaseModel:
        """구조화된 출력 (JSON schema 강제)"""

class OpenAIProvider(LLMProvider):
    """OpenAI (주력) — o3, GPT-5, o4-mini, GPT-5 Mini, GPT-5 Nano"""

class GeminiProvider(LLMProvider):
    """Gemini (보조/1차 분석) — 2.5 Pro, 2.5 Flash, 2.5 Flash-Lite"""

class AnthropicProvider(LLMProvider):
    """Claude (백업) — Opus 4.5, Sonnet 4.5, Haiku 4.5 (prompt caching 활용)"""
```

### 에이전트별 LLM 모델 배분 전략 (혼합 라우팅)

| 에이전트 | 라우팅 모드 | 1차 모델 | 에스컬레이션 모델 | 에스컬레이션 조건 | 예상 비용/호출 | 변경 이유 |
|---------|-----------|---------|-----------------|----------------|-------------|---------|
| Trader (매매 결정) | **고정 배분** | o3 | — | 항상 추론 특화 모델 사용 | ~$0.06 | 추론 특화, Opus 대비 60% 절감 |
| Risk Manager (리스크 평가) | **고정 배분** | o4-mini | — | 안전 관련, 추론 특화 경량 | ~$0.03 | 추론 특화 경량, 리스크 평가에 적합 |
| Stock Analyst (종목 분석) | **에스컬레이션** | Gemini 2.5 Flash | o3 | confidence < 0.6 또는 복합 분석 | ~$0.005→$0.06 | Flash로 1차 분석, 복합 분석 시 o3 |
| Market Analyst (시장 분석) | **에스컬레이션** | Gemini 2.5 Flash-Lite | GPT-5 Mini | 시장 급변 또는 판단 모호 시 | ~$0.003→$0.01 | 시장 급변 시 GPT-5 Mini로 재분석 |
| Sentiment Analyzer (감성 분석) | **에스컬레이션** | Gemini 2.5 Flash-Lite | GPT-5 Nano | 감성 판단 모호(중립 ±0.1) 시 | ~$0.003→$0.002 | 초저비용 GPT로 에스컬레이션 |
| Report Generator (리포트) | **고정 배분(저비용)** | GPT-5 Nano | — | 정형 리포트, 항상 저비용 | ~$0.002 | GPT-4o mini 대체, 더 저렴 |

> **총 비용 비교**: 트레이딩 사이클당 ~$0.30-$0.45(이전) → ~$0.10-$0.15(현재), 약 65-70% 절감.
> 위 배분은 **기본값**이며, Admin API(`/api/admin/llm/config`)로 런타임 변경 가능.

### LLM 비용 최적화: 혼합 라우팅 전략

**혼합 라우팅 원칙:**
- **고정 배분 (Critical Tasks)**: 매매 결정, 리스크 평가 → 항상 추론 특화 모델 (정확도가 곧 수익/손실)
- **단계적 에스컬레이션 (Standard Tasks)**: 시장/종목 분석, 감성 분석 → 저비용 모델 먼저, 확신도 낮으면 고비용 재분석
- **고정 배분(저비용)**: 리포트 생성 → 항상 최저비용 모델 (정형 출력)
- **DB 기반 설정**: 에이전트-모델 매핑을 `agent_model_config` 테이블에 저장, Admin API로 런타임 변경 가능
- **캐시 무효화**: Admin API로 설정 변경 시 Redis 캐시 즉시 무효화하여 지연 없이 반영

**에스컬레이션 흐름:**
```
[요청] → 라우팅 모드 확인
         │
         ├─ 고정 배분 → 지정 모델로 바로 호출
         │
         └─ 에스컬레이션 → 1차 모델(저비용) 호출
                           │
                           ├─ confidence ≥ 임계값 → 결과 사용 (비용 절감)
                           │
                           └─ confidence < 임계값 → 2차 모델(고비용) 재분석
                                                    └─ 최종 판단 채택
```

**추가 비용 최적화 기법:**
1. **응답 캐싱**: 동일 종목+시간대 분석 결과 Redis 캐시 (TTL: 분석 주기)
2. **프롬프트 캐싱**: Claude의 prompt caching 적극 활용 (cache hit 시 입력 비용 90% 절감)
3. **배치 API**: 비긴급 작업(리포트, 일괄 감성 분석)은 Batch API로 50% 할인
4. **프롬프트 최적화**: 구조화 입력(JSON) + 간결한 시스템 프롬프트로 토큰 절감
5. **성과 기반 조정**: 주간 단위로 모델별 정확도 vs 비용 분석 → 배분 자동 조정 제안

### LLM 라우팅 정책

```python
class LLMRouter:
    """
    LLM 라우팅 (DB 기반 설정):
    1. DB에서 agent_model_config 조회 (Redis 캐시, TTL: 5분)
    2. routing_mode에 따라 고정/에스컬레이션 실행
    3. 모든 호출의 비용/토큰/지연시간/confidence 추적
    4. 월간 예산 한도 초과 시 에스컬레이션 비활성화 (1차 모델만 사용)
    5. Admin API로 설정 변경 시 캐시 즉시 무효화
    """
    async def route(self, agent_type: AgentType, task_context: TaskContext) -> LLMResponse:
        ...
```

### 에이전트별 모델 설정 시스템 (DB 기반 + Admin API)

에이전트-모델 매핑을 DB에 저장하고 Admin API로 런타임 변경 가능하게 함. 기존 하드코딩 방식 대신 유연한 설정 관리 시스템.

**DB 테이블: `agent_model_config`**

```sql
CREATE TABLE agent_model_config (
    id              SERIAL PRIMARY KEY,
    agent_type      VARCHAR(30) NOT NULL UNIQUE,  -- trader, risk_manager, stock_analyst, ...
    routing_mode    VARCHAR(20) NOT NULL,          -- fixed, escalation
    primary_model   VARCHAR(60) NOT NULL,          -- openai/o3, google/gemini-2.5-flash, ...
    escalation_model VARCHAR(60),                  -- null이면 에스컬레이션 없음
    confidence_threshold DECIMAL(3,2),             -- 0.60 등 (에스컬레이션 임계값)
    is_active       BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMP DEFAULT NOW(),
    updated_by      VARCHAR(50) DEFAULT 'system'   -- admin API 호출자 기록
);
```

**Admin API 엔드포인트:**

```
GET    /api/admin/llm/config              — 전체 에이전트 모델 설정 조회
GET    /api/admin/llm/config/{agent_type} — 특정 에이전트 설정 조회
PUT    /api/admin/llm/config/{agent_type} — 에이전트 모델 변경
POST   /api/admin/llm/config/reset        — 기본값으로 초기화
GET    /api/admin/llm/models              — 사용 가능한 모델 목록 + 가격 정보
```

**PUT 요청 예시:**
```json
PUT /api/admin/llm/config/trader
{
    "primary_model": "anthropic/claude-opus-4.5",
    "escalation_model": null,
    "routing_mode": "fixed"
}
```

> 설정 변경 시 Redis 캐시를 즉시 무효화하여 다음 LLM 호출부터 반영. 기본값은 `config/default_model_assignments.yaml`에서 DB seed.

### LLM 비용 관리

| 항목 | 기준 |
|------|------|
| 월간 예산 상한 | 설정 가능 (기본 $100) |
| 비용 추적 | 프로바이더별/에이전트별/일별 토큰 사용량 DB 기록 |
| 예산 초과 정책 | 상한 80% 도달 시 에스컬레이션 비활성화 + 텔레그램 알림 |
| 폴백 순서 | o3 → GPT-5 → o4-mini → GPT-5 Mini → Gemini 2.5 Pro → Gemini 2.5 Flash → Claude Sonnet 4.5 → GPT-5 Nano |
| 에스컬레이션 비율 모니터링 | 에이전트별 에스컬레이션 빈도 추적 (>50% 시 1차 모델 업그레이드 검토) |
| 모델별 정확도 추적 | decision_log outcome 기반 모델별 판단 정확도 기록 |
| 프롬프트 캐시 적중률 | Claude prompt caching hit rate 추적 (목표: >60%) |
| 배치 API 활용률 | 비긴급 작업의 Batch API 처리 비율 추적 |

### LLM 모델 가격표 (참조)

> 모델 가격은 수시로 변동됩니다. **최종 확인일: 2026-02-09**

**Anthropic Claude:**

| 모델 | Input/MTok | Output/MTok | 특징 |
|------|-----------|------------|------|
| Claude Opus 4.5 | $5 | $25 | 최고 성능 플래그십 |
| Claude Sonnet 4.5 | $3 | $15 | 균형 (성능/비용) |
| Claude Haiku 4.5 | $1 | $5 | 최속 + 저비용 |

**OpenAI:**

> GPT-4.1 시리즈 및 GPT-4o mini는 지원 종료 예정으로 제외. GPT-5 시리즈로 대체.

| 모델 | Input/MTok | Output/MTok | Context | 특징 |
|------|-----------|------------|---------|------|
| o3 | $2 | $8 | 200K | 추론 특화 플래그십 |
| GPT-5 | $1.25 | $10 | 400K | 범용 플래그십 |
| o4-mini | $1.10 | $4.40 | 200K | 추론 특화 경량 |
| GPT-5 Mini | $0.25 | $2 | 400K | 범용 경량 |
| GPT-5 Nano | $0.05 | $0.40 | 400K | 초저비용 범용 |

**Google Gemini:**

| 모델 | Input/MTok | Output/MTok | Context | 특징 |
|------|-----------|------------|---------|------|
| Gemini 2.5 Pro | $1.25 | $10 | 2M | 고성능 멀티모달 |
| Gemini 2.5 Flash | $0.15 | $0.60 | 1M | 빠른 추론 **(가격 수정: $0.30/$2.50 → $0.15/$0.60)** |
| Gemini 2.5 Flash-Lite | $0.10 | $0.40 | 1M | 경량 저비용 |

> Gemini 3 Pro/Flash는 아직 Preview 상태이므로 안정 버전인 2.5 계열을 기본 채택.

---

## 의사결정 근거 기록 (Decision Audit Trail)

모든 분석과 매매 의사결정의 근거를 DB에 저장하여, 사후에 "왜 이 매매를 했는지"를 추적할 수 있다.

### 기록 대상

| 단계 | 기록 내용 | 저장 위치 |
|------|----------|----------|
| 시장 분석 | 시장 컨디션 판단 근거, 사용된 지표/뉴스, LLM 원문 응답 | `decision_log` |
| 종목 분석 | 기술적/펀더멘털/감성 분석 결과, LLM 추론 과정 | `decision_log` |
| 리스크 평가 | 포지션 사이징 계산 과정, 통과/거부 사유 | `decision_log` |
| 매매 결정 | 최종 주문 결정 근거, 시그널 종합 점수, LLM 판단문 | `decision_log` |
| 승인 과정 | 사용자 승인/거부 시점, 수정 내역 | `decision_log` |
| 주문 실행 | 체결가 vs 목표가 차이, 슬리피지, 체결 시간 | `decision_log` |
| 청산 사유 | 손절/익절/시간청산/펀더멘털 변화 등 청산 근거 | `decision_log` |

### DB 스키마 (decision_log)

```sql
CREATE TABLE decision_log (
    id              BIGSERIAL PRIMARY KEY,
    -- 추적 키
    decision_id     UUID NOT NULL UNIQUE,           -- 의사결정 고유 ID
    parent_id       UUID REFERENCES decision_log(decision_id),  -- 연관 의사결정 체인
    session_id      UUID NOT NULL,                  -- 분석 세션 ID (한 사이클의 분석들을 묶음)

    -- 분류
    stage           VARCHAR(30) NOT NULL,           -- market_analysis, stock_analysis, risk_check, trade_decision, approval, execution, exit
    agent_type      VARCHAR(30),                    -- market_analyst, stock_analyst, risk_manager, trader
    symbol          VARCHAR(20),                    -- 종목 코드 (해당 시)

    -- LLM 정보
    llm_provider    VARCHAR(20),                    -- anthropic, openai, google
    llm_model       VARCHAR(50),                    -- claude-opus-4-5, gpt-5, gemini-2.5-pro 등
    llm_prompt      TEXT,                           -- 입력 프롬프트 (요약 또는 전문)
    llm_response    TEXT,                           -- LLM 원문 응답
    llm_tokens_in   INTEGER,
    llm_tokens_out  INTEGER,
    llm_cost_usd    DECIMAL(10,6),                  -- 호출 비용

    -- 의사결정 내용
    decision        VARCHAR(20) NOT NULL,           -- buy, sell, hold, approve, reject, stop_loss, take_profit 등
    confidence      DECIMAL(3,2),                   -- 확신도 0.00~1.00
    reasoning       TEXT NOT NULL,                  -- 사람이 읽을 수 있는 판단 근거 요약
    data_snapshot   JSONB,                          -- 판단 시점의 데이터 스냅샷 (지표값, 호가, 뉴스 등)

    -- 결과 (사후 기록)
    outcome         VARCHAR(20),                    -- profit, loss, cancelled, expired
    outcome_pnl     DECIMAL(15,2),                  -- 실현 손익 (해당 시)
    outcome_note    TEXT,                           -- 결과에 대한 메모

    -- 메타
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 인덱스
CREATE INDEX idx_decision_log_session ON decision_log(session_id);
CREATE INDEX idx_decision_log_symbol ON decision_log(symbol, created_at);
CREATE INDEX idx_decision_log_stage ON decision_log(stage, created_at);
```

### 의사결정 체인 예시

```
session_id: abc-123
│
├─ [market_analysis] "시장 강세 판단" (decision_id: d1)
│   reasoning: "KOSPI +1.2%, 외국인 순매수 3일 연속, 반도체 업종 강세"
│
├─ [stock_analysis] "삼성전자 매수 추천" (decision_id: d2, parent: d1)
│   reasoning: "RSI 35 과매도, 60일선 지지, PER 12.5 업종 저평가"
│   data_snapshot: {rsi: 35, macd: -0.5, per: 12.5, news_sentiment: 0.7}
│
├─ [risk_check] "리스크 통과" (decision_id: d3, parent: d2)
│   reasoning: "포지션 7.2% (한도 10%), 반도체 섹터 28% (한도 30%)"
│
├─ [trade_decision] "매수 주문 생성" (decision_id: d4, parent: d3)
│   reasoning: "R:R 1:3.5, 손절 68000(-3.2%), 목표 78000(+11.1%)"
│
├─ [approval] "사용자 승인" (decision_id: d5, parent: d4)
│   reasoning: "텔레그램 승인 완료 (응답시간: 2분 30초)"
│
└─ [execution] "체결 완료" (decision_id: d6, parent: d5)
    reasoning: "15주 @ 70,200원 체결 (목표가 대비 +0.3% 슬리피지)"
    outcome: "profit", outcome_pnl: 125000
```

---

## 환경 관리 전략 (Development ↔ Production)

### 환경 구분

| 항목 | Development (로컬) | Production (AWS) |
|------|-------------------|-----------------|
| 인프라 | Docker Compose | AWS ECS + RDS + ElastiCache |
| DB | PostgreSQL (Docker) | RDS PostgreSQL |
| Redis | Redis (Docker) | ElastiCache Redis |
| 브로커 | KIS 모의투자 API | KIS 실전투자 API |
| LLM | 동일 (API 호출) | 동일 (API 호출) |
| 시크릿 | `.env` 파일 | AWS Secrets Manager |
| 로그 | 콘솔 + 파일 | CloudWatch Logs |
| 스케줄러 | APScheduler (인프로세스) | APScheduler + EventBridge |

### 설정 파일 구조

```
stock-trading-agent/
├── .env.example              # 환경변수 템플릿 (git 추적)
├── .env                      # 로컬 개발용 (git 무시)
├── .env.production           # 프로덕션 참조용 (git 무시, Secrets Manager가 실제 관리)
├── docker-compose.yml        # 개발 환경 (PostgreSQL + Redis)
├── docker-compose.prod.yml   # 프로덕션 로컬 테스트용
├── config/
│   ├── settings.py           # pydantic-settings 기반 설정 (환경별 자동 분기)
│   └── logging.py            # 환경별 로깅 설정
```

### 환경변수 관리 원칙

```python
class Settings(BaseSettings):
    # 환경 구분
    ENV: Literal["development", "production"] = "development"

    # Phase별 점진적 추가 — 필요한 Phase에서만 required
    # Phase 0: 기본
    DATABASE_URL: str
    REDIS_URL: str
    LOG_LEVEL: str = "DEBUG"           # DEBUG(dev) | INFO(prod)

    # Phase 1: 브로커
    KIS_APP_KEY: str = ""
    KIS_APP_SECRET: str = ""
    KIS_ACCOUNT_NO: str = ""
    KIS_IS_PAPER: bool = True              # True=모의투자, False=실전

    # Phase 3: LLM (멀티 프로바이더 — GPT-First)
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    LLM_MONTHLY_BUDGET_USD: Decimal = Decimal("100.00")
    LLM_DEFAULT_PROVIDER: Literal["openai", "anthropic", "google"] = "openai"

    # Phase 5: 알림
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Phase 8: AWS (프로덕션 전용)
    AWS_REGION: str = "ap-northeast-2"
    AWS_SECRET_NAME: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )
```

---

## Git 브랜치 전략

### 메인 브랜치

| 브랜치 | 용도 | 배포 환경 |
|--------|------|----------|
| `develop` | 개발 + 로컬 Docker 테스트 | Docker Compose (로컬) |
| `production` | 안정 배포 버전 | AWS (ECS + RDS + ElastiCache) |

### 작업 브랜치 (develop에서 분기)

| 패턴 | 용도 | 예시 |
|------|------|------|
| `feature/<이름>` | 새 기능 개발 | `feature/telegram-bot` |
| `phase/<번호>-<이름>` | Phase별 구현 작업 | `phase/2-analysis-engine` |
| `fix/<설명>` | 버그 수정 | `fix/kis-token-refresh` |
| `hotfix/<설명>` | 프로덕션 긴급 수정 | `hotfix/risk-check-bypass` |

### 브랜치 흐름

```
feature/xxx ──┐
phase/x-xxx ──┼──→ develop ──(수동 머지)──→ production
fix/xxx ──────┘        ▲                        │
                       │                        │
                       └── hotfix/xxx ──────────┘
                           (back-merge)
```

### 머지 규칙

1. **작업 브랜치 → develop**: 작업 완료 후 머지
2. **develop → production**: Docker 환경에서 충분히 테스트 후 **수동 머지**
3. **hotfix → production**: 긴급 시 production 직접 분기 → 수정 → production 머지 → develop back-merge

### 커밋 컨벤션

- `Phase N: <설명>` — Phase별 구현 작업
- `feat: <설명>` — 새 기능 추가
- `fix: <설명>` — 버그 수정
- `hotfix: <설명>` — 프로덕션 긴급 수정
- `docs: <설명>` — 문서 수정
- `refactor: <설명>` — 코드 리팩토링

---

## 프로젝트 디렉토리 구조

```
stock-trading-agent/
├── pyproject.toml                 # 프로젝트 설정 및 의존성
├── alembic.ini                    # Alembic 설정
├── alembic/                       # DB 마이그레이션
├── docker-compose.yml             # 로컬 개발 (PostgreSQL, Redis)
├── docker-compose.prod.yml        # 프로덕션 로컬 테스트
├── Dockerfile                     # 프로덕션 빌드
├── .env.example                   # 환경변수 템플릿
├── .gitignore
│
├── docs/                          # 프로젝트 문서
│   ├── DESIGN.md                    # 핵심 설계 문서 (이 파일)
│   ├── AWS_INFRASTRUCTURE_GUIDE.md  # AWS 인프라 운영 가이드 (초보자용)
│   ├── TRADING_LOGIC.md             # 매매 로직 + 차트 분석 통합 레퍼런스
│   ├── plans/phases/                # Phase별 상세 계획
│   │   └── phase0.md               # Phase 0 구현 계획
│   └── setup/                       # 초기 설정 문서
│       └── subagents-and-skills.md  # Subagent & Skill 설정 정리
│
├── src/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 앱 엔트리포인트
│   ├── config.py                  # pydantic-settings 기반 설정
│   │
│   ├── core/                      # 핵심 도메인 로직
│   │   ├── __init__.py
│   │   ├── models.py              # 도메인 모델 (Trade, Position, Signal 등)
│   │   ├── enums.py               # OrderSide, OrderType, MarketType 등
│   │   └── exceptions.py          # 커스텀 예외 정의
│   │
│   ├── db/                        # 데이터베이스 레이어
│   │   ├── __init__.py
│   │   ├── session.py             # async 세션 팩토리 (asyncpg)
│   │   ├── base.py                # SQLAlchemy Base
│   │   └── models/                # ORM 모델
│   │       ├── __init__.py
│   │       ├── market_data.py     # OHLCV, 종목 마스터
│   │       ├── trade.py           # 주문, 체결 기록
│   │       ├── portfolio.py       # 포트폴리오, 포지션
│   │       ├── strategy.py        # 전략 설정, 시그널 기록
│   │       ├── analysis.py        # LLM 분석 결과 저장
│   │       └── decision_log.py    # 의사결정 근거 기록 (Audit Trail)
│   │
│   ├── llm/                       # LLM 프로바이더 추상화 레이어
│   │   ├── __init__.py
│   │   ├── base.py                # LLMProvider (ABC)
│   │   ├── router.py              # LLM 라우터 (DB 기반 모델 설정 + 폴백 + 비용 추적)
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── openai.py          # OpenAI (주력) — o3, GPT-5, o4-mini, GPT-5 Mini/Nano
│   │   │   ├── google.py          # Gemini (보조) — 2.5 Pro, 2.5 Flash, 2.5 Flash-Lite
│   │   │   └── anthropic.py       # Claude (백업) — Opus 4.5, Sonnet 4.5, Haiku 4.5
│   │   └── cost_tracker.py        # LLM 비용 추적 및 예산 관리
│   │
│   ├── broker/                    # 브로커 추상화 레이어
│   │   ├── __init__.py
│   │   ├── base.py                # BrokerInterface (ABC)
│   │   ├── kis/                   # KIS 구현체 (open-trading-api 기반)
│   │   │   ├── __init__.py
│   │   │   ├── client.py          # KIS REST API 클라이언트
│   │   │   ├── auth.py            # OAuth 토큰 관리
│   │   │   ├── websocket.py       # 실시간 시세 (향후)
│   │   │   └── models.py          # KIS 응답 모델
│   │   └── mock/                  # 모의투자 클라이언트
│   │       ├── __init__.py
│   │       └── client.py          # 로컬 시뮬레이션
│   │
│   ├── data/                      # 데이터 수집 및 관리
│   │   ├── __init__.py
│   │   ├── collector.py           # 시세 데이터 수집기
│   │   ├── cache.py               # Redis 캐시 래퍼
│   │   └── providers/             # 데이터 제공자
│   │       ├── __init__.py
│   │       ├── base.py            # DataProvider (ABC)
│   │       └── kis_provider.py    # KIS 시세 데이터
│   │
│   ├── analysis/                  # 분석 엔진
│   │   ├── __init__.py
│   │   ├── technical/             # 기술적 분석
│   │   │   ├── __init__.py
│   │   │   ├── indicators.py      # RSI, MACD, BB 등 지표 계산 (상세 주석 포함)
│   │   │   └── patterns.py        # 차트 패턴 감지 (상세 주석 포함)
│   │   ├── fundamental/           # 펀더멘털 분석
│   │   │   ├── __init__.py
│   │   │   └── analyzer.py        # 재무제표, 밸류에이션
│   │   └── sentiment/             # 감성 분석
│   │       ├── __init__.py
│   │       └── analyzer.py        # 뉴스/공시 감성 분석
│   │
│   ├── agent/                     # LLM 에이전트 시스템
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # 멀티 에이전트 오케스트레이터
│   │   ├── decision_recorder.py   # 의사결정 근거 기록 유틸리티
│   │   ├── agents/                # 개별 에이전트
│   │   │   ├── __init__.py
│   │   │   ├── market_analyst.py  # 시장 분석 에이전트
│   │   │   ├── stock_analyst.py   # 종목 분석 에이전트
│   │   │   ├── risk_manager.py    # 리스크 관리 에이전트
│   │   │   └── trader.py          # 매매 결정 에이전트
│   │   ├── tools/                 # 에이전트가 사용하는 도구
│   │   │   ├── __init__.py
│   │   │   ├── technical.py       # 기술 지표 조회 도구
│   │   │   ├── fundamental.py     # 재무 데이터 조회 도구
│   │   │   ├── market_data.py     # 시세/호가 조회 도구
│   │   │   └── news.py            # 뉴스/공시 조회 도구
│   │   ├── prompts/               # 프롬프트 템플릿
│   │   │   ├── __init__.py
│   │   │   ├── market_analysis.py
│   │   │   ├── stock_analysis.py
│   │   │   └── trade_decision.py
│   │   └── memory.py              # 에이전트 메모리 (과거 분석/결정 저장)
│   │
│   ├── strategy/                  # 전략 엔진
│   │   ├── __init__.py
│   │   ├── base.py                # Strategy (ABC)
│   │   ├── position_trading.py    # 포지션 트레이딩 전략 (상세 주석 포함)
│   │   ├── swing_trading.py       # 스윙 트레이딩 전략 (상세 주석 포함)
│   │   └── risk_manager.py        # 리스크 관리 (포지션 사이징, 손절)
│   │
│   ├── execution/                 # 주문 실행
│   │   ├── __init__.py
│   │   ├── executor.py            # 주문 실행기 (승인 게이트 포함)
│   │   └── approval.py            # 사용자 승인 관리
│   │
│   ├── notification/              # 알림 시스템
│   │   ├── __init__.py
│   │   ├── telegram.py            # 텔레그램 봇 (알림 + 승인)
│   │   └── templates.py           # 메시지 템플릿
│   │
│   ├── scheduler/                 # 스케줄러
│   │   ├── __init__.py
│   │   └── jobs.py                # 정기 작업 (분석, 리포트, 리밸런싱)
│   │
│   └── api/                       # FastAPI 라우터
│       ├── __init__.py
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── portfolio.py       # 포트폴리오 조회
│       │   ├── trades.py          # 거래 내역
│       │   ├── analysis.py        # 분석 결과
│       │   ├── decisions.py       # 의사결정 근거 조회 API
│       │   └── control.py         # 수동 제어 (시작/중지/승인)
│       └── deps.py                # 공통 의존성
│
└── tests/
    ├── conftest.py                # 픽스처, 팩토리
    ├── unit/                      # 단위 테스트
    ├── integration/               # 통합 테스트
    └── fixtures/                  # 테스트용 목 데이터
```

---

## KIS Open Trading API 연동 전략

KIS SDK 레포지토리(`/Users/oliver.p/Desktop/Personal/open-trading-api`)를 참조하되, 직접 임포트하지 않고 필요한 API 패턴을 우리 프로젝트의 async 아키텍처에 맞게 재구현한다.

### 참조할 핵심 모듈

| KIS SDK 파일 | 참조 내용 | 우리 프로젝트 대응 파일 |
|-------------|----------|---------------------|
| `examples_user/kis_auth.py` | OAuth2 인증 흐름, 토큰 캐시, 헤더 구성 | `src/broker/kis/auth.py` |
| `examples_user/domestic_stock/domestic_stock_functions.py` | REST API 100+ 함수 (시세, 주문, 잔고) | `src/broker/kis/client.py` |
| `examples_user/domestic_stock/domestic_stock_functions_ws.py` | WebSocket 실시간 시세 | `src/broker/kis/websocket.py` |
| `kis_devlp.yaml` | API 키/계좌 설정 구조 | `src/config.py` (환경변수로 관리) |
| `stocks_info/` | 종목 마스터 데이터 (KOSPI/KOSDAQ/업종) | `src/data/providers/kis_provider.py` |

### KIS API 주요 엔드포인트 매핑

| 기능 | KIS API 경로 | TR ID | 우리 메서드 |
|------|-------------|-------|-----------|
| 현재가 조회 | `/uapi/domestic-stock/v1/quotations/inquire-price` | FHKST01010100 | `get_price()` |
| 일봉 차트 | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | FHKST03010100 | `get_daily_ohlcv()` |
| 주문 | `/uapi/trading/order-cash` | TTTC0802U(매수), TTTC0801U(매도) | `place_order()` |
| 잔고 조회 | `/uapi/trading/inquire-balance` | TTZS | `get_positions()` |
| 체결 조회 | `/uapi/trading/inquire-ccnl` | — | `get_executions()` |
| 호가 조회 | `/uapi/domestic-stock/v1/quotations/inquire-asking-price` | — | `get_orderbook()` |
| 투자자별 매매동향 | `/uapi/domestic-stock/v1/quotations/inquire-investor` | — | `get_investor_trend()` |

### KIS 인증 흐름 (kis_auth.py 기반)

```
1. 설정 로드: 환경변수에서 APP_KEY, APP_SECRET, ACCOUNT_NO 로드
2. 토큰 발급: POST /oauth2/tokenP (grant_type: client_credentials)
3. 토큰 캐시: Redis에 저장 (TTL: 23시간, 실제 만료: 24시간)
4. 요청 헤더 구성:
   - Authorization: "Bearer {token}"
   - appkey, appsecret
   - tr_id: 거래 ID (모의투자 시 V 접두사)
   - custtype: "P" (개인)
5. Rate Limiting: 초당 20건 (모의투자는 초당 2건)
6. 실전/모의 분기:
   - 실전: https://openapi.koreainvestment.com:9443
   - 모의: https://openapivts.koreainvestment.com:29443
```

---

## 구현 페이즈

### Phase 0: 프로젝트 기반 구축
> 프로젝트 스켈레톤, 설정, 로컬 개발 환경, 환경 관리 체계

**생성할 파일:**
- `pyproject.toml` — 의존성 관리 (uv/pip)
- `.env.example` — 환경변수 템플릿 (Phase별 주석 구분)
- `docker-compose.yml` — 개발 환경 (PostgreSQL + Redis)
- `.gitignore` — Python + IDE + .env + __pycache__ 등
- `src/config.py` — pydantic-settings 기반 설정 클래스 (환경별 분기)
- `src/main.py` — FastAPI 앱 + lifespan 이벤트
- `src/db/session.py` — async DB 세션 (환경별 연결 설정)
- `src/db/base.py` — SQLAlchemy Base 클래스
- `alembic.ini` + `alembic/env.py` — 마이그레이션 초기화
- `src/core/models.py` — 도메인 모델 (Pydantic)
- `src/core/enums.py` — Enum 정의
- `src/core/exceptions.py` — 커스텀 예외

**환경변수 (.env.example) — Phase 0 섹션:**
```bash
# ===== Phase 0: 기본 환경 =====
ENV=development                    # development | production
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/trading_agent
REDIS_URL=redis://localhost:6379/0
LOG_LEVEL=DEBUG                    # DEBUG(dev) | INFO(prod)

# ===== Phase 1: 브로커 (KIS) =====
# KIS_APP_KEY=
# KIS_APP_SECRET=
# ...이하 Phase별로 추가
```

**주요 의존성:**
```
fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic
pydantic, pydantic-settings, redis[hiredis], aiohttp
python-dotenv, structlog
```

**완료 기준:** `docker-compose up` → DB/Redis 기동 → `uvicorn` 서버 정상 시작 → `/health` 엔드포인트 응답

---

### Phase 1: 데이터 수집 레이어
> KIS API 연동 (open-trading-api 참조), 시세 데이터 수집 및 저장

**생성할 파일:**
- `src/broker/base.py` — `BrokerInterface` ABC
- `src/broker/kis/auth.py` — OAuth 토큰 발급/갱신 (open-trading-api의 kis_auth.py 참조)
- `src/broker/kis/client.py` — KIS REST 클라이언트 (domestic_stock_functions.py 참조)
- `src/broker/kis/models.py` — KIS 응답 파싱 모델
- `src/data/providers/base.py` — `DataProvider` ABC
- `src/data/providers/kis_provider.py` — KIS 시세 데이터 제공자
- `src/data/collector.py` — 데이터 수집 스케줄러
- `src/data/cache.py` — Redis 캐시 래퍼

**환경변수 추가 (.env.example) — Phase 1:**
```bash
# ===== Phase 1: 브로커 (KIS) =====
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=12345678
KIS_ACCOUNT_PROD=01
KIS_IS_PAPER=true                  # true=모의투자, false=실전
KIS_HTS_ID=your_hts_id            # WebSocket 콜백용
```

**DB 테이블:**
- `stock_master` — 종목 마스터 (코드, 이름, 시장구분, 업종)
- `daily_ohlcv` — 일봉 데이터 (종목, 날짜, OHLCV, 거래대금)
- `api_tokens` — OAuth 토큰 저장 (암호화)

**핵심 클래스:**

```python
class BrokerInterface(ABC):
    """브로커 추상 인터페이스 — KIS, Alpaca 등 교체 가능"""
    async def get_balance(self) -> AccountBalance
    async def get_positions(self) -> list[Position]
    async def place_order(self, order: OrderRequest) -> OrderResult
    async def cancel_order(self, order_id: str) -> bool
    async def get_price(self, symbol: str) -> PriceInfo
    async def get_daily_ohlcv(self, symbol: str, period: int) -> list[OHLCV]

class KISClient(BrokerInterface):
    """
    KIS Open API 구현체
    참조: open-trading-api/examples_user/domestic_stock/domestic_stock_functions.py
    """
    # 모의투자/실전투자 base_url 분기 (kis_devlp.yaml의 prod/vps 참조)
    # 자동 토큰 갱신 (kis_auth.py의 auth()/reAuth() 참조)
    # Rate limiting (실전 초당 20건, 모의 초당 2건)
    # 지수 백오프 재시도
```

**완료 기준:** KIS 모의투자 API 연결 성공 → 종목 마스터 수집 → 일봉 데이터 3년치 수집/저장 → Redis 캐시 동작

---

### Phase 2: 분석 엔진
> 기술적 분석 지표 + 펀더멘털 데이터 + 뉴스/감성 분석
> **모든 분석 로직에 상세 주석 작성, `docs/TRADING_LOGIC.md`에 통합 정리**

**생성할 파일:**
- `src/analysis/technical/indicators.py` — 기술 지표 계산 (pandas-ta 활용, **상세 주석 필수**)
- `src/analysis/technical/patterns.py` — 차트 패턴 감지 (**상세 주석 필수**)
- `src/analysis/fundamental/analyzer.py` — 재무 데이터 분석
- `src/analysis/sentiment/analyzer.py` — 뉴스 감성 분석
- `docs/TRADING_LOGIC.md` — 매매 로직 + 차트 분석 통합 레퍼런스 문서

**코드 주석 예시 (indicators.py):**
```python
def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI (Relative Strength Index) — 상대강도지수

    [매매 로직에서의 역할]
    - 과매수/과매도 판단의 핵심 지표
    - RSI > 70: 과매수 → 매도 시그널 고려
    - RSI < 30: 과매도 → 매수 시그널 고려
    - 다이버전스: 가격은 신고가인데 RSI는 하락 → 추세 반전 경고

    [계산 방식]
    1. 가격 변화량(delta) = 현재가 - 전일가
    2. 상승분(gain) = max(delta, 0), 하락분(loss) = abs(min(delta, 0))
    3. 평균 상승(avg_gain) = gain의 period일 지수이동평균
    4. 평균 하락(avg_loss) = loss의 period일 지수이동평균
    5. RS = avg_gain / avg_loss
    6. RSI = 100 - (100 / (1 + RS))

    [우리 전략에서의 활용]
    - 포지션 트레이딩: RSI < 35 + 이동평균선 지지 → 매수 후보
    - 스윙 트레이딩: RSI < 25 → 단기 반등 매수, RSI > 75 → 단기 매도
    - Stock Analyst 에이전트가 이 값을 참조하여 LLM에 보고

    Args:
        prices: 종가 시계열 데이터
        period: RSI 계산 기간 (기본 14일)

    Returns:
        RSI 시계열 (0~100)
    """
    ...
```

**기술 지표 (LLM 도구로 활용):**

| 카테고리 | 지표 | 용도 |
|---------|------|------|
| 추세 | SMA(20/60/120), EMA(12/26), MACD | 추세 방향 및 강도 판단 |
| 모멘텀 | RSI(14), 스토캐스틱, Williams %R | 과매수/과매도 판단 |
| 변동성 | 볼린저밴드(20,2), ATR(14) | 변동성 측정, 손절 기준 |
| 거래량 | OBV, VWAP, 거래량 MA | 추세 확인, 유동성 판단 |

**펀더멘털 분석:**
- KIS API에서 재무제표 데이터 수집 (PER, PBR, ROE 등)
- 섹터 대비 밸류에이션 비교
- 실적 성장 트렌드 분석

**감성 분석:**
- 뉴스/공시 수집 (웹 스크래핑 기반)
- LLM 기반 감성 점수 산출 (긍정/부정/중립 + 시장영향도)
- 종목별 일간 감성 트렌드

**완료 기준:** 임의 종목의 기술 지표 계산 → 펀더멘털 점수 산출 → 뉴스 감성 분석 → 종합 분석 데이터 DB 저장 → `docs/TRADING_LOGIC.md` 작성

---

### Phase 3: LLM 에이전트 시스템
> 멀티 LLM 기반 시장 분석, 종목 분석, 매매 판단 멀티 에이전트 + 의사결정 기록

**생성할 파일:**
- `src/llm/base.py` — LLMProvider ABC
- `src/llm/router.py` — LLM 라우터 (**DB 기반 설정** + 에스컬레이션 + 캐시)
- `src/llm/providers/openai.py` — OpenAI 프로바이더 (주력: o3, GPT-5, o4-mini, GPT-5 Mini/Nano)
- `src/llm/providers/google.py` — Gemini 프로바이더 (보조: 2.5 Pro, 2.5 Flash, 2.5 Flash-Lite)
- `src/llm/providers/anthropic.py` — Claude 프로바이더 (백업: Opus 4.5, Sonnet 4.5, Haiku 4.5)
- `src/llm/cost_tracker.py` — LLM 비용 추적
- `config/default_model_assignments.yaml` — 에이전트별 모델 기본값 (DB seed 데이터)
- `src/api/admin/llm_config.py` — Admin API 엔드포인트
- `src/db/models/agent_model_config.py` — ORM 모델
- `src/agent/orchestrator.py` — 에이전트 실행 조율
- `src/agent/decision_recorder.py` — 의사결정 근거 기록 유틸리티
- `src/agent/agents/market_analyst.py` — 시장 전체 분석
- `src/agent/agents/stock_analyst.py` — 개별 종목 심층 분석
- `src/agent/agents/risk_manager.py` — 리스크 평가
- `src/agent/agents/trader.py` — 최종 매매 결정
- `src/agent/tools/*.py` — 에이전트가 호출하는 도구 함수들
- `src/agent/prompts/*.py` — 프롬프트 템플릿
- `src/agent/memory.py` — 에이전트 메모리 관리

**환경변수 추가 (.env.example) — Phase 3:**
```bash
# ===== Phase 3: LLM 프로바이더 =====
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AIza...
LLM_MONTHLY_BUDGET_USD=100.00
LLM_DEFAULT_PROVIDER=openai     # openai | anthropic | google (기본 프로바이더)
```

**에이전트 파이프라인:**

```
1. Market Analyst (시장 분석)
   │ "오늘 시장 상황은 어떤가? 주요 이슈는?"
   │ → 도구: 시장 지수 조회, 뉴스 조회, 매크로 지표
   │ → 출력: 시장 컨디션 리포트 (강세/약세/혼조, 주의 업종)
   │ → **decision_log 기록: 시장 판단 근거 + LLM 원문 응답 저장**
   │
   ▼
2. Stock Analyst (종목 분석) — 관심 종목 각각에 대해
   │ "이 종목의 기술적/펀더멘털/뉴스 상황은?"
   │ → 도구: 기술지표 조회, 재무 데이터, 뉴스 감성
   │ → 출력: 종목 분석 리포트 (매수/매도/관망 + 근거)
   │ → **decision_log 기록: 분석 데이터 스냅샷 + 판단 근거 저장**
   │
   ▼
3. Risk Manager (리스크 평가)
   │ "이 매매가 포트폴리오에 미치는 영향은?"
   │ → 도구: 현재 포지션, 섹터 노출도, 상관관계
   │ → 출력: 리스크 평가서 (승인/조건부승인/거부 + 추천 포지션 크기)
   │ → **decision_log 기록: 리스크 체크 항목별 통과/실패 사유 저장**
   │
   ▼
4. Trader (매매 결정)
   │ "종합 분석을 기반으로 어떻게 실행할까?"
   │ → 위 3개 에이전트의 리포트 종합
   │ → 출력: 매매 주문서 (종목, 방향, 수량, 가격, 손절/익절)
   │ → **decision_log 기록: 최종 주문 결정 근거 + R:R 계산 저장**
```

**에이전트 메모리:**
- 과거 분석/판단 기록을 DB에 저장
- 매매 결과 피드백 → 향후 분석에 반영
- 종목별 컨텍스트 유지 (보유 이유, 목표가, 리스크 요인)

**DB 테이블:**
- `decision_log` — 의사결정 근거 기록 (위 스키마 참조)
- `agent_analyses` — 에이전트 분석 결과 (종목, 에이전트, 분석내용 JSON, 타임스탬프)
- `agent_decisions` — 매매 결정 (시그널, 근거, 승인상태)
- `agent_memory` — 에이전트 메모리 (종목별 컨텍스트)
- `llm_usage` — LLM 사용량/비용 추적

**완료 기준:** 지정 종목에 대해 4단계 에이전트 파이프라인 실행 → 매매 추천 리포트 생성 → decision_log에 전체 의사결정 체인 기록 → DB 저장

---

### Phase 4: 전략 엔진 + 리스크 관리
> 포지션/스윙 전략 프레임워크, 포지션 사이징, 손절 로직
> **모든 전략 로직에 상세 주석 작성, `docs/TRADING_LOGIC.md`에 통합 정리**

**생성할 파일:**
- `src/strategy/base.py` — `Strategy` ABC
- `src/strategy/position_trading.py` — 포지션 트레이딩 전략 (**상세 주석 필수**)
- `src/strategy/swing_trading.py` — 스윙 트레이딩 전략 (**상세 주석 필수**)
- `src/strategy/risk_manager.py` — 리스크 관리 모듈

**전략 프레임워크:**

```python
class Strategy(ABC):
    """전략 기본 인터페이스"""
    @abstractmethod
    async def scan_universe(self) -> list[str]:
        """분석 대상 종목 선정"""

    @abstractmethod
    async def analyze(self, symbol: str) -> AnalysisResult:
        """종목 분석 (에이전트 파이프라인 호출)"""

    @abstractmethod
    async def generate_signals(self) -> list[Signal]:
        """매매 시그널 생성"""

    @abstractmethod
    async def check_exit_conditions(self, position: Position) -> ExitSignal | None:
        """보유 종목 청산 조건 체크"""
```

**포지션 트레이딩 전략:**
- 분석 주기: 주 1~2회 (주말 + 수요일)
- 유니버스: KOSPI200 + KOSDAQ150 중 유동성/시총 필터
- 진입 기준: LLM 분석 결과 매수 판단 + 기술적 확인 + 리스크 체크
- 청산 기준: 목표가 도달 / 손절 조건 / LLM 매도 판단 / 펀더멘털 악화
- 보유 기간: 주~월 단위

**스윙 트레이딩 전략:**
- 분석 주기: 매일 장 마감 후
- 유니버스: 거래량 상위, 변동성 적정 종목
- 진입 기준: 기술 지표 시그널 + LLM 확인
- 청산 기준: 기술적 손절/익절 + 시간 기반 청산 (최대 2주)
- 보유 기간: 일~주 단위

**리스크 관리 (우회 불가능):**

| 규칙 | 기준 | 동작 |
|------|------|------|
| 포지션 사이징 | 고정비율 1~2% 리스크 | 손절폭에 따른 수량 자동 계산 |
| 최대 포지션 | 총 자산의 10% | 한 종목 최대 비중 제한 |
| 섹터 집중도 | 같은 업종 30% | 동일 업종 과집중 방지 |
| 최대 드로다운 | 총 자산의 -10% | 전체 매매 중단 + 텔레그램 긴급 알림 |
| 일일 손실 | -3% | 당일 신규 진입 중단 |
| 상관관계 | 기존 보유와 상관 0.7 이상 | 경고 + 포지션 축소 권고 |
| 매매 건수 | 일 5건 상한 | 과매매 방지 |

**완료 기준:** 포지션/스윙 전략 각각 실행 → 시그널 생성 → 리스크 체크 통과/거부 → 포지션 사이징 계산 → `docs/TRADING_LOGIC.md` 업데이트

---

### Phase 5: 주문 실행 + 사용자 승인
> 실제 주문 실행 흐름, 텔레그램 승인 워크플로우, 의사결정 기록 연동

**생성할 파일:**
- `src/execution/executor.py` — 주문 실행기
- `src/execution/approval.py` — 승인 관리
- `src/notification/telegram.py` — 텔레그램 봇
- `src/notification/templates.py` — 메시지 템플릿

**환경변수 추가 (.env.example) — Phase 5:**
```bash
# ===== Phase 5: 알림/승인 =====
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
APPROVAL_TIMEOUT_MINUTES=30
```

**주문 실행 흐름:**

```
시그널 생성
  │
  ▼
리스크 체크 ──(거부)──→ 로깅 + 알림 + decision_log 기록
  │(통과)
  ▼
승인 필요 여부 판단
  │
  ├─(자동 실행 가능)─→ 즉시 주문  [손절, 소규모 스윙]
  │                     └→ decision_log 기록 (auto_approved)
  │
  └─(승인 필요)─→ 텔레그램 승인 요청  [신규 진입, 대규모]
                    │
                    ├─(승인)─→ 주문 실행 → decision_log 기록 (approved)
                    ├─(거부)─→ 시그널 취소 → decision_log 기록 (rejected + 사유)
                    └─(타임아웃 30분)─→ 자동 취소 → decision_log 기록 (timeout)
```

**승인 기준:**
- **자동 실행**: 손절 주문 (사전 설정된 손절가), 소규모 스윙 매도 (5% 이하 포지션)
- **승인 필요**: 신규 종목 매수, 전체 포트폴리오 5% 이상 포지션, 포지션 트레이딩 진입/청산

**텔레그램 메시지:**

```
📊 매매 승인 요청

종목: 삼성전자 (005930)
방향: 매수
수량: 15주 (₩1,050,000)
포트폴리오 비중: 7.2%

📈 분석 요약:
- 기술적: RSI 35 (과매도), 60일선 지지
- 펀더멘털: PER 12.5 (업종 평균 15.2)
- 감성: 긍정 (AI 반도체 수요 증가)
- 리스크: 중 (섹터 편중 주의)

손절: ₩68,000 (-3.2%)
목표: ₩78,000 (+11.1%)
R:R = 1:3.5

🔗 분석 근거 상세: /decisions/{session_id}

[승인] [거부] [수정]
```

**DB 테이블:**
- `orders` — 주문 기록 (종목, 방향, 수량, 가격, 상태, 승인정보)
- `executions` — 체결 기록 (주문ID, 체결가, 체결수량, 수수료)
- `approval_requests` — 승인 요청 (주문, 상태, 응답시간)

**완료 기준:** 모의투자 환경에서 전체 흐름 동작 → 텔레그램 승인 요청/응답 → KIS 모의투자 주문 체결 → 전체 의사결정 체인이 decision_log에 기록됨

---

### Phase 6: 스케줄러 + 리포트 + 모니터링
> 정기 작업, 성과 리포트, 시스템 모니터링

**생성할 파일:**
- `src/scheduler/jobs.py` — APScheduler 기반 정기 작업
- `src/api/routes/portfolio.py` — 포트폴리오 API
- `src/api/routes/trades.py` — 거래 내역 API
- `src/api/routes/analysis.py` — 분석 결과 API
- `src/api/routes/decisions.py` — 의사결정 근거 조회 API
- `src/api/routes/control.py` — 수동 제어 API

**정기 작업 스케줄:**

| 작업 | 주기 | 설명 |
|------|------|------|
| 시세 수집 | 매일 장 마감 후 (15:40) | 일봉 데이터 업데이트 |
| 스윙 분석 | 매일 장 마감 후 (16:00) | 스윙 전략 시그널 스캔 |
| 포지션 분석 | 주 2회 (수, 토) | 포지션 전략 심층 분석 |
| 손절 체크 | 장중 5분 간격 | 보유 종목 손절 조건 모니터링 |
| 일간 리포트 | 매일 20:00 | 당일 거래/수익 요약 → 텔레그램 |
| 주간 리포트 | 매주 토요일 10:00 | 주간 성과 + 다음주 전망 → 텔레그램 |
| 월간 리포트 | 매월 1일 10:00 | 월간 성과 상세 분석 → 텔레그램 |
| 토큰 갱신 | 매일 06:00 | KIS OAuth 토큰 사전 갱신 |
| LLM 비용 리포트 | 매주 월요일 09:00 | 주간 LLM 사용량/비용 요약 → 텔레그램 |

**텔레그램 리포트 예시 (일간):**

```
📊 일간 트레이딩 리포트 (2026-02-08)

💰 오늘 성과
- 실현 손익: +₩125,000 (+0.8%)
- 미실현 손익: -₩45,000 (-0.3%)

📈 거래 내역
- 매수: SK하이닉스 5주 @ ₩185,000
- 매도: 카카오 10주 @ ₩52,000 (+8.3%)

📋 포트폴리오 현황
- 총 평가액: ₩15,230,000
- 현금: ₩3,450,000 (22.7%)
- 보유 종목: 6개
- 누적 수익률: +4.2%

🤖 LLM 비용 (오늘)
- OpenAI: $1.80 (o3 3회, o4-mini 5회, GPT-5 Nano 8회)
- Gemini: $0.25 (Flash 6회, Flash-Lite 12회)
- 월간 누적: $32.50 / $100.00

⚠️ 주의 사항
- 반도체 섹터 비중 28% (상한 30% 근접)
```

**의사결정 조회 API:**
- `GET /api/decisions/{session_id}` — 특정 분석 세션의 전체 의사결정 체인 조회
- `GET /api/decisions?symbol=005930&from=2026-01-01` — 종목별 의사결정 이력
- `GET /api/decisions/stats` — 의사결정 통계 (승률, 평균 확신도 vs 실제 성과)

**성과 지표:**
- 총 수익률, 연환산 수익률
- Sharpe Ratio, Sortino Ratio
- 최대 드로다운 (MDD)
- 승률, 평균 손익비 (R:R)
- 종목별/전략별/월별 성과 분석
- LLM 모델별 의사결정 정확도 비교

**완료 기준:** 스케줄러 정상 동작 → 일간/주간 리포트 텔레그램 전송 → FastAPI 대시보드 API 응답 → 의사결정 조회 API 동작

---

### Phase 7: 백테스팅 프레임워크
> 전략 검증을 위한 백테스팅 시스템

**생성할 파일:**
- `src/backtest/engine.py` — 백테스팅 엔진
- `src/backtest/portfolio.py` — 가상 포트폴리오 시뮬레이션
- `src/backtest/metrics.py` — 성과 지표 계산
- `src/backtest/report.py` — 백테스트 리포트 생성

**백테스팅 설계:**
- 일봉 기반 시뮬레이션 (포지션/스윙 전략에 적합)
- 슬리피지 모델링: 기본 10bps + 변동성 계수
- 수수료: 매수 0.015% + 매도 0.015% + 증권거래세 0.23%
- In-sample / Out-of-sample 분리 (70:30)
- Walk-forward 분석 (6개월 윈도우)

**LLM 백테스팅 특이사항:**
- 실제 LLM 호출은 비용이 크므로, 과거 분석 결과 캐싱 활용
- LLM 기반 전략의 경우 "시뮬레이션 모드" → 과거 데이터로 LLM에 분석 요청 후 결과 저장
- 비교 기준: 벤치마크(KOSPI), Buy&Hold, 순수 기술적 전략
- **멀티 LLM 비교**: 같은 데이터로 o3/GPT-5/GPT-5 Mini, Claude Opus 4.5/Sonnet 4.5, Gemini 2.5 Pro/Flash 분석 결과 비교 → 최적 에스컬레이션 임계값 및 모델 배분 근거

**완료 기준:** 3년 백테스트 실행 → Sharpe Ratio, MDD 등 지표 산출 → 벤치마크 대비 성과 비교 리포트

---

### Phase 8: Docker + AWS 배포
> 컨테이너화 및 프로덕션 배포
> **상세 가이드: [AWS_INFRASTRUCTURE_GUIDE.md](./AWS_INFRASTRUCTURE_GUIDE.md)**

**생성할 파일:**
- `Dockerfile` — 멀티스테이지 빌드
- `docker-compose.prod.yml` — 프로덕션 로컬 테스트용
- `.github/workflows/deploy.yml` — CI/CD 파이프라인 (`production` 브랜치 push 시 트리거)
- `docs/AWS_INFRASTRUCTURE_GUIDE.md` — AWS 인프라 운영 가이드 (초보자용 상세 문서)

**환경변수 추가 (.env.example) — Phase 8:**
```bash
# ===== Phase 8: AWS 프로덕션 =====
AWS_REGION=ap-northeast-2
AWS_SECRET_NAME=trading-agent/production
AWS_ECR_REPOSITORY=trading-agent
# 나머지 시크릿은 AWS Secrets Manager에서 관리
```

**AWS 아키텍처:**

| 서비스 | 용도 | 스펙 |
|--------|------|------|
| ECS Fargate | 메인 에이전트 서비스 | 1 vCPU, 2GB RAM |
| RDS PostgreSQL | 메인 DB | db.t3.medium, Multi-AZ |
| ElastiCache Redis | 캐시 | cache.t3.micro |
| Secrets Manager | API 키, 토큰 | KIS, LLM, Telegram 키 |
| CloudWatch | 로그, 메트릭, 알람 | 커스텀 대시보드 |
| EventBridge | 스케줄 트리거 | cron 기반 작업 스케줄링 |

**완료 기준:** Docker 빌드 → ECR 푸시 → ECS 배포 → 헬스체크 통과 → 모의투자 상시 운영

---

## 우선 구현 순서 요약

```
Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7 → Phase 8
기반구축    데이터     분석      AI에이전트   전략+리스크  실행+승인   스케줄+리포트  백테스팅    배포
[1주]      [1-2주]   [1-2주]   [2-3주]      [1-2주]     [1-2주]    [1-2주]       [1-2주]    [1주]
```

**Phase 0~2가 Critical Path** — 데이터 없이는 아무것도 동작하지 않음
**Phase 3이 핵심 차별점** — 멀티 LLM 에이전트가 이 프로젝트의 핵심 가치
**Phase 4~5는 안전장치** — 리스크 관리 없이는 실전 투입 불가
**Phase 7은 언제든 병행 가능** — 데이터가 쌓이면 바로 시작

---

## 검증 방법

1. **Phase 0~1 검증**: KIS 모의투자 API로 종목 시세 조회, 일봉 데이터 DB 저장 확인
2. **Phase 2~3 검증**: 특정 종목(삼성전자)에 대해 4단계 에이전트 분석 실행, 리포트 품질 확인, decision_log 기록 확인
3. **Phase 3 LLM 검증**: 동일 종목에 대해 Claude/GPT/Gemini 각각 분석 → 결과 비교 → 비용 대비 품질 평가
4. **Phase 4~5 검증**: 모의투자 환경에서 전체 매매 사이클 (분석→시그널→승인→주문→체결) 동작 확인
5. **Phase 6 검증**: 1주일간 모의 운영 후 일간/주간 리포트 정상 수신 + 의사결정 조회 API 확인
6. **Phase 7 검증**: 3년 백테스트 결과가 벤치마크(KOSPI) 대비 양의 초과수익 + Sharpe > 0.5
7. **Phase 8 검증**: AWS 배포 후 `/health-check` 스킬로 전체 서비스 정상 동작 확인
8. **통합 검증**: 모의투자로 최소 2주간 무인 운영 → 로그/리포트/의사결정 기록 확인 후 실전 전환 판단

---

## TODO 리스트 (진행 추적)

> 각 태스크 앞의 체크박스로 진행 상태를 추적합니다.
> - `[ ]` 미시작 | `[~]` 진행중 | `[x]` 완료

### Phase 0: 프로젝트 기반 구축
- [x] Git 브랜치 설정: `develop` + `production` 브랜치 생성, `main`에서 전환
- [x] `pyproject.toml` 생성 (의존성 정의)
- [x] `.gitignore` 생성
- [x] `.env.example` 환경변수 템플릿 작성 (Phase별 구분)
- [x] `docker-compose.yml` 작성 (PostgreSQL + Redis)
- [x] `src/config.py` 설정 클래스 구현 (환경별 분기)
- [x] `src/core/enums.py` Enum 정의
- [ ] `src/core/models.py` 도메인 모델 정의 (Pydantic)
- [x] `src/core/exceptions.py` 커스텀 예외 정의
- [x] `src/db/base.py` SQLAlchemy Base 클래스
- [x] `src/db/session.py` async 세션 팩토리
- [x] Alembic 초기화 (`alembic.ini`, `alembic/env.py`)
- [ ] `src/main.py` FastAPI 앱 + `/health` 엔드포인트
- [ ] Docker Compose 기동 테스트
- [ ] uvicorn 서버 시작 + 헬스체크 확인

### Phase 1: 데이터 수집 레이어
- [ ] `src/broker/base.py` BrokerInterface ABC 정의
- [ ] `src/broker/kis/auth.py` OAuth 토큰 관리 (open-trading-api 참조)
- [ ] `src/broker/kis/models.py` KIS 응답 모델
- [ ] `src/broker/kis/client.py` KIS REST 클라이언트 (open-trading-api 참조)
- [ ] `src/data/providers/base.py` DataProvider ABC
- [ ] `src/data/providers/kis_provider.py` KIS 시세 데이터 제공자
- [ ] `src/data/cache.py` Redis 캐시 래퍼
- [ ] `src/data/collector.py` 데이터 수집기
- [ ] DB 모델: `stock_master` 테이블
- [ ] DB 모델: `daily_ohlcv` 테이블
- [ ] DB 모델: `api_tokens` 테이블
- [ ] Alembic 마이그레이션 생성 및 적용
- [ ] KIS 모의투자 API 연결 테스트
- [ ] 종목 마스터 수집 테스트
- [ ] 일봉 데이터 수집/저장 테스트
- [ ] Redis 캐시 동작 테스트

### Phase 2: 분석 엔진
- [ ] `src/analysis/technical/indicators.py` 기술 지표 (상세 주석 포함)
- [ ] `src/analysis/technical/patterns.py` 차트 패턴 감지 (상세 주석 포함)
- [ ] `src/analysis/fundamental/analyzer.py` 펀더멘털 분석
- [ ] `src/analysis/sentiment/analyzer.py` 뉴스 감성 분석
- [ ] `docs/TRADING_LOGIC.md` 매매 로직 통합 레퍼런스 작성
- [ ] 기술 지표 단위 테스트
- [ ] 펀더멘털 점수 산출 테스트
- [ ] 감성 분석 통합 테스트

### Phase 3: LLM 에이전트 시스템
- [ ] `src/llm/base.py` LLMProvider ABC
- [ ] `src/llm/providers/openai.py` OpenAI 프로바이더 (주력: o3, GPT-5, o4-mini, GPT-5 Mini/Nano)
- [ ] `src/llm/providers/google.py` Gemini 프로바이더 (보조: 2.5 Pro, 2.5 Flash, 2.5 Flash-Lite)
- [ ] `src/llm/providers/anthropic.py` Claude 프로바이더 (백업: Opus 4.5, Sonnet 4.5, Haiku 4.5)
- [ ] `src/llm/router.py` LLM 라우터 (DB 기반 설정 + 에스컬레이션 + 캐시)
- [ ] `config/default_model_assignments.yaml` 에이전트별 모델 기본값 (DB seed)
- [ ] `src/api/admin/llm_config.py` Admin API 엔드포인트
- [ ] `src/db/models/agent_model_config.py` ORM 모델
- [ ] `src/llm/cost_tracker.py` LLM 비용 추적 (에스컬레이션 비율 + prompt cache hit rate 추적)
- [ ] `src/agent/decision_recorder.py` 의사결정 근거 기록 유틸리티
- [ ] `src/agent/tools/technical.py` 기술 지표 조회 도구
- [ ] `src/agent/tools/fundamental.py` 재무 데이터 조회 도구
- [ ] `src/agent/tools/market_data.py` 시세/호가 조회 도구
- [ ] `src/agent/tools/news.py` 뉴스/공시 조회 도구
- [ ] `src/agent/prompts/market_analysis.py` 시장 분석 프롬프트
- [ ] `src/agent/prompts/stock_analysis.py` 종목 분석 프롬프트
- [ ] `src/agent/prompts/trade_decision.py` 매매 결정 프롬프트
- [ ] `src/agent/agents/market_analyst.py` 시장 분석 에이전트
- [ ] `src/agent/agents/stock_analyst.py` 종목 분석 에이전트
- [ ] `src/agent/agents/risk_manager.py` 리스크 관리 에이전트
- [ ] `src/agent/agents/trader.py` 매매 결정 에이전트
- [ ] `src/agent/orchestrator.py` 오케스트레이터
- [ ] `src/agent/memory.py` 에이전트 메모리
- [ ] DB 모델: `decision_log`, `agent_analyses`, `agent_decisions`, `agent_memory`, `llm_usage`
- [ ] 4단계 파이프라인 통합 테스트 (삼성전자 등 테스트 종목)
- [ ] 멀티 LLM 비교 테스트 (동일 종목, Claude vs GPT vs Gemini)

### Phase 4: 전략 엔진 + 리스크 관리
- [ ] `src/strategy/base.py` Strategy ABC
- [ ] `src/strategy/position_trading.py` 포지션 트레이딩 전략 (상세 주석 포함)
- [ ] `src/strategy/swing_trading.py` 스윙 트레이딩 전략 (상세 주석 포함)
- [ ] `src/strategy/risk_manager.py` 리스크 관리 모듈
- [ ] 포지션 사이징 로직 구현 (고정비율 1~2%)
- [ ] 손절/익절 로직 구현 (ATR 기반, 트레일링)
- [ ] 섹터 집중도/드로다운 체크 구현
- [ ] `docs/TRADING_LOGIC.md` 전략 섹션 업데이트
- [ ] 리스크 관리 단위 테스트
- [ ] 전략 시그널 생성 통합 테스트

### Phase 5: 주문 실행 + 사용자 승인
- [ ] `src/notification/telegram.py` 텔레그램 봇 구현
- [ ] `src/notification/templates.py` 메시지 템플릿
- [ ] `src/execution/approval.py` 승인 관리 로직
- [ ] `src/execution/executor.py` 주문 실행기
- [ ] DB 모델: `orders`, `executions`, `approval_requests`
- [ ] 텔레그램 승인 워크플로우 테스트
- [ ] KIS 모의투자 주문 실행 테스트
- [ ] 전체 매매 사이클 E2E 테스트 (decision_log 기록 포함)

### Phase 6: 스케줄러 + 리포트 + 모니터링
- [ ] `src/scheduler/jobs.py` APScheduler 정기 작업 구현
- [ ] 일간/주간/월간 리포트 생성 로직
- [ ] LLM 비용 주간 리포트
- [ ] 텔레그램 리포트 전송 구현
- [ ] `src/api/routes/portfolio.py` 포트폴리오 API
- [ ] `src/api/routes/trades.py` 거래 내역 API
- [ ] `src/api/routes/analysis.py` 분석 결과 API
- [ ] `src/api/routes/decisions.py` 의사결정 근거 조회 API
- [ ] `src/api/routes/control.py` 수동 제어 API
- [ ] 성과 지표 계산 (Sharpe, Sortino, MDD, 승률)
- [ ] 스케줄러 동작 테스트
- [ ] 리포트 수신 테스트

### Phase 7: 백테스팅 프레임워크
- [ ] `src/backtest/engine.py` 백테스팅 엔진
- [ ] `src/backtest/portfolio.py` 가상 포트폴리오 시뮬레이션
- [ ] `src/backtest/metrics.py` 성과 지표 계산
- [ ] `src/backtest/report.py` 백테스트 리포트
- [ ] 슬리피지/수수료 모델링
- [ ] 3년 백테스트 실행 및 결과 검증
- [ ] 벤치마크 비교 리포트
- [ ] 멀티 LLM 성과 비교 백테스트

### Phase 8: Docker + AWS 배포
- [ ] `Dockerfile` 멀티스테이지 빌드
- [ ] `docker-compose.prod.yml` 프로덕션 로컬 테스트
- [ ] `docs/AWS_INFRASTRUCTURE_GUIDE.md` AWS 인프라 가이드 작성
- [ ] `.github/workflows/deploy.yml` CI/CD 파이프라인 (`production` 브랜치 기반 배포)
- [ ] AWS 인프라 설정 (ECS, RDS, ElastiCache, Secrets Manager)
- [ ] CloudWatch 모니터링 대시보드
- [ ] 스테이징 환경 배포 및 테스트
- [ ] 프로덕션 배포
- [ ] 모의투자 상시 운영 확인

---

## 진행 기록

> 각 세션에서 작업한 내용을 날짜별로 기록합니다.

| 날짜 | 작업 내용 | 완료된 Phase | 비고 |
|------|----------|-------------|------|
| 2026-02-08 | 프로젝트 설계 및 구현 계획 수립 | 설계 완료 | 아키텍처, 디렉토리 구조, 9개 Phase 정의 |
| 2026-02-08 | 설계 v1.1 업데이트 | 설계 갱신 | 멀티 LLM, 의사결정 기록, KIS SDK 참조, 환경 관리, AWS 가이드 추가 |
| 2026-02-09 | 설계 v1.2 업데이트 | 설계 갱신 | LLM 비용 최적화 혼합 라우팅, Git 브랜치 전략, 모델명 최신화, 문서 구조 정리 |
