import re

_WS = re.compile(r"\s+")


def clean(text: str | None) -> str | None:
    """Collapse whitespace, drop zero-width spaces; '' → None."""
    if text is None:
        return None
    text = _WS.sub(" ", str(text).replace("​", "")).strip()
    return text or None
