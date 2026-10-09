"""Helpers shared by the realtime client and the GTFS static loader."""

import logging
import os
import re
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


# Legacy second segment: route letters + train number, e.g. "BN1200" in
# BNSF_BN1200_V4_A or "UNRAV1" (a Ravinia special) in UP-N_UNRAV1_V1_A.
_LEGACY_TRAIN_SEG = re.compile(r"[A-Z]+?(\d+)")


def train_number(trip_id: str | None) -> str | None:
    """Public train number derived from a Metra trip_id, or None.

    Handles both trip_id formats so callers survive Metra's GTFS cutover
    (Developer Change Notice, 10/1/2026):
      legacy  BNSF_BN1200_V4_A          -> "1200"
      future  BNSF_1200_8_2009_5682524  -> "1200"
    In both, the train number is the second underscore-separated segment;
    the legacy form prefixes it with route letters. Returned as a string so
    leading zeros survive. trip_id itself stays the join key — this is for
    display only.
    """
    parts = (trip_id or "").split("_")
    if len(parts) < 2:
        return None
    seg = parts[1]
    if seg.isdigit():
        return seg
    m = _LEGACY_TRAIN_SEG.fullmatch(seg)
    return m.group(1) if m else None
