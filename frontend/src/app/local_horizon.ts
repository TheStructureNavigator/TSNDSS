import type { LocalHorizonPoint } from './api';

export function normalizeLocalHorizonProfile(points: LocalHorizonPoint[]): LocalHorizonPoint[] {
  const seenAzimuths = new Set<number>();
  const normalized = points.map((point) => {
    const azimuth = Number(point.azimuth_deg);
    const altitude = Number(point.min_altitude_deg);
    if (!Number.isFinite(azimuth) || azimuth < 0 || azimuth >= 360) {
      throw new Error('Local horizon azimuth_deg must be in [0, 360).');
    }
    if (!Number.isFinite(altitude) || altitude < 0 || altitude > 90) {
      throw new Error('Local horizon min_altitude_deg must be in [0, 90].');
    }
    if (seenAzimuths.has(azimuth)) {
      throw new Error(`Duplicate local horizon azimuth_deg: ${azimuth}`);
    }
    seenAzimuths.add(azimuth);
    return {
      azimuth_deg: azimuth,
      min_altitude_deg: altitude,
    };
  });

  return normalized.sort((left, right) => left.azimuth_deg - right.azimuth_deg);
}

export function getLocalHorizonAltitude(points: LocalHorizonPoint[], azimuthDeg: number): number | null {
  const profile = normalizeLocalHorizonProfile(points);
  if (!profile.length) {
    return null;
  }
  if (profile.length === 1) {
    return profile[0]!.min_altitude_deg;
  }

  const azimuth = normalizeAzimuth(azimuthDeg);
  for (let index = 0; index < profile.length; index += 1) {
    const left = profile[index]!;
    const right = profile[(index + 1) % profile.length]!;
    let leftAzimuth = left.azimuth_deg;
    let rightAzimuth = right.azimuth_deg;
    let candidateAzimuth = azimuth;

    if (index === profile.length - 1) {
      rightAzimuth += 360;
      if (candidateAzimuth < leftAzimuth) {
        candidateAzimuth += 360;
      }
    }

    if (candidateAzimuth >= leftAzimuth && candidateAzimuth <= rightAzimuth) {
      const span = rightAzimuth - leftAzimuth;
      if (span === 0) {
        return left.min_altitude_deg;
      }
      const fraction = (candidateAzimuth - leftAzimuth) / span;
      return left.min_altitude_deg + fraction * (right.min_altitude_deg - left.min_altitude_deg);
    }
  }

  return profile[0]!.min_altitude_deg;
}

function normalizeAzimuth(value: number): number {
  const normalized = value % 360;
  return normalized < 0 ? normalized + 360 : normalized;
}
