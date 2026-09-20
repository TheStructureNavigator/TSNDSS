import type { LightPollutionPointSnapshot } from './api';
import type { LightPollutionPointResult } from './light_pollution';

export type SiteLightPollutionPayload = {
  lp_artificial_brightness_mcd_m2: number | null;
  lp_natural_sky_ratio: number | null;
  lp_estimated_total_brightness_mcd_m2: number | null;
  lp_estimated_sqm_mag_arcsec2: number | null;
  lp_estimated_bortle_class: number | null;
  lp_dataset_name: string | null;
  lp_provider_name: string | null;
  lp_source: string | null;
  lp_source_unit: string | null;
  lp_data_kind: string | null;
  lp_updated_at: string | null;
};

export function buildCandidateSiteDefaultName(latitudeDeg: number, longitudeDeg: number): string {
  return `Site ${latitudeDeg.toFixed(5)}, ${longitudeDeg.toFixed(5)}`;
}

export function siteLightPollutionPayloadFromCandidate(
  result: LightPollutionPointResult,
  updatedAt: string,
): SiteLightPollutionPayload | null {
  if (result.status !== 'available' || result.measurement === null) {
    return null;
  }

  return {
    lp_artificial_brightness_mcd_m2: result.measurement.artificialBrightnessMcdM2,
    lp_natural_sky_ratio: result.measurement.naturalSkyRatio,
    lp_estimated_total_brightness_mcd_m2: result.measurement.estimatedTotalBrightnessMcdM2,
    lp_estimated_sqm_mag_arcsec2: result.measurement.estimatedSqmMagArcsec2,
    lp_estimated_bortle_class: result.measurement.estimatedBortleClass,
    lp_dataset_name: result.datasetName,
    lp_provider_name: result.providerName,
    lp_source: result.source,
    lp_source_unit: result.sourceUnit,
    lp_data_kind: 'modeled',
    lp_updated_at: updatedAt,
  };
}

export function siteLightPollutionPayloadFromApiSnapshot(
  snapshot: LightPollutionPointSnapshot,
  updatedAt: string,
): SiteLightPollutionPayload | null {
  if (snapshot.status !== 'available' || snapshot.measurement === null) {
    return null;
  }

  return {
    lp_artificial_brightness_mcd_m2: snapshot.measurement.artificial_brightness_mcd_m2,
    lp_natural_sky_ratio: snapshot.measurement.natural_sky_ratio,
    lp_estimated_total_brightness_mcd_m2: snapshot.measurement.estimated_total_brightness_mcd_m2,
    lp_estimated_sqm_mag_arcsec2: snapshot.measurement.estimated_sqm_mag_arcsec2,
    lp_estimated_bortle_class: snapshot.measurement.estimated_bortle_class,
    lp_dataset_name: snapshot.dataset_name,
    lp_provider_name: snapshot.provider_name,
    lp_source: snapshot.source,
    lp_source_unit: snapshot.source_unit,
    lp_data_kind: 'modeled',
    lp_updated_at: updatedAt,
  };
}
