"""Sky geometry for a pointing check, with astropy imported only when a function is called (DB-05b v2).

Plain numbers in, plain numbers out: it knows nothing about any device. Frames: ``ra_hours``/``dec_deg`` given to the altitude and Sun
functions are taken as FK5 coordinates with the equinox of ``when`` (of-date, "JNow"). ``j2000_to_of_date`` is the one explicit conversion
from ICRS/J2000 to that frame.
"""

from __future__ import annotations

from datetime import datetime

__all__ = ["j2000_to_of_date", "target_altitude_and_sun_separation"]


def _require_aware(when: datetime) -> None:
    if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("when must be a timezone-aware datetime.")


def target_altitude_and_sun_separation(
    ra_hours: float, dec_deg: float, latitude_deg: float, longitude_deg: float, elevation_m: float | None, when: datetime
) -> tuple[float, float]:
    """(topocentric altitude of the point in degrees, its angular separation from the Sun in degrees) at ``when``."""
    _require_aware(when)
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, FK5, GCRS, SkyCoord, get_body
    from astropy.time import Time

    moment = Time(when)
    location = EarthLocation.from_geodetic(lon=float(longitude_deg) * u.deg, lat=float(latitude_deg) * u.deg,
                                           height=float(elevation_m or 0.0) * u.m)
    of_date = FK5(equinox=moment)
    point = SkyCoord(ra=float(ra_hours) * u.hourangle, dec=float(dec_deg) * u.deg, frame=of_date)
    altitude = point.transform_to(AltAz(obstime=moment, location=location)).alt.deg
    sun_here = get_body("sun", moment, location)
    # Keep only the direction (no distance): a transform that keeps the 1 AU distance would move the Sun to the wrong place.
    sun = SkyCoord(ra=sun_here.ra, dec=sun_here.dec, frame=GCRS(obstime=moment)).transform_to(of_date)
    return float(altitude), float(point.separation(sun).deg)


def j2000_to_of_date(ra_hours: float, dec_deg: float, when: datetime) -> tuple[float, float]:
    """ICRS/J2000 -> FK5 of-date at ``when``; returns (RA hours in [0, 24), Dec degrees)."""
    _require_aware(when)
    from astropy import units as u
    from astropy.coordinates import FK5, ICRS, SkyCoord
    from astropy.time import Time

    converted = SkyCoord(ra=float(ra_hours) * u.hourangle, dec=float(dec_deg) * u.deg, frame=ICRS()).transform_to(FK5(equinox=Time(when)))
    return float(converted.ra.hour % 24.0), float(converted.dec.deg)
