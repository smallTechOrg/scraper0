"""Action plans shipped with scraper0, as data: site_plans/<site_id>/<action>.json (seeded as draft versions by
services/sites.seed_plan_actions; an admin locks them)."""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parent


def load(site_id: str, action: str) -> dict:
    return json.loads((_DIR / site_id / f"{action}.json").read_text())
