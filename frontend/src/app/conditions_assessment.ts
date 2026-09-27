import {
  classifyDewRisk,
  classifyWindGust,
  classifyWindSpeed,
} from './condition_rules.ts';

export type ConditionAssessmentStatus = 'good' | 'moderate' | 'poor';

export type ObservationIntentProfile = 'neutral' | 'broadband' | 'dual_band' | 'narrowband';

export type ObservationIntentContext = {
  observationType: string | null;
  filterName: string | null;
};

export type ObservingWindowConditionPoint = {
  moonIlluminationPct: number | null;
  moonAltitudeDeg: number | null;
  moonTargetSeparationDeg: number | null;
  dewRisk: string | null;
  windSpeedKmh: number | null;
  windGustsKmh: number | null;
};

export type ConditionAssessmentFactor = {
  key: 'moon_illumination' | 'moon_altitude' | 'moon_target_separation' | 'dew_risk' | 'wind' | 'gusts';
  label: string;
  status: ConditionAssessmentStatus;
  reason: string;
};

export type ConditionAssessment = {
  profile: ObservationIntentProfile;
  profileLabel: string;
  contextNote: string;
  overall: ConditionOverallAssessment;
  factors: ConditionAssessmentFactor[];
};

export type ConditionOverallAssessmentStatus = 'GOOD' | 'MODERATE' | 'POOR';

export type ConditionOverallAssessment = {
  status: ConditionOverallAssessmentStatus;
  reason: string;
};

type MoonThresholds = {
  illuminationGoodMax: number;
  illuminationModerateMax: number;
  altitudeModerateMax: number;
  separationGoodMin: number;
  separationModerateMin: number;
};

const MOON_THRESHOLDS_BY_PROFILE: Record<ObservationIntentProfile, MoonThresholds> = {
  neutral: {
    illuminationGoodMax: 35,
    illuminationModerateMax: 70,
    altitudeModerateMax: 30,
    separationGoodMin: 60,
    separationModerateMin: 35,
  },
  broadband: {
    illuminationGoodMax: 25,
    illuminationModerateMax: 55,
    altitudeModerateMax: 20,
    separationGoodMin: 70,
    separationModerateMin: 40,
  },
  dual_band: {
    illuminationGoodMax: 55,
    illuminationModerateMax: 85,
    altitudeModerateMax: 45,
    separationGoodMin: 35,
    separationModerateMin: 20,
  },
  narrowband: {
    illuminationGoodMax: 65,
    illuminationModerateMax: 92,
    altitudeModerateMax: 55,
    separationGoodMin: 30,
    separationModerateMin: 15,
  },
};

export function assessObservingWindowConditions(
  points: ObservingWindowConditionPoint[],
  context: ObservationIntentContext,
): ConditionAssessment {
  const profile = classifyObservationIntent(context);
  const thresholds = MOON_THRESHOLDS_BY_PROFILE[profile];
  const contextNote = buildContextNote(profile, context);

  const moonIlluminationValues = collectFinite(points.map((point) => point.moonIlluminationPct));
  const moonAltitudeValues = collectFinite(points.map((point) => point.moonAltitudeDeg));
  const moonSeparationValues = collectFinite(points.map((point) => point.moonTargetSeparationDeg));
  const windValues = collectFinite(points.map((point) => point.windSpeedKmh));
  const gustValues = collectFinite(points.map((point) => point.windGustsKmh));

  const peakMoonIllumination = maxOrNull(moonIlluminationValues);
  const peakMoonAltitude = maxOrNull(moonAltitudeValues);
  const minimumMoonSeparation = minOrNull(moonSeparationValues);
  const dewRisk = worstDewRisk(points.map((point) => point.dewRisk));
  const peakWind = maxOrNull(windValues);
  const peakGusts = maxOrNull(gustValues);
  const factors = [
    assessMoonIllumination(peakMoonIllumination, thresholds, profile),
    assessMoonAltitude(peakMoonAltitude, thresholds, profile),
    assessMoonSeparation(minimumMoonSeparation, thresholds, profile),
    assessDewRisk(dewRisk),
    assessWind(peakWind),
    assessGusts(peakGusts),
  ];

  return {
    profile,
    profileLabel: profileLabel(profile),
    contextNote,
    overall: aggregateConditionAssessment(factors, profile),
    factors,
  };
}

