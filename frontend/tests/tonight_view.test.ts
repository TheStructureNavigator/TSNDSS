import assert from 'node:assert/strict';
import test from 'node:test';

import type {
  AstronomicalConditionsSnapshot,
  AstronomicalNight,
  SiteForecastHour,
  SiteForecastSnapshot,
  VisibilityWindowsResult,
} from '../src/app/api.ts';
import { buildNightStory } from '../src/app/night_story.ts';
import {
  buildTonightVisibilityRequest,
  TONIGHT_ASTRONOMY_SCOPE,
  TONIGHT_FORECAST_DAYS,
  TONIGHT_FORECAST_HOURS,
  TONIGHT_PAST_DAYS,
} from '../src/app/tonight_data.ts';
import { formatHeadline, renderTonightStoryLevel } from '../src/app/tonight_view.ts';

const HOUR_MS = 60 * 60 * 1000;
const BASE = Date.UTC(2026, 8, 27, 16);

function iso(ms: number): string {
  return new Date(ms).toISOString().replace('.000Z', 'Z');
}

function at(hourOffset: number, minute = 0): string {
  return iso(BASE + hourOffset * HOUR_MS + minute * 60_000);
}

function night(overrides: Partial<AstronomicalNight> = {}): AstronomicalNight {
  return {
    status: 'ok',
    sunset_utc: at(0),
    sunrise_utc: at(13),
    civil_twilight_evening_start_utc: at(0),
    civil_twilight_evening_end_utc: at(1),
    nautical_twilight_evening_start_utc: at(1),
    nautical_twilight_evening_end_utc: at(2),
    astronomical_twilight_evening_start_utc: at(2),
    astronomical_twilight_evening_end_utc: at(3),
    astronomical_night_start_utc: at(3),
    astronomical_night_end_utc: at(11),
    astronomical_twilight_morning_start_utc: at(11),
    astronomical_twilight_morning_end_utc: at(12),
    nautical_twilight_morning_start_utc: at(12),
    nautical_twilight_morning_end_utc: at(12, 30),
    civil_twilight_morning_start_utc: at(12, 30),
    civil_twilight_morning_end_utc: at(13),
    moon_up_at_sunset: false,
    moon_events: [],
    ...overrides,
  };
}

function astronomy(options: {
  nightBlock?: AstronomicalNight;
  target?: AstronomicalConditionsSnapshot['target'];
} = {}): AstronomicalConditionsSnapshot {
  const target = options.target === undefined
    ? { target_name: 'M42', ra_deg: 83.8, dec_deg: -5.4, source_kind: 'catalog', source_id: 'messier:42' }
    : options.target;
  const nightBlock = options.nightBlock ?? night();
  return {
    site_id: 'SiteNo1',
    site_name: 'SiteNo1',
    latitude_deg: 52,
    longitude_deg: 21,
    elevation_m: null,
    generated_at_utc: at(0),
    provider: 'test',
    min_target_altitude_deg: 30,
    target,
    current: {
      time_utc: at(4),
      sky_state: 'astronomical_night',
      sun_altitude_deg: null,
      sun_azimuth_deg: null,
      sunrise_utc: nightBlock.sunrise_utc,
      sunset_utc: nightBlock.sunset_utc,
      civil_twilight_evening_start_utc: nightBlock.civil_twilight_evening_start_utc,
      civil_twilight_evening_end_utc: nightBlock.civil_twilight_evening_end_utc,
      civil_twilight_morning_start_utc: nightBlock.civil_twilight_morning_start_utc,
      civil_twilight_morning_end_utc: nightBlock.civil_twilight_morning_end_utc,
      nautical_twilight_evening_start_utc: nightBlock.nautical_twilight_evening_start_utc,
      nautical_twilight_evening_end_utc: nightBlock.nautical_twilight_evening_end_utc,
      nautical_twilight_morning_start_utc: nightBlock.nautical_twilight_morning_start_utc,
      nautical_twilight_morning_end_utc: nightBlock.nautical_twilight_morning_end_utc,
      astronomical_twilight_evening_start_utc: nightBlock.astronomical_twilight_evening_start_utc,
      astronomical_twilight_evening_end_utc: nightBlock.astronomical_twilight_evening_end_utc,
      astronomical_twilight_morning_start_utc: nightBlock.astronomical_twilight_morning_start_utc,
      astronomical_twilight_morning_end_utc: nightBlock.astronomical_twilight_morning_end_utc,
      astronomical_night_start_utc: nightBlock.astronomical_night_start_utc,
      astronomical_night_end_utc: nightBlock.astronomical_night_end_utc,
      moon_altitude_deg: -8,
      moon_azimuth_deg: null,
      moon_illumination_fraction: 0.61,
      moon_illumination_pct: 61,
      moon_phase_angle_deg: null,
      moon_phase_label: 'waxing gibbous',
      moonrise_utc: null,
      moonset_utc: null,
      target: null,
    },
    hourly: [],
    night: nightBlock,
  };
}

