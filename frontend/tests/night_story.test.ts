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

const HOUR_MS = 60 * 60 * 1000;
const BASE = Date.UTC(2026, 9, 15, 16);

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
    civil_twilight_morning_start_utc: at(12),
    civil_twilight_morning_end_utc: at(13),
    nautical_twilight_evening_start_utc: at(1),
    nautical_twilight_evening_end_utc: at(2),
    nautical_twilight_morning_start_utc: at(11),
    nautical_twilight_morning_end_utc: at(12),
    astronomical_twilight_evening_start_utc: at(2),
    astronomical_twilight_evening_end_utc: at(3, 23),
    astronomical_twilight_morning_start_utc: at(11, 52),
    astronomical_twilight_morning_end_utc: at(11),
    astronomical_night_start_utc: at(3, 23),
    astronomical_night_end_utc: at(11, 52),
    moon_up_at_sunset: false,
    moon_events: [],
    ...overrides,
  };
}

function astronomy(nightBlock: AstronomicalNight = night()): AstronomicalConditionsSnapshot {
  return {
    site_id: 'site:synthetic',
    site_name: 'Synthetic Site',
    latitude_deg: 0,
    longitude_deg: 0,
    elevation_m: null,
    generated_at_utc: at(0),
    provider: 'test',
    min_target_altitude_deg: 30,
    target: { target_name: 'Synthetic Target', ra_deg: 10, dec_deg: 20, source_kind: 'manual', source_id: null },
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
      moon_altitude_deg: -5,
      moon_azimuth_deg: null,
      moon_illumination_fraction: 0.42,
      moon_illumination_pct: 42,
      moon_phase_angle_deg: null,
      moon_phase_label: 'waxing crescent',
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
    cloud_cover_pct: 10,
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

function forecast(hours: SiteForecastHour[], timeZone = 'Synthetic/Zone'): SiteForecastSnapshot {
  return {
    site_id: 'site:synthetic',
    site_name: 'Synthetic Site',
    latitude_deg: 0,
    longitude_deg: 0,
    timezone: timeZone,
    utc_offset_seconds: 0,
    generated_at: at(0),
    provider: 'test',
    current: null,
    hourly: hours,
  };
}

function clearWeather(overrides: Record<number, Partial<SiteForecastHour>> = {}): SiteForecastSnapshot {
  return forecast(Array.from({ length: 14 }, (_, hour) => weatherHour(hour, overrides[hour] ?? {})));
}

function visibility(windows = [{ start_utc: at(3), end_utc: at(12), max_altitude_time_utc: at(8), max_altitude_deg: 70 }]): VisibilityWindowsResult {
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
      samples_evaluated: 10,
      local_horizon_available: false,
      any_above_geometric_horizon: true,
      any_above_minimum_altitude: true,
      any_altitude_constraints_satisfied: true,
      any_clear_of_local_horizon: true,
      any_visible: windows.length > 0,
    },
    site: {
      id: 'site:synthetic',
      name: 'Synthetic Site',
      latitude_deg: 0,
      longitude_deg: 0,
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
      south_horizon_open: false,
      notes: null,
      horizon_profile: [],
    },
    target: { target_name: 'Synthetic Target', ra_deg: 10, dec_deg: 20, source_kind: 'manual', source_id: null },
    visible_rule: 'test',
    excluded_constraints: ['weather'],
    diagnostic_semantics: 'evaluation_grid_derived_not_continuous_proof',
  };
}

test('clear autumn observing night produces one exact usable window', () => {
  const story = buildNightStory({ astronomy: astronomy(), forecast: clearWeather(), visibility: visibility(), nowMs: BASE + 4 * HOUR_MS });
  assert.equal(story.usableWindows.length, 1);
  assert.equal(story.usableWindows[0]!.startMs, Date.parse(at(3, 23)));
  assert.equal(story.usableWindows[0]!.endMs, Date.parse(at(11, 52)));
  assert.equal(story.usableWindows[0]!.truth, 'rule');
});

