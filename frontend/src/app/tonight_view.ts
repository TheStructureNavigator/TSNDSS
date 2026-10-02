import type { NightStory, NightStoryEvent, NightStoryPhase, NoWindowReason, UsableWindow } from './night_story.ts';
import { OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT } from './condition_rules.ts';
import { formatInstant, localDateKey, resolveDisplayTimeZone } from './conditions_time.ts';

const TIME_OPTIONS: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', hour12: false };
const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false };

export function renderTonightStoryLevel(story: NightStory | null): string {
  if (!story) {
    return `
      <section class="tonight-story" aria-labelledby="tonight-story-title">
        <div class="tonight-story__header">
          <div>
            <span class="tonight-story__eyebrow">Tonight</span>
            <h3 id="tonight-story-title">Observation Center</h3>
          </div>
          <span class="tonight-story__site-time">Waiting for night data</span>
        </div>
        <p class="muted">Tonight story is not available for this Site yet.</p>
      </section>
    `;
  }

  const timeZone = getStoryTimeZone(story);
  return `
    <section class="tonight-story" aria-labelledby="tonight-story-title">
      <div class="tonight-story__header">
        <div>
          <span class="tonight-story__eyebrow">Tonight</span>
          <h3 id="tonight-story-title">${escapeHtml(formatTargetTitle(story))}</h3>
        </div>
        <span class="tonight-story__site-time">${escapeHtml(formatTimeZoneLabel(story.timeZone, timeZone))}</span>
      </div>
      <p class="tonight-story__headline">${escapeHtml(formatHeadline(story))}</p>
      ${renderNightStrip(story)}
      <div class="tonight-story__lines" aria-label="Tonight story lines">
        ${renderStoryLine('Darkness', formatDarknessLine(story, timeZone))}
        ${renderStoryLine('Sky', formatSkyLine(story, timeZone))}
        ${renderStoryLine('Moon', formatMoonLine(story, timeZone))}
        ${renderStoryLine('Target', formatTargetLine(story, timeZone))}
        ${renderStoryLine('Watch out', formatWatchOutLine(story, timeZone))}
      </div>
      ${renderUsableBlock(story, timeZone)}
    </section>
  `;
}

export function formatHeadline(story: NightStory): string {
  const timeZone = getStoryTimeZone(story);
  const clauses = story.headline.slice(0, 4).map((clause) => {
    const at = clause.instantMs == null ? '' : ` at ${formatTime(clause.instantMs, timeZone)}`;
    switch (clause.kind) {
      case 'darkness':
        return clause.value === 'astronomical_darkness'
          ? `astronomical darkness starts${at}`
          : `${formatPhaseKind(String(clause.value ?? 'darkest_available'))} is the darkest available period${at}`;
      case 'cloud_or_weather':
        return clause.eventKind === 'weather_gap'
          ? `weather has a data gap${at}`
          : `clouds exceed ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%${at}`;
      case 'moon':
        return clause.value === true
          ? 'Moon remains up through darkness'
          : clause.value === false
            ? 'Moon does not stay up through darkness'
            : 'Moon timing is available';
      case 'warning':
        return `${formatWarningKind(clause.eventKind)}${at}`;
      default:
        return '';
    }
  }).filter(Boolean);
  return clauses.length ? clauses.join('; ') : 'Tonight data is available.';
}

export function renderNightStrip(story: NightStory): string {
  const base = story.night ?? story.darkness.darkestAvailable;
  if (!base) {
    return '<div class="night-strip night-strip--empty" aria-label="Night phases">No night phase data</div>';
  }
  const total = Math.max(base.endMs - base.startMs, 1);
  const segments = story.phases
    .filter((phase) => phase.kind !== 'observing_night')
    .map((phase) => {
      const start = Math.max(base.startMs, phase.startMs);
      const end = Math.min(base.endMs, phase.endMs);
      if (end <= start) {
        return '';
      }
      const width = ((end - start) / total) * 100;
      return `
        <span
          class="night-strip__segment night-strip__segment--${phase.kind}"
          style="width: ${width.toFixed(3)}%"
          title="${escapeHtml(`${formatPhaseKind(phase.kind)} ${formatRange(phase, getStoryTimeZone(story))}`)}"
        >
          <span class="night-strip__label">${escapeHtml(formatPhaseKind(phase.kind))}</span>
        </span>
      `;
    })
    .join('');
  return `
    <div class="night-strip" aria-label="Night phases from sunset to sunrise">
      ${segments || '<span class="night-strip__segment night-strip__segment--darkest_available" style="width: 100%"><span class="night-strip__label">No phase data</span></span>'}
    </div>
  `;
}

