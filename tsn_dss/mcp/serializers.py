from __future__ import annotations

from typing import Any

from ..domain.models import CatalogObject, CatalogObjectAlias, LocalHorizonPoint, Site, Target
from ..engine.catalog_service import CatalogObjectWithAliases, CatalogResolutionWithAliases


def local_horizon_point_to_dict(point: LocalHorizonPoint) -> dict[str, float]:
    return {
        "azimuth_deg": point.azimuth_deg,
        "min_altitude_deg": point.min_altitude_deg,
    }


def site_summary_to_dict(site: Site) -> dict[str, Any]:
    return {
        "id": site.id,
        "name": site.name,
        "latitude_deg": site.latitude_deg,
        "longitude_deg": site.longitude_deg,
        "elevation_m": site.elevation_m,
        "bortle_class": site.bortle_class,
        "sqm_mag_arcsec2": site.sqm_mag_arcsec2,
        "has_horizon_profile": bool(site.horizon_profile),
    }


def site_detail_to_dict(site: Site) -> dict[str, Any]:
    data = site_summary_to_dict(site)
    data.update(
        {
            "lp_artificial_brightness_mcd_m2": site.lp_artificial_brightness_mcd_m2,
            "lp_natural_sky_ratio": site.lp_natural_sky_ratio,
            "lp_estimated_total_brightness_mcd_m2": site.lp_estimated_total_brightness_mcd_m2,
            "lp_estimated_sqm_mag_arcsec2": site.lp_estimated_sqm_mag_arcsec2,
            "lp_estimated_bortle_class": site.lp_estimated_bortle_class,
            "lp_dataset_name": site.lp_dataset_name,
            "lp_provider_name": site.lp_provider_name,
            "lp_source": site.lp_source,
            "lp_source_unit": site.lp_source_unit,
            "lp_data_kind": site.lp_data_kind,
            "lp_updated_at": site.lp_updated_at,
            "south_horizon_open": site.south_horizon_open,
            "notes": site.notes,
            "horizon_profile": [local_horizon_point_to_dict(point) for point in site.horizon_profile],
        }
    )
    return data


def target_to_dict(target: Target) -> dict[str, Any]:
    return {
        "id": target.id,
        "catalog": target.catalog,
        "catalog_id": target.catalog_id,
        "name": target.name,
        "ra_deg": target.ra_deg,
        "dec_deg": target.dec_deg,
        "object_type": target.object_type,
        "angular_major_arcmin": target.angular_major_arcmin,
        "angular_minor_arcmin": target.angular_minor_arcmin,
        "distance_ly": target.distance_ly,
        "constellation": target.constellation,
        "notes": target.notes,
    }


def catalog_object_to_dict(catalog_object: CatalogObject) -> dict[str, Any]:
    return {
        "catalog_object_id": catalog_object.id,
        "canonical_designation": catalog_object.canonical_designation,
        "display_name": catalog_object.display_name,
        "object_type": catalog_object.object_type,
        "ra_deg": catalog_object.ra_deg,
        "dec_deg": catalog_object.dec_deg,
        "angular_major_arcmin": catalog_object.angular_major_arcmin,
        "angular_minor_arcmin": catalog_object.angular_minor_arcmin,
        "magnitude": catalog_object.magnitude,
        "source_provider": catalog_object.source_provider,
        "source_version": catalog_object.source_version,
    }


def catalog_alias_to_dict(alias: CatalogObjectAlias) -> dict[str, str]:
    return {
        "alias": alias.alias,
        "normalized_alias": alias.normalized_alias,
        "alias_kind": alias.alias_kind,
    }


def catalog_object_with_aliases_to_dict(item: CatalogObjectWithAliases) -> dict[str, Any]:
    data = catalog_object_to_dict(item.catalog_object)
    data["aliases"] = [catalog_alias_to_dict(alias) for alias in item.aliases]
    return data


def catalog_resolution_to_dict(result: CatalogResolutionWithAliases) -> dict[str, Any]:
    return {
        "status": result.status,
        "catalog_object": (
            catalog_object_with_aliases_to_dict(result.catalog_object)
            if result.catalog_object is not None
            else None
        ),
        "candidates": [catalog_object_with_aliases_to_dict(candidate) for candidate in result.candidates],
    }
