"""CRSF 11-bit channel values -> RC PWM microseconds.

Anchors: 172 -> 988, 992 -> 1500, 1811 -> 2012.
"""

from __future__ import annotations

CRSF_CENTER = 992
US_CENTER = 1500
DEFAULT_US_MIN = 988
DEFAULT_US_MAX = 2012


def crsf_to_us(value: int) -> int:
    return int(round((value - CRSF_CENTER) * 5 / 8 + US_CENTER))


def scale_channels(channels, us_min: int = DEFAULT_US_MIN,
                   us_max: int = DEFAULT_US_MAX) -> list:
    return [max(us_min, min(us_max, crsf_to_us(v))) for v in channels]
