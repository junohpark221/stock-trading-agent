# AWS 인프라 운영 가이드

> 이 문서는 AWS 클라우드 환경에서 주식 트레이딩 에이전트를 배포하고 운영하기 위한 **초보자용 상세 가이드**입니다.
> 물리 서버 배포 경험은 있으나 AWS 경험이 없는 개발자를 대상으로 작성되었습니다.
>
> **관련 문서:** [DESIGN.md](../DESIGN.md) (Phase 8: Docker + AWS 배포)

---

## 목차

1. [AWS 계정 준비](#1-aws-계정-준비)
2. [핵심 개념 — 물리 서버와의 대응](#2-핵심-개념--물리-서버와의-대응)
3. [사용하는 AWS 서비스 요약](#3-사용하는-aws-서비스-요약)
4. [사전 준비 — 로컬 도구 설치](#4-사전-준비--로컬-도구-설치)
5. [Step 1: VPC 및 네트워크 구성](#5-step-1-vpc-및-네트워크-구성)
6. [Step 2: RDS PostgreSQL 생성](#6-step-2-rds-postgresql-생성)
7. [Step 3: ElastiCache Redis 생성](#7-step-3-elasticache-redis-생성)
8. [Step 4: Secrets Manager에 시크릿 등록](#8-step-4-secrets-manager에-시크릿-등록)
9. [Step 5: ECR 레포지토리 생성 및 Docker 이미지 푸시](#9-step-5-ecr-레포지토리-생성-및-docker-이미지-푸시)
10. [Step 6: ECS Fargate 서비스 배포](#10-step-6-ecs-fargate-서비스-배포)
11. [Step 7: CloudWatch 모니터링 설정](#11-step-7-cloudwatch-모니터링-설정)
12. [Step 8: CI/CD 파이프라인 (GitHub Actions)](#12-step-8-cicd-파이프라인-github-actions)
13. [비용 관리](#13-비용-관리)
14. [문제 해결 (트러블슈팅)](#14-문제-해결-트러블슈팅)
15. [체크리스트](#15-체크리스트)

---

## 1. AWS 계정 준비

### 해야 할 일

1. **AWS 계정 생성**: https://aws.amazon.com 에서 가입
   - 신용카드 등록 필요 (프리티어 한도 내에서는 과금되지 않음)
   - 루트 계정은 일상적으로 사용하지 않음

2. **IAM 사용자 생성** (보안 필수)
   - AWS Console → IAM → Users → Create User
   - 이름: `trading-agent-admin`
   - 권한: `AdministratorAccess` (초기 설정용, 추후 최소 권한으로 변경)
   - Access Key 발급 → 안전한 곳에 저장

3. **MFA 활성화** (강력 권장)
   - 루트 계정과 IAM 사용자 모두에 MFA 설정
   - Google Authenticator 또는 1Password 사용

4. **리전 선택**: `ap-northeast-2` (서울)
   - KIS API 서버가 한국에 있으므로 지연시간 최소화

### 물리 서버와의 차이

| 물리 서버 | AWS |
|----------|-----|
| 서버실에 가서 서버 설치 | 웹 콘솔에서 클릭 몇 번으로 생성 |
| 고정 비용 (서버 구매) | 사용한 만큼 과금 (시간/초 단위) |
| 직접 OS 설치, 패치 | 관리형 서비스로 OS 관리 불필요 (RDS, Fargate 등) |
| IP/방화벽 직접 설정 | VPC/Security Group으로 관리 |
| 백업을 직접 스크립트로 | RDS 자동 백업, 스냅샷 |

---

## 2. 핵심 개념 — 물리 서버와의 대응

| 물리 서버 개념 | AWS 대응 | 설명 |
|--------------|---------|------|
| 내부 네트워크 | **VPC** (Virtual Private Cloud) | 가상 사설 네트워크. 내 서비스들만의 격리된 네트워크 공간 |
| 서브넷/VLAN | **Subnet** | VPC 안의 IP 대역 구분. Public(인터넷 가능) / Private(내부만) |
| 방화벽 | **Security Group** | 인바운드/아웃바운드 트래픽 허용 규칙 |
| 물리 서버 | **EC2** | 가상 서버 인스턴스 (우리는 EC2 대신 Fargate 사용) |
| Docker + 서버 | **ECS Fargate** | 서버 관리 없이 컨테이너만 실행. EC2 인스턴스 관리 불필요 |
| PostgreSQL 설치 | **RDS** | 관리형 데이터베이스. 백업, 패치, 이중화 자동 |
| Redis 설치 | **ElastiCache** | 관리형 Redis. 클러스터 관리 자동 |
| .env 파일 | **Secrets Manager** | 시크릿을 암호화하여 안전하게 저장/관리 |
| Docker Registry | **ECR** | AWS용 Docker 이미지 저장소 |
| crontab | **EventBridge** | 스케줄 기반 이벤트 트리거 |
| 로그 파일 (syslog) | **CloudWatch Logs** | 중앙 집중식 로그 수집/검색/알람 |

---

## 3. 사용하는 AWS 서비스 요약

```
┌─────────────────────────────────────────────────┐
│                  VPC (10.0.0.0/16)              │
│                                                  │
│  ┌──────────────┐  ┌──────────────┐             │
│  │ Public Subnet │  │ Public Subnet │             │
│  │ (AZ-a)        │  │ (AZ-c)        │             │
│  │               │  │               │             │
│  │ ALB (선택)    │  │               │             │
│  └──────┬───────┘  └───────────────┘             │
│         │                                        │
│  ┌──────▼───────┐  ┌──────────────┐             │
│  │Private Subnet │  │Private Subnet │             │
│  │ (AZ-a)        │  │ (AZ-c)        │             │
│  │               │  │               │             │
│  │ ECS Fargate   │  │ RDS (Standby) │             │
│  │ (트레이딩 앱)  │  │               │             │
│  │               │  │               │             │
│  │ RDS (Primary) │  │ ElastiCache   │             │
│  └──────────────┘  └──────────────┘             │
│                                                  │
└─────────────────────────────────────────────────┘
         │
    ┌────▼────┐
    │ Internet │  ← KIS API, LLM API, Telegram API
    └─────────┘
```

### 월간 예상 비용 (서울 리전)

| 서비스 | 스펙 | 월 예상 비용 |
|--------|------|------------|
| ECS Fargate | 1 vCPU, 2GB, 24시간 상시 | ~$35 |
| RDS PostgreSQL | db.t3.micro (프리티어 1년 무료) | $0~$15 |
| ElastiCache Redis | cache.t3.micro | ~$12 |
| Secrets Manager | 시크릿 5~10개 | ~$3 |
| CloudWatch | 로그 5GB/월, 알람 5개 | ~$5 |
| ECR | 이미지 1GB | ~$0.10 |
| **합계** | | **~$55~70/월** |

> 프리티어 기간(1년) 중에는 RDS가 무료이므로 ~$40~55/월

---

## 4. 사전 준비 — 로컬 도구 설치

```bash
# 1. AWS CLI 설치
brew install awscli              # macOS

# 2. AWS CLI 설정
aws configure
# AWS Access Key ID: (IAM에서 발급받은 키)
# AWS Secret Access Key: (IAM에서 발급받은 시크릿)
# Default region name: ap-northeast-2
# Default output format: json

# 3. 설정 확인
aws sts get-caller-identity      # 현재 인증된 사용자 확인

# 4. Docker 설치 (이미 있다면 건너뛰기)
# https://docs.docker.com/desktop/install/mac-install/

# 5. (선택) Terraform 설치 — 인프라를 코드로 관리할 때
brew install terraform
```

---

## 5. Step 1: VPC 및 네트워크 구성

### 왜 필요한가?

VPC는 AWS에서의 "내 네트워크"이다. 물리 서버에서 스위치/라우터로 내부 네트워크를 구성하는 것과 같다. 모든 AWS 리소스(RDS, ElastiCache, ECS)는 VPC 안에 배치된다.

### AWS 콘솔에서 하는 방법

1. **VPC 마법사 사용** (가장 쉬움)
   - AWS Console → VPC → "VPC 마법사 시작"
   - "퍼블릭 및 프라이빗 서브넷이 있는 VPC" 선택
   - VPC 이름: `trading-agent-vpc`
   - CIDR: `10.0.0.0/16`
   - 가용 영역: 2개 (ap-northeast-2a, ap-northeast-2c)
   - 퍼블릭 서브넷: 2개, 프라이빗 서브넷: 2개
   - NAT 게이트웨이: 1개 (프라이빗 서브넷에서 인터넷 접근용)

2. **Security Group 생성**

   | 이름 | 인바운드 | 용도 |
   |------|---------|------|
   | `sg-ecs` | 없음 (아웃바운드만) | ECS 태스크용 |
   | `sg-rds` | TCP 5432 from sg-ecs | RDS PostgreSQL |
   | `sg-redis` | TCP 6379 from sg-ecs | ElastiCache Redis |

### CLI로 하는 방법

```bash
# VPC 생성
aws ec2 create-vpc --cidr-block 10.0.0.0/16 --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=trading-agent-vpc}]'

# (이하 서브넷, 라우팅 테이블, NAT Gateway 등 — 콘솔 마법사가 훨씬 편함)
```

> **팁:** 처음에는 AWS 콘솔(웹)에서 하는 것을 추천. 익숙해진 후 Terraform으로 코드화.

---

## 6. Step 2: RDS PostgreSQL 생성

### 왜 필요한가?

물리 서버에 PostgreSQL을 직접 설치하면 백업, 업데이트, 장애 대응을 모두 직접 해야 한다. RDS는 이 모든 것을 AWS가 대신 해준다.

### 설정

```
엔진: PostgreSQL 16
인스턴스: db.t3.micro (프리티어) → 추후 db.t3.medium
스토리지: 20GB gp3 (자동 확장 활성화)
가용 영역: Multi-AZ (프로덕션, 추후 전환)
VPC: trading-agent-vpc
서브넷 그룹: 프라이빗 서브넷 2개
Security Group: sg-rds
DB 이름: trading
마스터 사용자: trading_admin
비밀번호: (Secrets Manager에서 자동 생성 권장)
자동 백업: 7일간 보관
```

### AWS 콘솔에서

1. RDS → "데이터베이스 생성"
2. "표준 생성" 선택
3. PostgreSQL 선택
4. 위 설정값 입력
5. "데이터베이스 생성" 클릭
6. 5~10분 후 엔드포인트 확인: `trading-agent.xxxxx.ap-northeast-2.rds.amazonaws.com`

### 연결 테스트 (ECS에서)

```python
# DATABASE_URL 형식
DATABASE_URL=postgresql+asyncpg://trading_admin:PASSWORD@trading-agent.xxxxx.ap-northeast-2.rds.amazonaws.com:5432/trading
```

> **주의:** RDS는 프라이빗 서브넷에 배치하므로, 로컬 PC에서 직접 접속 불가. ECS 태스크 내에서만 접속 가능.
> 로컬에서 DB 관리가 필요하면: AWS Console → RDS → Query Editor 사용, 또는 EC2 배스천 호스트 경유.

---

## 7. Step 3: ElastiCache Redis 생성

### 설정

```
엔진: Redis 7
노드 타입: cache.t3.micro
복제본: 0 (비용 절약, 추후 1로 변경)
VPC: trading-agent-vpc
서브넷 그룹: 프라이빗 서브넷 2개
Security Group: sg-redis
```

### AWS 콘솔에서

1. ElastiCache → "Redis 클러스터 생성"
2. "새 클러스터 설계 및 구성" 선택
3. 위 설정값 입력
4. 엔드포인트 확인: `trading-agent-redis.xxxxx.cache.amazonaws.com:6379`

```python
# REDIS_URL 형식
REDIS_URL=redis://trading-agent-redis.xxxxx.cache.amazonaws.com:6379/0
```

---

## 8. Step 4: Secrets Manager에 시크릿 등록

### 왜 필요한가?

`.env` 파일은 서버에 직접 두면 보안 위험이 있다. Secrets Manager는 시크릿을 암호화하여 저장하고, 접근 권한을 IAM으로 제어한다.

### 등록할 시크릿

```bash
# 하나의 시크릿에 JSON으로 모든 키를 묶어서 저장
aws secretsmanager create-secret \
  --name trading-agent/production \
  --description "Trading agent production secrets" \
  --secret-string '{
    "DATABASE_URL": "postgresql+asyncpg://trading_admin:PASSWORD@rds-endpoint:5432/trading",
    "REDIS_URL": "redis://elasticache-endpoint:6379/0",
    "KIS_APP_KEY": "your_app_key",
    "KIS_APP_SECRET": "your_app_secret",
    "KIS_ACCOUNT_NO": "12345678",
    "KIS_IS_PAPER": "false",
    "ANTHROPIC_API_KEY": "sk-ant-...",
    "OPENAI_API_KEY": "sk-...",
    "GOOGLE_API_KEY": "AIza...",
    "TELEGRAM_BOT_TOKEN": "your_bot_token",
    "TELEGRAM_CHAT_ID": "your_chat_id",
    "LLM_MONTHLY_BUDGET_USD": "100.00"
  }'
```

### 시크릿 업데이트

```bash
# 값 변경
aws secretsmanager update-secret \
  --secret-id trading-agent/production \
  --secret-string '{ ... 수정된 JSON ... }'

# 특정 키만 변경 (jq 활용)
CURRENT=$(aws secretsmanager get-secret-value --secret-id trading-agent/production --query SecretString --output text)
UPDATED=$(echo $CURRENT | jq '.ANTHROPIC_API_KEY = "new-key"')
aws secretsmanager update-secret --secret-id trading-agent/production --secret-string "$UPDATED"
```

### 애플리케이션에서 읽기

```python
# src/config.py에서 AWS Secrets Manager 연동
import boto3
import json

def load_secrets_from_aws(secret_name: str) -> dict:
    client = boto3.client("secretsmanager", region_name="ap-northeast-2")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])
```

---

## 9. Step 5: ECR 레포지토리 생성 및 Docker 이미지 푸시

### ECR이란?

Docker Hub의 AWS 버전. Docker 이미지를 저장하는 프라이빗 레지스트리.

### 레포지토리 생성

```bash
aws ecr create-repository \
  --repository-name trading-agent \
  --region ap-northeast-2
```

### Docker 이미지 빌드 및 푸시

```bash
# 1. ECR 로그인
aws ecr get-login-password --region ap-northeast-2 | \
  docker login --username AWS --password-stdin \
  YOUR_ACCOUNT_ID.dkr.ecr.ap-northeast-2.amazonaws.com

# 2. 이미지 빌드
docker build -t trading-agent .

# 3. 태그 지정
docker tag trading-agent:latest \
  YOUR_ACCOUNT_ID.dkr.ecr.ap-northeast-2.amazonaws.com/trading-agent:latest

# 4. 푸시
docker push \
  YOUR_ACCOUNT_ID.dkr.ecr.ap-northeast-2.amazonaws.com/trading-agent:latest
```

> `YOUR_ACCOUNT_ID`는 `aws sts get-caller-identity`로 확인 (12자리 숫자)

---

## 10. Step 6: ECS Fargate 서비스 배포

### ECS 핵심 개념

| 개념 | 물리 서버 대응 | 설명 |
|------|-------------|------|
| Cluster | 서버 그룹 | ECS 서비스를 묶는 논리적 단위 |
| Task Definition | Dockerfile + docker-compose | 컨테이너 스펙 정의 (이미지, CPU, 메모리, 환경변수) |
| Service | systemd 서비스 | Task를 지속적으로 실행하고 장애 시 재시작 |
| Task | docker run | 실행 중인 컨테이너 인스턴스 |

### 1. 클러스터 생성

```bash
aws ecs create-cluster --cluster-name trading-agent-cluster
```

### 2. Task Definition 생성

`ecs-task-definition.json`:
```json
{
  "family": "trading-agent",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::YOUR_ACCOUNT_ID:role/ecsTaskRole",
  "containerDefinitions": [
    {
      "name": "trading-agent",
      "image": "YOUR_ACCOUNT_ID.dkr.ecr.ap-northeast-2.amazonaws.com/trading-agent:latest",
      "essential": true,
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {"name": "ENV", "value": "production"},
        {"name": "AWS_SECRET_NAME", "value": "trading-agent/production"},
        {"name": "AWS_REGION", "value": "ap-northeast-2"}
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/trading-agent",
          "awslogs-region": "ap-northeast-2",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

```bash
aws ecs register-task-definition --cli-input-json file://ecs-task-definition.json
```

### 3. IAM 역할 생성

ECS Task가 Secrets Manager, CloudWatch 등에 접근하려면 IAM 역할이 필요:

- **ecsTaskExecutionRole**: ECR에서 이미지 풀, CloudWatch에 로그 쓰기
- **ecsTaskRole**: Secrets Manager에서 시크릿 읽기

```bash
# 실행 역할 (AWS 관리형 정책 사용)
aws iam attach-role-policy \
  --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# 태스크 역할 (Secrets Manager 읽기 권한)
# 커스텀 정책 생성 후 연결
```

### 4. 서비스 생성

```bash
aws ecs create-service \
  --cluster trading-agent-cluster \
  --service-name trading-agent-service \
  --task-definition trading-agent \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-private-a,subnet-private-c],securityGroups=[sg-ecs],assignPublicIp=DISABLED}"
```

### 5. 배포 확인

```bash
# 서비스 상태 확인
aws ecs describe-services \
  --cluster trading-agent-cluster \
  --services trading-agent-service

# 태스크 목록
aws ecs list-tasks --cluster trading-agent-cluster

# 태스크 상세
aws ecs describe-tasks \
  --cluster trading-agent-cluster \
  --tasks TASK_ARN
```

---

## 11. Step 7: CloudWatch 모니터링 설정

### 로그 확인

```bash
# 로그 그룹 생성 (Task Definition에서 지정한 이름)
aws logs create-log-group --log-group-name /ecs/trading-agent

# 최근 로그 보기
aws logs tail /ecs/trading-agent --follow

# 특정 키워드 검색
aws logs filter-log-events \
  --log-group-name /ecs/trading-agent \
  --filter-pattern "ERROR"
```

### 알람 설정 (권장)

```bash
# 1. ECS 태스크 실행 실패 알람
aws cloudwatch put-metric-alarm \
  --alarm-name "trading-agent-task-failure" \
  --metric-name "CPUUtilization" \
  --namespace "AWS/ECS" \
  --statistic "Average" \
  --period 300 \
  --threshold 90 \
  --comparison-operator "GreaterThanThreshold" \
  --evaluation-periods 2

# 2. RDS 디스크 공간 부족 알람
# 3. 비용 알람 (월간 예산 초과)
```

### 대시보드 (선택)

AWS Console → CloudWatch → 대시보드 → 생성
- ECS CPU/메모리 사용률
- RDS 연결 수, 디스크 사용량
- 애플리케이션 커스텀 메트릭 (매매 건수, LLM 호출 수)

---

## 12. Step 8: CI/CD 파이프라인 (GitHub Actions)

`.github/workflows/deploy.yml`:

```yaml
name: Deploy to AWS ECS

on:
  push:
    branches: [main]

env:
  AWS_REGION: ap-northeast-2
  ECR_REPOSITORY: trading-agent
  ECS_CLUSTER: trading-agent-cluster
  ECS_SERVICE: trading-agent-service

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to Amazon ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build, tag, and push image
        env:
          ECR_REGISTRY: ${{ steps.login-ecr.outputs.registry }}
          IMAGE_TAG: ${{ github.sha }}
        run: |
          docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG .
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG
          docker tag $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG $ECR_REGISTRY/$ECR_REPOSITORY:latest
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:latest

      - name: Deploy to ECS
        run: |
          aws ecs update-service \
            --cluster $ECS_CLUSTER \
            --service $ECS_SERVICE \
            --force-new-deployment
```

### GitHub Secrets 설정

GitHub repo → Settings → Secrets and variables → Actions:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

---

## 13. 비용 관리

### 비용 알람 설정 (필수)

```bash
# 월간 $80 초과 시 이메일 알림
aws budgets create-budget \
  --account-id YOUR_ACCOUNT_ID \
  --budget '{
    "BudgetName": "trading-agent-monthly",
    "BudgetLimit": {"Amount": "80", "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST"
  }' \
  --notifications-with-subscribers '[{
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80
    },
    "Subscribers": [{
      "SubscriptionType": "EMAIL",
      "Address": "your-email@example.com"
    }]
  }]'
```

### 비용 절약 팁

1. **RDS 프리티어 활용**: 첫 1년간 db.t3.micro 무료
2. **ElastiCache**: cache.t3.micro가 최소 사양
3. **ECS Fargate Spot**: 70% 할인 (중단 가능성 있음, 비-크리티컬 작업에 활용)
4. **사용하지 않을 때 서비스 중지**: 주말/야간에 desired-count를 0으로

```bash
# 서비스 중지 (비용 절약)
aws ecs update-service --cluster trading-agent-cluster --service trading-agent-service --desired-count 0

# 서비스 재시작
aws ecs update-service --cluster trading-agent-cluster --service trading-agent-service --desired-count 1
```

---

## 14. 문제 해결 (트러블슈팅)

### ECS 태스크가 시작되지 않을 때

```bash
# 1. 태스크 중지 사유 확인
aws ecs describe-tasks --cluster trading-agent-cluster --tasks TASK_ARN
# "stoppedReason" 필드 확인

# 2. 로그 확인
aws logs tail /ecs/trading-agent --since 30m

# 3. 흔한 원인
# - Security Group에서 아웃바운드 허용 안 됨 → 인터넷 접근 불가
# - Secrets Manager 접근 권한 없음 → IAM 역할 확인
# - 이미지 풀 실패 → ECR 로그인/이미지 존재 확인
# - 포트 충돌 → containerPort 확인
```

### RDS 연결 안 될 때

```bash
# 1. Security Group 확인: sg-rds가 sg-ecs의 5432 인바운드 허용하는지
# 2. 서브넷 확인: ECS와 RDS가 같은 VPC의 프라이빗 서브넷에 있는지
# 3. RDS 상태 확인
aws rds describe-db-instances --db-instance-identifier trading-agent-db
```

### 비용이 예상보다 높을 때

```bash
# Cost Explorer에서 서비스별 비용 확인
aws ce get-cost-and-usage \
  --time-period Start=2026-02-01,End=2026-02-28 \
  --granularity MONTHLY \
  --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE
```

---

## 15. 체크리스트

### 배포 전 체크리스트

- [ ] AWS 계정 생성 및 IAM 사용자 설정
- [ ] MFA 활성화
- [ ] AWS CLI 설치 및 `aws configure` 완료
- [ ] VPC + 서브넷 + Security Group 생성
- [ ] RDS PostgreSQL 생성 및 엔드포인트 확인
- [ ] ElastiCache Redis 생성 및 엔드포인트 확인
- [ ] Secrets Manager에 시크릿 등록
- [ ] ECR 레포지토리 생성
- [ ] Docker 이미지 빌드 및 ECR 푸시 성공
- [ ] ECS 클러스터 + Task Definition + Service 생성
- [ ] IAM 역할 (ecsTaskExecutionRole, ecsTaskRole) 설정
- [ ] CloudWatch 로그 그룹 생성
- [ ] 비용 알람 설정

### 배포 후 확인 체크리스트

- [ ] ECS 태스크 RUNNING 상태
- [ ] `/health` 엔드포인트 응답 (ECS 내부에서)
- [ ] RDS 연결 성공 (Alembic 마이그레이션 실행)
- [ ] Redis 연결 성공
- [ ] Secrets Manager에서 시크릿 읽기 성공
- [ ] CloudWatch에 로그 기록 확인
- [ ] 텔레그램 봇 메시지 전송 확인
- [ ] KIS API 연동 확인 (모의투자)
- [ ] GitHub Actions CI/CD 파이프라인 동작 확인
- [ ] 비용 모니터링 대시보드 확인

### 운영 중 정기 점검

- [ ] 주 1회: CloudWatch 로그 이상 유무 확인
- [ ] 주 1회: RDS 백업 정상 수행 확인
- [ ] 월 1회: AWS 비용 리뷰
- [ ] 월 1회: 보안 업데이트 (RDS 엔진, ECS 이미지)
- [ ] 분기 1회: IAM 접근 권한 리뷰
