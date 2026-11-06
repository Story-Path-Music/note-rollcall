"""Time helpers. Tests monkeypatch `now` to simulate day boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
PACIFIC = ZoneInfo("America/Los_Angeles")  # GCP billing months follow Pacific time


def now() -> datetime:
    return datetime.now(timezone.utc)


def billing_month() -> str:
    return now().astimezone(PACIFIC).strftime("%Y-%m")


def today_taipei() -> str:
    return now().astimezone(TAIPEI).strftime("%Y-%m-%d")
