"""
Calendar arithmetic and local-zone conversion -- SPEC 2.2, D1.

Everything here is integer arithmetic on NumPy arrays. `datetime` appears in
exactly one place -- resolving a local UTC offset, which genuinely needs the
platform's zone database -- and even there it is called once per *distinct day*
in a chunk, never once per entry.

D1: the merge core carries naive local `wall_us` and nothing else. A timestamp
that arrives with a zone is converted to local wall time at parse time and the
zone is then discarded. Within a DST transition hour local wall time is
ambiguous and ordering is explicitly undefined.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

US_PER_SEC = 1_000_000
US_PER_DAY = 86_400 * US_PER_SEC

# Plausibility window, SPEC 4.3 step 4. A match outside this is a false
# positive -- an order number, a version string, a byte count -- not a date.
MIN_PLAUSIBLE_YEAR = 1990
MAX_PLAUSIBLE_YEAR = 2100


def days_from_civil(y: np.ndarray, m: np.ndarray, d: np.ndarray) -> np.ndarray:
    """
    Days since 1970-01-01 from a proleptic Gregorian date, vectorised.

    Howard Hinnant's `days_from_civil`, which is branch-free and exact over the
    whole int64 range -- no table, no leap-year special case, no `datetime`.
    """
    y = y.astype(np.int64, copy=True)
    m64 = m.astype(np.int64)
    d64 = d.astype(np.int64)

    y -= m64 <= 2
    era = np.where(y >= 0, y, y - 399) // 400
    yoe = y - era * 400
    mp = np.where(m64 > 2, m64 - 3, m64 + 9)
    doy = (153 * mp + 2) // 5 + d64 - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def days_from_civil_scalar(y: int, m: int, d: int) -> int:
    """Scalar twin, for the constant-date fast path (SPEC 4.4)."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = m - 3 if m > 2 else m + 9
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def civil_from_days(z: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Inverse of `days_from_civil`. Used for display and for yearless rollover."""
    z = z.astype(np.int64, copy=True) + 719_468
    era = np.where(z >= 0, z, z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36_524 - doe // 146_096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = np.where(mp < 10, mp + 3, mp - 9)
    y = y + (m <= 2)
    return y, m, d


def plausible_date(y: np.ndarray, m: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Field-range validity. Does not check day-of-month against month length."""
    return ((y >= MIN_PLAUSIBLE_YEAR) & (y <= MAX_PLAUSIBLE_YEAR)
            & (m >= 1) & (m <= 12) & (d >= 1) & (d <= 31))


def plausible_time(h: np.ndarray, mi: np.ndarray, s: np.ndarray) -> np.ndarray:
    """Second may be 60: a leap second is a real thing that appears in logs."""
    return (h <= 23) & (mi <= 59) & (s <= 60)


PLAUSIBLE_MIN_US = (days_from_civil_scalar(MIN_PLAUSIBLE_YEAR, 1, 1)) * US_PER_DAY
PLAUSIBLE_MAX_US = (days_from_civil_scalar(MAX_PLAUSIBLE_YEAR, 1, 1)) * US_PER_DAY


def plausible_wall_us(ts: np.ndarray) -> np.ndarray:
    """Range check for epoch formats, where there are no fields to validate."""
    return (ts >= PLAUSIBLE_MIN_US) & (ts < PLAUSIBLE_MAX_US)


# ---------------------------------------------------------------------------
# local zone
# ---------------------------------------------------------------------------

def local_offset_us_at(epoch_us: int) -> int:
    """
    The local UTC offset, in microseconds, at a given instant.

    The one place a platform zone lookup happens. Positive east of Greenwich.
    """
    secs = epoch_us / US_PER_SEC
    naive_local = datetime.fromtimestamp(secs)
    naive_utc = datetime.fromtimestamp(secs, UTC).replace(tzinfo=None)
    return round((naive_local - naive_utc).total_seconds() * US_PER_SEC)


def utc_to_local_wall_us(utc_us: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """
    Convert UTC instants to naive local wall `wall_us`, per D1.

    The offset is resolved once per distinct UTC *day* present, not once per
    entry: a 64 MB chunk of a busy log typically spans one or two days, so this
    is one or two `datetime` calls for a million rows. Within the hour of a DST
    transition the result is whichever side of the transition the day lookup
    lands on -- D1 already declares ordering there undefined, and the error is
    bounded by the transition size.
    """
    utc_us = np.asarray(utc_us, dtype=np.int64)
    if utc_us.size == 0:
        return utc_us.copy()

    day = np.floor_divide(utc_us, US_PER_DAY)
    if valid is not None and valid.any():
        uniq = np.unique(day[valid])
    elif valid is not None:
        return utc_us.copy()
    else:
        uniq = np.unique(day)

    # Sample at noon of each day, well away from any transition.
    offsets = np.array(
        [local_offset_us_at(int(d) * US_PER_DAY + 12 * 3600 * US_PER_SEC) for d in uniq],
        dtype=np.int64,
    )
    idx = np.searchsorted(uniq, day)
    np.clip(idx, 0, uniq.size - 1, out=idx)
    return utc_us + offsets[idx]


def epoch_us_to_wall_us(epoch_us: int) -> int:
    """
    Convert an absolute instant to naive local `wall_us` (D1).

    The conversion every caller at the system boundary needs and none should
    write by hand. `st_mtime` is a UTC epoch; everything inside the merge core
    is naive local wall time. Passing one where the other is expected shifts a
    file by the local UTC offset -- which is invisible most of the time and
    mis-years a yearless log at a New Year boundary, so the two are deliberately
    given different names throughout.
    """
    return int(utc_to_local_wall_us(np.array([epoch_us], dtype=np.int64))[0])


def wall_us_to_datetime(wall: int) -> datetime:
    """For display and for tests. Naive, no zone attached (D1)."""
    days, rem = divmod(int(wall), US_PER_DAY)
    y, m, d = civil_from_days(np.array([days], dtype=np.int64))
    secs, us = divmod(rem, US_PER_SEC)
    h, rem2 = divmod(secs, 3600)
    mi, s = divmod(rem2, 60)
    return datetime(int(y[0]), int(m[0]), int(d[0]), int(h), int(mi), int(s), int(us))
