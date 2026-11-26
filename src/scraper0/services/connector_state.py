"""Encrypted per-(tenant, connector) key/value state (implements connectors.base.StateStore)."""
from __future__ import annotations

from scraper0.core.crypto import decrypt, encrypt
from scraper0.db.models import ConnectorState, now
from scraper0.db.session import session_scope


class DBStateStore:
    def __init__(self, tenant_id: str, connector: str):
        self.tenant_id, self.connector = tenant_id, connector

    def get(self, key: str) -> str | None:
        with session_scope() as s:
            row = s.get(ConnectorState, (self.tenant_id, self.connector, key))
            return decrypt(row.value_enc) if row else None

    def set(self, key: str, value: str) -> None:
        with session_scope() as s:
            row = s.get(ConnectorState, (self.tenant_id, self.connector, key))
            if row is None:
                s.add(ConnectorState(tenant_id=self.tenant_id, connector=self.connector, key=key, value_enc=encrypt(value)))
            else:
                row.value_enc, row.updated_at = encrypt(value), now()

    def delete(self, key: str) -> None:
        with session_scope() as s:
            row = s.get(ConnectorState, (self.tenant_id, self.connector, key))
            if row:
                s.delete(row)