test('cloud crossing above threshold and recovering splits usable windows and preserves events', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather({ 6: { cloud_cover_pct: 80 }, 7: { cloud_cover_pct: 80 } }),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.deepEqual(story.usableWindows.map((window) => [window.startMs, window.endMs]), [
    [Date.parse(at(3, 23)), Date.parse(at(6))],
    [Date.parse(at(8)), Date.parse(at(11, 52))],
  ]);
  assert.deepEqual(
    story.events.filter((event) => event.kind === 'cloud_threshold_crossing').map((event) => event.direction),
    ['above', 'below'],
  );
});

test('cloudy whole darkness creates zero usable windows and structured reasons', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather(Object.fromEntries(Array.from({ length: 14 }, (_, hour) => [hour, { cloud_cover_pct: 90 }]))),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.usableWindows.length, 0);
  assert.ok(story.noWindowReasons.some((reason) => reason.reasons.includes('cloud_above_threshold')));
});

test('no target keeps night weather and Moon story but usable windows are not applicable', () => {
  const story = buildNightStory({ astronomy: astronomy(), forecast: clearWeather(), visibility: null, nowMs: BASE + 4 * HOUR_MS });
  assert.equal(story.target.selected, true);
  assert.equal(story.usableWindows.length, 0);
  assert.ok(story.noWindowReasons.some((reason) => reason.reasons.includes('not_geometrically_visible')));
  const noTargetAstronomy = { ...astronomy(), target: null };
  const noTargetStory = buildNightStory({ astronomy: noTargetAstronomy, forecast: clearWeather(), visibility: null, nowMs: BASE + 4 * HOUR_MS });
  assert.equal(noTargetStory.target.selected, false);
  assert.ok(noTargetStory.noWindowReasons.some((reason) => reason.reasons.includes('target_not_selected')));
});

test('canonical geometric visibility fixture splits usable window without recomputing geometry', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather(),
    visibility: visibility([
      { start_utc: at(4), end_utc: at(6), max_altitude_time_utc: at(5), max_altitude_deg: 50 },
      { start_utc: at(8), end_utc: at(10), max_altitude_time_utc: at(9), max_altitude_deg: 60 },
    ]),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.deepEqual(story.usableWindows.map((window) => [window.startMs, window.endMs]), [
    [Date.parse(at(4)), Date.parse(at(6))],
    [Date.parse(at(8)), Date.parse(at(10))],
  ]);
});

test('missing weather hour breaks usable interval and emits weather gap fact', () => {
  const hourly = clearWeather().hourly.filter((hour) => hour.time_utc !== at(7));
  const story = buildNightStory({ astronomy: astronomy(), forecast: forecast(hourly), visibility: visibility(), nowMs: BASE + 4 * HOUR_MS });
  assert.ok(story.events.some((event) => event.kind === 'weather_gap' && event.instantMs === Date.parse(at(7))));
  assert.deepEqual(story.usableWindows.map((window) => [window.startMs, window.endMs]), [
    [Date.parse(at(3, 23)), Date.parse(at(7))],
    [Date.parse(at(8)), Date.parse(at(11, 52))],
  ]);
});

test('after-midnight now remains in same observing night and next points forward', () => {
  const story = buildNightStory({ astronomy: astronomy(), forecast: clearWeather(), visibility: visibility(), nowMs: BASE + 10 * HOUR_MS });
  assert.equal(story.mode, 'in_session');
  assert.ok(story.events.some((event) => event.instantMs < story.nowMs));
  assert.ok(story.next);
  assert.ok(story.next!.instantMs > story.nowMs);
});

test('no astronomical night exposes darkest available period and no usable window', () => {
  const noDark = night({
    astronomical_night_start_utc: null,
    astronomical_night_end_utc: null,
    astronomical_twilight_evening_start_utc: at(2),
    astronomical_twilight_morning_end_utc: at(11),
  });
  const story = buildNightStory({ astronomy: astronomy(noDark), forecast: clearWeather(), visibility: visibility(), nowMs: BASE + 5 * HOUR_MS });
  assert.equal(story.darkness.astronomical, null);
  assert.equal(story.darkness.darkestAvailable?.kind, 'astronomical_twilight');
  assert.equal(story.usableWindows.length, 0);
  assert.ok(story.noWindowReasons.some((reason) => reason.reasons.includes('not_dark')));
});

