import type {
  AstronomicalConditionsSnapshot,
  AstronomicalNight,
  SiteForecastHour,
  SiteForecastSnapshot,
  VisibilityWindowsResult,
} from './api.ts';
import {
  OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT,
  classifyDewRisk,
  classifyWindGust,
  classifyWindSpeed,
  isFogCode,
  isPrecipitationCode,
} from './condition_rules.ts';
import { parseInstantMs } from './conditions_time.ts';

export type Instant = number;
export type Interval = { startMs: Instant; endMs: Instant };
export type Truth = 'fact' | 'rule' | 'warning';
export type NightStoryMode = 'pre_session' | 'in_session' | 'post_session';
export type NightStoryEventKind =
  | 'sunset'
  | 'sunrise'
  | 'darkness_start'
  | 'darkness_end'
  | 'darkest_start'
  | 'darkest_end'
  | 'moonrise'
  | 'moonset'
  | 'target_visible_start'
  | 'target_visible_end'
  | 'target_max_altitude'
  | 'cloud_threshold_crossing'
  | 'dew_category_change'
  | 'wind_category_change'
  | 'gust_category_change'
  | 'fog_change'
  | 'precipitation_change'
  | 'weather_gap'
  | 'usable_window_start'
  | 'usable_window_end';

export type NightStoryEvent = {
  kind: NightStoryEventKind;
  truth: Truth;
  instantMs: Instant;
  interval?: Interval;
  direction?: 'start' | 'end' | 'above' | 'below' | 'enter' | 'leave' | 'change';
  value?: string | number | boolean | null;
  detail?: Record<string, unknown>;
};

export type NightStoryPhaseKind =
  | 'observing_night'
  | 'civil_twilight'
  | 'nautical_twilight'
  | 'astronomical_twilight'
  | 'astronomical_darkness'
  | 'darkest_available';

export type NightStoryPhase = Interval & {
  kind: NightStoryPhaseKind;
  truth: 'fact';
};

export type NoWindowReasonKind =
  | 'not_dark'
  | 'target_not_selected'
  | 'not_geometrically_visible'
  | 'target_blocked'
  | 'cloud_above_threshold'
  | 'weather_missing';

export type NoWindowReason = Interval & {
  truth: 'rule' | 'fact';
  reasons: NoWindowReasonKind[];
};

export type UsableWindow = Interval & {
  truth: 'rule';
  source: {
    darkness: Interval;
    visibility: Interval;
    weatherCells: Interval[];
  };
};

export type NightStoryMoon = {
  illuminationPct: number | null;
  phaseLabel: string | null;
  upAtSunset: boolean | null;
  events: NightStoryEvent[];
  upThroughAstronomicalDarkness: boolean | null;
};

export type HeadlineClause = {
  kind: 'darkness' | 'cloud_or_weather' | 'moon' | 'warning';
  truth: Truth;
  instantMs?: Instant;
  eventKind?: NightStoryEventKind;
  value?: string | number | boolean | null;
};

export type NightStory = {
  nowMs: Instant;
  timeZone: string | null;
  night: Interval | null;
  nightStatus: AstronomicalNight['status'] | 'missing';
  mode: NightStoryMode;
  phases: NightStoryPhase[];
  darkness: {
    astronomical: Interval | null;
    darkestAvailable: (Interval & { kind: NightStoryPhaseKind }) | null;
  };
  target: {
    selected: boolean;
    context: VisibilityWindowsResult['target'] | null;
    minTargetAltitudeDeg: number | null;
    visibilityWindows: VisibilityWindowsResult['windows'];
    diagnostics: VisibilityWindowsResult['diagnostics'] | null;
  };
  usableWindows: UsableWindow[];
  noWindowReasons: NoWindowReason[];
  moon: NightStoryMoon;
  events: NightStoryEvent[];
  warnings: NightStoryEvent[];
  current: {
    phase: NightStoryPhaseKind | null;
    weather: SiteForecastHour | null;
    targetVisible: boolean | null;
    usableWindow: UsableWindow | null;
  };
  next: NightStoryEvent | null;
  remaining: {
    kind: 'usable_window' | 'darkness' | 'darkest_available' | 'none';
    untilMs: Instant | null;
  };
  headline: HeadlineClause[];
};

