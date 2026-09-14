from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from threading import Lock

from app.config import get_settings


@dataclass
class CredentialLease:
    account: str
    password_bytes: bytearray
    expires_at: datetime

    def reveal_password(self) -> str:
        return self.password_bytes.decode("utf-8")

    def clear(self) -> None:
        for index in range(len(self.password_bytes)):
            self.password_bytes[index] = 0


class CredentialVault:
    def __init__(self) -> None:
        self._leases: dict[str, CredentialLease] = {}
        self._lock = Lock()

    def create(self, account: str, password: str) -> tuple[str, CredentialLease]:
        self.cleanup()
        token = token_urlsafe(32)
        lease = CredentialLease(
            account=account,
            password_bytes=bytearray(password.encode("utf-8")),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=get_settings().credential_ttl_minutes),
        )
        with self._lock:
            self._leases[token] = lease
        return token, lease

    def get(self, token: str | None) -> CredentialLease | None:
        if not token:
            return None
        self.cleanup()
        with self._lock:
            return self._leases.get(token)

    def release(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            lease = self._leases.pop(token, None)
        if lease:
            lease.clear()

    def cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        expired: list[CredentialLease] = []
        with self._lock:
            tokens = [token for token, lease in self._leases.items() if lease.expires_at <= now]
            for token in tokens:
                expired.append(self._leases.pop(token))
        for lease in expired:
            lease.clear()


def mask_account(account: str) -> str:
    if "@" in account:
        name, domain = account.split("@", 1)
        return f"{name[:2]}***@{domain}"
    if len(account) > 5:
        return f"{account[:3]}****{account[-2:]}"
    return f"{account[:1]}***"


vault = CredentialVault()
