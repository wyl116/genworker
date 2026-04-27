"""
TempAccessManager - temporary external link access with strict/permissive modes.

strict: requires user authorization (granted_by != worker itself)
permissive: allowed_domains whitelist can be auto-authorized
"""
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.worker.data_access.models import ExternalAccessConfig, TempAccessRecord


_DURATION_SUFFIXES = {"h": "hours", "m": "minutes", "d": "days", "s": "seconds"}


class TempAccessDeniedError(Exception):
    """Raised when temporary access is denied by policy."""


class TempAccessExpiredError(Exception):
    """Raised when a temp access record has expired."""


class TempAccessManager:
    """Temporary link manager: strict needs user auth, permissive allows whitelisted domains."""

    def __init__(self, scratch_dir: Path, config: ExternalAccessConfig) -> None:
        self._scratch_dir = scratch_dir
        self._config = config
        self._records: dict[str, TempAccessRecord] = {}

    @property
    def records(self) -> dict[str, TempAccessRecord]:
        return dict(self._records)

    def grant_access(
        self,
        source_url: str,
        granted_by: str,
        worker_id: str,
        expires_in: str | None = None,
    ) -> TempAccessRecord:
        """Create a temp access record respecting mode policy."""
        self._validate_grant(source_url, granted_by, worker_id)
        expires_at = _compute_expiry(expires_in or self._config.auto_expire)
        access_id = uuid.uuid4().hex[:16]
        record = TempAccessRecord(
            access_id=access_id,
            source_url=source_url,
            granted_by=granted_by,
            granted_to=worker_id,
            expires_at=expires_at.isoformat(),
        )
        self._records = {**self._records, access_id: record}
        return record

    async def fetch_to_local(self, access_id: str) -> Path:
        """Check expiry, download to scratch/, update record with local_path."""
        record = self._records.get(access_id)
        if record is None:
            raise KeyError(f"Access record not found: {access_id}")
        if _is_expired(record.expires_at):
            raise TempAccessExpiredError(f"Access record expired: {access_id}")
        local_path = self._scratch_dir / f"temp_{access_id}"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        # Simulate download - in production this would be an HTTP call
        local_path.write_bytes(b"")
        updated = replace(record, local_path=str(local_path))
        self._records = {**self._records, access_id: updated}
        return local_path

    async def cleanup_expired(self) -> int:
        """Remove expired records and their local files. Returns count removed."""
        expired_ids = [
            aid for aid, rec in self._records.items()
            if _is_expired(rec.expires_at)
        ]
        for aid in expired_ids:
            rec = self._records[aid]
            if rec.local_path:
                path = Path(rec.local_path)
                if path.exists():
                    path.unlink()
        remaining = {
            aid: rec for aid, rec in self._records.items()
            if aid not in expired_ids
        }
        self._records = remaining
        return len(expired_ids)

    def _validate_grant(
        self, source_url: str, granted_by: str, worker_id: str,
    ) -> None:
        """Validate access grant based on mode."""
        if self._config.mode == "strict":
            if granted_by == worker_id:
                raise TempAccessDeniedError(
                    "strict mode: worker cannot self-authorize external access"
                )
        elif self._config.mode == "permissive":
            domain = urlparse(source_url).hostname or ""
            if granted_by == worker_id and not _domain_matches(
                domain, self._config.allowed_domains,
            ):
                raise TempAccessDeniedError(
                    f"permissive mode: domain '{domain}' not in allowed list"
                )


def _compute_expiry(duration_str: str) -> datetime:
    """Parse duration like '24h', '30m', '7d' to absolute datetime."""
    if not duration_str:
        raise ValueError("Empty duration string")
    suffix = duration_str[-1].lower()
    if suffix not in _DURATION_SUFFIXES:
        raise ValueError(f"Unknown duration suffix: {suffix}")
    amount = int(duration_str[:-1])
    kwarg = _DURATION_SUFFIXES[suffix]
    return datetime.now(timezone.utc) + timedelta(**{kwarg: amount})


def _is_expired(expires_at_iso: str) -> bool:
    """Check if an ISO timestamp is in the past."""
    expires = datetime.fromisoformat(expires_at_iso)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > expires


def _domain_matches(domain: str, allowed: tuple[str, ...]) -> bool:
    """Check if domain matches any entry in allowed list (exact or suffix)."""
    for pattern in allowed:
        if domain == pattern or domain.endswith(f".{pattern}"):
            return True
    return False