export type BuildNightStoryInput = {
  astronomy: AstronomicalConditionsSnapshot | null;
  forecast: SiteForecastSnapshot | null;
  visibility: VisibilityWindowsResult | null;
  nowMs: number;
  timeZone?: string | null;
};

const HOUR_MS = 60 * 60 * 1000;

export function buildNightStory(input: BuildNightStoryInput): NightStory {
  const nightBlock = input.astronomy?.night ?? null;
  const nightStatus = nightBlock?.status ?? 'missing';
  const night = intervalFromIso(nightBlock?.sunset_utc, nightBlock?.sunrise_utc);
  const darkness = intervalFromIso(nightBlock?.astronomical_night_start_utc, nightBlock?.astronomical_night_end_utc);
  const darkestAvailable = buildDarkestAvailable(nightBlock, darkness);
  const phases = buildPhases(nightBlock, night, darkness, darkestAvailable);
  const weatherCells = buildWeatherCells(input.forecast?.hourly, night ?? darkness ?? darkestAvailable ?? null);
  const targetSelected = Boolean(input.visibility?.target ?? input.astronomy?.target);
  const visibilityWindows = (input.visibility?.windows ?? []).map((window) => intervalFromIso(window.start_utc, window.end_utc))
    .filter((interval): interval is Interval => interval != null);

  const usableWindows = darkness && input.visibility
    ? buildUsableWindows(darkness, visibilityWindows, weatherCells)
    : [];
  const noWindowReasons = buildNoWindowReasons({
    darkness,
    darkestAvailable,
    targetSelected,
    visibility: input.visibility,
    visibilityWindows,
    weatherCells,
  });
  const moon = buildMoon(input.astronomy, nightBlock, darkness);
  const events = sortEvents([
    ...boundaryEvents(nightBlock, darkness, darkestAvailable),
    ...visibilityEvents(input.visibility),
    ...weatherEvents(weatherCells),
    ...usableEvents(usableWindows),
    ...moon.events,
  ]);
  const warnings = events.filter((event) => event.truth === 'warning');
  const mode = sessionMode(input.nowMs, night);
  const current = {
    phase: currentPhase(input.nowMs, phases),
    weather: currentWeather(input.nowMs, weatherCells),
    targetVisible: input.visibility ? isInsideAny(input.nowMs, visibilityWindows) : (targetSelected ? false : null),
    usableWindow: usableWindows.find((window) => contains(window, input.nowMs)) ?? null,
  };
  const next = events.find((event) => event.instantMs > input.nowMs) ?? null;
  const remaining = remainingState(input.nowMs, usableWindows, darkness, darkestAvailable);

  return {
    nowMs: input.nowMs,
    timeZone: input.timeZone ?? input.forecast?.timezone ?? null,
    night,
    nightStatus,
    mode,
    phases,
    darkness: {
      astronomical: darkness,
      darkestAvailable,
    },
    target: {
      selected: targetSelected,
      context: input.visibility?.target ?? input.astronomy?.target ?? null,
      minTargetAltitudeDeg: input.visibility?.min_target_altitude_deg ?? input.astronomy?.min_target_altitude_deg ?? null,
      visibilityWindows: input.visibility?.windows ?? [],
      diagnostics: input.visibility?.diagnostics ?? null,
    },
    usableWindows,
    noWindowReasons,
    moon,
    events,
    warnings,
    current,
    next,
    remaining,
    headline: headlineClauses({ darkness, darkestAvailable, events, warnings, moon, noWindowReasons }),
  };
}

