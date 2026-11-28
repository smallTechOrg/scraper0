from datetime import datetime, timezone

from scraper0.core.signed_urls import expires_at, expires_at_iso


def at(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_airtable_expiry_in_path():
    u = "https://v5.airtableusercontent.com/v3/u/59/59/1795716000000/QTa_52OvmdbN46EOC9mPyw/GoBqF_X"
    assert expires_at(u) == at(2026, 11, 26, 18, 0)
    assert expires_at_iso(u) == "2026-11-26T18:00:00.000Z"


def test_s3_and_gcs_v4():
    s3 = ("https://b.s3.amazonaws.com/x.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20261128T050000Z"
          "&X-Amz-Expires=3600&X-Amz-Signature=abc")
    gcs = ("https://storage.googleapis.com/b/x.jpg?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Date=20261128T050000Z"
           "&X-Goog-Expires=900&X-Goog-Signature=abc")
    assert expires_at(s3) == at(2026, 11, 28, 6, 0)
    assert expires_at(gcs) == at(2026, 11, 28, 5, 15)


def test_epoch_expires_with_signature_azure_and_ms():
    assert expires_at("https://d1.cloudfront.net/a.jpg?Expires=1795708800&Signature=x&Key-Pair-Id=K") == at(2026, 11, 26, 16, 0)
    assert expires_at("https://b.s3.amazonaws.com/a.jpg?AWSAccessKeyId=A&Expires=1795708800&Signature=x") == at(2026, 11, 26, 16, 0)
    assert expires_at("https://a.blob.core.windows.net/c/a.jpg?sv=2022&se=2026-11-28T10:00:00Z&sig=x") == at(2026, 11, 28, 10)


def test_plain_and_odd_links_never_expire():
    for u in (None, "", "not a url", "https://cdn.x/1.jpg", "https://cdn.x/1.jpg?expires=1795708800",   # no signature
              "https://cdn.x/1.jpg?Expires=abc&Signature=x", "https://cdn.x/v/1700000000000.jpg"):          # not airtable
        assert expires_at(u) is None, u
