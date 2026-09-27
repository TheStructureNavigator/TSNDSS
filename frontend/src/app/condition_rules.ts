export type ConditionCategory = 'good' | 'moderate' | 'poor';

export const OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT = 35;

export const MAX_WIND_GOOD_KMH = 15;
export const MAX_WIND_MODERATE_KMH = 25;
export const MAX_GUST_GOOD_KMH = 20;
export const MAX_GUST_MODERATE_KMH = 35;

export function classifyDewRisk(value: string | null | undefined): ConditionCategory | null {
  switch (String(value ?? '').trim().toLowerCase()) {
    case 'low':
      return 'good';
    case 'moderate':
      return 'moderate';
    case 'high':
      return 'poor';
    default:
      return null;
  }
}

export function classifyWindSpeed(value: number | null | undefined): ConditionCategory | null {
  if (value == null || !Number.isFinite(value)) {
    return null;
  }
  if (value <= MAX_WIND_GOOD_KMH) {
    return 'good';
  }
  if (value <= MAX_WIND_MODERATE_KMH) {
    return 'moderate';
  }
  return 'poor';
}

export function classifyWindGust(value: number | null | undefined): ConditionCategory | null {
  if (value == null || !Number.isFinite(value)) {
    return null;
  }
  if (value <= MAX_GUST_GOOD_KMH) {
    return 'good';
  }
  if (value <= MAX_GUST_MODERATE_KMH) {
    return 'moderate';
  }
  return 'poor';
}

export function isFogCode(conditionCode: number | null | undefined): boolean {
  return conditionCode === 45 || conditionCode === 48;
}

export function isPrecipitationCode(conditionCode: number | null | undefined): boolean {
  if (conditionCode == null) {
    return false;
  }
  return (
    (conditionCode >= 51 && conditionCode <= 57)
    || (conditionCode >= 61 && conditionCode <= 67)
    || (conditionCode >= 71 && conditionCode <= 77)
    || (conditionCode >= 80 && conditionCode <= 82)
    || conditionCode === 85
    || conditionCode === 86
    || conditionCode === 95
    || conditionCode === 96
    || conditionCode === 99
  );
}