function buildPhases(
  nightBlock: AstronomicalNight | null,
  night: Interval | null,
  darkness: Interval | null,
  darkestAvailable: (Interval & { kind: NightStoryPhaseKind }) | null,
): NightStoryPhase[] {
  const phases: NightStoryPhase[] = [];
  if (night) {
    phases.push({ ...night, kind: 'observing_night', truth: 'fact' });
  }
  addPhase(phases, 'civil_twilight', nightBlock?.civil_twilight_evening_start_utc, nightBlock?.civil_twilight_evening_end_utc);
  addPhase(phases, 'nautical_twilight', nightBlock?.nautical_twilight_evening_start_utc, nightBlock?.nautical_twilight_evening_end_utc);
  addPhase(phases, 'astronomical_twilight', nightBlock?.astronomical_twilight_evening_start_utc, nightBlock?.astronomical_twilight_evening_end_utc);
  if (darkness) {
    phases.push({ ...darkness, kind: 'astronomical_darkness', truth: 'fact' });
  }
  addPhase(phases, 'astronomical_twilight', nightBlock?.astronomical_twilight_morning_start_utc, nightBlock?.astronomical_twilight_morning_end_utc);
  addPhase(phases, 'nautical_twilight', nightBlock?.nautical_twilight_morning_start_utc, nightBlock?.nautical_twilight_morning_end_utc);
  addPhase(phases, 'civil_twilight', nightBlock?.civil_twilight_morning_start_utc, nightBlock?.civil_twilight_morning_end_utc);
  if (!darkness && darkestAvailable) {
    phases.push({ ...darkestAvailable, truth: 'fact' });
  }
  return phases.sort((left, right) => left.startMs - right.startMs || phaseOrder(left.kind) - phaseOrder(right.kind));
}

function addPhase(phases: NightStoryPhase[], kind: NightStoryPhaseKind, startIso: string | null | undefined, endIso: string | null | undefined): void {
  const interval = intervalFromIso(startIso, endIso);
  if (interval) {
    phases.push({ ...interval, kind, truth: 'fact' });
  }
}

function buildDarkestAvailable(
  nightBlock: AstronomicalNight | null,
  darkness: Interval | null,
): (Interval & { kind: NightStoryPhaseKind }) | null {
  if (darkness) {
    return { ...darkness, kind: 'astronomical_darkness' };
  }
  const candidates: Array<[NightStoryPhaseKind, Interval | null]> = [
    ['astronomical_twilight', intervalFromIso(nightBlock?.astronomical_twilight_evening_start_utc, nightBlock?.astronomical_twilight_morning_end_utc)],
    ['nautical_twilight', intervalFromIso(nightBlock?.nautical_twilight_evening_start_utc, nightBlock?.nautical_twilight_morning_end_utc)],
    ['civil_twilight', intervalFromIso(nightBlock?.civil_twilight_evening_start_utc, nightBlock?.civil_twilight_morning_end_utc)],
  ];
  const found = candidates.find(([, interval]) => interval != null);
  return found?.[1] ? { ...found[1], kind: found[0] } : null;
}

type WeatherCell = Interval & {
  sample: SiteForecastHour | null;
  cloudQualifies: boolean;
  dewCategory: ReturnType<typeof classifyDewRisk>;
  windCategory: ReturnType<typeof classifyWindSpeed>;
  gustCategory: ReturnType<typeof classifyWindGust>;
  fog: boolean;
  precipitation: boolean;
};

function buildWeatherCells(samples: readonly SiteForecastHour[] | null | undefined, coverage: Interval | null): WeatherCell[] {
  if (!coverage) {
    return [];
  }
  const byStart = new Map<number, SiteForecastHour>();
  for (const sample of samples ?? []) {
    const start = parseInstantMs(sample.time_utc);
    if (start != null && !byStart.has(start)) {
      byStart.set(start, sample);
    }
  }
  const cells: WeatherCell[] = [];
  for (let start = floorHour(coverage.startMs); start < coverage.endMs; start += HOUR_MS) {
    const sample = byStart.get(start) ?? null;
    const cloud = sample?.cloud_cover_pct;
    cells.push({
      startMs: start,
      endMs: start + HOUR_MS,
      sample,
      cloudQualifies: cloud != null && Number.isFinite(cloud) && cloud <= OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT,
      dewCategory: classifyDewRisk(sample?.dew_risk),
      windCategory: classifyWindSpeed(sample?.wind_speed_kmh),
      gustCategory: classifyWindGust(sample?.wind_gusts_kmh),
      fog: isFogCode(sample?.condition_code),
      precipitation: isPrecipitationCode(sample?.condition_code),
    });
  }
  return cells;
}

