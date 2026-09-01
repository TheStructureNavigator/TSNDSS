import test from 'node:test';
import assert from 'node:assert/strict';

import {
  estimateBortleClass,
  estimateLightPollutionFromArtificialBrightness,
} from '../src/app/light_pollution.ts';

test('light pollution conversion derives ratio, total brightness, SQM and Bortle from artificial brightness', () => {
  const result = estimateLightPollutionFromArtificialBrightness(0.171168465);

  assert.equal(result.artificialBrightnessMcdM2, 0.171168465);
  assert.equal(result.naturalSkyRatio, 1);
  assert.equal(result.estimatedTotalBrightnessMcdM2, 0.34233693);
  assert.ok(result.estimatedSqmMagArcsec2 > 21.2);
  assert.ok(result.estimatedSqmMagArcsec2 < 21.3);
  assert.equal(result.estimatedBortleClass, 4);
});

test('light pollution conversion clamps negative artificial brightness to zero', () => {
  const result = estimateLightPollutionFromArtificialBrightness(-1);

  assert.equal(result.artificialBrightnessMcdM2, 0);
  assert.equal(result.naturalSkyRatio, 0);
  assert.equal(result.estimatedTotalBrightnessMcdM2, 0.171168465);
  assert.ok(result.estimatedSqmMagArcsec2 > 21.95);
  assert.ok(result.estimatedSqmMagArcsec2 < 22.05);
  assert.equal(result.estimatedBortleClass, 1);
});

test('Bortle estimate maps bright urban sky to class 9', () => {
  assert.equal(estimateBortleClass(17.2), 9);
});
