import test from 'node:test';
import assert from 'node:assert/strict';

import { assessObservingWindowConditions } from '../src/app/conditions_assessment.ts';

const brightMoonWindow = [
  {
    moonIlluminationPct: 92,
    moonAltitudeDeg: 48,
    moonTargetSeparationDeg: 24,
    dewRisk: 'moderate',
    windSpeedKmh: 11,
    windGustsKmh: 18,
  },
];

test('broadband imaging is assessed harshly under a bright nearby Moon', () => {
  const assessment = assessObservingWindowConditions(brightMoonWindow, {
    observationType: 'broadband imaging',
    filterName: 'UV/IR Cut',
  });

  assert.equal(assessment.profile, 'broadband');
  assert.match(assessment.contextNote, /broadband imaging/i);
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_illumination')?.status, 'poor');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_altitude')?.status, 'poor');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_target_separation')?.status, 'poor');
});

test('dual-band imaging is more tolerant than broadband under the same bright Moon', () => {
  const assessment = assessObservingWindowConditions(brightMoonWindow, {
    observationType: 'dual-band imaging',
    filterName: 'L-eXtreme',
  });

  assert.equal(assessment.profile, 'dual_band');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_illumination')?.status, 'poor');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_altitude')?.status, 'poor');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_target_separation')?.status, 'moderate');
});

test('missing observation context falls back to neutral interpretation', () => {
  const assessment = assessObservingWindowConditions(
    [
      {
        moonIlluminationPct: 40,
        moonAltitudeDeg: -5,
        moonTargetSeparationDeg: 70,
        dewRisk: 'low',
        windSpeedKmh: 9,
        windGustsKmh: 14,
      },
    ],
    {
      observationType: null,
      filterName: null,
    },
  );

  assert.equal(assessment.profile, 'neutral');
  assert.match(assessment.contextNote, /neutral assessment thresholds/i);
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_illumination')?.status, 'moderate');
  assert.equal(assessment.factors.find((factor) => factor.key === 'moon_altitude')?.status, 'good');
  assert.equal(assessment.overall.status, 'MODERATE');
});

test('overall assessment stays moderate for dual-band when Moon is bright but other factors soften the impact', () => {
  const assessment = assessObservingWindowConditions(
    [
      {
        moonIlluminationPct: 91,
        moonAltitudeDeg: 41,
        moonTargetSeparationDeg: 39,
        dewRisk: 'moderate',
        windSpeedKmh: 9,
        windGustsKmh: 14,
      },
    ],
    {
      observationType: 'dual-band imaging',
      filterName: 'L-eXtreme',
    },
  );

  assert.equal(assessment.overall.status, 'MODERATE');
  assert.match(assessment.overall.reason, /Bright Moon/i);
  assert.match(assessment.overall.reason, /softens its impact|reduces some of that pressure/i);
  assert.match(assessment.overall.reason, /Wind conditions are good/i);
  assert.match(assessment.overall.reason, /Dew requires monitoring/i);
});

test('overall assessment becomes poor when multiple strong limitations stack up', () => {
  const assessment = assessObservingWindowConditions(
    [
      {
        moonIlluminationPct: 96,
        moonAltitudeDeg: 55,
        moonTargetSeparationDeg: 12,
        dewRisk: 'high',
        windSpeedKmh: 29,
        windGustsKmh: 41,
      },
    ],
    {
      observationType: 'broadband imaging',
      filterName: 'UV/IR Cut',
    },
  );

  assert.equal(assessment.overall.status, 'POOR');
  assert.match(assessment.overall.reason, /main limitation/i);
  assert.match(assessment.overall.reason, /Dew risk is high/i);
  assert.match(assessment.overall.reason, /Wind stability is a meaningful risk/i);
});

test('overall assessment can stay good when assessed factors remain supportive', () => {
  const assessment = assessObservingWindowConditions(
    [
      {
        moonIlluminationPct: 12,
        moonAltitudeDeg: -9,
        moonTargetSeparationDeg: 108,
        dewRisk: 'low',
        windSpeedKmh: 7,
        windGustsKmh: 11,
      },
    ],
    {
      observationType: 'broadband imaging',
      filterName: 'UV/IR Cut',
    },
  );

  assert.equal(assessment.overall.status, 'GOOD');
  assert.match(assessment.overall.reason, /Wind conditions are good|supportive/i);
});
