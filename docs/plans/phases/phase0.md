# Phase 0: 프로젝트 기반 구축 — 구현 계획

> **상태**: 미시작
> **작성일**: 2026-02-09
> **완료 기준**: `docker-compose up` → DB/Redis 기동 → `uvicorn` 서버 시작 → `/health` 엔드포인트 응답

---

## 사용자 확인 사항
- **패키지 매니저**: uv
- **Git 전략**: develop/production 브랜치. 작업 브랜치(feature/phase/fix)에서 develop으로 머지, Docker 테스트 후 수동으로 production 머지. 로컬 전용.
- **테스트**: Phase 0에 기본 테스트 포함 (tests/test_health.py)

---

## 생성할 파일 (18개 파일 + 6개 디렉토리)

### Step 1: 인프라 파일 (의존성 없음) → commit #1

| 파일 | 역할 |
|------|------|
| `.gitignore` | Python + IDE + .env + __pycache__ + docker-data |
| `pyproject.toml` | 의존성 관리 (uv), 도구 설정 (ruff, pytest, mypy) |
| `.env.example` | 환경변수 템플릿 (Phase별 그룹핑, Phase 0만 활성) |
| `docker-compose.yml` | PostgreSQL 16 + Redis 7 (Alpine, healthcheck 포함) |

**pyproject.toml 핵심 의존성:**
```
fastapi, uvicorn[standard], sqlalchemy[asyncio], asyncpg, alembic
pydantic, pydantic-settings, python-dotenv
redis[hiredis], aiohttp, structlog
```

**dev 의존성:**
```
pytest, pytest-asyncio, pytest-cov, httpx, ruff, mypy
```

**docker-compose.yml 구성:**
- `postgres:16-alpine` — port 5432, volume `postgres_data`, healthcheck `pg_isready`
- `redis:7-alpine` — port 6379, volume `redis_data`, healthcheck `redis-cli ping`, AOF persistence, maxmemory 256mb

### Step 2: 패키지 초기화 + 코어 도메인 + 설정 → commit #2

| 파일 | 역할 |
|------|------|
| `src/__init__.py` | 빈 파일 (패키지 선언) |
| `src/core/__init__.py` | 빈 파일 |
| `src/db/__init__.py` | 빈 파일 |
| `src/core/enums.py` | StrEnum 정의 — Environment, MarketType, OrderSide, OrderType, OrderStatus, PositionStatus, SignalAction, DecisionStage, AgentType, LLMProviderType, ApprovalStatus, RoutingMode, DecisionAction, DecisionOutcome |
| `src/core/exceptions.py` | 예외 계층 — TradingAgentError → ConfigurationError, DatabaseError, BrokerError(+AuthError, APIError, OrderError, InsufficientFundsError), RiskLimitError, LLMError(+ProviderError, BudgetExceededError), ApprovalError(+TimeoutError) |
| `src/config.py` | pydantic-settings Settings 클래스 + `@lru_cache` 싱글톤 `get_settings()`. `Literal["development", "production"]` 검증. Phase별 환경변수 그룹핑 |

### Step 3: DB 레이어 → commit #3

| 파일 | 역할 |
|------|------|
| `src/db/base.py` | SQLAlchemy 2.0 `DeclarativeBase` + `TimestampMixin` (created_at, updated_at with `DateTime(timezone=True)`) |
| `src/db/session.py` | async engine/session 관리 — `init_db()`, `close_db()`, `get_db_session()` (FastAPI DI용), `get_engine()` |

**핵심 설정:**
- `expire_on_commit=False` (async 필수)
- `pool_pre_ping=True` (연결 검증)
- dev: `echo=True`, pool_size=5 / prod: `echo=False`, pool_size=10

### Step 4: Alembic → commit #4

| 파일 | 역할 |
|------|------|
| `alembic.ini` | 기본 설정 (URL은 env.py에서 Settings 주입) |
| `alembic/env.py` | async 브릿지 패턴 — `async_engine_from_config` + `connection.run_sync()` |
| `alembic/script.py.mako` | 마이그레이션 템플릿 |
| `alembic/versions/.gitkeep` | 빈 디렉토리 유지 |

### Step 5: 도메인 모델 + FastAPI 앱 → commit #5

