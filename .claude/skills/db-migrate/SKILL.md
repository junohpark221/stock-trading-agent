---
name: db-migrate
description: 데이터베이스 마이그레이션 생성 및 적용
allowed-tools:
  - Bash
  - Read
---

# DB 마이그레이션

## 사용법
```
/db-migrate [generate|apply|rollback|status]
```

기본값: `status` (현재 마이그레이션 상태 확인)

## 명령별 동작

### `status` - 마이그레이션 상태 확인
```bash
alembic current
alembic history --verbose -r -5:
```
- 현재 적용된 리비전 표시
- 최근 5개 마이그레이션 히스토리 표시
- 미적용 마이그레이션 목록 표시

### `generate` - 새 마이그레이션 생성
1. SQLAlchemy 모델 변경사항 확인
```bash
alembic check
```
2. 자동 마이그레이션 스크립트 생성
```bash
alembic revision --autogenerate -m "설명"
```
3. 생성된 마이그레이션 파일 리뷰
   - `upgrade()` / `downgrade()` 함수 확인
   - 데이터 손실 가능성 확인 (컬럼 삭제, 타입 변경 등)
   - 인덱스 생성/삭제 확인

### `apply` - 마이그레이션 적용
1. 현재 상태 확인
2. 적용할 마이그레이션 목록 표시
3. 사용자 확인 후 적용
```bash
alembic upgrade head
```
4. 적용 결과 검증

### `rollback` - 마이그레이션 롤백
1. 현재 상태 및 이전 리비전 확인
2. 롤백 영향 범위 설명
3. 사용자 확인 후 롤백
```bash
alembic downgrade -1
```
4. 롤백 결과 검증

## 주의사항
- Production DB 마이그레이션은 반드시 백업 후 진행
- 장 운영 시간에는 대규모 스키마 변경 자제
- 데이터 마이그레이션이 포함된 경우 별도 검증 필요
- 롤백 불가능한 변경(데이터 삭제)은 별도 경고
