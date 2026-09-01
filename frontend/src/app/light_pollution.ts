export type LightPollutionStatus = 'loading' | 'available' | 'unavailable' | 'error';

export type LightPollutionMeasurement = {
  artificialBrightnessMcdM2: number;
  naturalSkyRatio: number;
  estimatedTotalBrightnessMcdM2: number;
  estimatedSqmMagArcsec2: number;
  estimatedBortleClass: number;
};

export type LightPollutionPointResult = {
  status: LightPollutionStatus;
  providerName: string;
  datasetName: string;
  source: string;
  sourceValue: number | null;
  sourceUnit: string | null;
  measurement: LightPollutionMeasurement | null;
  message: string;
};

export type LightPollutionPointQueryProvider = {
  name: string;
  datasetName: string;
  queryPoint(latitudeDeg: number, longitudeDeg: number): Promise<LightPollutionPointResult>;
};

type LightPollutionApiMeasurement = {
  artificial_brightness_mcd_m2: number;
  natural_sky_ratio: number;
  estimated_total_brightness_mcd_m2: number;
  estimated_sqm_mag_arcsec2: number;
  estimated_bortle_class: number;
};

type LightPollutionApiPoint = {
  status: string;
  provider_name: string;
  dataset_name: string;
  source: string;
  source_value: number | null;
  source_unit: string | null;
  measurement: LightPollutionApiMeasurement | null;
  message: string;
};

const NATURAL_SKY_BRIGHTNESS_MCD_M2 = 0.171168465;
const SQM_ZERO_POINT_LUMINANCE_MCD_M2 = 108000000;

export const ARTIFICIAL_SKY_BRIGHTNESS_LAYER = {
  name: 'ArtificialSkyBrightness',
  attribution: 'ArtificialSkyBrightness',
  datasetName: 'ArtificialSkyBrightness',
  tileUrl: 'https://tiles.arcgis.com/tiles/b3fMqPOmotX6SV4k/arcgis/rest/services/ArtificialSkyBrightness/MapServer/tile/{z}/{y}/{x}',
  maxNativeZoom: 19,
};

export const localRasterLightPollutionPointProvider: LightPollutionPointQueryProvider = {
  name: 'local-raster',
  datasetName: 'New World Atlas',
  async queryPoint(latitudeDeg: number, longitudeDeg: number): Promise<LightPollutionPointResult> {
    try {
      const payload = await fetchLightPollutionPoint(latitudeDeg, longitudeDeg);
      return {
        status: normalizeLightPollutionStatus(payload.status),
        providerName: payload.provider_name,
        datasetName: payload.dataset_name,
        source: payload.source,
        sourceValue: payload.source_value,
        sourceUnit: payload.source_unit,
        measurement: payload.measurement
          ? {
              artificialBrightnessMcdM2: payload.measurement.artificial_brightness_mcd_m2,
              naturalSkyRatio: payload.measurement.natural_sky_ratio,
              estimatedTotalBrightnessMcdM2: payload.measurement.estimated_total_brightness_mcd_m2,
              estimatedSqmMagArcsec2: payload.measurement.estimated_sqm_mag_arcsec2,
              estimatedBortleClass: payload.measurement.estimated_bortle_class,
            }
          : null,
        message: payload.message,
      };
    } catch (error) {
      return {
        ...unavailableLightPollutionResult(getErrorMessage(error), {
          providerName: 'local-raster',
          datasetName: 'New World Atlas',
        }),
        status: 'error',
      };
    }
  },
};

async function fetchLightPollutionPoint(latitudeDeg: number, longitudeDeg: number): Promise<LightPollutionApiPoint> {
  const query = new URLSearchParams({
    lat: String(latitudeDeg),
    lon: String(longitudeDeg),
  });
  const response = await fetch(`${getApiBaseUrl()}/api/light-pollution?${query.toString()}`);
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status} ${response.statusText}`);
  }
  const payload = (await response.json()) as { light_pollution: LightPollutionApiPoint };
  return payload.light_pollution;
}

function getApiBaseUrl(): string {
  const viteEnv = (import.meta as unknown as { env?: { VITE_API_BASE_URL?: string } }).env;
  return (viteEnv?.VITE_API_BASE_URL ?? 'http://127.0.0.1:8765').replace(/\/+$/, '');
}

export function estimateLightPollutionFromArtificialBrightness(
  artificialBrightnessMcdM2: number,
): LightPollutionMeasurement {
  const safeArtificialBrightness = Math.max(0, artificialBrightnessMcdM2);
  const estimatedTotalBrightnessMcdM2 = safeArtificialBrightness + NATURAL_SKY_BRIGHTNESS_MCD_M2;
  const estimatedSqmMagArcsec2 = Math.log10(
    estimatedTotalBrightnessMcdM2 / SQM_ZERO_POINT_LUMINANCE_MCD_M2,
  ) / -0.4;

  return {
    artificialBrightnessMcdM2: safeArtificialBrightness,
    naturalSkyRatio: safeArtificialBrightness / NATURAL_SKY_BRIGHTNESS_MCD_M2,
    estimatedTotalBrightnessMcdM2,
    estimatedSqmMagArcsec2,
    estimatedBortleClass: estimateBortleClass(estimatedSqmMagArcsec2),
  };
}

export function estimateBortleClass(sqmMagArcsec2: number): number {
  if (sqmMagArcsec2 >= 21.99) {
    return 1;
  }
  if (sqmMagArcsec2 >= 21.89) {
    return 2;
  }
  if (sqmMagArcsec2 >= 21.69) {
    return 3;
  }
  if (sqmMagArcsec2 >= 20.49) {
    return 4;
  }
  if (sqmMagArcsec2 >= 19.50) {
    return 5;
  }
  if (sqmMagArcsec2 >= 18.94) {
    return 6;
  }
  if (sqmMagArcsec2 >= 18.38) {
    return 7;
  }
  if (sqmMagArcsec2 >= 17.80) {
    return 8;
  }
  return 9;
}

function normalizeLightPollutionStatus(status: string): LightPollutionStatus {
  if (status === 'available' || status === 'loading' || status === 'error') {
    return status;
  }
  return 'unavailable';
}

function unavailableLightPollutionResult(
  message: string,
  options?: {
    providerName?: string;
    datasetName?: string;
  },
): LightPollutionPointResult {
  return {
    status: 'unavailable',
    providerName: options?.providerName ?? ARTIFICIAL_SKY_BRIGHTNESS_LAYER.name,
    datasetName: options?.datasetName ?? ARTIFICIAL_SKY_BRIGHTNESS_LAYER.datasetName,
    source: 'unknown',
    sourceValue: null,
    sourceUnit: null,
    measurement: null,
    message,
  };
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return 'Provider request failed.';
}