export function classifyObservationIntent(context: ObservationIntentContext): ObservationIntentProfile {
  const observationType = normalizeContextText(context.observationType);
  const filterName = normalizeContextText(context.filterName);

  if (containsAny(observationType, ['broadband', 'broad band', 'rgb', 'lrgb', 'one shot color', 'osc color'])) {
    return 'broadband';
  }
  if (containsAny(observationType, ['dual-band', 'dual band', 'duoband', 'narrowband', 'narrow band'])) {
    return containsAny(observationType, ['narrowband', 'narrow band']) ? 'narrowband' : 'dual_band';
  }

  if (containsAny(filterName, ['l-extreme', 'lextreme', 'l-ultimate', 'lultimate', 'l-enhance', 'lenhance', 'dual', 'duo', 'tri-band', 'triband'])) {
    return 'dual_band';
  }
  if (containsAny(filterName, ['ha', 'h-alpha', 'oiii', 'o3', 'sii', 'narrowband', 'narrow band'])) {
    return 'narrowband';
  }
  if (containsAny(filterName, ['uv/ir', 'uvir', 'uv-ir', 'ir cut', 'ir-cut', 'ircut', 'l-pro', 'lpro', 'cls'])) {
    return 'broadband';
  }

  return 'neutral';
}

function assessMoonIllumination(
  value: number | null,
  thresholds: MoonThresholds,
  profile: ObservationIntentProfile,
): ConditionAssessmentFactor {
  if (value == null) {
    return {
      key: 'moon_illumination',
      label: 'Moon illumination',
      status: 'moderate',
      reason: 'Moon illumination is unavailable for this observing window.',
    };
  }

  if (value <= thresholds.illuminationGoodMax) {
    return {
      key: 'moon_illumination',
      label: 'Moon illumination',
      status: 'good',
      reason: `${value.toFixed(0)}% illumination stays favorable for ${profileLabel(profile)} work.`,
    };
  }
  if (value <= thresholds.illuminationModerateMax) {
    return {
      key: 'moon_illumination',
      label: 'Moon illumination',
      status: 'moderate',
      reason: `${value.toFixed(0)}% illumination is usable, but Moon brightness starts to matter for ${profileLabel(profile)} work.`,
    };
  }
  return {
    key: 'moon_illumination',
    label: 'Moon illumination',
    status: 'poor',
    reason: `${value.toFixed(0)}% illumination is harsh for ${profileLabel(profile)} work.`,
  };
}

function assessMoonAltitude(
  value: number | null,
  thresholds: MoonThresholds,
  profile: ObservationIntentProfile,
): ConditionAssessmentFactor {
  if (value == null) {
    return {
      key: 'moon_altitude',
      label: 'Moon altitude',
      status: 'moderate',
      reason: 'Moon altitude is unavailable for this observing window.',
    };
  }

  if (value <= 0) {
    return {
      key: 'moon_altitude',
      label: 'Moon altitude',
      status: 'good',
      reason: `Moon stays below the horizon during the assessed window.`,
    };
  }
  if (value <= thresholds.altitudeModerateMax) {
    return {
      key: 'moon_altitude',
      label: 'Moon altitude',
      status: 'moderate',
      reason: `Moon rises to ${value.toFixed(1)}°, which adds some sky glow for ${profileLabel(profile)} work.`,
    };
  }
  return {
    key: 'moon_altitude',
    label: 'Moon altitude',
    status: 'poor',
    reason: `Moon climbs to ${value.toFixed(1)}°, making it intrusive for ${profileLabel(profile)} work.`,
  };
}

function assessMoonSeparation(
  value: number | null,
  thresholds: MoonThresholds,
  profile: ObservationIntentProfile,
): ConditionAssessmentFactor {
  if (value == null) {
    return {
      key: 'moon_target_separation',
      label: 'Moon-target separation',
      status: 'moderate',
      reason: 'Moon-target separation is unavailable for this observing window.',
    };
  }

  if (value >= thresholds.separationGoodMin) {
    return {
      key: 'moon_target_separation',
      label: 'Moon-target separation',
      status: 'good',
      reason: `${value.toFixed(1)}° separation keeps the target well away from Moon glow for ${profileLabel(profile)} work.`,
    };
  }
  if (value >= thresholds.separationModerateMin) {
    return {
      key: 'moon_target_separation',
      label: 'Moon-target separation',
      status: 'moderate',
      reason: `${value.toFixed(1)}° separation is workable, but Moon proximity may start to affect contrast for ${profileLabel(profile)} work.`,
    };
  }
  return {
    key: 'moon_target_separation',
    label: 'Moon-target separation',
    status: 'poor',
    reason: `${value.toFixed(1)}° separation places the target too close to the Moon for ${profileLabel(profile)} work.`,
  };
}

