---
name: health-check
description: 배포된 서비스의 상태 및 로그 확인
allowed-tools:
  - Bash
  - Read
---

# 서비스 상태 확인

## 사용법
```
/health-check [local|staging|production]
```

기본값: `local`

## 점검 항목

### 1. 애플리케이션 상태
- API 서버 응답 확인 (`/health` 엔드포인트)
- 프로세스 상태 확인
- 메모리/CPU 사용량 확인

### 2. 데이터베이스 연결
- PostgreSQL 연결 상태
- 활성 커넥션 수 확인
- 최근 슬로우 쿼리 확인

### 3. Redis 캐시 상태
- Redis 연결 상태
- 메모리 사용량
- 키 수 및 히트율

### 4. 외부 API 연결 상태
- KIS API 토큰 유효성 확인
- KIS API 응답 시간 측정
- 텔레그램 봇 연결 상태

### 5. 트레이딩 에이전트 상태
- 활성 전략 목록 및 상태
- 최근 매매 기록 확인
- 미체결 주문 확인
- 오늘의 손익 요약

### 6. 로그 분석
- 최근 에러 로그 확인 (최근 1시간)
- 경고 로그 빈도 확인
- 비정상 패턴 감지

## 환경별 접근 방법
- **local**: 로컬 프로세스 및 Docker 컨테이너 직접 확인
- **staging**: AWS ECS 서비스 상태 및 CloudWatch 로그
- **production**: AWS ECS 서비스 상태 및 CloudWatch 로그 + 메트릭 대시보드

## 출력 형식
```
🟢 정상 | 🟡 경고 | 🔴 에러

서비스 상태 리포트 (환경: xxx, 시간: YYYY-MM-DD HH:MM:SS)
─────────────────────────
API 서버:      🟢 정상 (응답시간: 45ms)
PostgreSQL:    🟢 정상 (커넥션: 5/20)
Redis:         🟢 정상 (메모리: 128MB)
KIS API:       🟢 정상 (토큰 만료: 12시간 후)
텔레그램 봇:   🟢 정상
매매 에이전트:  🟢 활성 (전략 3개 실행 중)
─────────────────────────
에러 로그: 없음
경고 로그: 2건 (rate limit 접근)
```
