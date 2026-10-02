import type { AstronomicalConditionsSnapshot } from './api.ts';

export const TONIGHT_FORECAST_DAYS = 2;
export const TONIGHT_PAST_DAYS = 1;
export const TONIGHT_FORECAST_HOURS = TONIGHT_FORECAST_DAYS * 24;
export const TONIGHT_ASTRONOMY_SCOPE = 'night' as const;

export type TonightVisibilityRequest = {
  site_id: string;
  start_time_utc: string;
  end_time_utc: string;
  target_name?: string | null;
  target_ra_deg?: number | null;
  target_dec_deg?: number | null;
  source_kind?: string | null;
  source_id?: string | null;
  min_target_altitude_deg?: number | null;
};

export function buildTonightVisibilityRequest(
  siteId: string | null | undefined,
  astronomy: AstronomicalConditionsSnapshot | null,
): TonightVisibilityRequest | null {
  if (!siteId || !astronomy) {
    return null;
  }
  const target = astronomy.target ?? astronomy.current.target ?? null;
  const night = astronomy.night ?? null;
  if (!target || !night?.sunset_utc || !night?.sunrise_utc) {
    return null;
  }

  return {
    site_id: siteId,
    start_time_utc: night.sunset_utc,
    end_time_utc: night.sunrise_utc,
    target_name: target.target_name,
    target_ra_deg: target.ra_deg,
    target_dec_deg: target.dec_deg,
    source_kind: target.source_kind,
    source_id: target.source_id,
    min_target_altitude_deg: astronomy.min_target_altitude_deg,
  };
}
