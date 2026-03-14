"""Agent prompt templates for Korean stock trading analysis.

Each prompt module exports:
- SYSTEM_PROMPT: str — 시스템 메시지 (역할 + 규칙 + 출력 포맷)
- build_user_prompt(data: dict) -> str — 데이터 → 유저 메시지
"""
