"""Account credentials dataclass for multi-account broker management.

Separated from registry.py to avoid circular imports:
registry.py → kis/client.py → credentials.py (no cycle)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountCredentials:
    """복호화된 단일 계좌 인증정보.

    Args:
        account_id: 계좌 식별자 (e.g. "default", "acct-1").
        app_key: KIS API app key (평문).
        app_secret: KIS API app secret (평문).
        account_no: KIS 계좌번호 (e.g. "50123456-01").
        account_prod: 계좌 상품코드 (default "01").
        is_paper: 모의투자 여부 (default True).
        hts_id: HTS ID.
    """

    account_id: str
    app_key: str
    app_secret: str
    account_no: str
    account_prod: str = "01"
    is_paper: bool = True
    hts_id: str = ""
