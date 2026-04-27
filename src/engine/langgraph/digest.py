"""State digest helpers for langgraph interrupt/resume."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def compute_state_digest(state: Mapping[str, Any], whitelist: tuple[str, ...]) -> str:
    """Compute a stable digest from the declared state fields only."""
    canonical = {
        key: state[key]
        for key in sorted(whitelist)
        if key in state
    }
    payload = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
