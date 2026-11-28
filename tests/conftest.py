"""Every test gets a fresh in-memory database and test settings. No network, no real AI."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("SCRAPER0_ENV", "test")
os.environ["SCRAPER0_DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SCRAPER0_SECRET_KEY", Fernet.generate_key().decode())
os.environ["SCRAPER0_JOB_MODE"] = "inline"
os.environ["SCRAPER0_AUTO_MIGRATE"] = "false"
os.environ["SCRAPER0_BOOTSTRAP_LEGACY_TENANT"] = ""
os.environ["SCRAPER0_MIN_HOST_INTERVAL_S"] = "0"
os.environ["SCRAPER0_RESPECT_ROBOTS"] = "false"
os.environ["SCRAPER0_RENDER_JS"] = "never"
os.environ.pop("GROQ_API_KEY", None)

import pytest  # noqa: E402

from scraper0.db import session as db_session  # noqa: E402
from scraper0.db.models import Base  # noqa: E402


PG_URL = os.getenv("SCRAPER0_TEST_POSTGRES_URL")   # e.g. postgresql+psycopg://scraper0:pw@127.0.0.1:55432/scraper0


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    if PG_URL:   # same pool cap as production (2 + 1 overflow)
        eng = db_session.make_engine(PG_URL, pool_size=2, max_overflow=1)
        Base.metadata.drop_all(eng)
    else:        # file-backed (not :memory:) so crawler worker threads get their own connections, like production
        eng = db_session.make_engine(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(eng)
    db_session.set_engine(eng)
    yield eng
    eng.dispose()


def request_via_get(self, method, url, headers=None, data=None):
    """Fetcher.request() for test fakes that only implement get(url) → (text, content_type) | None."""
    from scraper0.core.fetch import HTTPResult
    if method.upper() != "GET":
        return HTTPResult(405, "", "text/plain")
    got = self.get(url)
    if got is None:
        return HTTPResult(404, "not found", "text/plain")
    return HTTPResult(200, got[0], got[1] or "text/html")


def no_cookie(self, name):
    return None


def json_calls(pairs):
    """[(url, body)] → RenderResult.requests for fake browsers: GET calls that returned JSON."""
    from scraper0.core.render import CapturedRequest
    return [CapturedRequest("GET", u, {"accept": "application/json"}, None, 200, "application/json", b) for u, b in pairs]
