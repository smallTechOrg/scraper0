"""Entity specs per tenant: built-ins (shared) + tenant-defined (private)."""
from __future__ import annotations

from sqlalchemy import select

from scraper0.core.entities import BUILTIN_SPECS, EntitySpec
from scraper0.db.models import EntitySpecRow, now
from scraper0.db.session import session_scope


class UnknownEntity(ValueError):
    pass


def resolve(tenant_id: str, entity) -> EntitySpec:
    """entity = built-in name | this tenant's stored name | inline spec dict (ad-hoc, not stored)."""
    if isinstance(entity, dict):
        return EntitySpec.from_dict({**entity, "name": entity.get("name") or "adhoc"}, tenant_id=tenant_id)
    if not isinstance(entity, str) or not entity:
        raise UnknownEntity("entity must be a name or a spec object")
    if entity in BUILTIN_SPECS:
        return BUILTIN_SPECS[entity]
    with session_scope() as s:
        row = s.get(EntitySpecRow, (tenant_id, entity))
        if row is None:
            raise UnknownEntity(f"unknown entity {entity!r}")
        return EntitySpec.from_dict({**row.spec, "version": row.version}, tenant_id=tenant_id)


def list_specs(tenant_id: str) -> list[dict]:
    out = [s.to_dict() for s in BUILTIN_SPECS.values()]
    with session_scope() as s:
        for row in s.scalars(select(EntitySpecRow).where(EntitySpecRow.tenant_id == tenant_id).order_by(EntitySpecRow.name)):
            out.append(EntitySpec.from_dict({**row.spec, "version": row.version}, tenant_id=tenant_id).to_dict())
    return out


def put_spec(tenant_id: str, name: str, body: dict) -> dict:
    if name in BUILTIN_SPECS:
        raise ValueError("built-in entities cannot be replaced")
    with session_scope() as s:
        row = s.get(EntitySpecRow, (tenant_id, name))
        version = (row.version + 1) if row else 1
        spec = EntitySpec.from_dict({"name": name, "description": body.get("description", ""),
                                     "fields": body.get("fields") or [], "version": version}, tenant_id=tenant_id)
        data = spec.to_dict()
        data.pop("builtin", None)
        if row is None:
            s.add(EntitySpecRow(tenant_id=tenant_id, name=name, spec=data, version=version))
        else:
            row.spec, row.version, row.updated_at = data, version, now()
        return spec.to_dict()


def delete_spec(tenant_id: str, name: str) -> bool:
    with session_scope() as s:
        row = s.get(EntitySpecRow, (tenant_id, name))
        if row is None:
            return False
        s.delete(row)
        return True