function buildUsableWindows(
  darkness: Interval,
  visibilityWindows: Interval[],
  weatherCells: WeatherCell[],
): UsableWindow[] {
  const raw: UsableWindow[] = [];
  for (const visibility of visibilityWindows) {
    const darkVisible = intersect(darkness, visibility);
    if (!darkVisible) {
      continue;
    }
    for (const cell of weatherCells) {
      if (!cell.sample || !cell.cloudQualifies) {
        continue;
      }
      const interval = intersect(darkVisible, cell);
      if (interval) {
        raw.push({
          ...interval,
          truth: 'rule',
          source: {
            darkness,
            visibility,
            weatherCells: [pickInterval(cell)],
          },
        });
      }
    }
  }
  return mergeUsable(raw);
}

function buildNoWindowReasons(input: {
  darkness: Interval | null;
  darkestAvailable: Interval | null;
  targetSelected: boolean;
  visibility: VisibilityWindowsResult | null;
  visibilityWindows: Interval[];
  weatherCells: WeatherCell[];
}): NoWindowReason[] {
  const base = input.darkness ?? input.darkestAvailable;
  if (!base) {
    return [];
  }
  if (!input.darkness) {
    return [{ ...base, truth: 'rule', reasons: ['not_dark'] }];
  }
  const reasons: NoWindowReason[] = [];
  for (const cell of input.weatherCells) {
    const interval = intersect(input.darkness, cell);
    if (!interval) {
      continue;
    }
    const cellReasons: NoWindowReasonKind[] = [];
    if (!input.targetSelected) {
      cellReasons.push('target_not_selected');
    } else if (!overlapsAny(interval, input.visibilityWindows)) {
      cellReasons.push(visibilityFailureReason(input.visibility));
    }
    if (!cell.sample) {
      cellReasons.push('weather_missing');
    } else if (!cell.cloudQualifies) {
      cellReasons.push('cloud_above_threshold');
    }
    if (cellReasons.length) {
      reasons.push({ ...interval, truth: cellReasons.includes('weather_missing') ? 'fact' : 'rule', reasons: cellReasons });
    }
  }
  return mergeReasons(reasons);
}

function visibilityFailureReason(visibility: VisibilityWindowsResult | null): NoWindowReasonKind {
  const diagnostics = visibility?.diagnostics;
  if (diagnostics?.local_horizon_available && diagnostics.any_altitude_constraints_satisfied && !diagnostics.any_clear_of_local_horizon) {
    return 'target_blocked';
  }
  return 'not_geometrically_visible';
}

function buildMoon(
  astronomy: AstronomicalConditionsSnapshot | null,
  nightBlock: AstronomicalNight | null,
  darkness: Interval | null,
): NightStoryMoon {
  const events = (nightBlock?.moon_events ?? [])
    .map((event): NightStoryEvent | null => {
      const instantMs = parseInstantMs(event.time_utc);
      return instantMs == null ? null : {
        kind: event.kind,
        truth: 'fact',
        instantMs,
        value: event.kind,
      };
    })
    .filter((event): event is NightStoryEvent => event != null);
  const upThroughAstronomicalDarkness = darkness == null
    ? null
    : isMoonUpAt(darkness.startMs, nightBlock?.moon_up_at_sunset ?? null, events)
      && !events.some((event) => event.kind === 'moonset' && event.instantMs > darkness.startMs && event.instantMs < darkness.endMs);
  return {
    illuminationPct: astronomy?.current.moon_illumination_pct ?? null,
    phaseLabel: astronomy?.current.moon_phase_label ?? null,
    upAtSunset: nightBlock?.moon_up_at_sunset ?? null,
    events,
    upThroughAstronomicalDarkness,
  };
}

function isMoonUpAt(
  instantMs: number,
  upAtSunset: boolean | null,
  events: NightStoryEvent[],
): boolean | null {
  if (upAtSunset == null) {
    return null;
  }
  let up = upAtSunset;
  for (const event of [...events].sort((left, right) => left.instantMs - right.instantMs)) {
    if (event.instantMs > instantMs) {
      break;
    }
    if (event.kind === 'moonrise') {
      up = true;
    } else if (event.kind === 'moonset') {
      up = false;
    }
  }
  return up;
}

