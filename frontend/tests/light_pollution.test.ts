import test from 'node:test';
import assert from 'node:assert/strict';

import {
  estimateBortleClass,
  estimateLightPollutionFromArtificialBrightness,
} from '../src/app/light_pollution.ts';
import {
  buildCandidateSiteDefaultName,
  siteLightPollutionPayloadFromApiSnapshot,
  siteLightPollutionPayloadFromCandidate,
} from '../src/app/site_light_pollution.ts';

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

test('site light pollution mapper creates modeled Site update payload from API snapshot', () => {
  const payload = siteLightPollutionPayloadFromApiSnapshot(
    {
      status: 'available',
      provider_name: 'local-raster',
      dataset_name: 'New World Atlas',
      source: 'Falchi et al. 2016',
      latitude_deg: 50.1,
      longitude_deg: 18.2,
      source_value: 0.25,
      source_unit: 'mcd/m²',
      measurement: {
        artificial_brightness_mcd_m2: 0.25,
        natural_sky_ratio: 1.46,
        estimated_total_brightness_mcd_m2: 0.4212,
        estimated_sqm_mag_arcsec2: 21.02,
        estimated_bortle_class: 4,
      },
      message: 'available',
    },
    '2026-09-20T20:00:00.000Z',
  );

  assert.deepEqual(payload, {
    lp_artificial_brightness_mcd_m2: 0.25,
    lp_natural_sky_ratio: 1.46,
    lp_estimated_total_brightness_mcd_m2: 0.4212,
    lp_estimated_sqm_mag_arcsec2: 21.02,
    lp_estimated_bortle_class: 4,
    lp_dataset_name: 'New World Atlas',
    lp_provider_name: 'local-raster',
    lp_source: 'Falchi et al. 2016',
    lp_source_unit: 'mcd/m²',
    lp_data_kind: 'modeled',
    lp_updated_at: '2026-09-20T20:00:00.000Z',
  });
});

test('site light pollution mapper returns null for unavailable point data', () => {
  const payload = siteLightPollutionPayloadFromApiSnapshot(
    {
      status: 'dataset unavailable',
      provider_name: 'local-raster',
      dataset_name: 'New World Atlas',
      source: 'Falchi et al. 2016',
      latitude_deg: 50.1,
      longitude_deg: 18.2,
      source_value: null,
      source_unit: null,
      measurement: null,
      message: 'Light pollution dataset unavailable',
    },
    '2026-09-20T20:00:00.000Z',
  );

  assert.equal(payload, null);
});

test('candidate Site default name uses fixed coordinate precision', () => {
  assert.equal(buildCandidateSiteDefaultName(50.3352912, 17.9628849), 'Site 50.33529, 17.96288');
});

test('candidate light pollution mapper preserves provenance for Create Site preview', () => {
  const payload = siteLightPollutionPayloadFromCandidate(
    {
      status: 'available',
      providerName: 'local-raster',
      datasetName: 'New World Atlas',
      source: 'Falchi et al. 2016',
      sourceValue: 0.1785,
      sourceUnit: 'mcd/m²',
      measurement: {
        artificialBrightnessMcdM2: 0.1785,
        naturalSkyRatio: 1.04,
        estimatedTotalBrightnessMcdM2: 0.3497,
        estimatedSqmMagArcsec2: 21.22,
        estimatedBortleClass: 4,
      },
      message: 'available',
    },
    '2026-09-20T21:00:00.000Z',
  );

  assert.equal(payload?.lp_estimated_sqm_mag_arcsec2, 21.22);
  assert.equal(payload?.lp_estimated_bortle_class, 4);
  assert.equal(payload?.lp_dataset_name, 'New World Atlas');
  assert.equal(payload?.lp_source, 'Falchi et al. 2016');
  assert.equal(payload?.lp_data_kind, 'modeled');
});

test('candidate light pollution mapper returns null when Candidate has no LP data', () => {
  const payload = siteLightPollutionPayloadFromCandidate(
    {
      status: 'unavailable',
      providerName: 'local-raster',
      datasetName: 'New World Atlas',
      source: 'Falchi et al. 2016',
      sourceValue: null,
      sourceUnit: null,
      measurement: null,
      message: 'Light pollution data unavailable for this location.',
    },
    '2026-09-20T21:00:00.000Z',
  );

  assert.equal(payload, null);
});
