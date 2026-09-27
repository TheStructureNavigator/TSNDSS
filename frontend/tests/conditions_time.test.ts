import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_CONDITIONS_FORECAST_DAYS,
  formatInstant,
  joinConditionsByInstant,
  localDateKey,
  parseInstantMs,
  resolveDisplayTimeZone,
} from '../src/app/conditions_time.ts';
import { getLocalHorizonObservingWindows } from '../src/app/local_horizon_conditions.ts';

const HOUR_MS = 60 * 60 * 1000;
const WARSAW = 'Europe/Warsaw';

type WeatherSample = { time: string; time_utc: string; cloud_cover_pct: number | null };
type AstronomySample = { time_utc: string; sky_state: string; target_altitude_deg: number };

function isoUtc(ms: number): string {
  return new Date(ms).toISOString().replace('.000Z', 'Z');
}

/** Open-Meteo shape: instants plus labels shifted by one response-level offset. */
function weatherSeries(startMs: number, count: number, utcOffsetSeconds: number, cloud: (ms: number) => number | null = () => 50): WeatherSample[] {
  return Array.from({ length: count }, (_, index) => {
    const ms = startMs + index * HOUR_MS;
    return {
      time: new Date(ms + utcOffsetSeconds * 1000).toISOString().slice(0, 16),
      time_utc: isoUtc(ms),
      cloud_cover_pct: cloud(ms),
    };
  });
}

function astronomySeries(startMs: number, count: number): AstronomySample[] {
  return Array.from({ length: count }, (_, index) => ({
    time_utc: isoUtc(startMs + index * HOUR_MS),
    sky_state: 'astronomical_night',
    target_altitude_deg: 60,
  }));
}