function renderUsableBlock(story: NightStory, timeZone: string): string {
  if (!story.target.selected) {
    return `
      <section class="tonight-usable" aria-labelledby="tonight-usable-title">
        <h4 id="tonight-usable-title">Usable window</h4>
        <p class="tonight-usable__empty">Choose a target to evaluate a usable window.</p>
      </section>
    `;
  }

  if (!story.darkness.astronomical) {
    return `
      <section class="tonight-usable" aria-labelledby="tonight-usable-title">
        <h4 id="tonight-usable-title">Usable window</h4>
        <p class="tonight-usable__empty">No astronomical night. Darkest available: ${escapeHtml(formatOptionalRange(story.darkness.darkestAvailable, timeZone))}.</p>
      </section>
    `;
  }

  if (story.usableWindows.length) {
    return `
      <section class="tonight-usable" aria-labelledby="tonight-usable-title">
        <h4 id="tonight-usable-title">Usable windows</h4>
        <ol class="tonight-usable__list">
          ${story.usableWindows.map((window) => `
            <li>
              <strong>${escapeHtml(formatRange(window, timeZone))}</strong>
              <span>${escapeHtml(formatDuration(window.endMs - window.startMs))}</span>
            </li>
          `).join('')}
        </ol>
        <div class="tonight-rules">
          <span>Requirements</span>
          <ul>
            <li>Astronomical darkness</li>
            <li>Target visible at or above ${formatNumber(story.target.minTargetAltitudeDeg ?? 30, 0)} deg</li>
            <li>Cloud cover at or below ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%</li>
            <li>Weather sample present</li>
          </ul>
        </div>
        ${renderWarningList(story, timeZone)}
      </section>
    `;
  }

  return `
    <section class="tonight-usable" aria-labelledby="tonight-usable-title">
      <h4 id="tonight-usable-title">Usable window</h4>
      <p class="tonight-usable__empty">${escapeHtml(formatNoWindowReason(story.noWindowReasons))}</p>
      <div class="tonight-rules">
        <span>Limiting intervals</span>
        <ul>
          ${story.noWindowReasons.slice(0, 4).map((reason) => `<li>${escapeHtml(formatReasonLine(reason, timeZone))}</li>`).join('')}
        </ul>
      </div>
      ${renderWarningList(story, timeZone)}
    </section>
  `;
}

function renderWarningList(story: NightStory, timeZone: string): string {
  const warnings = story.warnings.slice(0, 4);
  if (!warnings.length && story.moon.upThroughAstronomicalDarkness !== true) {
    return '';
  }
  const moonWarning = story.moon.upThroughAstronomicalDarkness === true
    ? '<li>Moon remains up through astronomical darkness.</li>'
    : '';
  return `
    <div class="tonight-warnings">
      <span>Warnings</span>
      <ul>
        ${moonWarning}
        ${warnings.map((warning) => `<li>${escapeHtml(`${formatWarningKind(warning.kind)} at ${formatTime(warning.instantMs, timeZone)}`)}</li>`).join('')}
      </ul>
    </div>
  `;
}