function boundaryEvents(
  nightBlock: AstronomicalNight | null,
  darkness: Interval | null,
  darkestAvailable: (Interval & { kind: NightStoryPhaseKind }) | null,
): NightStoryEvent[] {
  const events: NightStoryEvent[] = [];
  addBoundary(events, 'sunset', nightBlock?.sunset_utc);
  addBoundary(events, 'sunrise', nightBlock?.sunrise_utc);
  if (darkness) {
    events.push({ kind: 'darkness_start', truth: 'fact', instantMs: darkness.startMs, direction: 'start' });
    events.push({ kind: 'darkness_end', truth: 'fact', instantMs: darkness.endMs, direction: 'end' });
  } else if (darkestAvailable) {
    events.push({ kind: 'darkest_start', truth: 'fact', instantMs: darkestAvailable.startMs, direction: 'start', value: darkestAvailable.kind });
    events.push({ kind: 'darkest_end', truth: 'fact', instantMs: darkestAvailable.endMs, direction: 'end', value: darkestAvailable.kind });
  }
  return events;
}

function addBoundary(events: NightStoryEvent[], kind: NightStoryEventKind, value: string | null | undefined): void {
  const instantMs = parseInstantMs(value);
  if (instantMs != null) {
    events.push({ kind, truth: 'fact', instantMs });
  }
}

function visibilityEvents(visibility: VisibilityWindowsResult | null): NightStoryEvent[] {
  const events: NightStoryEvent[] = [];
  for (const window of visibility?.windows ?? []) {
    const interval = intervalFromIso(window.start_utc, window.end_utc);
    if (!interval) {
      continue;
    }
    events.push({ kind: 'target_visible_start', truth: 'fact', instantMs: interval.startMs, interval, direction: 'start' });
    events.push({ kind: 'target_visible_end', truth: 'fact', instantMs: interval.endMs, interval, direction: 'end' });
    const maxMs = parseInstantMs(window.max_altitude_time_utc);
    if (maxMs != null) {
      events.push({ kind: 'target_max_altitude', truth: 'fact', instantMs: maxMs, value: window.max_altitude_deg });
    }
  }
  return events;
}

function weatherEvents(cells: WeatherCell[]): NightStoryEvent[] {
  const events: NightStoryEvent[] = [];
  let previousCloud: boolean | null = null;
  let previousDew: WeatherCell['dewCategory'] = null;
  let previousWind: WeatherCell['windCategory'] = null;
  let previousGust: WeatherCell['gustCategory'] = null;
  let previousFog = false;
  let previousPrecipitation = false;
  for (const cell of cells) {
    if (!cell.sample) {
      events.push({ kind: 'weather_gap', truth: 'fact', instantMs: cell.startMs, interval: pickInterval(cell) });
      continue;
    }
    const cloudAbove = !cell.cloudQualifies;
    if (previousCloud != null && cloudAbove !== previousCloud) {
      events.push({
        kind: 'cloud_threshold_crossing',
        truth: 'rule',
        instantMs: cell.startMs,
        direction: cloudAbove ? 'above' : 'below',
        value: cell.sample.cloud_cover_pct,
      });
    }
    previousCloud = cloudAbove;
    previousDew = categoryEvent(events, 'dew_category_change', previousDew, cell.dewCategory, cell.startMs);
    previousWind = categoryEvent(events, 'wind_category_change', previousWind, cell.windCategory, cell.startMs);
    previousGust = categoryEvent(events, 'gust_category_change', previousGust, cell.gustCategory, cell.startMs);
    if (cell.fog !== previousFog) {
      events.push({ kind: 'fog_change', truth: 'warning', instantMs: cell.startMs, direction: cell.fog ? 'enter' : 'leave', value: cell.fog });
    }
    previousFog = cell.fog;
    if (cell.precipitation !== previousPrecipitation) {
      events.push({ kind: 'precipitation_change', truth: 'warning', instantMs: cell.startMs, direction: cell.precipitation ? 'enter' : 'leave', value: cell.precipitation });
    }
    previousPrecipitation = cell.precipitation;
  }
  return events;
}