function weatherHour(hourOffset: number, overrides: Partial<SiteForecastHour> = {}): SiteForecastHour {
  return {
    time: at(hourOffset).slice(0, 16),
    time_utc: at(hourOffset),
    temperature_c: null,
    relative_humidity_pct: null,
    dew_point_c: null,
    dew_margin_c: null,
    dew_risk: 'low',
    apparent_temperature_c: null,
    cloud_cover_pct: 12,
    cloud_cover_low_pct: null,
    cloud_cover_mid_pct: null,
    cloud_cover_high_pct: null,
    visibility_m: null,
    surface_pressure_hpa: null,
    wind_speed_kmh: 8,
    wind_direction_deg: null,
    wind_gusts_kmh: 12,
    precipitation_mm: 0,
    precipitation_probability_pct: null,
    condition_code: 0,
    is_day: 0,
    ...overrides,
  };
}

function forecast(overrides: Record<number, Partial<SiteForecastHour>> = {}, timeZone = 'Europe/Warsaw'): SiteForecastSnapshot {
  return {
    site_id: 'SiteNo1',
    site_name: 'SiteNo1',
    latitude_deg: 52,
    longitude_deg: 21,
    timezone: timeZone,
    utc_offset_seconds: 7200,
    generated_at: at(0),
    provider: 'test',
    current: null,
    hourly: Array.from({ length: 14 }, (_, hour) => weatherHour(hour, overrides[hour] ?? {})),
  };
}

function visibility(windows = [{ start_utc: at(3), end_utc: at(11), max_altitude_time_utc: at(7), max_altitude_deg: 54 }]): VisibilityWindowsResult {
  return {
    interval_start_utc: at(0),
    interval_end_utc: at(13),
    min_target_altitude_deg: 30,
    windows: windows.map((window) => ({
      starts_at_interval_start: false,
      ends_at_interval_end: false,
      ...window,
    })),
    diagnostics: {
      samples_evaluated: 1200,
      local_horizon_available: true,
      any_above_geometric_horizon: true,
      any_above_minimum_altitude: true,
      any_altitude_constraints_satisfied: true,
      any_clear_of_local_horizon: true,
      any_visible: windows.length > 0,
    },
    site: {
      id: 'SiteNo1',
      name: 'SiteNo1',
      latitude_deg: 52,
      longitude_deg: 21,
      elevation_m: null,
      sqm_mag_arcsec2: null,
      bortle_class: null,
      lp_artificial_brightness_mcd_m2: null,
      lp_natural_sky_ratio: null,
      lp_estimated_total_brightness_mcd_m2: null,
      lp_estimated_sqm_mag_arcsec2: null,
      lp_estimated_bortle_class: null,
      lp_dataset_name: null,
      lp_provider_name: null,
      lp_source: null,
      lp_source_unit: null,
      lp_data_kind: null,
      lp_updated_at: null,
      south_horizon_open: true,
      notes: null,
      horizon_profile: [{ azimuth_deg: 180, min_altitude_deg: 12 }],
    },
    target: { target_name: 'M42', ra_deg: 83.8, dec_deg: -5.4, source_kind: 'catalog', source_id: 'messier:42' },
    visible_rule: 'target altitude >= minimum and clear of local horizon',
    excluded_constraints: ['weather'],
    diagnostic_semantics: 'evaluation_grid_derived_not_continuous_proof',
  };
}

function story(input: {
  astro?: AstronomicalConditionsSnapshot | null;
  wx?: SiteForecastSnapshot | null;
  vis?: VisibilityWindowsResult | null;
} = {}) {
  return buildNightStory({
    astronomy: input.astro === undefined ? astronomy() : input.astro,
    forecast: input.wx === undefined ? forecast() : input.wx,
    visibility: input.vis === undefined ? visibility() : input.vis,
    nowMs: Date.parse(at(2)),
    timeZone: input.wx?.timezone ?? 'Europe/Warsaw',
  });
}

test('renders normal Tonight story with five lines and usable interval', () => {
  const html = renderTonightStoryLevel(story());
  assert.match(html, /Tonight for M42/);
  assert.match(html, /Darkness/);
  assert.match(html, /Sky/);
  assert.match(html, /Moon/);
  assert.match(html, /Target/);
  assert.match(html, /Watch out/);
  assert.match(html, /Usable windows/);
});

