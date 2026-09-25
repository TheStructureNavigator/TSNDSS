from __future__ import annotations

"""Read observational metadata from a FITS file's primary header, read-only.

Conservative by design: only unambiguous standard keywords are normalized, an absent or
unusable value stays None, and the raw header cards are kept separately as evidence so nothing
is invented. Deliberately not normalized: pointing (RA/DEC/OBJCTRA/CRVAL are ambiguous between
target, mount and plate-solve), EGAIN (electrons per ADU, not the camera gain), SET-TEMP (a
setpoint, not the measured temperature) and any ISO or stacking keywords.
"""

import math
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astropy.io import fits

MAX_HEADER_CARDS = 500
MAX_VALUE_LENGTH = 300
_SKIPPED_KEYWORDS = {"", "COMMENT", "HISTORY", "CONTINUE"}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# IMAGETYP values that clearly name a frame type (matched case-insensitively, exactly).
IMAGETYP_FRAME_TYPES = {
    "light": "light", "light frame": "light",
    "dark": "dark", "dark frame": "dark",
    "flat": "flat", "flat frame": "flat", "flat field": "flat",
    "bias": "bias", "bias frame": "bias", "zero": "bias",
    "dark flat": "dark_flat", "dark flat frame": "dark_flat", "darkflat": "dark_flat",
}


class FitsReadError(Exception):
    """The file is not a readable FITS file. The caller isolates this to the one file."""


@dataclass(slots=True)
class FitsMetadata:
    fields: dict[str, Any] = field(default_factory=dict)   # normalized values (only those found)
    header: dict[str, Any] = field(default_factory=dict)   # raw evidence: JSON-safe primary header cards
    truncated: bool = False
    imagetyp: str | None = None
    warnings: list[str] = field(default_factory=list)


def read_fits_metadata(path: Path) -> FitsMetadata:
    """Open ``path`` read-only and return its primary-header metadata. Never modifies the file."""
    result = FitsMetadata()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            with fits.open(path, mode="readonly", memmap=False, lazy_load_hdus=True) as hdul:
                if len(hdul) == 0:
                    raise FitsReadError("FITS file has no HDUs.")
                header = hdul[0].header
                result.header, result.truncated = _header_evidence(header)
                _normalize(header, result)
        except FitsReadError:
            raise
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            raise FitsReadError(f"{type(error).__name__}: {error}") from error
    for item in caught:
        message = str(item.message)
        if message not in result.warnings and len(result.warnings) < 5:
            result.warnings.append(f"FITS: {message}")
    return result


def _header_evidence(header: Any) -> tuple[dict[str, Any], bool]:
    cards: dict[str, Any] = {}
    truncated = False
    for card in header.cards:
        keyword = str(card.keyword)
        if keyword in _SKIPPED_KEYWORDS:
            continue
        if len(cards) >= MAX_HEADER_CARDS:
            truncated = True
            break
        value = _json_safe(card.value)
        if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
            value = value[:MAX_VALUE_LENGTH]
            truncated = True
        cards[keyword] = value
    return cards, truncated


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if type(value).__name__ == "Undefined":
        return None
    return str(value)


def _normalize(header: Any, result: FitsMetadata) -> None:
    fields = result.fields

    naxis = _to_int(header.get("NAXIS"))
    if naxis is not None and naxis >= 2:
        width, height = _to_int(header.get("NAXIS1")), _to_int(header.get("NAXIS2"))
        if width and width > 0 and height and height > 0:
            fields["width_px"], fields["height_px"] = width, height

    captured_at = _parse_date_obs(header.get("DATE-OBS"), result.warnings)
    if captured_at is not None:
        fields["captured_at"] = captured_at
        fields["captured_at_source"] = "fits_header"

    for keyword in ("EXPTIME", "EXPOSURE"):  # EXPTIME is the standard; EXPOSURE is a fallback
        if keyword in header:
            exposure = _to_float(header.get(keyword))
            if exposure is not None and exposure >= 0:
                fields["exposure_s"] = exposure
                break
            result.warnings.append(f"FITS: {keyword} is not a usable exposure time: {header.get(keyword)!r}")

    for keyword, name in (("GAIN", "gain"), ("OFFSET", "offset_value"), ("CCD-TEMP", "camera_temp_c")):
        if keyword in header:
            value = _to_float(header.get(keyword))
            if value is not None:
                fields[name] = value
            else:
                result.warnings.append(f"FITS: {keyword} is not numeric: {header.get(keyword)!r}")

    for keyword, name in (("XBINNING", "binning_x"), ("YBINNING", "binning_y")):
        if keyword in header:
            value = _to_int(header.get(keyword))
            if value is not None and value > 0:
                fields[name] = value
            else:
                result.warnings.append(f"FITS: {keyword} is not a positive integer: {header.get(keyword)!r}")

    filter_name = _to_text(header.get("FILTER"))
    if filter_name:
        fields["filter_name"] = filter_name
    instrument = _to_text(header.get("INSTRUME"))
    if instrument:
        fields["instrument_name"] = instrument

    result.imagetyp = _to_text(header.get("IMAGETYP"))


def _parse_date_obs(value: Any, warn: list[str]) -> str | None:
    """DATE-OBS as a UTC ISO-8601 string. FITS defines it as UTC when no zone is given."""
    text = _to_text(value)
    if not text:
        return None
    if _DATE_ONLY.match(text):
        warn.append(f"FITS: DATE-OBS has a date but no time, so it is not an acquisition time: {text!r}")
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        warn.append(f"FITS: DATE-OBS is not an ISO-8601 timestamp: {text!r}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    precision = "milliseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=precision).replace("+00:00", "Z")


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def _to_text(value: Any) -> str | None:
    if value is None or type(value).__name__ == "Undefined":
        return None
    text = str(value).strip()
    return text or None