function categoryEvent<T extends string | null>(
  events: NightStoryEvent[],
  kind: NightStoryEventKind,
  previous: T,
  current: T,
  instantMs: number,
): T {
  if (previous != null && current != null && current !== previous) {
    events.push({ kind, truth: 'warning', instantMs, direction: 'change', value: current });
  }
  return current;
}

function usableEvents(windows: UsableWindow[]): NightStoryEvent[] {
  return windows.flatMap((window) => [
    { kind: 'usable_window_start', truth: 'rule' as const, instantMs: window.startMs, interval: pickInterval(window), direction: 'start' as const },
    { kind: 'usable_window_end', truth: 'rule' as const, instantMs: window.endMs, interval: pickInterval(window), direction: 'end' as const },
  ]);
}

function sessionMode(nowMs: number, night: Interval | null): NightStoryMode {
  if (!night) {
    return 'pre_session';
  }
  if (nowMs < night.startMs) {
    return 'pre_session';
  }
  if (nowMs > night.endMs) {
    return 'post_session';
  }
  return 'in_session';
}

function currentPhase(nowMs: number, phases: NightStoryPhase[]): NightStoryPhaseKind | null {
  return phases
    .filter((phase) => phase.kind !== 'observing_night')
    .find((phase) => contains(phase, nowMs))?.kind ?? null;
}

function currentWeather(nowMs: number, cells: WeatherCell[]): SiteForecastHour | null {
  return cells.find((cell) => contains(cell, nowMs))?.sample ?? null;
}

function remainingState(
  nowMs: number,
  usableWindows: UsableWindow[],
  darkness: Interval | null,
  darkestAvailable: Interval | null,
): NightStory['remaining'] {
  const activeWindow = usableWindows.find((window) => contains(window, nowMs));
  if (activeWindow) {
    return { kind: 'usable_window', untilMs: activeWindow.endMs };
  }
  if (darkness) {
    if (nowMs < darkness.startMs) {
      return { kind: 'darkness', untilMs: darkness.startMs };
    }
    if (nowMs < darkness.endMs) {
      return { kind: 'darkness', untilMs: darkness.endMs };
    }
  }
  if (darkestAvailable && nowMs < darkestAvailable.endMs) {
    return { kind: 'darkest_available', untilMs: nowMs < darkestAvailable.startMs ? darkestAvailable.startMs : darkestAvailable.endMs };
  }
  return { kind: 'none', untilMs: null };
}

function headlineClauses(input: {
  darkness: Interval | null;
  darkestAvailable: (Interval & { kind: NightStoryPhaseKind }) | null;
  events: NightStoryEvent[];
  warnings: NightStoryEvent[];
  moon: NightStoryMoon;
  noWindowReasons: NoWindowReason[];
}): HeadlineClause[] {
  const clauses: HeadlineClause[] = [];
  clauses.push({
    kind: 'darkness',
    truth: 'fact',
    instantMs: input.darkness?.startMs ?? input.darkestAvailable?.startMs,
    value: input.darkness ? 'astronomical_darkness' : input.darkestAvailable?.kind ?? 'unavailable',
  });
  const weatherGap = input.events.find((event) => event.kind === 'weather_gap');
  const cloudProblem = input.noWindowReasons.find((reason) => reason.reasons.includes('cloud_above_threshold'));
  if (weatherGap) {
    clauses.push({ kind: 'cloud_or_weather', truth: 'fact', instantMs: weatherGap.instantMs, eventKind: 'weather_gap' });
  } else if (cloudProblem) {
    clauses.push({ kind: 'cloud_or_weather', truth: 'rule', instantMs: cloudProblem.startMs, value: 'cloud_above_threshold' });
  }
  clauses.push({
    kind: 'moon',
    truth: 'fact',
    instantMs: input.moon.events[0]?.instantMs,
    value: input.moon.upThroughAstronomicalDarkness ?? input.moon.upAtSunset,
  });
  const prioritizedWarning = [...input.warnings].sort((left, right) => warningPriority(left) - warningPriority(right) || left.instantMs - right.instantMs)[0];
  if (prioritizedWarning) {
    clauses.push({
      kind: 'warning',
      truth: 'warning',
      instantMs: prioritizedWarning.instantMs,
      eventKind: prioritizedWarning.kind,
      value: prioritizedWarning.value,
    });
  }
  return clauses.slice(0, 4);
}