function assessDewRisk(value: string | null): ConditionAssessmentFactor {
  switch (classifyDewRisk(value)) {
    case 'good':
      return {
        key: 'dew_risk',
        label: 'Dew risk',
        status: 'good',
        reason: 'Low dew risk suggests optics should stay manageable without extra concern.',
      };
    case 'moderate':
      return {
        key: 'dew_risk',
        label: 'Dew risk',
        status: 'moderate',
        reason: 'Moderate dew risk means heaters or closer monitoring may be needed.',
      };
    case 'poor':
      return {
        key: 'dew_risk',
        label: 'Dew risk',
        status: 'poor',
        reason: 'High dew risk can quickly degrade optics and interrupt the session.',
      };
    default:
      return {
        key: 'dew_risk',
        label: 'Dew risk',
        status: 'moderate',
        reason: 'Dew-risk context is unavailable for this observing window.',
      };
  }
}

function assessWind(value: number | null): ConditionAssessmentFactor {
  const category = classifyWindSpeed(value);
  if (value == null || category == null) {
    return {
      key: 'wind',
      label: 'Wind',
      status: 'moderate',
      reason: 'Wind speed is unavailable for this observing window.',
    };
  }

  if (category === 'good') {
    return {
      key: 'wind',
      label: 'Wind',
      status: 'good',
      reason: `${value.toFixed(1)} km/h should be comfortable for routine tracking and imaging.`,
    };
  }
  if (category === 'moderate') {
    return {
      key: 'wind',
      label: 'Wind',
      status: 'moderate',
      reason: `${value.toFixed(1)} km/h may start to affect tracking or small rigs.`,
    };
  }
  return {
    key: 'wind',
    label: 'Wind',
    status: 'poor',
    reason: `${value.toFixed(1)} km/h is strong enough to threaten tracking stability.`,
  };
}

function assessGusts(value: number | null): ConditionAssessmentFactor {
  const category = classifyWindGust(value);
  if (value == null || category == null) {
    return {
      key: 'gusts',
      label: 'Gusts',
      status: 'moderate',
      reason: 'Wind gust data is unavailable for this observing window.',
    };
  }

  if (category === 'good') {
    return {
      key: 'gusts',
      label: 'Gusts',
      status: 'good',
      reason: `${value.toFixed(1)} km/h gusts stay mild.`,
    };
  }
  if (category === 'moderate') {
    return {
      key: 'gusts',
      label: 'Gusts',
      status: 'moderate',
      reason: `${value.toFixed(1)} km/h gusts can introduce intermittent shake.`,
    };
  }
  return {
    key: 'gusts',
    label: 'Gusts',
    status: 'poor',
    reason: `${value.toFixed(1)} km/h gusts are likely to cause unstable frames or pointing disturbances.`,
  };
}

