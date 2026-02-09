---
name: setup-env
description: 새로운 개발 환경을 처음부터 세팅하는 워크플로우
allowed-tools:
  - Bash
  - Read
  - Write
---

# 개발 환경 초기화

## 사용법
```
/setup-env
```

## 설정 단계

### 1. 시스템 요구사항 확인
- Python 3.11+ 설치 여부 확인
- Docker / Docker Compose 설치 여부 확인
- Git 설정 확인
- AWS CLI 설치 여부 확인 (선택)

### 2. Python 가상환경 생성
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 의존성 설치
```bash
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 개발 의존성
```

### 4. 환경변수 설정
`.env.example`을 기반으로 `.env` 파일 생성:
```
# KIS API
KIS_APP_KEY=your_app_key
KIS_APP_SECRET=your_app_secret
KIS_ACCOUNT_NO=your_account_number
KIS_MOCK=true  # 모의투자 모드

# Database
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/trading
REDIS_URL=redis://localhost:6379/0

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# LLM
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key
```

### 5. 로컬 서비스 시작 (Docker Compose)
```bash
docker-compose up -d postgres redis
```

### 6. 데이터베이스 초기화
```bash
alembic upgrade head
```

### 7. 설정 검증
- Python 환경 확인
- DB 연결 테스트
- Redis 연결 테스트
- KIS API 연결 테스트 (모의투자)
- 기본 테스트 실행: `pytest tests/ -x -q`

### 8. 완료
- 환경 설정 요약 출력
- 다음 단계 안내 (첫 전략 실행 가이드)