function renderStoryLine(label: string, value: string): string {
  return `
    <div class="tonight-story-line">
      <span class="tonight-story-line__label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function formatDarknessLine(story: NightStory, timeZone: string): string {
  if (story.darkness.astronomical) {
    return `Astronomical darkness ${formatRange(story.darkness.astronomical, timeZone)}`;
  }
  if (story.darkness.darkestAvailable) {
    return `No astronomical night; darkest available ${formatRange(story.darkness.darkestAvailable, timeZone)}`;
  }
  return 'No night interval available';
}

function formatSkyLine(story: NightStory, timeZone: string): string {
  const weatherGap = story.events.find((event) => event.kind === 'weather_gap');
  if (weatherGap) {
    return `Weather missing from ${formatTime(weatherGap.instantMs, timeZone)}`;
  }
  const cloudReason = story.noWindowReasons.find((reason) => reason.reasons.includes('cloud_above_threshold'));
  if (cloudReason) {
    return `Clouds above ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}% from ${formatTime(cloudReason.startMs, timeZone)}`;
  }
  return `Cloud rule uses <= ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}% during usable checks`;
}

function formatMoonLine(story: NightStory, timeZone: string): string {
  const parts = [
    story.moon.phaseLabel ?? 'Moon phase unavailable',
    story.moon.illuminationPct == null ? null : `${story.moon.illuminationPct.toFixed(0)}% illuminated`,
  ].filter(Boolean);
  const eventText = story.moon.events
    .slice(0, 2)
    .map((event) => `${event.kind === 'moonrise' ? 'rises' : 'sets'} ${formatTime(event.instantMs, timeZone)}`)
    .join(', ');
  if (story.moon.upThroughAstronomicalDarkness === true) {
    parts.push('up through darkness');
  } else if (eventText) {
    parts.push(eventText);
  }
  return parts.join(' · ');
}

function formatTargetLine(story: NightStory, timeZone: string): string {
  if (!story.target.selected) {
    return 'No target';
  }
  const targetName = story.target.context?.target_name ?? 'Target';
  if (!story.target.visibilityWindows.length) {
    return `${targetName}: no visibility window`;
  }
  const first = story.target.visibilityWindows[0]!;
  const maxAltitude = first.max_altitude_deg == null ? '' : ` · max ${first.max_altitude_deg.toFixed(1)} deg`;
  const maxTime = first.max_altitude_time_utc == null ? '' : ` at ${formatIsoTime(first.max_altitude_time_utc, timeZone)}`;
  return `${targetName}: ${formatIsoRange(first.start_utc, first.end_utc, timeZone)}${maxAltitude}${maxTime}`;
}

function formatWatchOutLine(story: NightStory, timeZone: string): string {
  const warning = story.warnings[0] ?? null;
  if (warning) {
    return `${formatWarningKind(warning.kind)} at ${formatTime(warning.instantMs, timeZone)}`;
  }
  if (story.moon.upThroughAstronomicalDarkness === true) {
    return 'Moon remains up through darkness';
  }
  return 'No warning events in the current story';
}

function formatNoWindowReason(reasons: NoWindowReason[]): string {
  const kinds = new Set(reasons.flatMap((reason) => reason.reasons));
  if (kinds.has('target_not_selected')) {
    return 'Choose a target to evaluate a usable window.';
  }
  if (kinds.has('not_dark')) {
    return 'No usable window because there is no astronomical night.';
  }
  if (kinds.has('target_blocked')) {
    return 'No usable window because the target is blocked by the local horizon.';
  }
  if (kinds.has('not_geometrically_visible')) {
    return 'No usable window because the target is not geometrically visible in darkness.';
  }
  if (kinds.has('cloud_above_threshold')) {
    return `No usable window because clouds exceed ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%.`;
  }
  if (kinds.has('weather_missing')) {
    return 'No usable window because weather data is missing.';
  }
  return 'No usable window in the requested night.';
}

function formatReasonLine(reason: NoWindowReason, timeZone: string): string {
  const labels = reason.reasons.map(formatReasonKind).join(', ');
  return `${formatRange(reason, timeZone)}: ${labels}`;
}

function formatReasonKind(kind: string): string {
  switch (kind) {
    case 'not_dark':
      return 'not astronomical darkness';
    case 'target_not_selected':
      return 'target not selected';
    case 'not_geometrically_visible':
      return 'target not geometrically visible';
    case 'target_blocked':
      return 'blocked by local horizon';
    case 'cloud_above_threshold':
      return `clouds above ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%`;
    case 'weather_missing':
      return 'weather missing';
    default:
      return kind;
  }
}

function formatWarningKind(kind: NightStoryEvent['kind'] | undefined): string {
  switch (kind) {
    case 'fog_change':
      return 'fog warning';
    case 'precipitation_change':
      return 'precipitation warning';
    case 'dew_category_change':
      return 'dew risk change';
    case 'wind_category_change':
      return 'wind change';
    case 'gust_category_change':
      return 'gust change';
    default:
      return 'warning';
  }
}

function formatTargetTitle(story: NightStory): string {
  const target = story.target.context?.target_name;
  return target ? `Tonight for ${target}` : 'Tonight';
}

function formatPhaseKind(kind: string): string {
  return kind.replaceAll('_', ' ');
}

function formatOptionalRange(interval: UsableWindow | NightStoryPhase | { startMs: number; endMs: number } | null, timeZone: string): string {
  return interval ? formatRange(interval, timeZone) : 'unavailable';
}

function formatRange(interval: { startMs: number; endMs: number }, timeZone: string): string {
  const options = localDateKey(interval.startMs, timeZone) === localDateKey(interval.endMs, timeZone)
    ? TIME_OPTIONS
    : DATE_TIME_OPTIONS;
  return `${formatInstant(interval.startMs, timeZone, options)} -> ${formatInstant(interval.endMs, timeZone, options)}`;
}

function formatIsoRange(startIso: string, endIso: string, timeZone: string): string {
  return `${formatIsoTime(startIso, timeZone)} -> ${formatIsoTime(endIso, timeZone)}`;
}

function formatIsoTime(value: string, timeZone: string): string {
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? formatTime(ms, timeZone) : value;
}

function formatTime(instantMs: number, timeZone: string): string {
  return formatInstant(instantMs, timeZone, TIME_OPTIONS);
}

function formatDuration(durationMs: number): string {
  const totalMinutes = Math.max(0, Math.round(durationMs / 60_000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h ${minutes.toString().padStart(2, '0')}m` : `${minutes}m`;
}

function formatNumber(value: number, digits: number): string {
  return Number.isFinite(value) ? value.toFixed(digits) : '30';
}

function getStoryTimeZone(story: NightStory): string {
  return resolveDisplayTimeZone(story.timeZone) ?? 'UTC';
}

function formatTimeZoneLabel(original: string | null, resolved: string): string {
  return original && original === resolved ? original : `${resolved} display`;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
