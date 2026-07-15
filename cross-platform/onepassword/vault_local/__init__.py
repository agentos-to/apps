"""Local 1Password B5 vault — AgentOS-owned decrypt (no `op` / Integrate).

Unlock material (master password + secret key) lives in the per-user
AgentOS credential store under domain ``op.local``. Crypto primitives
dispatch to Rust ``crypto.*`` ops. DB reads go through ``sql.query``.
"""

from __future__ import annotations

from .unlock import (
    UnlockSession,
    load_unlock_secrets,
    setup_unlock_secret,
    unlock,
)
from .items import (
    CATEGORIES,
    find_item,
    get_item_details,
    list_overviews,
)

__all__ = [
    "CATEGORIES",
    "UnlockSession",
    "find_item",
    "get_item_details",
    "list_overviews",
    "load_unlock_secrets",
    "setup_unlock_secret",
    "unlock",
]