function warningPriority(event: NightStoryEvent): number {
  switch (event.kind) {
    case 'fog_change':
    case 'precipitation_change':
      return 0;
    case 'dew_category_change':
    case 'wind_category_change':
    case 'gust_category_change':
      return event.value === 'poor' ? 1 : 2;
    default:
      return 3;
  }
}

function mergeUsable(windows: UsableWindow[]): UsableWindow[] {
  const sorted = [...windows].sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs);
  const merged: UsableWindow[] = [];
  for (const window of sorted) {
    const previous = merged[merged.length - 1];
    if (previous && previous.endMs === window.startMs) {
      previous.endMs = window.endMs;
      previous.source.weatherCells.push(...window.source.weatherCells);
    } else {
      merged.push({ ...window, source: { ...window.source, weatherCells: [...window.source.weatherCells] } });
    }
  }
  return merged;
}

function mergeReasons(reasons: NoWindowReason[]): NoWindowReason[] {
  const sorted = [...reasons].sort((left, right) => left.startMs - right.startMs || left.reasons.join(',').localeCompare(right.reasons.join(',')));
  const merged: NoWindowReason[] = [];
  for (const reason of sorted) {
    const previous = merged[merged.length - 1];
    if (previous && previous.endMs === reason.startMs && sameReasons(previous.reasons, reason.reasons)) {
      previous.endMs = reason.endMs;
    } else {
      merged.push({ ...reason, reasons: [...reason.reasons] });
    }
  }
  return merged;
}

function sameReasons(left: NoWindowReasonKind[], right: NoWindowReasonKind[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function sortEvents(events: NightStoryEvent[]): NightStoryEvent[] {
  return [...events].sort((left, right) => (
    left.instantMs - right.instantMs
    || eventOrder(left.kind) - eventOrder(right.kind)
    || JSON.stringify(left.value ?? '').localeCompare(JSON.stringify(right.value ?? ''))
  ));
}

function eventOrder(kind: NightStoryEventKind): number {
  return [
    'sunset',
    'darkest_start',
    'darkness_start',
    'target_visible_start',
    'usable_window_start',
    'target_max_altitude',
    'cloud_threshold_crossing',
    'dew_category_change',
    'wind_category_change',
    'gust_category_change',
    'fog_change',
    'precipitation_change',
    'weather_gap',
    'moonrise',
    'moonset',
    'target_visible_end',
    'usable_window_end',
    'darkness_end',
    'darkest_end',
    'sunrise',
  ].indexOf(kind);
}

function phaseOrder(kind: NightStoryPhaseKind): number {
  return ['observing_night', 'civil_twilight', 'nautical_twilight', 'astronomical_twilight', 'astronomical_darkness', 'darkest_available'].indexOf(kind);
}

function intervalFromIso(startIso: string | null | undefined, endIso: string | null | undefined): Interval | null {
  const startMs = parseInstantMs(startIso);
  const endMs = parseInstantMs(endIso);
  return startMs != null && endMs != null && endMs > startMs ? { startMs, endMs } : null;
}

function pickInterval(interval: Interval): Interval {
  return { startMs: interval.startMs, endMs: interval.endMs };
}

function floorHour(value: number): number {
  return Math.floor(value / HOUR_MS) * HOUR_MS;
}

function intersect(left: Interval, right: Interval): Interval | null {
  const startMs = Math.max(left.startMs, right.startMs);
  const endMs = Math.min(left.endMs, right.endMs);
  return endMs > startMs ? { startMs, endMs } : null;
}

function contains(interval: Interval, instantMs: number): boolean {
  return instantMs >= interval.startMs && instantMs < interval.endMs;
}

function overlapsAny(interval: Interval, others: Interval[]): boolean {
  return others.some((other) => intersect(interval, other) != null);
}

function isInsideAny(instantMs: number, intervals: Interval[]): boolean {
  return intervals.some((interval) => contains(interval, instantMs));
}
