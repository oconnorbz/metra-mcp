"""Helpers shared by the realtime client and the GTFS static loader."""

import logging
import os
import ssl
from datetime import timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo

    CHICAGO_TZ = ZoneInfo("America/Chicago")
except Exception as _e:  # ZoneInfoNotFoundError when tzdata is missing, not ImportError
    # Fixed CST is wrong for half the year, but it beats crashing at import.
    logging.getLogger(__name__).warning(
        "America/Chicago zoneinfo unavailable (%s); falling back to fixed UTC-6. "
        "Install the tzdata package for correct DST handling.",
        _e,
    )
    CHICAGO_TZ = timezone(timedelta(hours=-6), name="CST")


def get_ssl_context() -> ssl.SSLContext | bool:
    """SSL verification config honoring SSL_CERT_FILE (corporate TLS inspection)."""
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file and Path(cert_file).exists():
        return ssl.create_default_context(cafile=cert_file)
    return True
