import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getLocalHorizonAltitude,
  normalizeLocalHorizonProfile,
} from '../src/app/local_horizon.ts';

test('local horizon profile normalization sorts points by azimuth', () => {
  const normalized = normalizeLocalHorizonProfile([
    { azimuth_deg: 180, min_altitude_deg: 40 },
    { azimuth_deg: 0, min_altitude_deg: 12 },
    { azimuth_deg: 90, min_altitude_deg: 25 },
  ]);

  assert.deepEqual(normalized.map((point) => point.azimuth_deg), [0, 90, 180]);
});

test('local horizon interpolation works between ordinary points', () => {
  const altitude = getLocalHorizonAltitude(
    [
      { azimuth_deg: 0, min_altitude_deg: 10 },
      { azimuth_deg: 90, min_altitude_deg: 30 },
    ],
    45,
  );

  assert.equal(altitude, 20);
});

test('local horizon interpolation wraps through 360/0 degrees', () => {
  const altitude = getLocalHorizonAltitude(
    [
      { azimuth_deg: 0, min_altitude_deg: 10 },
      { azimuth_deg: 315, min_altitude_deg: 20 },
    ],
    337.5,
  );

  assert.equal(altitude, 15);
});

test('local horizon validation rejects duplicate azimuth values', () => {
  assert.throws(() => {
    normalizeLocalHorizonProfile([
      { azimuth_deg: 90, min_altitude_deg: 10 },
      { azimuth_deg: 90, min_altitude_deg: 20 },
    ]);
  }, /Duplicate local horizon/);
});