test('no target keeps neutral usable block', () => {
  const html = renderTonightStoryLevel(story({ astro: astronomy({ target: null }), vis: null }));
  assert.match(html, /No target/);
  assert.match(html, /Choose a target to evaluate a usable window/);
  assert.doesNotMatch(html, /\b(BAD|POOR)\b/);
});

test('no usable window uses structured reasons', () => {
  const html = renderTonightStoryLevel(story({ vis: visibility([]) }));
  assert.match(html, /No usable window/);
  assert.match(html, /not geometrically visible|blocked by local horizon|target is not/);
});

test('multiple usable windows remain chronological', () => {
  const html = renderTonightStoryLevel(story({
    vis: visibility([
      { start_utc: at(3), end_utc: at(5), max_altitude_time_utc: at(4), max_altitude_deg: 48 },
      { start_utc: at(8), end_utc: at(10), max_altitude_time_utc: at(9), max_altitude_deg: 60 },
    ]),
  }));
  assert.match(html, /21:00 -&gt; 23:00/);
  assert.match(html, /02:00 -&gt; 04:00/);
  assert.ok(html.indexOf('21:00 -&gt; 23:00') < html.indexOf('02:00 -&gt; 04:00'));
});

test('no astronomical night reports darkest available and no fake usable window', () => {
  const noDark = night({
    astronomical_night_start_utc: null,
    astronomical_night_end_utc: null,
    astronomical_twilight_evening_start_utc: at(2),
    astronomical_twilight_morning_end_utc: at(11),
  });
  const html = renderTonightStoryLevel(story({ astro: astronomy({ nightBlock: noDark }) }));
  assert.match(html, /No astronomical night/);
  assert.doesNotMatch(html, /Usable windows/);
});

test('missing weather is shown without substitution', () => {
  const wx = forecast();
  wx.hourly = wx.hourly.filter((hour) => hour.time_utc !== at(6));
  const html = renderTonightStoryLevel(story({ wx }));
  assert.match(html, /Weather missing/);
  assert.match(html, /weather missing/i);
});

test('Moon up all night is a warning-style fact and not a score', () => {
  const html = renderTonightStoryLevel(story({
    astro: astronomy({ nightBlock: night({ moon_up_at_sunset: true, moon_events: [] }) }),
  }));
  assert.match(html, /Moon remains up through darkness|up through darkness/);
});

test('fog is presented as warning and does not remove usable window', () => {
  const html = renderTonightStoryLevel(story({ wx: forecast({ 7: { condition_code: 45 } }) }));
  assert.match(html, /fog warning/);
  assert.match(html, /Usable windows/);
});

test('Site timezone controls visible times', () => {
  const warsaw = renderTonightStoryLevel(story({ wx: forecast({}, 'Europe/Warsaw') }));
  const utc = renderTonightStoryLevel(story({ wx: forecast({}, 'UTC') }));
  assert.match(warsaw, /21:00 -&gt; 28\.09, 05:00/);
  assert.match(utc, /19:00 -&gt; 28\.09, 03:00/);
});

test('process timezone does not change Site-local times', () => {
  const previous = process.env.TZ;
  process.env.TZ = 'Pacific/Kiritimati';
  const first = renderTonightStoryLevel(story({ wx: forecast({}, 'Europe/Warsaw') }));
  process.env.TZ = 'America/Los_Angeles';
  const second = renderTonightStoryLevel(story({ wx: forecast({}, 'Europe/Warsaw') }));
  process.env.TZ = previous;
  assert.equal(first, second);
});

test('headline uses structured clauses with exact times', () => {
  const headline = formatHeadline(story());
  assert.match(headline, /astronomical darkness starts at 21:00/);
  assert.doesNotMatch(headline, /soon|later|best/i);
});

test('new Level 1 does not expose composite status verdicts', () => {
  const html = renderTonightStoryLevel(story());
  assert.doesNotMatch(html, /\b(GOOD|MODERATE|POOR)\b/);
});

test('Tonight data requests use night scope and sunset to sunrise visibility interval', () => {
  assert.equal(TONIGHT_ASTRONOMY_SCOPE, 'night');
  assert.equal(TONIGHT_FORECAST_DAYS, 2);
  assert.equal(TONIGHT_PAST_DAYS, 1);
  assert.equal(TONIGHT_FORECAST_HOURS, 48);
  const request = buildTonightVisibilityRequest('SiteNo1', astronomy());
  assert.deepEqual(request, {
    site_id: 'SiteNo1',
    start_time_utc: at(0),
    end_time_utc: at(13),
    target_name: 'M42',
    target_ra_deg: 83.8,
    target_dec_deg: -5.4,
    source_kind: 'catalog',
    source_id: 'messier:42',
    min_target_altitude_deg: 30,
  });
  assert.equal(buildTonightVisibilityRequest('SiteNo1', astronomy({ target: null })), null);
});