test('Moon up all night is represented from night-specific Moon facts', () => {
  const story = buildNightStory({
    astronomy: astronomy(night({ moon_up_at_sunset: true, moon_events: [] })),
    forecast: clearWeather(),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.moon.upAtSunset, true);
  assert.equal(story.moon.upThroughAstronomicalDarkness, true);
});

test('Moon rise before astronomical darkness counts as up through darkness', () => {
  const story = buildNightStory({
    astronomy: astronomy(night({ moon_up_at_sunset: false, moon_events: [{ kind: 'moonrise', time_utc: at(2) }] })),
    forecast: clearWeather(),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.moon.upAtSunset, false);
  assert.equal(story.moon.upThroughAstronomicalDarkness, true);
});

test('Moon set before astronomical darkness is not treated as up through darkness', () => {
  const story = buildNightStory({
    astronomy: astronomy(night({ moon_up_at_sunset: true, moon_events: [{ kind: 'moonset', time_utc: at(2) }] })),
    forecast: clearWeather(),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.moon.upThroughAstronomicalDarkness, false);
});

test('Moon set during darkness is represented as fact and not a score', () => {
  const story = buildNightStory({
    astronomy: astronomy(night({ moon_up_at_sunset: true, moon_events: [{ kind: 'moonset', time_utc: at(6) }] })),
    forecast: clearWeather(),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.moon.upThroughAstronomicalDarkness, false);
  assert.ok(story.events.some((event) => event.kind === 'moonset' && event.truth === 'fact'));
});

test('fog late emits warning and does not change usable window while clouds remain clear', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather({ 9: { condition_code: 45 } }),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.usableWindows.length, 1);
  assert.ok(story.warnings.some((event) => event.kind === 'fog_change' && event.direction === 'enter'));
});

test('dew category transition emits warning and leaves usable unchanged', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather({ 8: { dew_risk: 'high' } }),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.equal(story.usableWindows.length, 1);
  assert.ok(story.warnings.some((event) => event.kind === 'dew_category_change' && event.value === 'poor'));
});

test('one-hour deterioration is not smoothed away', () => {
  const story = buildNightStory({
    astronomy: astronomy(),
    forecast: clearWeather({ 7: { cloud_cover_pct: 90 } }),
    visibility: visibility(),
    nowMs: BASE + 4 * HOUR_MS,
  });
  assert.deepEqual(story.usableWindows.map((window) => [window.startMs, window.endMs]), [
    [Date.parse(at(3, 23)), Date.parse(at(7))],
    [Date.parse(at(8)), Date.parse(at(11, 52))],
  ]);
});

test('Site timezone metadata does not affect story instants', () => {
  const first = buildNightStory({ astronomy: astronomy(), forecast: clearWeather(), visibility: visibility(), nowMs: BASE + 4 * HOUR_MS, timeZone: 'Europe/Warsaw' });
  const second = buildNightStory({ astronomy: astronomy(), forecast: clearWeather(), visibility: visibility(), nowMs: BASE + 4 * HOUR_MS, timeZone: 'Pacific/Kiritimati' });
  assert.deepEqual(
    { night: first.night, usableWindows: first.usableWindows, events: first.events },
    { night: second.night, usableWindows: second.usableWindows, events: second.events },
  );
});

test('identical inputs produce deterministic deeply equal output and stable ordering', () => {
  const input = { astronomy: astronomy(), forecast: clearWeather({ 7: { condition_code: 61 }, 8: { wind_speed_kmh: 30 } }), visibility: visibility(), nowMs: BASE + 4 * HOUR_MS };
  const first = buildNightStory(input);
  const second = buildNightStory(input);
  assert.deepEqual(first, second);
  for (let index = 1; index < first.events.length; index += 1) {
    assert.ok(first.events[index - 1]!.instantMs <= first.events[index]!.instantMs);
  }
});
