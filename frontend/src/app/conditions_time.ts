/**
 * Time model for Observation Center Conditions.
 *
 * Cross-domain time series (weather + astronomy) are joined by unambiguous
 * UTC instant, never by array position or wall-clock label. Display uses the
 * Site timezone (IANA name from the forecast), never the browser timezone
 * unless the Site timezone is unknown.
 */

/**
 * Initial Conditions range. Open-Meteo days start and end at Site-local midnight, so one day
 * stops mid-night; two days cover the whole coming night (same default as MCP get_site_forecast).
 */
export const DEFAULT_CONDITIONS_FORECAST_DAYS = 2;

const EXPLICIT_OFFSET_PATTERN = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;

/** Epoch milliseconds for an ISO timestamp with an explicit UTC offset; null for naive or invalid values. */
export function parseInstantMs(value: string | null | undefined): number | null {
  if (!value || !EXPLICIT_OFFSET_PATTERN.test(value.trim())) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export type TimedSample = { time_utc?: string | null };

export type ConditionsJoinedRow<W, A> = {
  index: number;
  instantMs: number | null;
  timeUtc: string | null;
  weather: W | null;
  astronomy: A | null;
};

/**
 * Pair weather and astronomy samples that describe the same real instant.
 *
 * The astronomy series is the timeline axis when present; otherwise the weather
 * series is. A weather sample is attached only when its instant equals the row
 * instant exactly. Missing, unparseable or duplicated instants stay unpaired.
 */
export function joinConditionsByInstant<W extends TimedSample, A extends TimedSample>(
  weatherSamples: readonly W[] | null | undefined,
  astronomySamples: readonly A[] | null | undefined,
): ConditionsJoinedRow<W, A>[] {
  const weatherByInstant = new Map<number, W>();
  const duplicatedInstants = new Set<number>();
  for (const sample of weatherSamples ?? []) {
    const instantMs = parseInstantMs(sample.time_utc);
    if (instantMs == null) {
      continue;
    }
    if (weatherByInstant.has(instantMs)) {
      duplicatedInstants.add(instantMs);
    }
    weatherByInstant.set(instantMs, sample);
  }
  const weatherAt = (instantMs: number | null): W | null => (
    instantMs == null || duplicatedInstants.has(instantMs) ? null : weatherByInstant.get(instantMs) ?? null
  );

  if (astronomySamples && astronomySamples.length > 0) {
    return astronomySamples.map((astronomy, index) => {
      const instantMs = parseInstantMs(astronomy.time_utc);
      return {
        index,
        instantMs,
        timeUtc: instantMs == null ? null : astronomy.time_utc ?? null,
        weather: weatherAt(instantMs),
        astronomy,
      };
    });
  }

  return (weatherSamples ?? [])
    .map((weather) => ({ weather, instantMs: parseInstantMs(weather.time_utc) }))
    .filter((entry): entry is { weather: W; instantMs: number } => entry.instantMs != null)
    .sort((left, right) => left.instantMs - right.instantMs)
    .map((entry, index) => ({
      index,
      instantMs: entry.instantMs,
      timeUtc: entry.weather.time_utc ?? null,
      weather: weatherAt(entry.instantMs),
      astronomy: null,
    }));
}

/** The IANA timezone if the runtime supports it, otherwise undefined (callers fall back explicitly). */
export function resolveDisplayTimeZone(timeZone: string | null | undefined): string | undefined {
  if (!timeZone) {
    return undefined;
  }
  try {
    new Intl.DateTimeFormat('en-US', { timeZone });
    return timeZone;
  } catch {
    return undefined;
  }
}

/** Calendar date (YYYY-MM-DD) of an instant in the given timezone. */
export function localDateKey(instantMs: number, timeZone: string | undefined): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(instantMs));
  const part = (type: string): string => parts.find((entry) => entry.type === type)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function formatInstant(
  instantMs: number,
  timeZone: string | undefined,
  options: Intl.DateTimeFormatOptions,
): string {
  return new Date(instantMs).toLocaleString([], { ...options, timeZone });
}
