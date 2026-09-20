import assert from 'node:assert/strict';
import test from 'node:test';

import {
  analyzeLocalHorizonVisibility,
  getLocalHorizonObservingWindows,
  isBlockedByLocalHorizon,
  isClearOfLocalHorizon,
  isLocalHorizonObservingWindowPoint,
  type LocalHorizonObservingPoint,
} from '../src/app/local_horizon_conditions.ts';

const profile = [
  { azimuth_deg: 0, min_altitude_deg: 10 },
  { azimuth_deg: 90, min_altitude_deg: 30 },
  { azimuth_deg: 180, min_altitude_deg: 20 },
  { azimuth_deg: 315, min_altitude_deg: 20 },
];

function point(
  index: number,
  targetAltitudeDeg: number,
  targetAzimuthDeg: number,
  overrides: Partial<LocalHorizonObservingPoint> = {},
): LocalHorizonObservingPoint {
  return {
    index,
    timeMs: index * 60 * 60 * 1000,
    skyState: 'astronomical_night',
    targetAltitudeDeg,
    targetAzimuthDeg,
    cloudCoverPct: 10,
    ...overrides,
  };
}

test('local horizon condition is not configured when Site has no profile', () => {
  const analysis = analyzeLocalHorizonVisibility([], {
    targetAltitudeDeg: 5,
    targetAzimuthDeg: 90,
  });

  assert.equal(analysis.visibility, 'not_configured');
  assert.equal(analysis.localHorizonAltitudeDeg, null);
  assert.equal(analysis.clearanceDeg, null);
  assert.equal(isClearOfLocalHorizon([], { targetAltitudeDeg: -5, targetAzimuthDeg: 90 }), true);
});

test('local horizon classifies above, below, and exactly-on-horizon targets', () => {
  assert.equal(isClearOfLocalHorizon(profile, { targetAltitudeDeg: 35, targetAzimuthDeg: 90 }), true);
  assert.equal(isBlockedByLocalHorizon(profile, { targetAltitudeDeg: 25, targetAzimuthDeg: 90 }), true);
  assert.equal(isBlockedByLocalHorizon(profile, { targetAltitudeDeg: 30, targetAzimuthDeg: 90 }), true);
});

test('local horizon clearance is altitude minus interpolated local horizon', () => {
  const analysis = analyzeLocalHorizonVisibility(profile, {
    targetAltitudeDeg: 26,
    targetAzimuthDeg: 45,
  });

  assert.equal(analysis.localHorizonAltitudeDeg, 20);
  assert.equal(analysis.clearanceDeg, 6);
  assert.equal(analysis.visibility, 'clear');
});

test('local horizon condition supports interpolation through 360/0 degrees', () => {
  const analysis = analyzeLocalHorizonVisibility(profile, {
    targetAltitudeDeg: 14,
    targetAzimuthDeg: 337.5,
  });

  assert.equal(analysis.localHorizonAltitudeDeg, 15);
  assert.equal(analysis.clearanceDeg, -1);
  assert.equal(analysis.visibility, 'blocked');
});

test('observing window check is unchanged when no local horizon profile exists', () => {
  const observingPoint = point(0, 31, 90);

  assert.equal(isLocalHorizonObservingWindowPoint(observingPoint, 30, 40, []), true);
});

test('observing window is shortened when target is initially behind local horizon', () => {
  const points = [
    point(0, 25, 90),
    point(1, 31, 90),
    point(2, 35, 90),
  ];

  const windows = getLocalHorizonObservingWindows(points, 20, 40, profile);

  assert.deepEqual(windows, [{ startIndex: 1, endIndex: 2, startMs: points[1].timeMs!, endMs: points[2].timeMs! }]);
});

test('observing window is eliminated when every potential point is behind local horizon', () => {
  const points = [
    point(0, 25, 90),
    point(1, 26, 90),
  ];

  assert.deepEqual(getLocalHorizonObservingWindows(points, 20, 40, profile), []);
});

test('target can emerge from behind obstruction and create a later window', () => {
  const points = [
    point(0, 25, 90),
    point(1, 30, 90),
    point(2, 31, 90),
    point(3, 34, 90),
  ];

  const windows = getLocalHorizonObservingWindows(points, 20, 40, profile);

  assert.deepEqual(windows, [{ startIndex: 2, endIndex: 3, startMs: points[2].timeMs!, endMs: points[3].timeMs! }]);
});