| 파일 | 역할 |
|------|------|
| `src/core/models.py` | Pydantic 도메인 모델 — StockInfo, PriceInfo, OHLCV, Signal, OrderRequest, OrderResult, Position, AccountBalance, HealthStatus. 모든 금융 필드 `Decimal` |
| `src/main.py` | FastAPI 앱 + lifespan (DB/Redis init/close) + structlog 설정 + `/health` 엔드포인트 |

**structlog 설정:**
- Development: `ConsoleRenderer(colors=True)` → 컬러 터미널 출력
- Production: `JSONRenderer()` → CloudWatch 연동용 JSON 포맷

**/health 응답 형태:**
```json
{
  "status": "ok",
  "environment": "development",
  "database": "connected",
  "redis": "connected",
  "timestamp": "2026-02-09T..."
}
```

**Redis 클라이언트:**
- `redis.asyncio.from_url()`, `decode_responses=True`
- lifespan에서 init/close 관리

**Swagger/ReDoc:**
- Development만 `/docs`, `/redoc` 노출

### Step 6: 테스트 → commit #6

| 파일 | 역할 |
|------|------|
| `tests/__init__.py` | 빈 파일 |
| `tests/test_health.py` | /health 엔드포인트 스모크 테스트 (pytest-asyncio + httpx ASGITransport) |

---

## 실행 순서

```
1. Git 브랜치 설정:
   - main 브랜치를 develop으로 리네임: git branch -m main develop
   - production 브랜치 생성: git branch production
   - develop 브랜치에서 작업 계속
2. Step 1 파일 생성 → git commit #1 "Phase 0: project infrastructure"
3. uv sync --extra dev (의존성 설치)
4. Step 2 파일 생성 → git commit #2 "Phase 0: core domain + config"
5. Step 3 파일 생성 → git commit #3 "Phase 0: async DB session layer"
6. Step 4 파일 생성 → git commit #4 "Phase 0: Alembic async migration setup"
7. Step 5 파일 생성 → git commit #5 "Phase 0: FastAPI app + /health endpoint"
8. Step 6 파일 생성 → git commit #6 "Phase 0: health endpoint smoke test"
9. cp .env.example .env
10. docker compose up -d
11. uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
12. curl localhost:8000/health 확인
13. uv run pytest tests/ 실행
14. Phase 0 완료 후 develop → production 최초 머지
15. git tag v0.1.0-phase0
```

---

## 주의사항

1. **DATABASE_URL 스킴**: 반드시 `postgresql+asyncpg://` 사용 (`postgresql://` 아님)
2. **포트 충돌**: 로컬에 PostgreSQL/Redis가 설치되어 있으면 5432/6379 포트 충돌 가능 → `docker compose` 포트 변경 또는 로컬 서비스 중지
3. **Alembic ORM import**: Phase 1에서 ORM 모델 추가 시 `alembic/env.py`에 import 추가 필수
4. **Python 버전**: `requires-python = ">=3.12"`, 현재 시스템 Python에서 동작
5. **expire_on_commit=False**: async SQLAlchemy 필수. 없으면 `MissingGreenlet` 에러
6. **uv.lock**: .gitignore에 추가하지 말 것 (재현 가능한 빌드를 위해 추적)

---

## 검증 방법

1. `docker compose up -d` → `docker compose ps`에서 postgres/redis 모두 "healthy"
2. `uv run uvicorn src.main:app --reload` → 서버 정상 시작, structlog 컬러 출력
3. `curl localhost:8000/health` → `{"status": "ok", "database": "connected", "redis": "connected"}`
4. `curl localhost:8000/docs` → Swagger UI 정상 로딩
5. `uv run alembic current` → DB 연결 성공
6. `uv run pytest tests/test_health.py` → 테스트 통과

---

## 진행 추적

- [x] Git 브랜치 설정: main → develop 리네임, production 브랜치 생성
- [x] Step 1: 인프라 파일 + commit #1
- [x] uv sync 성공
- [x] Step 2: 코어 도메인 + 설정 + commit #2
- [x] Step 3: DB 레이어 + commit #3
- [x] Step 4: Alembic + commit #4
- [ ] Step 5: 도메인 모델 + FastAPI 앱 + commit #5
- [ ] Step 6: 테스트 + commit #6
- [ ] docker compose up 성공
- [ ] /health 엔드포인트 응답 확인
- [ ] pytest 통과
- [ ] git tag v0.1.0-phase0
