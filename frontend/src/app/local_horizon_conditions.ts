import type { LocalHorizonPoint } from './api';
import { getLocalHorizonAltitude } from './local_horizon.ts';

export type LocalHorizonVisibility = 'clear' | 'blocked' | 'not_configured' | 'unknown';

export type LocalHorizonAnalysis = {
  localHorizonAltitudeDeg: number | null;
  clearanceDeg: number | null;
  visibility: LocalHorizonVisibility;
};

export type LocalHorizonPointInput = {
  targetAltitudeDeg: number | null | undefined;
  targetAzimuthDeg: number | null | undefined;
};

export type LocalHorizonObservingPoint = LocalHorizonPointInput & {
  index: number;
  timeMs: number | null;
  skyState: string | null;
  cloudCoverPct: number | null;
};

export type LocalHorizonObservingWindowRange = {
  startIndex: number;
  endIndex: number;
  startMs: number;
  endMs: number;
};

export function analyzeLocalHorizonVisibility(
  profile: LocalHorizonPoint[],
  point: LocalHorizonPointInput,
): LocalHorizonAnalysis {
  if (!profile.length) {
    return {
      localHorizonAltitudeDeg: null,
      clearanceDeg: null,
      visibility: 'not_configured',
    };
  }

  const altitude = point.targetAltitudeDeg;
  const azimuth = point.targetAzimuthDeg;
  if (
    altitude == null
    || azimuth == null
    || !Number.isFinite(altitude)
    || !Number.isFinite(azimuth)
  ) {
    return {
      localHorizonAltitudeDeg: null,
      clearanceDeg: null,
      visibility: 'unknown',
    };
  }

  const localHorizonAltitude = getLocalHorizonAltitude(profile, azimuth);
  if (localHorizonAltitude == null || !Number.isFinite(localHorizonAltitude)) {
    return {
      localHorizonAltitudeDeg: null,
      clearanceDeg: null,
      visibility: 'unknown',
    };
  }

  const clearance = altitude - localHorizonAltitude;
  return {
    localHorizonAltitudeDeg: localHorizonAltitude,
    clearanceDeg: clearance,
    visibility: clearance > 0 ? 'clear' : 'blocked',
  };
}

export function isClearOfLocalHorizon(
  profile: LocalHorizonPoint[],
  point: LocalHorizonPointInput,
): boolean {
  const analysis = analyzeLocalHorizonVisibility(profile, point);
  return analysis.visibility === 'not_configured' || analysis.visibility === 'clear';
}

export function isBlockedByLocalHorizon(
  profile: LocalHorizonPoint[],
  point: LocalHorizonPointInput,
): boolean {
  return analyzeLocalHorizonVisibility(profile, point).visibility === 'blocked';
}

export function isLocalHorizonObservingWindowPoint(
  point: LocalHorizonObservingPoint,
  minTargetAltitudeDeg: number,
  maxCloudCoverPct: number,
  profile: LocalHorizonPoint[] = [],
): boolean {
  return point.skyState === 'astronomical_night'
    && point.targetAltitudeDeg != null
    && Number.isFinite(point.targetAltitudeDeg)
    && point.targetAltitudeDeg >= minTargetAltitudeDeg
    && point.cloudCoverPct != null
    && Number.isFinite(point.cloudCoverPct)
    && point.cloudCoverPct <= maxCloudCoverPct
    && isClearOfLocalHorizon(profile, point);
}

export function getLocalHorizonObservingWindows(
  points: LocalHorizonObservingPoint[],
  minTargetAltitudeDeg: number,
  maxCloudCoverPct: number,
  profile: LocalHorizonPoint[] = [],
): LocalHorizonObservingWindowRange[] {
  const observingFlags = points.map((point) => (
    isLocalHorizonObservingWindowPoint(point, minTargetAltitudeDeg, maxCloudCoverPct, profile)
  ));
  let activeWindowStartIndex: number | null = null;
  const observingWindows: LocalHorizonObservingWindowRange[] = [];

  for (let index = 0; index < observingFlags.length; index += 1) {
    const isActive = observingFlags[index];
    if (isActive && activeWindowStartIndex == null) {
      activeWindowStartIndex = index;
      continue;
    }
    if (!isActive && activeWindowStartIndex != null) {
      const startPoint = points[activeWindowStartIndex];
      const endPoint = points[index] ?? points[index - 1];
      if (startPoint?.timeMs != null && endPoint?.timeMs != null) {
        observingWindows.push({
          startIndex: activeWindowStartIndex,
          endIndex: index - 1,
          startMs: startPoint.timeMs,
          endMs: endPoint.timeMs,
        });
      }
      activeWindowStartIndex = null;
    }
  }

  if (activeWindowStartIndex != null) {
    const startPoint = points[activeWindowStartIndex];
    const lastPoint = points[points.length - 1];
    if (startPoint?.timeMs != null && lastPoint?.timeMs != null) {
      observingWindows.push({
        startIndex: activeWindowStartIndex,
        endIndex: points.length - 1,
        startMs: startPoint.timeMs,
        endMs: lastPoint.timeMs,
      });
    }
  }

  return observingWindows;
}
