---
name: deploy
description: 스테이징/프로덕션 환경으로의 AWS 배포 프로세스 실행
allowed-tools:
  - Bash
  - Read
---

# AWS 배포 워크플로우

## 사용법
```
/deploy [staging|production]
```

기본값: `staging`

## 배포 단계

### 1. 사전 검증
- 현재 브랜치 확인 (production 배포 시 `main` 브랜치 필수)
- 커밋되지 않은 변경사항 확인
- 환경변수 설정 검증 (`.env` 파일 또는 AWS Secrets Manager)

### 2. 테스트 실행
```bash
pytest tests/ -v --tb=short
```
- 테스트 실패 시 배포 중단
- 커버리지 리포트 출력

### 3. Docker 이미지 빌드
```bash
docker build -t stock-trading-agent:$(git rev-parse --short HEAD) .
docker tag stock-trading-agent:$(git rev-parse --short HEAD) stock-trading-agent:latest
```

### 4. 이미지 푸시 (ECR)
```bash
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin <ECR_REGISTRY>
docker push <ECR_REGISTRY>/stock-trading-agent:$(git rev-parse --short HEAD)
```

### 5. 배포 실행
- **Staging**: ECS 서비스 업데이트
- **Production**: 블루/그린 배포 또는 롤링 업데이트

### 6. 헬스체크 검증
- 배포 후 서비스 상태 확인
- API 엔드포인트 응답 검증
- 로그 확인으로 에러 없는지 검증

### 7. 완료 알림
- 배포 결과 요약 출력
- 실패 시 롤백 절차 안내

## 주의사항
- Production 배포는 반드시 사용자 확인 후 진행
- 장 운영 시간(09:00-15:30)에는 production 배포 자제
- 배포 전 현재 운영 중인 매매 전략 상태 확인