function hourMinute(ms: number, timeZone: string | undefined): string {
  return formatInstant(ms, timeZone, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

function assertEveryPairShareInstant(rows: ReturnType<typeof joinConditionsByInstant<WeatherSample, AstronomySample>>): void {
  for (const row of rows) {
    if (row.weather && row.astronomy) {
      assert.equal(row.weather.time_utc, row.astronomy.time_utc);
    }
  }
}

// TSNKitchen-like afternoon load: weather from Site-local midnight, astronomy from the current UTC hour.
const WEATHER_START = Date.UTC(2026, 8, 26, 22); // 2026-09-27 00:00 Europe/Warsaw (CEST)
const ASTRONOMY_START = Date.UTC(2026, 8, 27, 12); // 14:00 local, page loaded 14:25

test('afternoon load pairs 22:00 astronomy with 22:00 weather, not the same array index', () => {
  const weather = weatherSeries(WEATHER_START, 48, 7200);
  const astronomy = astronomySeries(ASTRONOMY_START, 48);
  const rows = joinConditionsByInstant(weather, astronomy);

  const row22 = rows.find((row) => row.timeUtc === '2026-09-27T20:00:00Z');
  assert.ok(row22);
  assert.equal(row22.weather?.time, '2026-09-27T22:00');
  assert.equal(row22.weather?.time_utc, '2026-09-27T20:00:00Z');
  assert.equal(hourMinute(row22.instantMs!, WARSAW), '22:00');
  // The old positional join paired this row with weather[8], i.e. 08:00 that morning.
  assert.equal(weather[row22.index]?.time, '2026-09-27T08:00');
  assert.notEqual(row22.weather, weather[row22.index]);
  assertEveryPairShareInstant(rows);
});

test('cross-midnight night stays aligned from evening to the following morning', () => {
  const rows = joinConditionsByInstant(weatherSeries(WEATHER_START, 48, 7200), astronomySeries(ASTRONOMY_START, 48));
  const night = rows.filter((row) => row.instantMs! >= Date.UTC(2026, 8, 27, 18) && row.instantMs! <= Date.UTC(2026, 8, 28, 3));

  assert.deepEqual(
    night.map((row) => row.weather?.time),
    ['2026-09-27T20:00', '2026-09-27T21:00', '2026-09-27T22:00', '2026-09-27T23:00', '2026-09-28T00:00',
      '2026-09-28T01:00', '2026-09-28T02:00', '2026-09-28T03:00', '2026-09-28T04:00', '2026-09-28T05:00'],
  );
  assert.deepEqual(
    night.map((row) => localDateKey(row.instantMs!, WARSAW)),
    ['2026-09-27', '2026-09-27', '2026-09-27', '2026-09-27', '2026-09-28',
      '2026-09-28', '2026-09-28', '2026-09-28', '2026-09-28', '2026-09-28'],
  );
  assertEveryPairShareInstant(rows);
});

test('composition and Site-local display do not depend on the browser timezone', () => {
  const weather = weatherSeries(WEATHER_START, 48, 7200);
  const astronomy = astronomySeries(ASTRONOMY_START, 48);
  const previousTz = process.env.TZ;
  const observe = () => {
    const rows = joinConditionsByInstant(weather, astronomy);
    return rows.map((row) => [row.timeUtc, row.weather?.time_utc ?? null, localDateKey(row.instantMs!, WARSAW), hourMinute(row.instantMs!, WARSAW)]);
  };
  try {
    process.env.TZ = 'Pacific/Kiritimati';
    const fromKiritimati = observe();
    process.env.TZ = 'America/Los_Angeles';
    const fromLosAngeles = observe();
    assert.deepEqual(fromKiritimati, fromLosAngeles);
    assert.equal(fromLosAngeles.find((entry) => entry[0] === '2026-09-27T20:00:00Z')?.[3], '22:00');
  } finally {
    if (previousTz === undefined) {
      delete process.env.TZ;
    } else {
      process.env.TZ = previousTz;
    }
  }
});

test('a missing weather hour stays missing and does not shift later rows', () => {
  const weather = weatherSeries(WEATHER_START, 48, 7200).filter((sample) => sample.time_utc !== '2026-09-27T19:00:00Z');
  const rows = joinConditionsByInstant(weather, astronomySeries(ASTRONOMY_START, 48));

  assert.equal(rows.find((row) => row.timeUtc === '2026-09-27T19:00:00Z')?.weather, null);
  assert.equal(rows.find((row) => row.timeUtc === '2026-09-27T20:00:00Z')?.weather?.time, '2026-09-27T22:00');
  assert.equal(rows.find((row) => row.timeUtc === '2026-09-27T18:00:00Z')?.weather?.time, '2026-09-27T20:00');
  assertEveryPairShareInstant(rows);
});

test('different series lengths never fall back to positional pairing', () => {
  const weather = weatherSeries(ASTRONOMY_START + 10 * HOUR_MS, 3, 7200);
  const rows = joinConditionsByInstant(weather, astronomySeries(ASTRONOMY_START, 16));

  assert.equal(rows.length, 16);
  assert.deepEqual(
    rows.map((row) => row.weather?.time_utc ?? null),
    [...Array(10).fill(null), '2026-09-27T22:00:00Z', '2026-09-27T23:00:00Z', '2026-09-28T00:00:00Z', null, null, null],
  );
  // Astronomy tail beyond weather coverage is explicit missing weather.
  const long = joinConditionsByInstant(weatherSeries(WEATHER_START, 24, 7200), astronomySeries(ASTRONOMY_START, 24));
  assert.equal(long.filter((row) => row.weather === null).length, 14);
});

test('without astronomy the weather series is the axis, ordered by instant', () => {
  const weather = weatherSeries(WEATHER_START, 3, 7200).reverse();
  const rows = joinConditionsByInstant(weather, null);
  assert.deepEqual(rows.map((row) => row.weather?.time), ['2026-09-27T00:00', '2026-09-27T01:00', '2026-09-27T02:00']);
  assert.deepEqual(rows.map((row) => row.index), [0, 1, 2]);
});

test('Europe/Warsaw DST fall-back: repeated civil 02:00 is two distinct instants', () => {
  // 2026-10-25 01:00Z: CEST (+02) -> CET (+01). Open-Meteo labels keep the single response offset (+7200).
  const start = Date.UTC(2026, 9, 24, 23);
  const weather = weatherSeries(start, 4, 7200, (ms) => (ms - start) / HOUR_MS);
  const rows = joinConditionsByInstant(weather, astronomySeries(start, 4));

  assert.deepEqual(rows.map((row) => row.weather?.cloud_cover_pct), [0, 1, 2, 3]);
  assert.deepEqual(rows.map((row) => row.weather?.time), ['2026-10-25T01:00', '2026-10-25T02:00', '2026-10-25T03:00', '2026-10-25T04:00']);
  assert.deepEqual(rows.map((row) => hourMinute(row.instantMs!, WARSAW)), ['01:00', '02:00', '02:00', '03:00']);
  assert.equal(new Set(rows.map((row) => row.instantMs)).size, 4);
  assertEveryPairShareInstant(rows);
});

test('Europe/Warsaw DST spring-forward: skipped civil hour does not shift pairing', () => {
  // 2026-03-29 01:00Z: CET (+01) -> CEST (+02); civil 02:00-02:59 does not exist.
  const start = Date.UTC(2026, 2, 29, 0);
  const weather = weatherSeries(start, 3, 3600, (ms) => (ms - start) / HOUR_MS);
  const rows = joinConditionsByInstant(weather, astronomySeries(start, 3));

  assert.deepEqual(rows.map((row) => row.weather?.cloud_cover_pct), [0, 1, 2]);
  assert.deepEqual(rows.map((row) => hourMinute(row.instantMs!, WARSAW)), ['01:00', '03:00', '04:00']);
});

test('naive or duplicated instants are never paired', () => {
  assert.equal(parseInstantMs('2026-10-25T02:00'), null);
  assert.equal(parseInstantMs('2026-10-25T02:00:00+02:00'), Date.UTC(2026, 9, 25, 0));
  const astronomy = astronomySeries(Date.UTC(2026, 9, 25, 0), 2);
  const naive = joinConditionsByInstant([{ time: '2026-10-25T02:00', time_utc: '2026-10-25T02:00', cloud_cover_pct: 1 }], astronomy);
  assert.deepEqual(naive.map((row) => row.weather), [null, null]);

  const duplicated = [
    { time: 'a', time_utc: '2026-10-25T00:00:00Z', cloud_cover_pct: 1 },
    { time: 'b', time_utc: '2026-10-25T00:00:00Z', cloud_cover_pct: 99 },
    { time: 'c', time_utc: '2026-10-25T01:00:00Z', cloud_cover_pct: 5 },
  ];
  assert.deepEqual(joinConditionsByInstant(duplicated, astronomy).map((row) => row.weather?.time ?? null), [null, 'c']);
});

test('observing windows consume aligned weather (clear evening is not hidden behind morning cloud)', () => {
  // This morning overcast (00:00-11:59 local), clear afterwards: the positional join fed tonight this morning's cloud.
  const weather = weatherSeries(WEATHER_START, 48, 7200, (ms) => ((ms - WEATHER_START) / HOUR_MS < 12 ? 100 : 0));
  const astronomy = astronomySeries(Date.UTC(2026, 8, 27, 18), 6); // 20:00-01:00 local
  const rows = joinConditionsByInstant(weather, astronomy);
  const points = rows.map((row) => ({
    index: row.index,
    timeMs: row.instantMs,
    skyState: row.astronomy?.sky_state ?? null,
    targetAltitudeDeg: row.astronomy?.target_altitude_deg ?? null,
    targetAzimuthDeg: 180,
    cloudCoverPct: row.weather?.cloud_cover_pct ?? null,
  }));
  const windows = getLocalHorizonObservingWindows(points, 30, 35);
  assert.equal(windows.length, 1);
  assert.equal(windows[0]!.startIndex, 0);
  assert.equal(windows[0]!.endIndex, 5);

  const positionalClouds = astronomy.map((_, index) => weather[index]!.cloud_cover_pct);
  assert.deepEqual(positionalClouds, [100, 100, 100, 100, 100, 100]);
});

test('display timezone falls back explicitly when the Site timezone is unknown or invalid', () => {
  assert.equal(resolveDisplayTimeZone('Europe/Warsaw'), 'Europe/Warsaw');
  assert.equal(resolveDisplayTimeZone(null), undefined);
  assert.equal(resolveDisplayTimeZone('Not/AZone'), undefined);
});

test('default range covers the whole coming night after an evening load', () => {
  assert.equal(DEFAULT_CONDITIONS_FORECAST_DAYS, 2);
  // Loaded at 20:00 local; main.ts requests forecast_days=N weather and forecast_hours=N*24 astronomy.
  const eveningLoad = Date.UTC(2026, 8, 27, 18);
  const nightThroughDawn = (days: number) => joinConditionsByInstant(
    weatherSeries(WEATHER_START, days * 24, 7200),
    astronomySeries(eveningLoad, days * 24),
  ).filter((row) => row.instantMs! >= eveningLoad && row.instantMs! <= Date.UTC(2026, 8, 28, 5));

  const defaultNight = nightThroughDawn(DEFAULT_CONDITIONS_FORECAST_DAYS);
  assert.equal(defaultNight.length, 12);
  assert.ok(defaultNight.every((row) => row.weather?.time_utc === row.astronomy?.time_utc));
  // One day stops at Site-local midnight: the post-midnight half of the night has no weather.
  assert.equal(nightThroughDawn(1).filter((row) => row.weather === null).length, 8);
});
