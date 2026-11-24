"""Enqueue a job on Cloud Tasks (REST + metadata-server token; no SDK)."""
from __future__ import annotations

import httpx

from scraper0.config import get_settings

_META = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def _access_token() -> str:
    r = httpx.get(_META, headers={"Metadata-Flavor": "Google"}, timeout=5)
    r.raise_for_status()
    return r.json()["access_token"]


def enqueue(job_id: str) -> None:
    st = get_settings()
    if not st.cloud_tasks_queue or st.internal_token is None:
        raise RuntimeError("cloud_tasks mode needs SCRAPER0_CLOUD_TASKS_QUEUE and SCRAPER0_INTERNAL_TOKEN")
    import base64
    task = {"task": {
        "dispatchDeadline": "1800s",   # Cloud Tasks max; the request itself may run to the Cloud Run timeout
        "httpRequest": {
            "httpMethod": "POST",
            "url": f"{st.public_base_url.rstrip('/')}/internal/jobs/{job_id}/run",
            "headers": {"Authorization": f"Bearer {st.internal_token.get_secret_value()}",
                        "Content-Type": "application/json"},
            "body": base64.b64encode(b"{}").decode(),
        }}}
    r = httpx.post(f"https://cloudtasks.googleapis.com/v2/{st.cloud_tasks_queue}/tasks", json=task,
                   headers={"Authorization": f"Bearer {_access_token()}"}, timeout=15)
    r.raise_for_status()
