# Subagent & Skill 설정 정리

**작성일**: 2026-02-08
**목적**: Claude Code의 subagent와 skill을 정의하여 개발 생산성 향상

---

## 프로젝트 개요

- Python 기반 주식 자동매매 에이전트
- 한국투자증권(KIS) REST API 연동
- 여러 LLM 혼합 사용
- 실시간 자동매매, 데이터 수집, 기술적 분석, 포트폴리오 리밸런싱
- AWS 배포 (상시 운영), 텔레그램 알림 연동

---

## Subagents (8개)

| 에이전트 | 모델 | 역할 | 비고 |
|---------|------|------|------|
| `code-reviewer` | sonnet | 코드 리뷰, 보안 분석 | 읽기 전용 |
| `trading-strategist` | opus | 매매 전략 검증, 리스크 분석 | 프로젝트 메모리 |
| `data-api-specialist` | sonnet | KIS API, 데이터 파이프라인 | 프로젝트 메모리 |
| `debugger` | sonnet | 버그 추적, 테스트, 프로파일링 | |
| `infra-devops` | sonnet | AWS 인프라, CI/CD, 모니터링 | 편집 허용 |
| `backend-engineer` | sonnet | DB, 캐시, API, 아키텍처 | 프로젝트 메모리 |
| `product-strategist` | opus | 기능 기획, 우선순위, 로드맵 | 읽기 전용 |
| `market-analyst` | opus | 시장 동향, 업종 분석, 리포트 | 프로젝트 메모리 |

### 텔레그램 알림 담당
별도 에이전트 없이 기존 에이전트 협업:
- 구현: `backend-engineer`
- 메시지 설계: `product-strategist`
- 분석 리포트: `market-analyst`

---

## Skills (5개)

| 스킬 | 설명 |
|------|------|
| `/deploy` | AWS 배포 워크플로우 (staging/production) |
| `/setup-env` | 개발 환경 초기화 |
| `/db-migrate` | DB 마이그레이션 생성/적용/롤백 |
| `/health-check` | 서비스 상태 점검 |
| `/review-pr` | 트레이딩 도메인 PR 리뷰 |

---

## 파일 구조

```
.claude/
├── agents/
│   ├── code-reviewer.md
│   ├── trading-strategist.md
│   ├── data-api-specialist.md
│   ├── debugger.md
│   ├── infra-devops.md
│   ├── backend-engineer.md
│   ├── product-strategist.md
│   └── market-analyst.md
├── skills/
│   ├── deploy/SKILL.md
│   ├── setup-env/SKILL.md
│   ├── db-migrate/SKILL.md
│   ├── health-check/SKILL.md
│   └── review-pr/SKILL.md
└── settings.json
```