function aggregateConditionAssessment(
  factors: ConditionAssessmentFactor[],
  profile: ObservationIntentProfile,
): ConditionOverallAssessment {
  const statusScore = (status: ConditionAssessmentStatus): number => {
    switch (status) {
      case 'good':
        return 0;
      case 'moderate':
        return 1;
      case 'poor':
        return 2;
    }
  };

  const moonWeights: Record<ConditionAssessmentFactor['key'], number> = {
    moon_illumination: profile === 'broadband' ? 1.4 : profile === 'neutral' ? 1.1 : 0.8,
    moon_altitude: profile === 'broadband' ? 1.25 : profile === 'neutral' ? 1.0 : 0.75,
    moon_target_separation: profile === 'broadband' ? 1.35 : profile === 'neutral' ? 1.0 : 0.7,
    dew_risk: 0.95,
    wind: 0.85,
    gusts: 0.75,
  };

  const weightedSeverity = factors.reduce((total, factor) => total + statusScore(factor.status) * moonWeights[factor.key], 0);
  const poorFactors = factors.filter((factor) => factor.status === 'poor');
  const moderateFactors = factors.filter((factor) => factor.status === 'moderate');
  const goodFactors = factors.filter((factor) => factor.status === 'good');
  const moonFactors = factors.filter((factor) => factor.key.startsWith('moon_'));
  const poorMoonCount = moonFactors.filter((factor) => factor.status === 'poor').length;
  const moderateMoonCount = moonFactors.filter((factor) => factor.status === 'moderate').length;
  const moonIllumination = factors.find((factor) => factor.key === 'moon_illumination');
  const moonAltitude = factors.find((factor) => factor.key === 'moon_altitude');
  const moonSeparation = factors.find((factor) => factor.key === 'moon_target_separation');
  const dewFactor = factors.find((factor) => factor.key === 'dew_risk');
  const windFactor = factors.find((factor) => factor.key === 'wind');
  const gustFactor = factors.find((factor) => factor.key === 'gusts');

  let overallStatus: ConditionOverallAssessmentStatus;
  if (
    weightedSeverity >= 7.2
    || poorMoonCount >= 3
    || (poorFactors.length >= 3 && goodFactors.length <= 1)
  ) {
    overallStatus = 'POOR';
  } else if (
    weightedSeverity >= 3.4
    || poorFactors.length >= 1
    || moderateFactors.length >= 2
    || moderateMoonCount >= 2
    || profile === 'neutral'
  ) {
    overallStatus = 'MODERATE';
  } else {
    overallStatus = 'GOOD';
  }

  const reasonParts: string[] = [];

  if (
    poorMoonCount >= 2
    || (moonIllumination?.status === 'poor' && moonAltitude?.status !== 'good')
  ) {
    if (profile === 'dual_band' || profile === 'narrowband') {
      if (moonSeparation?.status === 'good') {
        reasonParts.push('Bright Moon is the main limitation, but Moon-target separation softens its impact for the current filter intent');
      } else if (moonSeparation?.status === 'moderate') {
        reasonParts.push('Bright Moon is the main limitation, though the current filter intent reduces some of that pressure');
      } else {
        reasonParts.push('Bright Moon remains the main limitation even with the current filter intent');
      }
    } else if (profile === 'neutral') {
      reasonParts.push('Moon conditions are the main limitation, and TSN DSS is using neutral interpretation because observation intent is incomplete');
    } else {
      reasonParts.push('Bright, intrusive Moon is the main limitation for broadband-style work');
    }
  } else if (moderateMoonCount >= 2 || poorMoonCount === 1) {
    reasonParts.push('Moon conditions are noticeable but not dominant across the whole observing window');
  }

  if (dewFactor?.status === 'poor') {
    reasonParts.push('Dew risk is high enough to threaten the session');
  } else if (dewFactor?.status === 'moderate') {
    reasonParts.push('Dew requires monitoring');
  }

  if (windFactor?.status === 'poor' || gustFactor?.status === 'poor') {
    reasonParts.push('Wind stability is a meaningful risk');
  } else if (windFactor?.status === 'good' && gustFactor?.status === 'good') {
    reasonParts.push('Wind conditions are good');
  } else if (windFactor?.status === 'moderate' || gustFactor?.status === 'moderate') {
    reasonParts.push('Wind may need some caution');
  }

  if (profile === 'neutral') {
    reasonParts.push('Observation intent is missing or unrecognized, so this uses neutral thresholds');
  }

  if (!reasonParts.length) {
    if (overallStatus === 'GOOD') {
      reasonParts.push('The main assessed factors stay supportive through the current observing window');
    } else if (overallStatus === 'MODERATE') {
      reasonParts.push('Conditions are workable overall, but one or more factors need attention');
    } else {
      reasonParts.push('Multiple limiting factors stack up inside the current observing window');
    }
  }

  return {
    status: overallStatus,
    reason: reasonParts.join('. ') + '.',
  };
}

function collectFinite(values: Array<number | null | undefined>): number[] {
  return values.filter((value): value is number => value != null && Number.isFinite(value));
}

function maxOrNull(values: number[]): number | null {
  return values.length ? Math.max(...values) : null;
}

function minOrNull(values: number[]): number | null {
  return values.length ? Math.min(...values) : null;
}

function worstDewRisk(values: Array<string | null | undefined>): string | null {
  const normalizedValues = values
    .map((value) => normalizeContextText(value))
    .filter((value): value is string => Boolean(value));
  if (normalizedValues.includes('high')) {
    return 'high';
  }
  if (normalizedValues.includes('moderate')) {
    return 'moderate';
  }
  if (normalizedValues.includes('low')) {
    return 'low';
  }
  return null;
}

function buildContextNote(profile: ObservationIntentProfile, context: ObservationIntentContext): string {
  if (profile === 'neutral') {
    return context.observationType || context.filterName
      ? 'Observation intent is not recognized yet, so TSN DSS is using neutral assessment thresholds.'
      : 'Observation type and filter are not set, so TSN DSS is using neutral assessment thresholds.';
  }
  return `Moon sensitivity is being interpreted for ${profileLabel(profile)} intent.`;
}

function profileLabel(profile: ObservationIntentProfile): string {
  switch (profile) {
    case 'broadband':
      return 'broadband imaging';
    case 'dual_band':
      return 'dual-band imaging';
    case 'narrowband':
      return 'narrowband imaging';
    default:
      return 'neutral imaging';
  }
}

function normalizeContextText(value: string | null | undefined): string {
  return String(value ?? '')
    .trim()
    .toLowerCase();
}

function containsAny(value: string, needles: string[]): boolean {
  return needles.some((needle) => value.includes(needle));
}
