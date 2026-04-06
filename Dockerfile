# =============================================================================
# Stock Trading Agent - Production Dockerfile
# Multi-stage build: builder(uv + deps) → runtime(slim)
# =============================================================================

# ── Stage 1: Builder ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# uv 설치
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 의존성 파일만 먼저 복사 → Docker layer cache 활용
COPY pyproject.toml uv.lock ./

# production 의존성만 설치 (dev 제외)
RUN uv sync --frozen --no-dev --no-install-project

# 프로젝트 소스 복사
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY config/ ./config/

# 프로젝트 자체를 설치
RUN uv sync --frozen --no-dev

# ── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# non-root 사용자 생성
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home appuser

# builder에서 virtualenv + 소스 복사
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/config /app/config

# entrypoint 스크립트 복사
COPY scripts/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# virtualenv의 Python을 PATH에 추가
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# static/templates 복사 (백오피스 웹 UI)
COPY --from=builder /app/src/static /app/src/static
COPY --from=builder /app/src/templates /app/src/templates

# 소유권 변경
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
