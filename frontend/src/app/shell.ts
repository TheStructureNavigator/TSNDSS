import iconUrl from '../assets/tsn_dss_icon.png';
import {
  assessObservingWindowConditions,
  type ConditionAssessmentFactor,
  type ObservationIntentContext,
  type ObservingWindowConditionPoint,
} from './conditions_assessment';
import {
  type AstronomicalConditionsHour,
  type AstronomicalConditionsSnapshot,
  type CaptureDetails,
  type CaptureFileEntry,
  type CoreContent,
  getCaptureFileUrl,
  getCaptureThumbnailUrl,
  getApiBaseUrl,
  type LocalHorizonPoint,
  type MosaicPanel,
  type MosaicPlan,
  getProjectRunArtifactUrl,
  getProjectRunPreviewLogUrl,
  getProjectRunPreviewUrl,
  type ApiHealth,
  type ProjectRun,
  type ProjectSummary,
  type RunArtifactImage,
  type Site,
  type SiteForecastHour,
  type SiteForecastSnapshot,
  type TelescopeAdapterDescriptor,
  type TelescopeSnapshot,
  type VisibilityWindowsResult,
} from './api';
import {
  type ConditionsJoinedRow,
  formatInstant,
  joinConditionsByInstant,
  localDateKey,
  parseInstantMs,
  resolveDisplayTimeZone,
} from './conditions_time';
import { normalizeLocalHorizonProfile } from './local_horizon';
import type { LightPollutionPointResult } from './light_pollution';
import {
  analyzeLocalHorizonVisibility,
  getLocalHorizonObservingWindows,
  isBlockedByLocalHorizon,
  isLocalHorizonObservingWindowPoint,
  type LocalHorizonAnalysis,
} from './local_horizon_conditions';
import type { NightStory } from './night_story';
import { buildCandidateSiteDefaultName, siteLightPollutionPayloadFromCandidate } from './site_light_pollution';
import { renderTonightStoryLevel } from './tonight_view';

/**
 * Pure rendering layer for the TSN DSS frontend.
 *
 * `shell.ts` turns the current `AppState` into HTML, but does not own
 * fetching, polling or business actions. Those stay in `main.ts`.
 */
export type ViewName = 'core' | 'projects' | 'processing' | 'sky' | 'observationcenter';
export type ThemeName = 'dark' | 'observation';

/**
 * Single in-memory UI state shared by the lightweight Vite app.
 *
 * The structure is grouped by workspace so we can keep rendering flat while
 * still making ownership boundaries obvious:
 * - app/meta state
 * - project workspace selection
 * - processing workspace selection
 * - sky workspace selection
 * - transient modal / toast UI state
 */
export type AppState = {
  coreContent: CoreContent | null;
  health: ApiHealth | null;
  sites: Site[];
  activeSiteId: string | null;
  lightPollutionOverlayEnabled: boolean;
  lightPollutionOverlayOpacity: number;
  lpUpdatingSiteId: string | null;
  currentDeviceLocating: boolean;
  currentDeviceLocationStatus: string | null;
  conditionsForecastDays: number;
  siteForecast: SiteForecastSnapshot | null;
  astronomicalConditions: AstronomicalConditionsSnapshot | null;
  visibilityWindows: VisibilityWindowsResult | null;
  nightStory: NightStory | null;
  projects: ProjectSummary[];
  selectedProject: ProjectSummary | null;
  selectedProjectRunId: string | null;
  selectedProjectCapture: string | null;
  selectedProjectCaptureDetails: CaptureDetails | null;
  selectedProjectCaptureFile: string | null;
  selectedProjectCaptureFolder: string | null;
  projectRuns: ProjectRun[];
  mosaics: MosaicPlan[];
  selectedMosaicId: string | null;
  selectedMosaicPanelId: string | null;
  activeRun: ProjectRun | null;
  selectedProcessingCapture: string | null;
  selectedObservingWindowIndex: number | null;
  currentView: ViewName;
  theme: ThemeName;
  telescopeSnapshot: TelescopeSnapshot | null;
  telescopeAdapters: TelescopeAdapterDescriptor[];
  activeTelescopeAdapterId: string | null;
  followTelescope: boolean;
  message: string | null;
  error: string | null;
  busy: boolean;
  projectDetailTab: ProjectDetailTab;
  processingDetailTab: ProcessingDetailTab;
  skyDetailTab: SkyDetailTab;
  observationCenterTab: ObservationCenterTab;
  createProjectModalOpen: boolean;
  createRunModalOpen: boolean;
  createImportCaptureModalOpen: boolean;
  siteEditorModalOpen: boolean;
  siteEditorSiteId: string | null;
  siteEditorCandidateDraft: SiteEditorCandidateDraft | null;
  horizonProfileEditorModalOpen: boolean;
  horizonProfileEditorSiteId: string | null;
};

export type SiteEditorCandidateDraft = {
  latitudeDeg: number;
  longitudeDeg: number;
  lightPollution: LightPollutionPointResult;
};

export type ProjectDetailTab = 'gallery' | 'import' | 'settings';
export type ProcessingDetailTab = 'overview' | 'logs' | 'settings';
export type SkyDetailTab = 'telescope' | 'mosaic';
export type ObservationCenterTab = 'sites' | 'conditions';

const OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT = 35;
const NIGHT_TIMELINE_REJECTION_REASONS = [
  'Not astronomical night',
  'Target below 30°',
  'Cloud cover above threshold',
  'Weather data unavailable',
  'Blocked by local horizon',
] as const;

type ConditionsRow = ConditionsJoinedRow<SiteForecastHour, AstronomicalConditionsHour>;

type ConditionsTimelinePoint = {
  index: number;
  /** UTC instant (ISO 8601 with Z) of this row; weather and astronomy in a row share it exactly. */
  timeText: string | null;
  timeMs: number | null;
  skyState: string | null;
  targetAltitudeDeg: number | null;
  targetAzimuthDeg: number | null;
  moonAltitudeDeg: number | null;
  moonAzimuthDeg: number | null;
  cloudCoverPct: number | null;
  moonIlluminationPct: number | null;
  moonTargetSeparationDeg: number | null;
  dewRisk: string | null;
  windSpeedKmh: number | null;
  windGustsKmh: number | null;
};

type ObservingWindowRange = {
  startIndex: number;
  endIndex: number;
  startMs: number;
  endMs: number;
};

export function renderAppShell(state: AppState): string {
  const appVersion = state.coreContent?.current_version ?? 'dev';
  const primaryProject = state.selectedProject ?? state.projects[0] ?? null;
  const latestRun = state.activeRun ?? state.projectRuns[0] ?? null;
  const telescopeState = state.telescopeSnapshot?.telescope_state ?? null;
  const apiStatusLabel = state.health?.status === 'ok' ? 'API online' : 'API offline';
  const apiStatusDetail = state.health
    ? `${state.health.service} | ${state.health.status} | ${getApiBaseUrl()} | ${state.health.default_siril_executable ?? 'siril-cli'}`
    : `API unavailable | ${getApiBaseUrl()}`;
  const activeProjectLabel = primaryProject?.slug ?? 'No active project';
  const activeProjectDetail = primaryProject
    ? [
        primaryProject.sky_target ?? 'No sky target',
        `${primaryProject.capture_count} capture${primaryProject.capture_count === 1 ? '' : 's'}`,
        `${primaryProject.run_count} run${primaryProject.run_count === 1 ? '' : 's'}`,
      ].join(' | ')
    : 'Select or create a project';
  const hardwareLabel = getHardwareLabel(telescopeState);
  const hardwareDetail = getHardwareDetail(telescopeState);

  return `
    <div class="app-frame" data-theme="${state.theme}">
      <header class="topbar">
        <div class="topbar__brand">
          <img class="topbar__icon" src="${iconUrl}" alt="TSN DSS icon" />
          <div>
            <h1 class="topbar__title">TSN | DSS</h1>
          </div>
        </div>
        <div class="topbar__status-group">
          <button class="status-pill status-pill--button" type="button" data-theme-toggle>
            <span class="status-pill__label">Theme</span>
            <strong>${state.theme === 'observation' ? 'Observation' : 'Dark'}</strong>
          </button>
          <div class="status-pill status-pill--interactive" title="${escapeHtml(hardwareDetail)}">
            <span class="status-pill__label">Hardware</span>
            <strong>${escapeHtml(hardwareLabel)}</strong>
            <span class="status-pill__tooltip">${escapeHtml(hardwareDetail)}</span>
          </div>
          <div class="status-pill status-pill--interactive" title="${escapeHtml(apiStatusDetail)}">
            <span class="status-pill__label">API</span>
            <strong>${apiStatusLabel}</strong>
            <span class="status-pill__tooltip">${escapeHtml(apiStatusDetail)}</span>
          </div>
          <div class="status-pill status-pill--interactive" title="${escapeHtml(activeProjectDetail)}">
            <span class="status-pill__label">Project</span>
            <strong>${escapeHtml(activeProjectLabel)}</strong>
            <span class="status-pill__tooltip">${escapeHtml(activeProjectDetail)}</span>
          </div>
        </div>
      </header>

      <div class="app-shell">
        <aside class="sidebar">
          <nav class="nav">
            ${renderNavItem('core', 'Core', state.currentView)}
            ${renderNavItem('projects', 'Projects', state.currentView)}
            ${renderNavItem('processing', 'Processing', state.currentView)}
            ${renderNavItem('sky', 'Sky', state.currentView)}
            ${renderNavItem('observationcenter', 'Observation Center', state.currentView)}
          </nav>

          <div class="sidebar-watermark" aria-hidden="true">
            <img class="sidebar-watermark__icon" src="${iconUrl}" alt="" />
          </div>
        </aside>

        <main class="workspace">
          ${renderView(state, primaryProject, latestRun)}
        </main>
      </div>

      <footer class="app-footer">
        <span>TSN DSS | TheStructureNavigator | ${appVersion}</span>
      </footer>

      ${state.createProjectModalOpen ? renderCreateProjectModal(state.busy) : ''}
      ${state.createRunModalOpen ? renderCreateRunModal(state, primaryProject, state.selectedProcessingCapture) : ''}
      ${state.createImportCaptureModalOpen ? renderImportCaptureModal(state, primaryProject, state.selectedProjectCapture) : ''}
      ${state.siteEditorModalOpen ? renderSiteEditorModal(state, state.sites.find((site) => site.id === state.siteEditorSiteId) ?? null) : ''}
      ${state.horizonProfileEditorModalOpen ? renderHorizonProfileEditorModal(state, state.sites.find((site) => site.id === state.horizonProfileEditorSiteId) ?? null) : ''}
      ${renderToast(state)}
    </div>
  `;
}

function renderNavItem(view: ViewName, label: string, currentView: ViewName): string {
  const selectedClass = currentView === view ? ' nav__item--active' : '';
  return `<button class="nav__item${selectedClass}" type="button" data-view="${view}">${label}</button>`;
}

function renderView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  if (state.currentView === 'core') {
    return renderCoreView(state, primaryProject, latestRun);
  }

  if (state.currentView === 'processing') {
    return renderProcessingView(state, primaryProject, latestRun);
  }

  if (state.currentView === 'observationcenter') {
    return renderObservationCenterView(state);
  }

  if (state.currentView === 'sky') {
    return renderSkyView(state, primaryProject, latestRun);
  }

  return renderProjectsView(state, primaryProject);
}

function renderCoreView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  const coreContent = state.coreContent ?? { current_version: 'dev', releases: [], todo: [] };
  const currentRelease =
    coreContent.releases.find((release) => release.version === coreContent.current_version)
    ?? coreContent.releases[0]
    ?? { version: coreContent.current_version, changelog: [] };
  const historicalReleases = coreContent.releases.filter((release) => release.version !== currentRelease.version);

  return `
    <section class="core-workspace">
      <section class="project-detail-stack">
        <div class="project-summary-strip">
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Version</span>
            <span class="project-summary-strip__value">${coreContent.current_version}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">API</span>
            <span class="project-summary-strip__value">${state.health?.status === 'ok' ? 'online' : 'offline'}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Active project</span>
            <span class="project-summary-strip__value">${escapeHtml(primaryProject?.slug ?? 'none')}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Latest run</span>
            <span class="project-summary-strip__value">${escapeHtml(latestRun?.status ?? 'none')}</span>
          </div>
        </div>
      </section>

      <section class="core-content-layout">
        <div class="core-content-main">
          <article class="panel">
            <div class="panel__header">
              <h3>Changelog</h3>
              <span>v${escapeHtml(coreContent.current_version)}</span>
            </div>
            <div class="release-stack">
              <details class="release-disclosure" open>
                <summary class="release-disclosure__summary">
                  <span>v${escapeHtml(currentRelease.version)}</span>
                  <span>${escapeHtml(currentRelease.date ?? 'Current')}</span>
                </summary>
                <div class="release-disclosure__body">
                  <ul class="core-list">
                    ${currentRelease.changelog.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
                  </ul>
                </div>
              </details>

              ${historicalReleases
                .map(
                  (release) => `
                    <details class="release-disclosure">
                      <summary class="release-disclosure__summary">
                        <span>v${escapeHtml(release.version)}</span>
                        <span>${escapeHtml(release.date ?? 'Release')}</span>
                      </summary>
                      <div class="release-disclosure__body">
                        <ul class="core-list">
                          ${release.changelog.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
                        </ul>
                      </div>
                    </details>
                  `,
                )
                .join('')}
            </div>
          </article>

          <article class="panel">
            <div class="panel__header">
              <h3>TODO</h3>
              <span>next</span>
            </div>
            <ul class="core-list">
              ${coreContent.todo.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
            </ul>
          </article>
        </div>

        <div class="core-watermark-surface" aria-hidden="true">
          <img class="core-watermark-surface__icon" src="${iconUrl}" alt="" />
        </div>
      </section>
    </section>
  `;
}

function renderProjectsView(state: AppState, primaryProject: ProjectSummary | null): string {
  return `
    <section class="projects-workspace">
      <div class="projects-toolbar">
        <span class="projects-toolbar__label">Projects:</span>
        ${renderProjectTabs(state.projects, primaryProject?.slug ?? null)}
        <button class="action-button projects-toolbar__create" type="button" data-open-create-project-modal ${state.busy ? 'disabled' : ''}>
          Create project
        </button>
      </div>

      <div class="projects-detail-column">
        ${renderSelectedProjectPanel(state, primaryProject)}
      </div>
    </section>
  `;
}

/**
 * Project workspace: left side owns selection, right side owns media/results.
 */
function renderSelectedProjectPanel(state: AppState, project: ProjectSummary | null): string {
  if (project === null) {
    return `
      <article class="panel project-detail-panel">
        <div class="panel__header">
          <h3>Select a project</h3>
          <span>waiting</span>
        </div>
        <p class="muted">
          Pick a project from the list to expand its details, import a capture and manage its target.
        </p>
      </article>
    `;
  }

  return `
    <section class="project-detail-shell">
      <div class="project-detail-stack">
        ${renderProjectSummaryStrip(project)}
      </div>

      <nav class="tab-strip project-detail-tabs" aria-label="Project details">
        ${renderProjectTab('gallery', 'Gallery', state.projectDetailTab)}
        ${renderProjectTab('import', 'Import capture', state.projectDetailTab)}
        ${renderProjectTab('settings', 'Project settings', state.projectDetailTab)}
      </nav>

      <div class="project-detail-stack">
        ${renderSelectedProjectTab(state, project)}
      </div>
    </section>
  `;
}

function renderProjectTab(tab: ProjectDetailTab, label: string, activeTab: ProjectDetailTab): string {
  const activeClass = tab === activeTab ? ' tab-strip__button--active' : '';
  return `<button class="tab-strip__button${activeClass}" type="button" data-project-tab="${tab}">${label}</button>`;
}

function renderSelectedProjectTab(state: AppState, project: ProjectSummary): string {
  if (state.projectDetailTab === 'settings') {
    return `
      <section class="detail-stack">
        <div class="panel__header panel__header--nested">
          <h3>Project settings</h3>
          <span>${project.slug}</span>
        </div>
        <div class="detail-stack">
          <p class="muted">Administrative actions for this project live here.</p>
          <div class="danger-zone">
            <div class="danger-zone__copy">
              <h4>Delete project</h4>
              <p class="muted">Remove the entire project with all captures, runs, logs and generated outputs.</p>
            </div>
            <button class="action-button action-button--danger" type="button" data-delete-project-slug="${project.slug}" ${state.busy ? 'disabled' : ''}>
              Delete project
            </button>
          </div>
        </div>
      </section>
    `;
  }

  if (state.projectDetailTab === 'gallery') {
    const selectedCaptureName =
      (state.selectedProjectCapture && project.capture_names.includes(state.selectedProjectCapture)
        ? state.selectedProjectCapture
        : null)
      ?? project.capture_names[0]
      ?? null;
    const runsForSelectedCapture = selectedCaptureName
      ? state.projectRuns.filter((run) => run.capture_name === selectedCaptureName)
      : state.projectRuns;
    const selectedProjectRun =
      (state.selectedProjectRunId
        ? runsForSelectedCapture.find((run) => run.id === state.selectedProjectRunId)
        : null)
      ?? runsForSelectedCapture[0]
      ?? null;
    return `
      <section class="detail-stack">
        ${renderProjectDetails(project, runsForSelectedCapture, selectedProjectRun, selectedCaptureName)}
      </section>
    `;
  }

  return `
    <section class="detail-stack">
      ${renderCaptureBrowser(
        project.capture_names,
        state.selectedProjectCapture,
        state.selectedProjectCaptureDetails,
        state.selectedProjectCaptureFolder,
        state.selectedProjectCaptureFile,
      )}
    </section>
  `;
}

/**
 * Processing workspace: capture selection -> run selection -> run details.
 */
function renderProcessingView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  if (primaryProject === null) {
    return `
      <section class="processing-workspace">
        <article class="panel">
          <div class="panel__header">
            <h3>Processing</h3>
          </div>
          <p class="muted">Select the active project in the Projects tab first, then come back here to run processing.</p>
        </article>
      </section>
    `;
  }

  const selectedCapture = state.selectedProcessingCapture;
  const runsForCapture = selectedCapture
    ? state.projectRuns.filter((run) => run.capture_name === selectedCapture)
    : [];
  const activeProcessingRun =
    latestRun !== null && latestRun.capture_name === selectedCapture ? latestRun : (runsForCapture[0] ?? null);

  return `
    <section class="processing-workspace">
      <section class="project-gallery-layout processing-layout-shell">
        <aside class="project-gallery-sidebar">
          <div class="project-gallery-sidebar__action">
            <button class="action-button" type="button" data-open-create-run-modal ${state.busy ? 'disabled' : ''}>
              Create run
            </button>
          </div>

          <section class="project-gallery-group">
            <div class="panel__header panel__header--nested">
              <h3>Captures</h3>
            </div>
            ${renderProcessingCaptureTabs(primaryProject.capture_names, selectedCapture)}
          </section>

          <section class="project-gallery-group">
            <div class="panel__header panel__header--nested">
              <h3>Runs</h3>
            </div>
            ${renderRunTabs(runsForCapture, activeProcessingRun?.id ?? null)}
          </section>
        </aside>

        <section class="project-gallery-main processing-main">
          <div class="project-detail-stack">
            ${renderProcessingSummaryStrip(activeProcessingRun)}

            <nav class="tab-strip project-detail-tabs" aria-label="Run details">
              ${renderProcessingTab('overview', 'Overview', state.processingDetailTab)}
              ${renderProcessingTab('logs', 'Logs', state.processingDetailTab)}
              ${renderProcessingTab('settings', 'Run settings', state.processingDetailTab)}
            </nav>

            <div class="project-detail-stack">
              ${renderRunDetailPanel(state.processingDetailTab, activeProcessingRun)}
            </div>
          </div>
        </section>
      </section>
    </section>
  `;
}

/**
 * Sky workspace keeps one shared Aladin surface and swaps the control panel
 * above it between telescope control and mosaic planning.
 */
function renderSkyView(state: AppState, primaryProject: ProjectSummary | null, _latestRun: ProjectRun | null): string {
  if (primaryProject === null) {
    return `
      <section class="sky-workspace">
        <article class="panel">
          <div class="panel__header">
            <h3>Sky</h3>
          </div>
          <p class="muted">Select the active project in the Projects tab first, then come back here to inspect the sky view.</p>
        </article>
      </section>
    `;
  }

  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;
  const selectedPanel =
    (state.selectedMosaicPanelId
      ? selectedMosaic?.panels.find((panel) => panel.id === state.selectedMosaicPanelId)
      : null)
    ?? (selectedMosaic?.selected_panel_id
      ? selectedMosaic.panels.find((panel) => panel.id === selectedMosaic.selected_panel_id)
      : null)
    ?? selectedMosaic?.panels[0]
    ?? null;

  return `
    <section class="sky-workspace">
      <nav class="tab-strip project-detail-tabs sky-detail-tabs" aria-label="Sky controls">
        ${renderSkyTab('telescope', 'Telescope control', state.skyDetailTab)}
        ${renderSkyTab('mosaic', 'Mosaic planner', state.skyDetailTab)}
      </nav>

      <section class="project-detail-stack">
        ${state.skyDetailTab === 'telescope'
          ? renderSkyTelescopeControls(
            primaryProject.sky_target,
            state.followTelescope,
            state.telescopeSnapshot,
            state.telescopeAdapters,
            state.activeTelescopeAdapterId,
          )
          : renderSkyMosaicSidebar(state, primaryProject, selectedMosaic)}
        ${renderSkyDetailPanel(selectedMosaic, selectedPanel)}
      </section>
    </section>
  `;
}

function renderSkyTab(tab: SkyDetailTab, label: string, activeTab: SkyDetailTab): string {
  const activeClass = tab === activeTab ? ' tab-strip__button--active' : '';
  return `<button class="tab-strip__button${activeClass}" type="button" data-sky-tab="${tab}">${label}</button>`;
}

function renderSkyDetailPanel(
  selectedMosaic: MosaicPlan | null,
  selectedPanel: MosaicPanel | null,
): string {
  return `
    <section class="detail-stack">
      ${renderSkyMosaicSummary(selectedMosaic, selectedPanel)}
      <article class="panel sky-panel sky-panel--full">
        <div id="aladin-sky-view" class="aladin-container"></div>
      </article>
    </section>
  `;
}

function renderSkyTelescopeControls(
  projectSkyTarget: string | null,
  followTelescope: boolean,
  telescopeSnapshot: TelescopeSnapshot | null,
  telescopeAdapters: TelescopeAdapterDescriptor[],
  activeAdapterId: string | null,
): string {
  const telescopeState = telescopeSnapshot?.telescope_state ?? null;
  const plannedPointing = telescopeSnapshot?.planned_pointing ?? null;
  const activeAdapter =
    telescopeAdapters.find((adapter) => adapter.adapter_id === activeAdapterId)
    ?? telescopeAdapters.find((adapter) => adapter.adapter_id === telescopeState?.adapter_id)
    ?? null;
  const adapterLabel = activeAdapter?.label || telescopeState?.source_kind || telescopeState?.adapter_id || 'unknown';
  const adapterMode = telescopeState?.is_simulated ? 'simulated' : 'live';
  const isEditable = telescopeState?.is_simulated ?? false;
  const capabilities = getSkyCapabilities(activeAdapter, telescopeState);
  const liveStatus = telescopeState?.status ?? 'unknown';
  const plannedStateLabel = plannedPointing ? 'ready' : 'empty';

  return `
    <article class="panel telescope-panel">
      <div class="panel__header">
        <h3>Telescope control</h3>
        <span>hardware-agnostic</span>
      </div>
      <div class="telescope-panel__summary">
        <div class="telescope-panel__meta">
          <div class="telescope-panel__meta-item">
            <span class="telescope-panel__meta-label">Adapter</span>
            <strong>${escapeHtml(adapterLabel)}</strong>
          </div>
          <div class="telescope-panel__meta-item">
            <span class="telescope-panel__meta-label">Mode</span>
            <strong>${escapeHtml(adapterMode)}</strong>
          </div>
          <div class="telescope-panel__meta-item">
            <span class="telescope-panel__meta-label">Control</span>
            <strong>${isEditable ? 'manual + telemetry' : 'telemetry only'}</strong>
          </div>
        </div>
        <div class="telescope-panel__capabilities">
          <span class="telescope-panel__meta-label">Capabilities</span>
          <div class="project-tags">${capabilities.map((value) => `<span>${escapeHtml(value)}</span>`).join('')}</div>
        </div>
      </div>
      <form class="telescope-adapter-form" data-form="telescope-adapter">
        <label class="field">
          <span>Active adapter</span>
          <select name="adapter_id">
            ${telescopeAdapters.map((adapter) => `
              <option value="${escapeHtml(adapter.adapter_id)}" ${adapter.adapter_id === (activeAdapterId ?? telescopeState?.adapter_id) ? 'selected' : ''}>
                ${escapeHtml(adapter.label)}
              </option>
            `).join('')}
          </select>
        </label>
        <button class="action-button telescope-panel__submit" type="submit">
          Switch adapter
        </button>
      </form>
      <section class="telescope-panel__section">
        <div class="telescope-panel__section-header">
          <div>
            <span class="telescope-panel__section-kicker">Live telescope</span>
            <strong class="telescope-panel__section-title">${escapeHtml(telescopeState?.target_name ?? projectSkyTarget ?? 'No current target')}</strong>
          </div>
          <span class="status-chip">${escapeHtml(liveStatus)}</span>
        </div>
        <div class="sky-simulator-state" data-sky-simulator-state></div>
      </section>
      <section class="telescope-panel__section">
        <div class="telescope-panel__section-header">
          <div>
            <span class="telescope-panel__section-kicker">Planned target</span>
            <strong class="telescope-panel__section-title">${escapeHtml(plannedPointing?.target_name ?? 'No planned target')}</strong>
          </div>
          <span class="status-chip status-chip--subtle">${escapeHtml(plannedStateLabel)}</span>
        </div>
        <div class="project-summary-strip project-summary-strip--sky">
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Source</span>
            <span class="project-summary-strip__value">${escapeHtml(plannedPointing?.source_kind ?? 'none')}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">RA</span>
            <span class="project-summary-strip__value">${plannedPointing ? formatRaDisplay(plannedPointing.ra_hours) : '—'}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Dec</span>
            <span class="project-summary-strip__value">${plannedPointing ? formatDecDisplay(plannedPointing.dec_deg) : '—'}</span>
          </div>
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Updated</span>
            <span class="project-summary-strip__value">${formatTimestampDisplay(plannedPointing?.updated_at_utc)}</span>
          </div>
        </div>
      </section>
      <div class="telescope-panel__actions">
        <label class="checkbox-field sky-follow-toggle">
          <input name="follow_telescope" type="checkbox" data-follow-telescope-toggle ${followTelescope ? 'checked' : ''} />
          <span>Center map on telescope</span>
        </label>
        <div class="telescope-panel__action-row">
          <button class="action-button" type="button" data-slew-to-planned-pointing ${plannedPointing ? '' : 'disabled'}>
            Slew to planned target
          </button>
          <button class="action-button action-button--secondary" type="button" data-clear-planned-pointing ${plannedPointing ? '' : 'disabled'}>
            Clear planned target
          </button>
        </div>
      </div>
      ${
        isEditable
          ? `
            <form class="sky-simulator-form" data-form="sky-simulator">
              <label class="field">
                <span>Target name</span>
                <input
                  name="target_name"
                  type="text"
                  value="${escapeHtml(projectSkyTarget ?? 'M42')}"
                  placeholder="M42"
                />
              </label>
              <label class="field">
                <span>RA (hours / hh mm ss)</span>
                <input name="ra_hours" type="text" placeholder="5.588 or 00 54 22.4" />
              </label>
              <label class="field">
                <span>Dec (deg / dd mm ss)</span>
                <input name="dec_deg" type="text" placeholder="-5.391 or +56 40 25.1" />
              </label>
              <label class="field">
                <span>Alt (deg)</span>
                <input name="alt_deg" type="text" placeholder="optional" />
              </label>
              <label class="field">
                <span>Az (deg)</span>
                <input name="az_deg" type="text" placeholder="optional" />
              </label>
              <label class="field">
                <span>Status</span>
                <select name="status">
                  <option value="">Keep current</option>
                  <option value="idle">idle</option>
                  <option value="slewing">slewing</option>
                  <option value="tracking">tracking</option>
                  <option value="parked">parked</option>
                </select>
              </label>
              <button class="action-button telescope-panel__submit" type="submit">Update telescope</button>
            </form>
            <p class="muted telescope-panel__helper">
              Simulator updates only move the abstract telescope state. Real hardware adapters can later map the same actions to device-specific commands.
            </p>
          `
          : `
            <div class="telescope-panel__readonly">
              <p class="muted">This adapter currently exposes telemetry in read-only mode. Manual updates stay available only for the simulator adapter.</p>
            </div>
          `
      }
    </article>
  `;
}

function renderObservationCenterView(state: AppState): string {
  const activeSite = state.telescopeSnapshot?.active_site ?? null;

  return `
    <section class="observation-center-workspace projects-workspace">
      <div class="projects-toolbar observation-center-sites-toolbar">
        <span class="projects-toolbar__label">Sites:</span>
        ${renderObservationSiteTabs(state.sites, state.activeSiteId)}
        <button class="action-button projects-toolbar__create" type="button" data-open-site-editor-modal>
          Create site
        </button>
      </div>

      <nav class="tab-strip project-detail-tabs" aria-label="Observation Center">
        ${renderObservationCenterTab('sites', 'Sites', state.observationCenterTab)}
        ${renderObservationCenterTab('conditions', 'Conditions', state.observationCenterTab)}
      </nav>

      <div class="project-detail-stack">
        ${state.observationCenterTab === 'conditions'
          ? renderObservationConditionsPanel(state, activeSite)
          : renderObservationSitesPanel(state, activeSite)}
      </div>
    </section>
  `;
}

function renderObservationCenterTab(
  tab: ObservationCenterTab,
  label: string,
  activeTab: ObservationCenterTab,
): string {
  const activeClass = tab === activeTab ? ' tab-strip__button--active' : '';
  return `<button class="tab-strip__button${activeClass}" type="button" data-observation-center-tab="${tab}">${label}</button>`;
}

function renderObservationSitesPanel(state: AppState, activeSite: Site | null): string {
  return `
    <article class="panel telescope-panel site-details-panel">
      <section class="site-details-layout">
        <div class="site-details-sidebar">
          <div class="panel__header panel__header--nested">
            <h3>Site details</h3>
          </div>
          <div class="site-detail-list">
            ${renderSiteDetailRow('Site name', activeSite?.name ?? 'No active site')}
            ${renderSiteDetailRow('Latitude', formatAngleValue(activeSite?.latitude_deg))}
            ${renderSiteDetailRow('Longitude', formatAngleValue(activeSite?.longitude_deg))}
            ${renderSiteDetailRow('Elevation', formatMeters(activeSite?.elevation_m))}
          </div>
          <section class="site-detail-section">
            <span class="site-detail-section__title">Measured / Manual</span>
            <div class="site-detail-list">
            ${renderSiteDetailRow('Measured SQM', activeSite?.sqm_mag_arcsec2 == null ? '—' : `${activeSite.sqm_mag_arcsec2.toFixed(2)} mag/arcsec²`)}
            ${renderSiteDetailRow('Manual Bortle', activeSite?.bortle_class == null ? '—' : `Class ${activeSite.bortle_class}`)}
            ${renderSiteDetailRow('Manual sky quality', formatSiteQuality(activeSite))}
            ${renderSiteDetailRow('South horizon', activeSite?.south_horizon_open ? 'open' : 'not marked open')}
            ${activeSite?.notes ? renderSiteDetailRow('Notes', activeSite.notes) : ''}
            </div>
          </section>
          ${renderSiteLightPollutionDetails(activeSite, state)}
          ${renderSiteLocalHorizonDetails(activeSite, state)}

          <div class="site-details-actions">
            <button class="action-button" type="button" data-open-site-editor-modal="${escapeHtml(activeSite?.id ?? '')}" ${activeSite ? '' : 'disabled'}>
              Edit active site
            </button>
            ${activeSite ? `<button class="action-button action-button--danger" type="button" data-delete-site-id="${escapeHtml(activeSite.id)}">Delete site</button>` : ''}
          </div>
        </div>

        <div class="site-details-map">
          <div class="panel__header panel__header--nested site-map-header">
            <h3>Site map</h3>
            <div class="site-map-controls" aria-label="Map overlays">
              <button
                class="site-map-controls__button"
                type="button"
                data-current-device-position
                ${state.currentDeviceLocating ? 'disabled' : ''}
              >
                ${state.currentDeviceLocating ? 'Locating device…' : 'Current position'}
              </button>
              <label class="site-map-controls__toggle">
                <input
                  type="checkbox"
                  data-light-pollution-toggle
                  ${state.lightPollutionOverlayEnabled ? 'checked' : ''}
                />
                <span>Light Pollution</span>
              </label>
              <label class="site-map-controls__opacity">
                <span>Opacity</span>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value="${state.lightPollutionOverlayOpacity}"
                  data-light-pollution-opacity
                  ${state.lightPollutionOverlayEnabled ? '' : 'disabled'}
                />
                <span data-light-pollution-opacity-value>${Math.round(state.lightPollutionOverlayOpacity * 100)}%</span>
              </label>
              <span class="site-map-controls__provider">ArtificialSkyBrightness</span>
            </div>
          </div>
          ${state.currentDeviceLocationStatus ? `<p class="site-map-status">${escapeHtml(state.currentDeviceLocationStatus)}</p>` : ''}
          <div id="observation-center-map" class="observation-map-container"></div>
        </div>
      </section>
    </article>
  `;
}

function renderSiteLocalHorizonDetails(site: Site | null, state: AppState): string {
  const profile = site?.horizon_profile ?? [];
  const normalizedProfile = profile.length ? normalizeLocalHorizonProfile(profile) : [];
  const minObstruction = normalizedProfile.length
    ? Math.min(...normalizedProfile.map((point) => point.min_altitude_deg))
    : null;
  const maxObstruction = normalizedProfile.length
    ? Math.max(...normalizedProfile.map((point) => point.min_altitude_deg))
    : null;

  return `
    <section class="site-detail-section">
      <div class="site-detail-section__header">
        <span class="site-detail-section__title">Local Horizon</span>
        ${site ? `
          <button class="action-button action-button--secondary site-detail-section__action" type="button" data-open-horizon-profile-editor="${escapeHtml(site.id)}" ${state.busy ? 'disabled' : ''}>
            Edit horizon profile
          </button>
        ` : ''}
      </div>
      <div class="site-detail-list">
        ${normalizedProfile.length
          ? `
            ${renderSiteDetailRow('Profile', `${normalizedProfile.length} points`)}
            ${renderSiteDetailRow('Min obstruction', `${minObstruction?.toFixed(0)}°`)}
            ${renderSiteDetailRow('Max obstruction', `${maxObstruction?.toFixed(0)}°`)}
          `
          : renderSiteDetailRow('Profile', site ? 'No local horizon profile' : '—')}
      </div>
      ${site && normalizedProfile.length ? `
        <button class="action-button action-button--danger site-detail-section__action site-detail-section__action--inline" type="button" data-clear-horizon-profile="${escapeHtml(site.id)}" ${state.busy ? 'disabled' : ''}>
          Clear profile
        </button>
      ` : ''}
    </section>
  `;
}

function renderSiteDetailRow(label: string, value: string): string {
  return `
    <div class="site-detail-row">
      <span class="site-detail-row__label">${escapeHtml(label)}</span>
      <span class="site-detail-row__value">${escapeHtml(value)}</span>
    </div>
  `;
}

function renderSiteLightPollutionDetails(site: Site | null, state: AppState): string {
  const isUpdating = site !== null && state.lpUpdatingSiteId === site.id;
  const canUpdate = site !== null && site.latitude_deg != null && site.longitude_deg != null && !isUpdating && !state.busy;
  const updateButton = site
    ? `
      <button
        class="action-button action-button--secondary site-detail-section__action"
        type="button"
        data-update-site-light-pollution="${escapeHtml(site.id)}"
        ${canUpdate ? '' : 'disabled'}
        title="${site.latitude_deg == null || site.longitude_deg == null ? 'Latitude and longitude are required.' : 'Refresh modeled light pollution data for this site.'}"
      >
        ${isUpdating ? 'Updating light pollution…' : 'Update light pollution data'}
      </button>
    `
    : '';
  const hasModeledData = site != null && (
    site.lp_artificial_brightness_mcd_m2 != null
    || site.lp_natural_sky_ratio != null
    || site.lp_estimated_total_brightness_mcd_m2 != null
    || site.lp_estimated_sqm_mag_arcsec2 != null
    || site.lp_estimated_bortle_class != null
    || site.lp_dataset_name != null
    || site.lp_source != null
  );

  if (!hasModeledData) {
    return `
      <section class="site-detail-section">
        <div class="site-detail-section__header">
          <span class="site-detail-section__title">Modeled / Estimated Light Pollution</span>
          ${updateButton}
        </div>
        ${renderSiteDetailRow('Modeled data', site ? 'No modeled light pollution data saved yet.' : '—')}
      </section>
    `;
  }

  return `
    <section class="site-detail-section">
      <div class="site-detail-section__header">
        <span class="site-detail-section__title">Modeled / Estimated Light Pollution</span>
        ${updateButton}
      </div>
      <div class="site-detail-list">
        ${renderSiteDetailRow('Estimated SQM', site.lp_estimated_sqm_mag_arcsec2 == null ? '—' : `${site.lp_estimated_sqm_mag_arcsec2.toFixed(2)} mag/arcsec² · estimated`)}
        ${renderSiteDetailRow('Estimated Bortle', site.lp_estimated_bortle_class == null ? '—' : `Class ${site.lp_estimated_bortle_class} · estimated`)}
        ${renderSiteDetailRow('Artificial brightness', formatMcdPerSquareMeter(site.lp_artificial_brightness_mcd_m2))}
        ${renderSiteDetailRow('Artificial / natural ratio', formatEstimatedMultiplier(site.lp_natural_sky_ratio))}
        ${renderSiteDetailRow('Estimated total brightness', formatEstimatedMcdPerSquareMeter(site.lp_estimated_total_brightness_mcd_m2))}
        ${renderSiteDetailRow('Dataset', site.lp_dataset_name ?? '—')}
        ${renderSiteDetailRow('Source', site.lp_source ?? site.lp_provider_name ?? '—')}
        ${renderSiteDetailRow('Data kind', site.lp_data_kind ?? 'modeled / estimated')}
        ${renderSiteDetailRow('Updated', formatTimestampDisplay(site.lp_updated_at))}
      </div>
    </section>
  `;
}

function renderObservationConditionsPanel(state: AppState, activeSite: Site | null): string {
  if (!activeSite) {
    return `
      <article class="panel">
        <p class="muted">Select an active site first to load observation conditions.</p>
      </article>
    `;
  }

  if (activeSite.latitude_deg == null || activeSite.longitude_deg == null) {
    return `
      <article class="panel">
        <p class="muted">The active site needs latitude and longitude before TSN DSS can load observation conditions.</p>
      </article>
    `;
  }

  const forecast = state.siteForecast;
  const astronomy = state.astronomicalConditions;
  const currentWeather = forecast?.current ?? null;
  const currentAstronomy = astronomy?.current ?? null;
  const timeZone = getConditionsTimeZone(forecast);
  if (!forecast && !astronomy) {
    return `
      <article class="panel">
        <p class="muted">Observation conditions are not available right now for this site.</p>
      </article>
    `;
  }

  return `
    ${renderTonightStoryLevel(state.nightStory)}

    <details class="panel conditions-detail-disclosure">
      <summary class="conditions-detail-disclosure__summary">
        <span>Detailed conditions</span>
        <span>${escapeHtml(activeSite.name)}</span>
      </summary>
      <div class="telescope-panel conditions-detail-disclosure__body">
      <div class="panel__header">
        <h3>Conditions</h3>
        <span>${escapeHtml(activeSite.name)}</span>
      </div>
      ${renderConditionsMetadata([
        ['Site', activeSite.name],
        ['Provider', [forecast?.provider, astronomy?.provider].filter(Boolean).join(' + ') || 'pending'],
        ['Timezone', timeZone ?? 'browser local (Site timezone unavailable)'],
        ['Updated', formatConditionsTimestamp(currentAstronomy?.time_utc ?? currentWeather?.time_utc ?? null, timeZone)],
      ])}
      <div class="conditions-grid conditions-grid--summary">
        ${renderConditionCard('Current sky', formatSkyState(currentAstronomy?.sky_state))}
        ${renderConditionCard('Target', currentAstronomy?.target?.target_name ?? 'Site only')}
        ${renderConditionCard('Dew risk', formatDewRisk(currentWeather?.dew_risk))}
        ${renderConditionCard('Clouds', formatWeatherValue(currentWeather?.cloud_cover_pct, '%'))}
        ${renderConditionCard('Wind', formatWind(currentWeather?.wind_speed_kmh, currentWeather?.wind_direction_deg))}
      </div>
      <div class="conditions-sections">
        ${renderConditionSection(
          'Sky darkness',
          [
            ['Astronomical night', formatTimeRange(currentAstronomy?.astronomical_night_start_utc, currentAstronomy?.astronomical_night_end_utc, timeZone)],
          ],
        )}
        ${renderAstronomicalTargetSummary(
          currentAstronomy?.target ?? null,
          forecast,
          astronomy,
          astronomy?.min_target_altitude_deg ?? 30,
          state.selectedObservingWindowIndex,
          activeSite.horizon_profile,
        )}
        ${renderConditionSection(
          'Weather',
          [
            ['Temperature', formatWeatherValue(currentWeather?.temperature_c, '°C')],
            ['Humidity', formatWeatherValue(currentWeather?.relative_humidity_pct, '%')],
            ['Dew point', formatWeatherValue(currentWeather?.dew_point_c, '°C')],
            ['Dew margin', formatWeatherValue(currentWeather?.dew_margin_c, '°C')],
            ['Visibility', formatDistanceMeters(currentWeather?.visibility_m)],
            ['Surface pressure', formatWeatherValue(currentWeather?.surface_pressure_hpa, 'hPa')],
            ['Gusts', formatWeatherValue(currentWeather?.wind_gusts_kmh, 'km/h')],
            ['Precipitation', formatWeatherValue(currentWeather?.precipitation_mm, 'mm')],
            ['Precip chance', formatWeatherValue(currentWeather?.precipitation_probability_pct, '%')],
            ['Conditions', formatConditionSummary(currentWeather?.condition_code, currentWeather?.is_day)],
          ],
        )}
      </div>
      </div>
    </details>

    <details class="panel conditions-detail-disclosure">
      <summary class="conditions-detail-disclosure__summary">
        <span>Timeline and hourly detail</span>
        <span>${Math.max(forecast?.hourly.length ?? 0, astronomy?.hourly.length ?? 0)} points</span>
      </summary>
      <div class="conditions-detail-disclosure__body">
      <div class="panel__header panel__header--conditions">
        <div>
          <h3>Tonight detail</h3>
          <span>${Math.max(forecast?.hourly.length ?? 0, astronomy?.hourly.length ?? 0)} points</span>
        </div>
      </div>
      ${renderConditionsTimeline(state, forecast, astronomy)}
      ${renderConditionsHourlyTable(state, forecast, astronomy)}
      </div>
    </details>
  `;
}

function renderAstronomicalTargetSummary(
  target: AstronomicalConditionsSnapshot['current']['target'] | null,
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
  minTargetAltitudeDeg: number,
  selectedObservingWindowIndex: number | null,
  localHorizonProfile: LocalHorizonPoint[],
): string {
  if (!target) {
    return renderConditionSection('Target visibility', [
      ['Target', 'Select a planned pointing, mosaic panel, or project sky target to see target-specific conditions.'],
    ]);
  }

  const timeZone = getConditionsTimeZone(forecast);
  const points = buildConditionsTimelinePoints(forecast, astronomy);
  const observingWindows = getObservingWindows(points, minTargetAltitudeDeg, localHorizonProfile);
  const observingWindowIndex = getActiveObservingWindowIndex(observingWindows, selectedObservingWindowIndex);
  const observingWindow = observingWindowIndex != null ? observingWindows[observingWindowIndex] ?? null : null;
  const compassPoints = getHorizonCompassTimelinePoints(points, observingWindow, timeZone);
  const currentLocalHorizon = analyzeLocalHorizonVisibility(localHorizonProfile, {
    targetAltitudeDeg: target.altitude_deg,
    targetAzimuthDeg: target.azimuth_deg,
  });

  return `
    <section class="conditions-section">
      <div class="panel__header panel__header--nested conditions-section__header">
        <h3>Target visibility</h3>
      </div>
      <div class="conditions-target-layout">
        <div class="conditions-grid conditions-grid--target-visibility">
          ${renderConditionCard('Target', target.target_name ?? 'Target')}
          ${renderConditionCard('Source', target.source_kind ?? 'target')}
          ${renderConditionCard('Alt / az', `${formatAngleValue(target.altitude_deg)} / ${formatAngleValue(target.azimuth_deg)}`)}
          ${renderConditionCard('Airmass', formatAirmass(target.airmass))}
          ${renderConditionCard('Transit', formatConditionsTimestamp(target.transit_time_utc, timeZone))}
          ${renderConditionCard('Max altitude', formatAngleValue(target.max_altitude_deg))}
          ${renderConditionCard('Above horizon', formatObservationFlag(target.above_horizon))}
          ${renderConditionCard(`Above ${minTargetAltitudeDeg.toFixed(0)}°`, formatObservationFlag(target.above_observation_threshold))}
          ${renderConditionCard('Night horizon window', formatWindowSummary(target.above_horizon_window_start_utc, target.above_horizon_window_end_utc, target.above_horizon_window_status, timeZone))}
          ${renderConditionCard('Local horizon', formatLocalHorizonAltitude(currentLocalHorizon))}
          ${renderConditionCard('Clearance', formatLocalHorizonClearance(currentLocalHorizon))}
          ${renderConditionCard('Local visibility', formatLocalHorizonVisibility(currentLocalHorizon))}
        </div>
        ${renderHorizonCompass(target.azimuth_deg, target.altitude_deg, compassPoints, observingWindow, localHorizonProfile, currentLocalHorizon, timeZone)}
      </div>
    </section>
  `;
}

function getHorizonCompassTimelinePoints(
  points: ConditionsTimelinePoint[],
  observingWindow: ObservingWindowRange | null,
  timeZone: string | undefined,
): ConditionsTimelinePoint[] {
  if (!observingWindow) {
    return points.slice(0, 24);
  }

  const windowStartPoint = points[observingWindow.startIndex] ?? null;
  if (windowStartPoint?.timeMs == null || !Number.isFinite(windowStartPoint.timeMs)) {
    return points.slice(Math.max(0, observingWindow.startIndex - 12), observingWindow.endIndex + 13);
  }

  const windowDay = localDateKey(windowStartPoint.timeMs, timeZone);
  const sameDayPoints = points.filter((point) => (
    point.timeMs != null
    && Number.isFinite(point.timeMs)
    && localDateKey(point.timeMs, timeZone) === windowDay
  ));

  return sameDayPoints.length > 0
    ? sameDayPoints
    : points.slice(Math.max(0, observingWindow.startIndex - 12), observingWindow.endIndex + 13);
}

/**
 * Weather and astronomy rows are joined by exact UTC instant (see conditions_time.ts),
 * never by array position or wall-clock label.
 */
function joinConditionsRows(
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): ConditionsRow[] {
  return joinConditionsByInstant(forecast?.hourly, astronomy?.hourly);
}

function getConditionsTimeZone(forecast: SiteForecastSnapshot | null): string | undefined {
  return resolveDisplayTimeZone(forecast?.timezone);
}

function buildConditionsTimelinePoints(
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): ConditionsTimelinePoint[] {
  return joinConditionsRows(forecast, astronomy).map((row) => {
    const weatherHour = row.weather;
    const astronomyHour = row.astronomy;
    return {
      index: row.index,
      timeText: row.timeUtc,
      timeMs: row.instantMs,
      skyState: astronomyHour?.sky_state ?? null,
      targetAltitudeDeg: astronomyHour?.target_altitude_deg ?? null,
      targetAzimuthDeg: astronomyHour?.target_azimuth_deg ?? null,
      moonAltitudeDeg: astronomyHour?.moon_altitude_deg ?? null,
      moonAzimuthDeg: astronomyHour?.moon_azimuth_deg ?? null,
      cloudCoverPct: weatherHour?.cloud_cover_pct ?? null,
      moonIlluminationPct: astronomyHour?.moon_illumination_pct ?? null,
      moonTargetSeparationDeg: astronomyHour?.moon_target_separation_deg ?? null,
      dewRisk: weatherHour?.dew_risk ?? null,
      windSpeedKmh: weatherHour?.wind_speed_kmh ?? null,
      windGustsKmh: weatherHour?.wind_gusts_kmh ?? null,
    };
  });
}

function isObservingWindowPoint(
  point: ConditionsTimelinePoint,
  minTargetAltitudeDeg: number,
  localHorizonProfile: LocalHorizonPoint[] = [],
): boolean {
  return isLocalHorizonObservingWindowPoint(
    point,
    minTargetAltitudeDeg,
    OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT,
    localHorizonProfile,
  );
}

function getObservingWindows(
  points: ConditionsTimelinePoint[],
  minTargetAltitudeDeg: number,
  localHorizonProfile: LocalHorizonPoint[] = [],
): ObservingWindowRange[] {
  return getLocalHorizonObservingWindows(
    points,
    minTargetAltitudeDeg,
    OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT,
    localHorizonProfile,
  );
}

function getActiveObservingWindowIndex(
  observingWindows: ObservingWindowRange[],
  selectedObservingWindowIndex: number | null,
): number | null {
  const defaultObservingWindowIndex = observingWindows
    .map((window, index) => ({ window, index }))
    .sort((leftWindow, rightWindow) => {
      const leftDuration = leftWindow.window.endMs - leftWindow.window.startMs;
      const rightDuration = rightWindow.window.endMs - rightWindow.window.startMs;
      if (rightDuration !== leftDuration) {
        return rightDuration - leftDuration;
      }
      return leftWindow.window.startMs - rightWindow.window.startMs;
    })[0]?.index ?? null;

  return selectedObservingWindowIndex != null && observingWindows[selectedObservingWindowIndex]
    ? selectedObservingWindowIndex
    : defaultObservingWindowIndex;
}

function getConditionsTableRowClass(
  point: ConditionsTimelinePoint | null,
  observingWindow: ObservingWindowRange | null,
  minTargetAltitudeDeg: number,
  localHorizonProfile: LocalHorizonPoint[] = [],
): string {
  const classes = ['weather-table__row'];
  if (!point) {
    return classes.join(' ');
  }

  const inActiveObservingWindow = observingWindow != null
    && point.index >= observingWindow.startIndex
    && point.index <= observingWindow.endIndex;

  if (inActiveObservingWindow) {
    classes.push('weather-table__row--observing');
    return classes.join(' ');
  }

  switch (point.skyState) {
    case 'day':
      classes.push('weather-table__row--day');
      break;
    case 'civil_twilight':
      classes.push('weather-table__row--civil');
      break;
    case 'nautical_twilight':
      classes.push('weather-table__row--nautical');
      break;
    case 'astronomical_twilight':
      classes.push('weather-table__row--astronomical-twilight');
      break;
    case 'astronomical_night':
      classes.push('weather-table__row--night');
      if (!isObservingWindowPoint(point, minTargetAltitudeDeg, localHorizonProfile)) {
        classes.push('weather-table__row--limited');
      }
      break;
    default:
      break;
  }

  return classes.join(' ');
}

function renderConditionsTimeline(
  state: AppState,
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): string {
  const points = buildConditionsTimelinePoints(forecast, astronomy);
  const rowCount = points.length;
  if (rowCount < 2) {
    return '';
  }

  const currentAstronomy = astronomy?.current ?? null;
  const minTargetAltitudeDeg = astronomy?.min_target_altitude_deg ?? 30;
  const activeSite = state.sites.find((site) => site.id === state.activeSiteId) ?? null;
  const localHorizonProfile = activeSite?.horizon_profile ?? [];
  const timeZone = getConditionsTimeZone(forecast);

  const validTimes = points
    .map((point) => point.timeMs)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const startMs = validTimes[0] ?? 0;
  const endMs = validTimes[validTimes.length - 1] ?? Math.max(1, rowCount - 1);
  const spanMs = Math.max(endMs - startMs, 1);

  const width = 960;
  const height = 380;
  const left = 64;
  const right = 24;
  const top = 30;
  const altitudeTop = top;
  const altitudeHeight = 134;
  const cloudTop = 188;
  const cloudHeight = 56;
  const axisBottom = 268;
  const innerWidth = width - left - right;
  const minAltitude = -18;
  const maxAltitude = 90;

  const xAtPoint = (point: { index: number; timeMs: number | null }): number => {
    if (point.timeMs != null && validTimes.length >= 2) {
      return left + ((point.timeMs - startMs) / spanMs) * innerWidth;
    }
    return left + (point.index / Math.max(rowCount - 1, 1)) * innerWidth;
  };

  const xForTime = (value: string | null | undefined): number | null => {
    const ms = parseInstantMs(value);
    if (ms == null || ms < startMs || ms > endMs) {
      return null;
    }
    return left + ((ms - startMs) / spanMs) * innerWidth;
  };

  const altitudeY = (value: number): number => {
    const clamped = Math.max(minAltitude, Math.min(maxAltitude, value));
    return altitudeTop + ((maxAltitude - clamped) / (maxAltitude - minAltitude)) * altitudeHeight;
  };

  const cloudY = (value: number): number => {
    const clamped = Math.max(0, Math.min(100, value));
    return cloudTop + ((100 - clamped) / 100) * cloudHeight;
  };

  const stateColor = (value: string | null): string => {
    switch (value) {
      case 'day':
        return 'rgba(124, 156, 198, 0.16)';
      case 'civil_twilight':
        return 'rgba(132, 122, 180, 0.22)';
      case 'nautical_twilight':
        return 'rgba(84, 98, 156, 0.28)';
      case 'astronomical_twilight':
        return 'rgba(54, 62, 108, 0.36)';
      case 'astronomical_night':
        return 'rgba(8, 12, 24, 0.72)';
      default:
        return 'rgba(255, 255, 255, 0.03)';
    }
  };

  const observingFlags = points.map((point) => isObservingWindowPoint(point, minTargetAltitudeDeg, localHorizonProfile));

  const rejectionReasonByPoint = points.map((point) => {
    const reasons: string[] = [];
    if (point.skyState !== 'astronomical_night') {
      reasons.push('Not astronomical night');
    }
    if (
      point.targetAltitudeDeg == null
      || !Number.isFinite(point.targetAltitudeDeg)
      || point.targetAltitudeDeg < minTargetAltitudeDeg
    ) {
      reasons.push(`Target below ${minTargetAltitudeDeg.toFixed(0)}°`);
    }
    if (point.cloudCoverPct == null || !Number.isFinite(point.cloudCoverPct)) {
      // Same rule as before (no cloud value never qualifies), but reported truthfully.
      reasons.push('Weather data unavailable');
    } else if (point.cloudCoverPct > OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT) {
      reasons.push('Cloud cover above threshold');
    }
    if (isBlockedByLocalHorizon(localHorizonProfile, {
      targetAltitudeDeg: point.targetAltitudeDeg,
      targetAzimuthDeg: point.targetAzimuthDeg,
    })) {
      reasons.push('Blocked by local horizon');
    }
    return reasons;
  });

  const observingWindows = getObservingWindows(points, minTargetAltitudeDeg, localHorizonProfile);
  const observingWindowIndex = getActiveObservingWindowIndex(observingWindows, state.selectedObservingWindowIndex);
  const observingWindow = observingWindowIndex != null
    ? observingWindows[observingWindowIndex] ?? null
    : null;
  const observingWindowPoints = observingWindow
    ? points.slice(observingWindow.startIndex, observingWindow.endIndex + 1)
    : [];
  const potentialWindowsWithoutLocalHorizon = localHorizonProfile.length
    ? getObservingWindows(points, minTargetAltitudeDeg, [])
    : [];
  const noWindowLocalHorizonReason = localHorizonProfile.length > 0
    && observingWindows.length === 0
    && potentialWindowsWithoutLocalHorizon.length > 0
    ? 'Target is blocked by the local horizon during the usable period.'
    : null;

  const buildLinePath = (
    selector: (point: typeof points[number]) => number | null,
    yMapper: (value: number) => number,
  ): string => {
    let path = '';
    let open = false;
    for (const point of points) {
      const value = selector(point);
      if (value == null || !Number.isFinite(value)) {
        open = false;
        continue;
      }
      const x = xAtPoint(point);
      const y = yMapper(value);
      path += `${open ? ' L' : 'M'} ${x.toFixed(2)} ${y.toFixed(2)}`;
      open = true;
    }
    return path;
  };

  const backgroundSegments = points
    .map((point, index) => {
      const nextPoint = points[index + 1];
      const x1 = xAtPoint(point);
      const x2 = nextPoint ? xAtPoint(nextPoint) : left + innerWidth;
      return `<rect x="${x1.toFixed(2)}" y="${top}" width="${Math.max(1, x2 - x1).toFixed(2)}" height="${cloudTop + cloudHeight - top}" fill="${stateColor(point.skyState)}" />`;
    })
    .join('');

  const observingWindowOverlay = observingWindow
    ? (() => {
        const startPoint = points[observingWindow.startIndex];
        const endPoint = points[Math.min(observingWindow.endIndex + 1, points.length - 1)] ?? points[observingWindow.endIndex];
        const x1 = xAtPoint(startPoint);
        const x2 = Math.max(x1 + 8, xAtPoint(endPoint));
        return `
          <rect
            class="conditions-timeline__observing-window"
            x="${x1.toFixed(2)}"
            y="${top}"
            width="${Math.max(8, x2 - x1).toFixed(2)}"
            height="${(axisBottom - top).toFixed(2)}"
          />
          <text
            class="conditions-timeline__observing-label"
            x="${((x1 + x2) / 2).toFixed(2)}"
            y="${(top + 14).toFixed(2)}"
            text-anchor="middle"
          >Observing window</text>
        `;
      })()
    : '';

  const targetPath = buildLinePath((point) => point.targetAltitudeDeg, altitudeY);
  const moonPath = buildLinePath((point) => point.moonAltitudeDeg, altitudeY);
  const cloudBarWidth = Math.max(6, innerWidth / rowCount - 2);
  const cloudBars = points
    .map((point) => {
      if (point.cloudCoverPct == null || !Number.isFinite(point.cloudCoverPct)) {
        return '';
      }
      const x = xAtPoint(point) - cloudBarWidth / 2;
      const y = cloudY(point.cloudCoverPct);
      return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${cloudBarWidth.toFixed(2)}" height="${(cloudTop + cloudHeight - y).toFixed(2)}" rx="1" fill="rgba(203, 228, 255, 0.18)" />`;
    })
    .join('');

  const rejectionBands = NIGHT_TIMELINE_REJECTION_REASONS
    .map((reason, reasonIndex) => {
      const y = axisBottom + 34 + reasonIndex * 16;
      const height = 10;
      const segments = points
        .map((point, index) => {
          if (observingFlags[index] || !rejectionReasonByPoint[index].includes(reason)) {
            return '';
          }
          const nextPoint = points[index + 1];
          const x1 = xAtPoint(point);
          const x2 = nextPoint ? xAtPoint(nextPoint) : left + innerWidth;
          return `<rect class="conditions-timeline__rejection-block" x="${x1.toFixed(2)}" y="${y.toFixed(2)}" width="${Math.max(1, x2 - x1).toFixed(2)}" height="${height}" rx="1" />`;
        })
        .join('');

      return `
        <text class="conditions-timeline__rejection-label" x="${left - 8}" y="${(y + 8).toFixed(2)}" text-anchor="end">${escapeHtml(reason)}</text>
        ${segments}
      `;
    })
    .join('');

  const guideLines = [60, minTargetAltitudeDeg, 0]
    .map((value) => {
      const y = altitudeY(value);
      const className = value === minTargetAltitudeDeg ? 'conditions-timeline__threshold' : 'conditions-timeline__guide';
      return `
        <line class="${className}" x1="${left}" y1="${y.toFixed(2)}" x2="${(left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" />
        <text class="conditions-timeline__axis-label" x="${left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${value.toFixed(0)}°</text>
      `;
    })
    .join('');

  const cloudGuides = [100, 50, 0]
    .map((value) => {
      const y = cloudY(value);
      return `
        <line class="conditions-timeline__cloud-guide" x1="${left}" y1="${y.toFixed(2)}" x2="${(left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" />
        <text class="conditions-timeline__cloud-label" x="${left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${value}%</text>
      `;
    })
    .join('');

  const astronomicalNightEvents: Array<[string, number | null]> = [
    ['Night start', xForTime(currentAstronomy?.astronomical_night_start_utc)],
    ['Night end', xForTime(currentAstronomy?.astronomical_night_end_utc)],
  ];
  const astronomicalNightLines = astronomicalNightEvents
    .map(([label, x], index) => {
      if (x == null) {
        return '';
      }
      const clampedX = Math.max(left + 34, Math.min(left + innerWidth - 34, x));
      const textAnchor = index === 0 ? 'start' : 'end';
      const textX = index === 0 ? clampedX + 6 : clampedX - 6;
      return `
        <line class="conditions-timeline__night-boundary" x1="${x.toFixed(2)}" y1="${top}" x2="${x.toFixed(2)}" y2="${axisBottom}" />
        <text class="conditions-timeline__event-label conditions-timeline__event-label--night" x="${textX.toFixed(2)}" y="16" text-anchor="${textAnchor}">${escapeHtml(label)}</text>
      `;
    })
    .join('');

  const moonEvents: Array<[string, number | null]> = [
    ['Moonrise', xForTime(currentAstronomy?.moonrise_utc)],
    ['Moonset', xForTime(currentAstronomy?.moonset_utc)],
  ];
  const moonEventLines = moonEvents
    .map(([label, x], index) => {
      if (x == null) {
        return '';
      }
      const clampedX = Math.max(left + 34, Math.min(left + innerWidth - 34, x));
      const stackedY = axisBottom + 16 + index * 14;
      return `
        <line class="conditions-timeline__moon-event" x1="${x.toFixed(2)}" y1="${top}" x2="${x.toFixed(2)}" y2="${axisBottom}" />
        <text class="conditions-timeline__event-label conditions-timeline__event-label--moon" x="${clampedX.toFixed(2)}" y="${stackedY.toFixed(2)}" text-anchor="middle">${escapeHtml(label)}</text>
      `;
    })
    .join('');

  const tickStep = Math.max(1, Math.ceil((rowCount - 1) / 6));
  const tickIndexes = Array.from({ length: rowCount }, (_, index) => index)
    .filter((index) => index === 0 || index === rowCount - 1 || index % tickStep === 0);
  const axisTicks = tickIndexes
    .map((index, tickIndex) => {
      const point = points[index];
      const x = xAtPoint(point);
      const labelY = axisBottom + 18 + (tickIndex % 2) * 12;
      return `
        <line class="conditions-timeline__tick" x1="${x.toFixed(2)}" y1="${axisBottom}" x2="${x.toFixed(2)}" y2="${(axisBottom + 5).toFixed(2)}" />
        <text class="conditions-timeline__tick-label" x="${x.toFixed(2)}" y="${labelY.toFixed(2)}" text-anchor="middle">${escapeHtml(formatTimelineTick(point.timeText, timeZone))}</text>
      `;
    })
    .join('');

  const rejectionSummary = NIGHT_TIMELINE_REJECTION_REASONS
    .map((reason) => {
      const count = rejectionReasonByPoint.filter((reasons, index) => !observingFlags[index] && reasons.includes(reason)).length;
      if (count === 0) {
        return '';
      }
      return `<span class="conditions-timeline__factor-item">${escapeHtml(reason)}</span>`;
    })
    .filter(Boolean)
    .join('');
  const observingWindowButtons = observingWindows.length
    ? `
      <div class="conditions-window-selector">
        <span class="conditions-window-selector__label">Windows</span>
        <div class="conditions-window-selector__list">
          ${observingWindows.map((window, index) => {
            const activeClass = index === observingWindowIndex ? ' conditions-window-selector__button--active' : '';
            const startLabel = points[window.startIndex]?.timeText ?? null;
            const endLabel = points[Math.min(window.endIndex + 1, points.length - 1)]?.timeText
              ?? points[window.endIndex]?.timeText
              ?? null;
            return `
              <button
                class="conditions-window-selector__button${activeClass}"
                type="button"
                data-observing-window-index="${index}"
              >
                Window ${index + 1} · ${escapeHtml(formatObservingWindowRange(startLabel, endLabel, timeZone))}
              </button>
            `;
          }).join('')}
        </div>
      </div>
    `
    : '';

  return `
    <section class="conditions-timeline">
      <div class="conditions-timeline__legend">
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--day"></span>Day → twilight → night</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--target"></span>Target altitude</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--threshold"></span>${minTargetAltitudeDeg.toFixed(0)}° threshold</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--clouds"></span>Cloud cover</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--moon"></span>Moon altitude</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--night-boundary"></span>Night start / end</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--observing-window"></span>Observing window</span>
        <span class="conditions-timeline__legend-item"><span class="conditions-timeline__swatch conditions-timeline__swatch--rejection"></span>Limiting factors</span>
      </div>
      <div class="conditions-timeline__chart">
        <svg class="conditions-timeline__svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Timeline of darkness, target altitude, moon altitude, cloud cover and observing window for the selected forecast range.">
          <rect x="${left}" y="${top}" width="${innerWidth}" height="${cloudTop + cloudHeight - top}" fill="rgba(255,255,255,0.01)" />
          ${backgroundSegments}
          ${observingWindowOverlay}
          ${guideLines}
          ${cloudGuides}
          <text class="conditions-timeline__track-label" x="12" y="${(altitudeTop + 14).toFixed(2)}">Altitude</text>
          <text class="conditions-timeline__track-label" x="12" y="${(cloudTop + 14).toFixed(2)}">Cloud cover</text>
          <rect x="${left}" y="${cloudTop}" width="${innerWidth}" height="${cloudHeight}" fill="rgba(255,255,255,0.015)" />
          ${cloudBars}
          ${astronomicalNightLines}
          ${moonEventLines}
          ${targetPath ? `<path class="conditions-timeline__line conditions-timeline__line--target" d="${targetPath}" />` : ''}
          ${moonPath ? `<path class="conditions-timeline__line conditions-timeline__line--moon" d="${moonPath}" />` : ''}
          <line class="conditions-timeline__axis" x1="${left}" y1="${axisBottom}" x2="${(left + innerWidth).toFixed(2)}" y2="${axisBottom}" />
          ${axisTicks}
          ${rejectionBands}
        </svg>
      </div>
      <div class="conditions-timeline__summary">
        <span class="conditions-timeline__summary-label">Observing window</span>
        <strong class="conditions-timeline__summary-value">
          ${escapeHtml(
            observingWindow
              ? formatObservingWindowRange(
                  points[observingWindow.startIndex]?.timeText ?? null,
                  points[Math.min(observingWindow.endIndex + 1, points.length - 1)]?.timeText
                    ?? points[observingWindow.endIndex]?.timeText
                    ?? null,
                  timeZone,
                )
              : 'No observing window',
          )}
        </strong>
        <span class="conditions-timeline__summary-hint">
          target ≥ ${minTargetAltitudeDeg.toFixed(0)}° · clouds ≤ ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%${localHorizonProfile.length ? ' · clear of local horizon' : ''}
        </span>
      </div>
      ${observingWindowButtons}
      ${renderObservingWindowObservationContext(state, astronomy)}
      ${renderObservingWindowConditions(observingWindowPoints, getObservationIntentContext(state), noWindowLocalHorizonReason)}
      <div class="conditions-timeline__factors">
        <span class="conditions-timeline__factors-label">Limiting factors</span>
        ${rejectionSummary || '<span class="conditions-timeline__factors-empty">None inside the visible range.</span>'}
      </div>
    </section>
  `;
}

function renderConditionsHourlyTable(
  state: AppState,
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): string {
  const rows = joinConditionsRows(forecast, astronomy);
  const points = buildConditionsTimelinePoints(forecast, astronomy);
  const rowCount = rows.length;
  if (rowCount === 0) {
    return '<p class="muted">No hourly observation points are available for this site yet.</p>';
  }
  const timeZone = getConditionsTimeZone(forecast);

  const minTargetAltitudeDeg = astronomy?.min_target_altitude_deg ?? 30;
  const activeSite = state.sites.find((site) => site.id === state.activeSiteId) ?? null;
  const localHorizonProfile = activeSite?.horizon_profile ?? [];
  const observingWindows = getObservingWindows(points, minTargetAltitudeDeg, localHorizonProfile);
  const observingWindowIndex = getActiveObservingWindowIndex(observingWindows, state.selectedObservingWindowIndex);
  const activeObservingWindow = observingWindowIndex != null ? observingWindows[observingWindowIndex] ?? null : null;

  return `
    <div class="weather-table-wrap">
      <table class="weather-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Sky</th>
            <th>Sun alt</th>
            <th>Moon alt</th>
            <th>Moon</th>
            <th>Temp</th>
            <th>Dew risk</th>
            <th>Total</th>
            <th>High</th>
            <th>Wind</th>
            <th>Target alt</th>
            <th>Local horizon</th>
            <th>Clearance</th>
            <th>Airmass</th>
            <th>Moon sep</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row, index) => {
            const weatherHour = row.weather;
            const astronomyHour = row.astronomy;
            const point = points[index] ?? null;
            const rowClass = getConditionsTableRowClass(point, activeObservingWindow, minTargetAltitudeDeg, localHorizonProfile);
            const localHorizon = analyzeLocalHorizonVisibility(localHorizonProfile, {
              targetAltitudeDeg: astronomyHour?.target_altitude_deg,
              targetAzimuthDeg: astronomyHour?.target_azimuth_deg,
            });
            return `
            <tr class="${rowClass}">
              <td>${escapeHtml(formatConditionsTimestamp(row.timeUtc, timeZone))}</td>
              <td>${escapeHtml(astronomyHour ? formatSkyState(astronomyHour.sky_state) : formatConditionSummary(weatherHour?.condition_code, weatherHour?.is_day))}</td>
              <td>${escapeHtml(formatAngleValue(astronomyHour?.sun_altitude_deg))}</td>
              <td>${escapeHtml(formatAngleValue(astronomyHour?.moon_altitude_deg))}</td>
              <td>${escapeHtml(formatPercent(astronomyHour?.moon_illumination_pct))}</td>
              <td>${escapeHtml(formatWeatherValue(weatherHour?.temperature_c, '°C'))}</td>
              <td>${escapeHtml(formatDewRisk(weatherHour?.dew_risk))}</td>
              <td>${escapeHtml(formatWeatherValue(weatherHour?.cloud_cover_pct, '%'))}</td>
              <td>${escapeHtml(formatWeatherValue(weatherHour?.cloud_cover_high_pct, '%'))}</td>
              <td>${escapeHtml(formatWind(weatherHour?.wind_speed_kmh, weatherHour?.wind_direction_deg))}</td>
              <td>${escapeHtml(formatAngleValue(astronomyHour?.target_altitude_deg))}</td>
              <td>${escapeHtml(formatLocalHorizonAltitude(localHorizon))}</td>
              <td>${escapeHtml(formatLocalHorizonClearance(localHorizon))}</td>
              <td>${escapeHtml(formatAirmass(astronomyHour?.target_airmass))}</td>
              <td>${escapeHtml(formatAngleValue(astronomyHour?.moon_target_separation_deg))}</td>
            </tr>
          `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderObservationSiteTabs(sites: Site[], activeSiteId: string | null): string {
  if (!sites.length) {
    return '<span class="muted observation-center-sites-empty">No sites yet</span>';
  }

  return `
    <div class="run-tab-list observation-center-site-tabs">
      ${sites
        .map((site) => {
          const selectedClass = site.id === activeSiteId ? ' run-tab-button--active' : '';
          return `
            <button class="run-tab-button${selectedClass}" type="button" data-observation-site-id="${escapeHtml(site.id)}">
              ${escapeHtml(site.name)}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderSkyMosaicSidebar(
  state: AppState,
  project: ProjectSummary,
  selectedMosaic: MosaicPlan | null,
): string {
  const profile = state.telescopeSnapshot?.imaging_profile ?? null;
  return `
    <div class="project-gallery-sidebar__action">
      <div class="panel__header panel__header--nested">
        <h3>Mosaic planner</h3>
      </div>
      <form class="form-stack mosaic-create-form" data-form="create-mosaic">
        <input name="project_slug" type="hidden" value="${escapeHtml(project.slug)}" />
        <label class="field">
          <span>Name</span>
          <input name="name" type="text" placeholder="Cygnus Loop" />
        </label>
        <label class="field">
          <span>Target name</span>
          <input name="target_name" type="text" value="${escapeHtml(project.sky_target ?? '')}" placeholder="Cygnus Loop" />
        </label>
        <label class="field">
          <span>Observation type</span>
          <input name="observation_type" type="text" placeholder="broadband imaging" />
        </label>
        <label class="field">
          <span>Filter</span>
          <input name="filter" type="text" placeholder="L-Pro" />
        </label>
        <label class="field">
          <span>Center RA (deg)</span>
          <input name="center_ra_deg" type="text" placeholder="312.5" />
        </label>
        <label class="field">
          <span>Center Dec (deg)</span>
          <input name="center_dec_deg" type="text" placeholder="31.0" />
        </label>
        <label class="field">
          <span>Region width (deg)</span>
          <input name="region_width_deg" type="text" placeholder="3.0" />
        </label>
        <label class="field">
          <span>Region height (deg)</span>
          <input name="region_height_deg" type="text" placeholder="3.0" />
        </label>
        <label class="field">
          <span>Overlap (%)</span>
          <input name="overlap_percent" type="text" value="25" />
        </label>
        <label class="field">
          <span>Rotation (deg)</span>
          <input name="rotation_deg" type="text" value="${escapeHtml(String(profile?.rotation_deg ?? 0))}" />
        </label>
        <button class="action-button" type="submit" ${state.busy ? 'disabled' : ''}>Create mosaic plan</button>
      </form>
    </div>

    <section class="project-gallery-group">
      <div class="panel__header panel__header--nested">
        <h3>Mosaics</h3>
        ${selectedMosaic ? `<button class="action-button action-button--secondary sky-generate-button" type="button" data-delete-mosaic-id="${escapeHtml(selectedMosaic.id)}">Delete</button>` : ''}
      </div>
      ${renderMosaicTabs(state.mosaics, selectedMosaic?.id ?? null)}
    </section>

    <section class="project-gallery-group">
      <div class="panel__header panel__header--nested">
        <h3>Panels</h3>
        ${selectedMosaic ? `<button class="action-button action-button--secondary sky-generate-button" type="button" data-generate-mosaic-panels="${escapeHtml(selectedMosaic.id)}">Generate</button>` : ''}
      </div>
      ${renderMosaicPanelTabs(selectedMosaic, state.selectedMosaicPanelId)}
    </section>
  `;
}

function renderSkyMosaicSummary(selectedMosaic: MosaicPlan | null, selectedPanel: MosaicPanel | null): string {
  return `
    <div class="detail-stack">
      <div class="project-summary-strip project-summary-strip--sky project-summary-strip--sky-mosaic">
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Mosaic</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedMosaic?.name ?? 'none')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Profile</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedMosaic?.imaging_profile_label ?? 'none')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Observation</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedMosaic?.observation_type ?? '—')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Filter</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedMosaic?.filter ?? '—')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Panels</span>
          <span class="project-summary-strip__value">${selectedMosaic?.panels.length ?? 0}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Overlap</span>
          <span class="project-summary-strip__value">${selectedMosaic ? `${selectedMosaic.overlap_percent}%` : '—'}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Selected panel</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedPanel?.panel_label ?? 'none')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Panel center</span>
          <span class="project-summary-strip__value">${selectedPanel ? `${selectedPanel.center_ra_deg.toFixed(3)}°, ${selectedPanel.center_dec_deg.toFixed(3)}°` : '—'}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Panel FOV</span>
          <span class="project-summary-strip__value">${selectedPanel ? `${selectedPanel.fov_width_deg.toFixed(2)}° × ${selectedPanel.fov_height_deg.toFixed(2)}°` : '—'}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Status</span>
          <span class="project-summary-strip__value">${escapeHtml(selectedPanel?.status ?? selectedMosaic?.status ?? 'none')}</span>
        </div>
      </div>
      ${selectedMosaic ? renderSelectedMosaicPlanControls(selectedMosaic) : ''}
      ${selectedPanel ? renderSelectedMosaicPanelControls(selectedMosaic, selectedPanel) : ''}
    </div>
  `;
}

function renderSelectedMosaicPlanControls(selectedMosaic: MosaicPlan): string {
  return `
    <article class="panel">
      <div class="panel__header">
        <h3>Mosaic details</h3>
        <span>${escapeHtml(selectedMosaic.name)}</span>
      </div>
      <form class="sky-panel-status-form" data-form="update-mosaic-plan">
        <input type="hidden" name="mosaic_id" value="${escapeHtml(selectedMosaic.id)}" />
        <label class="field">
          <span>Name</span>
          <input name="name" type="text" value="${escapeHtml(selectedMosaic.name)}" />
        </label>
        <label class="field">
          <span>Target name</span>
          <input name="target_name" type="text" value="${escapeHtml(selectedMosaic.target_name ?? '')}" placeholder="optional" />
        </label>
        <label class="field">
          <span>Observation type</span>
          <input name="observation_type" type="text" value="${escapeHtml(selectedMosaic.observation_type ?? '')}" placeholder="optional" />
        </label>
        <label class="field">
          <span>Filter</span>
          <input name="filter" type="text" value="${escapeHtml(selectedMosaic.filter ?? '')}" placeholder="optional" />
        </label>
        <label class="field">
          <span>Status</span>
          <select name="status">
            <option value="draft" ${selectedMosaic.status === 'draft' ? 'selected' : ''}>draft</option>
            <option value="ready" ${selectedMosaic.status === 'ready' ? 'selected' : ''}>ready</option>
            <option value="active" ${selectedMosaic.status === 'active' ? 'selected' : ''}>active</option>
            <option value="archived" ${selectedMosaic.status === 'archived' ? 'selected' : ''}>archived</option>
          </select>
        </label>
        <button class="action-button" type="submit">Save mosaic</button>
      </form>
    </article>
  `;
}

function renderSelectedMosaicPanelControls(selectedMosaic: MosaicPlan | null, selectedPanel: MosaicPanel): string {
  return `
    <article class="panel">
      <div class="panel__header">
        <h3>Panel details</h3>
        <span>${escapeHtml(selectedPanel.panel_label)}</span>
      </div>
      <form class="sky-panel-status-form" data-form="update-mosaic-panel-status">
        <input type="hidden" name="panel_id" value="${escapeHtml(selectedPanel.id)}" />
        <label class="field">
          <span>Status</span>
          <select name="status">
            <option value="not_started" ${selectedPanel.status === 'not_started' ? 'selected' : ''}>not_started</option>
            <option value="in_progress" ${selectedPanel.status === 'in_progress' ? 'selected' : ''}>in_progress</option>
            <option value="complete" ${selectedPanel.status === 'complete' ? 'selected' : ''}>complete</option>
          </select>
        </label>
        <label class="field">
          <span>Target integration (s)</span>
          <input name="target_integration_seconds" type="text" value="${selectedPanel.target_integration_seconds ?? ''}" placeholder="optional" />
        </label>
        <label class="field">
          <span>Acquired integration (s)</span>
          <input name="acquired_integration_seconds" type="text" value="${selectedPanel.acquired_integration_seconds ?? ''}" placeholder="optional" />
        </label>
        <button class="action-button" type="button" data-plan-mosaic-panel="${escapeHtml(selectedPanel.id)}">Set as planned target</button>
        <button class="action-button" type="submit">Save panel</button>
        ${selectedMosaic ? `<button class="action-button action-button--secondary" type="button" data-generate-mosaic-panels="${escapeHtml(selectedMosaic.id)}">Regenerate panels</button>` : ''}
      </form>
    </article>
  `;
}

function renderMosaicTabs(mosaics: MosaicPlan[], selectedMosaicId: string | null): string {
  if (!mosaics.length) {
    return '<p class="muted">No mosaic plans yet.</p>';
  }

  return `
    <div class="project-tab-list project-tab-list--stacked">
      ${mosaics
        .map((mosaic) => {
          const selectedClass = mosaic.id === selectedMosaicId ? ' project-tab-button--active' : '';
          return `<button class="project-tab-button${selectedClass}" type="button" data-mosaic-id="${escapeHtml(mosaic.id)}">${escapeHtml(mosaic.name)}</button>`;
        })
        .join('')}
    </div>
  `;
}

function renderMosaicPanelTabs(selectedMosaic: MosaicPlan | null, selectedPanelId: string | null): string {
  if (!selectedMosaic) {
    return '<p class="muted">Create or select a mosaic first.</p>';
  }

  if (!selectedMosaic.panels.length) {
    return `
      <div class="detail-stack">
        <p class="muted">No panels generated yet for this mosaic.</p>
        <button class="action-button action-button--secondary" type="button" data-generate-mosaic-panels="${escapeHtml(selectedMosaic.id)}">
          Generate panels
        </button>
      </div>
    `;
  }

  return `
    <div class="project-tab-list project-tab-list--stacked">
      ${selectedMosaic.panels
        .map((panel) => {
          const selectedClass = panel.id === selectedPanelId ? ' project-tab-button--active' : '';
          return `<button class="project-tab-button${selectedClass}" type="button" data-mosaic-panel-id="${escapeHtml(panel.id)}">${escapeHtml(panel.panel_label)} · ${escapeHtml(panel.status)}</button>`;
        })
        .join('')}
    </div>
  `;
}

function renderProjectTabs(projects: ProjectSummary[], selectedSlug: string | null): string {
  if (!projects.length) {
    return `
      <p class="muted">
        No projects were found under the local <code>projects/</code> directory yet.
      </p>
    `;
  }

  return `
    <div class="project-tab-list">
      ${projects
        .map((project) => {
          const selectedClass = project.slug === selectedSlug ? ' project-tab-button--active' : '';
          return `
            <button class="project-tab-button${selectedClass}" type="button" data-project-slug="${project.slug}">
              ${project.slug}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderProcessingCaptureTabs(captureNames: string[], selectedCapture: string | null): string {
  if (!captureNames.length) {
    return '<p class="muted">No captures imported for this project yet.</p>';
  }

  return `
    <div class="project-tab-list">
      ${captureNames
        .map((captureName) => {
          const selectedClass = captureName === selectedCapture ? ' project-tab-button--active' : '';
          return `
            <button class="project-tab-button${selectedClass}" type="button" data-processing-capture="${escapeHtml(captureName)}">
              ${escapeHtml(captureName)}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderProjectCaptureTabs(captureNames: string[], selectedCapture: string | null): string {
  if (!captureNames.length) {
    return '<p class="muted">No captures imported yet.</p>';
  }

  return `
    <div class="project-tab-list">
      ${captureNames
        .map((captureName) => {
          const selectedClass = captureName === selectedCapture ? ' project-tab-button--active' : '';
          return `
            <button class="project-tab-button${selectedClass}" type="button" data-project-capture="${escapeHtml(captureName)}">
              ${escapeHtml(captureName)}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderCaptureBrowser(
  captureNames: string[],
  selectedCaptureName: string | null,
  capture: CaptureDetails | null,
  selectedCaptureFolder: string | null,
  selectedCaptureFile: string | null,
): string {
  if (capture === null) {
    return `
      <section class="project-gallery-layout">
        <aside class="project-gallery-sidebar">
          <div class="project-gallery-sidebar__action">
            <button class="action-button" type="button" data-open-import-capture-modal ${captureNames.length >= 0 ? '' : ''}>
              Import capture
            </button>
          </div>
          <section class="project-gallery-group">
            <div class="panel__header panel__header--nested">
              <h3>Captures</h3>
            </div>
            ${renderProjectCaptureTabs(captureNames, selectedCaptureName)}
          </section>
        </aside>
        <section class="project-gallery-main">
          <p class="muted">Select a capture to inspect its folder structure.</p>
        </section>
      </section>
    `;
  }

  const selectedFolder = findSelectedCaptureFolder(capture, selectedCaptureFolder);
  const selectedFile = findSelectedCaptureFile(selectedFolder, selectedCaptureFile);

  return `
    <section class="project-gallery-layout">
      <aside class="project-gallery-sidebar">
        <div class="project-gallery-sidebar__action">
          <button class="action-button" type="button" data-open-import-capture-modal>
            Import capture
          </button>
        </div>

        <section class="project-gallery-group">
          <div class="panel__header panel__header--nested">
            <h3>Captures</h3>
          </div>
          ${renderProjectCaptureTabs(captureNames, selectedCaptureName)}
        </section>

        <section class="project-gallery-group">
          <div class="panel__header panel__header--nested">
            <h3>Folders</h3>
          </div>
          <div class="project-tab-list project-tab-list--stacked">
            ${capture.folders
              .map((folder) => {
                const activeClass = folder.name === selectedFolder?.name ? ' project-tab-button--active' : '';
                return `
                  <button class="project-tab-button${activeClass}" type="button" data-capture-folder="${escapeHtml(folder.name)}">
                    ${escapeHtml(folder.name)}
                  </button>
                `;
              })
              .join('')}
          </div>
        </section>
      </aside>

      <section class="project-gallery-main">
        <article class="capture-folder">
          ${
            selectedFolder && selectedFolder.files.length
              ? `<ul class="capture-file-list">
                  ${selectedFolder.files
                    .map((fileEntry) => renderCaptureFileItem(capture, fileEntry, selectedFile))
                    .join('')}
                </ul>`
              : '<p class="muted">No files in this folder.</p>'
          }
        </article>
      </section>
    </section>
  `;
}

function renderCaptureFileItem(
  capture: CaptureDetails,
  fileEntry: CaptureFileEntry,
  selectedFile: CaptureFileEntry | null,
): string {
  const isSelected = selectedFile?.relative_path === fileEntry.relative_path;
  const previewable = isPreviewableCaptureFile(fileEntry);
  const fileUrl = getCaptureFileUrl(capture.project_slug, capture.capture_name, fileEntry.relative_path);
  const thumbnailUrl = getCaptureThumbnailUrl(capture.project_slug, capture.capture_name, fileEntry.relative_path, 384);
  return `
    <li class="${isSelected ? 'capture-file-list__item capture-file-list__item--selected' : 'capture-file-list__item'}">
      ${
        isSelected
          ? `<a
              class="capture-file-button__open-link"
              href="${fileUrl}"
              target="_blank"
              rel="noreferrer"
              title="Open in new tab"
              aria-label="Open ${escapeHtml(fileEntry.name)} in new tab"
              data-capture-open-link
            >
              ↗
            </a>`
          : ''
      }
      <button
        class="capture-file-button"
        type="button"
        data-capture-file="${escapeHtml(fileEntry.relative_path)}"
        title="${escapeHtml(`${fileEntry.name} • ${formatBytes(fileEntry.size_bytes)}`)}"
      >
        ${
          previewable
            ? `<img
                class="capture-file-button__image"
                src="${thumbnailUrl}"
                alt="${escapeHtml(fileEntry.name)}"
                loading="lazy"
                decoding="async"
                fetchpriority="low"
              />`
            : `<div class="capture-file-button__placeholder">
                <span>${escapeHtml((fileEntry.suffix || 'file').replace('.', '').toUpperCase())}</span>
              </div>`
        }
        <div class="capture-file-button__overlay">
          <strong>${escapeHtml(fileEntry.name)}</strong>
          <small>${formatBytes(fileEntry.size_bytes)}</small>
        </div>
      </button>
    </li>
  `;
}

function renderRunMonitor(run: ProjectRun | null, options?: { includeLogs?: boolean }): string {
  if (run === null) {
    return `<p class="muted">No processing run yet. Import a capture and launch Siril.</p>`;
  }

  const includeLogs = options?.includeLogs ?? true;
  const errorBlock = run.error_message ? `<div class="banner banner--error">${run.error_message}</div>` : '';
  const previewBlock = renderRunPreview(run);

  return `
    <div class="run-monitor">
      <div class="details-split-layout run-overview-layout">
        <section class="detail-stack">
          <div class="run-monitor__summary">
            <dl class="health-list">
              <div>
                <dt>Capture</dt>
                <dd>${run.capture_name}</dd>
              </div>
              <div>
                <dt>Workspace</dt>
                <dd>${run.workspace_dir}</dd>
              </div>
              <div>
                <dt>Artifacts</dt>
                <dd>${run.artifacts_dir}</dd>
              </div>
              <div>
                <dt>Output</dt>
                <dd>${run.output_path ?? 'pending'}</dd>
              </div>
            </dl>
          </div>

          ${errorBlock}

          <div class="progress-block">
            <div class="progress-block__top">
              <strong>${run.status}</strong>
              <span>${run.progress_pct}%</span>
            </div>
            <div class="progress-bar">
              <div class="progress-bar__fill" style="width: ${run.progress_pct}%"></div>
            </div>
            <p class="muted">${run.stage}</p>
          </div>
        </section>

        <section class="detail-stack details-preview-panel run-overview-preview">
          ${previewBlock}
        </section>
      </div>

      ${includeLogs
        ? `
      <section>
        <pre class="log-view">${escapeHtml(formatLogForDisplay(run.combined_log || 'Waiting for process output...'))}</pre>
      </section>`
        : ''}
    </div>
  `;
}

function renderRunTabs(runs: ProjectRun[], selectedRunId: string | null): string {
  if (!runs.length) {
    return '<p class="muted">No runs recorded for this capture yet.</p>';
  }

  return `
    <div class="run-tab-list">
      ${runs
        .map((run) => {
          const selectedClass = run.id === selectedRunId ? ' run-tab-button--active' : '';
          return `
            <button class="run-tab-button${selectedClass}" type="button" data-run-id="${run.id}">
              ${escapeHtml(formatRunTabLabel(run))}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderProcessingSummaryStrip(run: ProjectRun | null): string {
  return `
    <div class="project-summary-strip">
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Run</span>
        <span class="project-summary-strip__value">${escapeHtml(run?.id ?? 'none')}</span>
      </div>
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Status</span>
        <span class="project-summary-strip__value">${escapeHtml(run?.status ?? 'idle')}</span>
      </div>
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Capture</span>
        <span class="project-summary-strip__value">${escapeHtml(run?.capture_name ?? 'none')}</span>
      </div>
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Progress</span>
        <span class="project-summary-strip__value">${run ? `${run.progress_pct}%` : '0%'}</span>
      </div>
    </div>
  `;
}

function renderProcessingTab(tab: ProcessingDetailTab, label: string, activeTab: ProcessingDetailTab): string {
  const activeClass = tab === activeTab ? ' tab-strip__button--active' : '';
  return `<button class="tab-strip__button${activeClass}" type="button" data-processing-tab="${tab}">${label}</button>`;
}

function renderRunDetailPanel(activeTab: ProcessingDetailTab, run: ProjectRun | null): string {
  if (run === null) {
    return '<p class="muted">No runs recorded for this project yet. Create a new run to begin processing.</p>';
  }

  if (activeTab === 'settings') {
    return `
      <section class="detail-stack">
        <p class="muted">Administrative actions for this run live here.</p>
        <div class="danger-zone">
          <div class="danger-zone__copy">
            <h4>Delete run</h4>
            <p class="muted">Remove the run workspace, artifacts, logs and generated outputs for this run.</p>
          </div>
          <button class="action-button action-button--danger" type="button" data-delete-run-id="${run.id}">
            Delete run
          </button>
        </div>
      </section>
    `;
  }

  if (activeTab === 'logs') {
    return `
      <section class="detail-stack">
        <div class="run-log-meta">
          <div class="run-log-meta__row">
            <span class="run-log-meta__label">command</span>
            <code class="command run-log-meta__value">${run.command.join(' ')}</code>
          </div>
          <div class="run-log-meta__row">
            <span class="run-log-meta__label">output</span>
            <code class="command run-log-meta__value">${run.output_path ?? 'pending'}</code>
          </div>
        </div>
        <section>
          <pre class="log-view">${escapeHtml(formatLogForDisplay(run.combined_log || 'Waiting for process output...'))}</pre>
        </section>
      </section>
    `;
  }

  return renderRunMonitor(run, { includeLogs: false });
}

function renderRunPreview(run: ProjectRun): string {
  if (run.preview_path) {
    return `
      <div class="preview-card">
        <a class="preview-card__link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">
          <img
            class="preview-card__image preview-card__image--project"
            src="${getProjectRunPreviewUrl(run.id)}"
            alt="Preview for run ${run.id}"
            data-preview-image
            data-preview-fallback="Preview image is not available yet."
          />
        </a>
      </div>
    `;
  }

  if (run.status === 'completed' && run.output_path) {
    const previewError = run.preview_error ? `<div class="banner banner--error">${escapeHtml(run.preview_error)}</div>` : '';
    const previewLogLink = run.preview_log_path
      ? `<a class="secondary-link" href="${getProjectRunPreviewLogUrl(run.id)}" target="_blank" rel="noreferrer">Open preview export log</a>`
      : '';
    return `
      <div class="detail-stack">
        <p class="muted">
          Run finished and FITS output is available, but no JPEG preview was generated for this result.
        </p>
        ${previewError}
        <div class="inline-actions">
          <button class="action-button action-button--secondary" type="button" data-generate-preview-run-id="${run.id}">Generate preview</button>
          ${previewLogLink}
        </div>
      </div>
    `;
  }

  return `
    <p class="muted">
      Preview will appear here after the run finishes and TSN DSS exports a browser-friendly image.
    </p>
  `;
}

function renderProjectDetails(
  project: ProjectSummary | null,
  projectRuns: ProjectRun[],
  selectedProjectRun: ProjectRun | null,
  selectedCaptureName: string | null,
): string {
  if (project === null) {
    return `<p class="muted">No project selected.</p>`;
  }

  return `
    <section class="project-gallery-layout">
      <aside class="project-gallery-sidebar">
        <section class="project-gallery-group">
          <div class="panel__header panel__header--nested">
            <h3>Captures</h3>
          </div>
          ${renderProjectCaptureTabs(project.capture_names, selectedCaptureName)}
        </section>

        <section class="project-gallery-group">
          <div class="panel__header panel__header--nested">
            <h3>Runs</h3>
          </div>
          ${renderProjectRunTabs(projectRuns, selectedProjectRun?.id ?? null)}
        </section>
      </aside>

      <section class="project-gallery-main">
        ${renderProjectMediaPanel(selectedProjectRun)}
      </section>
    </section>
  `;
}

function renderProjectSummaryStrip(project: ProjectSummary): string {
  return `
    <div class="project-summary-strip">
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Project root</span>
        <span class="project-summary-strip__value">${escapeHtml(project.project_root)}</span>
      </div>
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Captures dir</span>
        <span class="project-summary-strip__value">${escapeHtml(project.captures_dir)}</span>
      </div>
      <div class="project-summary-strip__item">
        <span class="project-summary-strip__label">Runs dir</span>
        <span class="project-summary-strip__value">${escapeHtml(project.runs_dir)}</span>
      </div>
      <label class="project-summary-strip__item project-summary-strip__item--input">
        <span class="project-summary-strip__label">Sky target</span>
        <input
          class="inline-edit-input project-summary-strip__input"
          type="text"
          value="${escapeHtml(project.sky_target ?? '')}"
          placeholder="M42 / NGC 1976 / Orion Nebula"
          data-inline-sky-target
          data-project-slug-inline="${escapeHtml(project.slug)}"
        />
      </label>
    </div>
  `;
}

function renderProjectRunTabs(runs: ProjectRun[], selectedRunId: string | null): string {
  if (!runs.length) {
    return '<p class="muted">No runs recorded for this project yet.</p>';
  }

  return `
    <div class="run-tab-list">
      ${runs
        .map((run) => {
          const selectedClass = run.id === selectedRunId ? ' run-tab-button--active' : '';
          return `
            <button class="run-tab-button${selectedClass}" type="button" data-project-run-id="${run.id}">
              ${escapeHtml(formatRunTabLabel(run))}
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderProjectEditedPanel(run: ProjectRun): string {
  if (run === null) {
    return '<p class="muted">No processing output is available for this project yet.</p>';
  }

  return `
    <div class="edited-image-grid project-media-grid">
      ${renderProjectPreviewGalleryCard(run)}
      ${run.artifact_images.map((image) => renderEditedImageCard(run, image)).join('')}
    </div>
  `;
}

function renderProjectMediaPanel(run: ProjectRun | null): string {
  if (run === null) {
    return `
      <article class="panel project-media-panel">
        <p class="muted">No processing output is available for this project yet.</p>
      </article>
    `;
  }

  return `
    <article class="panel project-media-panel">
      ${renderProjectEditedPanel(run)}
    </article>
  `;
}

function renderProjectPreviewGalleryCard(run: ProjectRun): string {
  if (run.preview_path) {
    return `
      <a class="edited-image-card edited-image-card--preview" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer" title="Preview">
        <img
          class="edited-image-card__image edited-image-card__image--preview"
          src="${getProjectRunPreviewUrl(run.id)}"
          alt="Preview for run ${run.id}"
          loading="eager"
          decoding="async"
          data-preview-image
          data-preview-fallback="Preview image is not available yet."
        />
        <div class="edited-image-card__overlay">
          <strong>Preview</strong>
          <small>${escapeHtml(run.id)}</small>
        </div>
      </a>
    `;
  }

  if (run.output_path) {
    return `
      <div class="edited-image-card edited-image-card--empty">
        <div class="edited-image-card__empty-copy">
          <strong>Preview</strong>
          <p class="muted">Output FITS exists, but preview image is not available yet.</p>
        </div>
      </div>
    `;
  }

  return `
    <div class="edited-image-card edited-image-card--empty">
      <div class="edited-image-card__empty-copy">
        <strong>Preview</strong>
        <p class="muted">The selected run has not produced an output file yet.</p>
      </div>
    </div>
  `;
}

function renderEditedImageCard(run: ProjectRun, image: RunArtifactImage): string {
  const imageUrl = getProjectRunArtifactUrl(run.id, image.relative_path);
  return `
    <a class="edited-image-card" href="${imageUrl}" target="_blank" rel="noreferrer" title="${escapeHtml(image.name)}">
      <img
        class="edited-image-card__image"
        src="${imageUrl}"
        alt="${escapeHtml(image.name)}"
        loading="lazy"
        decoding="async"
      />
      <div class="edited-image-card__overlay">
        <strong>${escapeHtml(image.name)}</strong>
        <small>${formatBytes(image.size_bytes)}</small>
      </div>
    </a>
  `;
}

function renderCreateProjectModal(busy: boolean): string {
  return `
    <div class="modal-backdrop" data-close-create-project-modal>
      <div class="modal-card" role="dialog" aria-modal="true" aria-label="Create project" data-modal-card>
        <div class="panel__header">
          <h3>Create project</h3>
          <button class="action-button action-button--secondary" type="button" data-close-create-project-modal ${busy ? 'disabled' : ''}>
            Close
          </button>
        </div>
        <form class="form-stack" data-form="create-project">
          <label class="field">
            <span>Project slug</span>
            <input name="slug" type="text" placeholder="m42_rebuild" required autofocus />
          </label>
          <button class="action-button" type="submit" ${busy ? 'disabled' : ''}>Create project</button>
        </form>
      </div>
    </div>
  `;
}

function renderCreateRunModal(state: AppState, project: ProjectSummary | null, selectedCapture: string | null): string {
  const selectedCaptureValue =
    (selectedCapture && project?.capture_names.includes(selectedCapture) ? selectedCapture : null)
    ?? project?.capture_names[0]
    ?? '';

  return `
    <div class="modal-backdrop" data-close-create-run-modal>
      <div class="modal-card" role="dialog" aria-modal="true" aria-label="Create run" data-run-modal-card>
        <div class="panel__header">
          <h3>Create run</h3>
          <button class="action-button action-button--secondary" type="button" data-close-create-run-modal ${state.busy ? 'disabled' : ''}>
            Close
          </button>
        </div>
        <form class="form-stack" data-form="start-run">
          <label class="field">
            <span>Project slug</span>
            <input name="project_slug" type="text" value="${project?.slug ?? ''}" readonly />
          </label>
          <label class="field">
            <span>Capture name</span>
            <select name="capture_name" required ${project?.capture_names.length ? '' : 'disabled'}>
              ${project?.capture_names.length
                ? project.capture_names
                    .map(
                      (captureName) => `
                        <option value="${escapeHtml(captureName)}" ${captureName === selectedCaptureValue ? 'selected' : ''}>
                          ${escapeHtml(captureName)}
                        </option>
                      `,
                    )
                    .join('')
                : '<option value="">No captures available</option>'}
            </select>
          </label>
          <label class="field">
            <span>Siril executable</span>
            <input
              name="executable"
              type="text"
              value="${state.health?.default_siril_executable ?? 'siril-cli'}"
              placeholder="siril-cli"
            />
          </label>
          <label class="field">
            <span>OSC script path</span>
            <input name="script_path" type="text" placeholder="default repo OSC_Preprocessing.ssf" />
          </label>
          <label class="checkbox-field">
            <input name="keep_process_dir" type="checkbox" />
            <span>Keep Siril process folder in workspace</span>
          </label>
          <button class="action-button" type="submit" ${state.busy || project === null || !project?.capture_names.length ? 'disabled' : ''}>Run OSC_Preprocessing</button>
        </form>
      </div>
    </div>
  `;
}

function renderImportCaptureModal(state: AppState, project: ProjectSummary | null, selectedCapture: string | null): string {
  return `
    <div class="modal-backdrop" data-close-import-capture-modal>
      <div class="modal-card" role="dialog" aria-modal="true" aria-label="Import capture" data-import-capture-modal-card>
        <div class="panel__header">
          <h3>Import capture</h3>
          <button class="action-button action-button--secondary" type="button" data-close-import-capture-modal ${state.busy ? 'disabled' : ''}>
            Close
          </button>
        </div>
        <form class="form-stack" data-form="import-capture">
          <label class="field">
            <span>Project slug</span>
            <input name="project_slug" type="text" value="${project?.slug ?? ''}" readonly />
          </label>
          <label class="field">
            <span>Capture name</span>
            <input name="capture_name" type="text" value="${selectedCapture ?? ''}" placeholder="OrionNebula" required />
          </label>
          <label class="field">
            <span>Source folder</span>
            <input name="source_dir" type="text" placeholder="C:\\path\\to\\OrionNebula" required />
          </label>
          <label class="checkbox-field">
            <input name="move" type="checkbox" />
            <span>Move instead of copy</span>
          </label>
          <button class="action-button" type="submit" ${state.busy || project === null ? 'disabled' : ''}>Import capture</button>
        </form>
      </div>
    </div>
  `;
}

function renderSiteEditorModal(state: AppState, site: Site | null): string {
  const isEditing = site !== null;
  const candidateDraft = isEditing ? null : state.siteEditorCandidateDraft;
  const candidateLpPayload = candidateDraft
    ? siteLightPollutionPayloadFromCandidate(candidateDraft.lightPollution, new Date().toISOString())
    : null;
  const latitudeValue = site?.latitude_deg ?? candidateDraft?.latitudeDeg ?? '';
  const longitudeValue = site?.longitude_deg ?? candidateDraft?.longitudeDeg ?? '';
  const siteNameValue = site?.name ?? (candidateDraft ? buildCandidateSiteDefaultName(candidateDraft.latitudeDeg, candidateDraft.longitudeDeg) : '');
  const lpArtificialBrightness = site?.lp_artificial_brightness_mcd_m2 ?? candidateLpPayload?.lp_artificial_brightness_mcd_m2 ?? null;
  const lpNaturalSkyRatio = site?.lp_natural_sky_ratio ?? candidateLpPayload?.lp_natural_sky_ratio ?? null;
  const lpTotalBrightness = site?.lp_estimated_total_brightness_mcd_m2 ?? candidateLpPayload?.lp_estimated_total_brightness_mcd_m2 ?? null;
  const lpEstimatedSqm = site?.lp_estimated_sqm_mag_arcsec2 ?? candidateLpPayload?.lp_estimated_sqm_mag_arcsec2 ?? null;
  const lpEstimatedBortle = site?.lp_estimated_bortle_class ?? candidateLpPayload?.lp_estimated_bortle_class ?? null;
  const lpDatasetName = site?.lp_dataset_name ?? candidateLpPayload?.lp_dataset_name ?? null;
  const lpProviderName = site?.lp_provider_name ?? candidateLpPayload?.lp_provider_name ?? null;
  const lpSource = site?.lp_source ?? candidateLpPayload?.lp_source ?? null;
  const lpSourceUnit = site?.lp_source_unit ?? candidateLpPayload?.lp_source_unit ?? null;
  const lpDataKind = site?.lp_data_kind ?? candidateLpPayload?.lp_data_kind ?? null;
  const lpUpdatedAt = site?.lp_updated_at ?? candidateLpPayload?.lp_updated_at ?? null;
  const candidateNote = candidateDraft
    ? candidateLpPayload
      ? 'Candidate coordinates and modeled light pollution data are prefilled from the map.'
      : 'Candidate coordinates are prefilled from the map. No modeled light pollution value will be saved for this site.'
    : null;
  return `
    <div class="modal-backdrop" data-close-site-editor-modal>
      <div class="modal-card modal-card--wide" role="dialog" aria-modal="true" aria-label="${isEditing ? 'Edit site' : 'Create site'}" data-site-editor-modal-card>
        <div class="panel__header">
          <h3>${isEditing ? 'Edit site' : 'Create site'}</h3>
          <button class="action-button action-button--secondary" type="button" data-close-site-editor-modal ${state.busy ? 'disabled' : ''}>
            Close
          </button>
        </div>
        <form class="form-stack site-editor-form" data-form="site-editor">
          <input name="site_id" type="hidden" value="${escapeHtml(site?.id ?? '')}" />
          <input name="lp_artificial_brightness_mcd_m2" type="hidden" value="${formatHiddenNumber(lpArtificialBrightness)}" />
          <input name="lp_natural_sky_ratio" type="hidden" value="${formatHiddenNumber(lpNaturalSkyRatio)}" />
          <input name="lp_estimated_total_brightness_mcd_m2" type="hidden" value="${formatHiddenNumber(lpTotalBrightness)}" />
          <input name="lp_estimated_sqm_mag_arcsec2" type="hidden" value="${formatHiddenNumber(lpEstimatedSqm)}" />
          <input name="lp_estimated_bortle_class" type="hidden" value="${formatHiddenNumber(lpEstimatedBortle)}" />
          <input name="lp_dataset_name" type="hidden" value="${escapeHtml(lpDatasetName ?? '')}" />
          <input name="lp_provider_name" type="hidden" value="${escapeHtml(lpProviderName ?? '')}" />
          <input name="lp_source" type="hidden" value="${escapeHtml(lpSource ?? '')}" />
          <input name="lp_source_unit" type="hidden" value="${escapeHtml(lpSourceUnit ?? '')}" />
          <input name="lp_data_kind" type="hidden" value="${escapeHtml(lpDataKind ?? '')}" />
          <input name="lp_updated_at" type="hidden" value="${escapeHtml(lpUpdatedAt ?? '')}" />
          ${candidateNote ? `<p class="muted field--full">${escapeHtml(candidateNote)}</p>` : ''}
          <label class="field field--full">
            <span>Site name</span>
            <input name="name" type="text" value="${escapeHtml(siteNameValue)}" placeholder="Backyard / SiteNo1 / Ridge coordinates" required autofocus />
          </label>
          <label class="field">
            <span>Latitude (deg)</span>
            <input name="latitude_deg" type="text" value="${latitudeValue}" placeholder="50.1234" />
          </label>
          <label class="field">
            <span>Longitude (deg)</span>
            <input name="longitude_deg" type="text" value="${longitudeValue}" placeholder="19.1234" />
          </label>
          <label class="field">
            <span>Elevation (m)</span>
            <input name="elevation_m" type="text" value="${site?.elevation_m ?? ''}" placeholder="optional" />
          </label>
          <label class="field">
            <span>Measured SQM</span>
            <input name="sqm_mag_arcsec2" type="text" value="${site?.sqm_mag_arcsec2 ?? ''}" placeholder="optional" />
          </label>
          <label class="field">
            <span>Manual Bortle</span>
            <input name="bortle_class" type="text" value="${site?.bortle_class ?? ''}" placeholder="1-9" />
          </label>
          <label class="checkbox-field site-editor-form__checkbox">
            <input name="south_horizon_open" type="checkbox" ${site?.south_horizon_open ? 'checked' : ''} />
            <span>South horizon open</span>
          </label>
          <label class="field field--full">
            <span>Notes</span>
            <textarea name="notes" rows="3" placeholder="Optional site notes">${escapeHtml(site?.notes ?? '')}</textarea>
          </label>
          ${candidateDraft ? `
            <div class="site-editor-lp-summary field--full">
              <span class="site-detail-section__title">Light pollution</span>
              ${candidateLpPayload ? `
                <div class="site-detail-list">
                  ${renderSiteDetailRow('Estimated SQM', lpEstimatedSqm == null ? '—' : `${lpEstimatedSqm.toFixed(2)} mag/arcsec² · estimated`)}
                  ${renderSiteDetailRow('Estimated Bortle', lpEstimatedBortle == null ? '—' : `Class ${lpEstimatedBortle} · estimated`)}
                  ${renderSiteDetailRow('Dataset', lpDatasetName ?? '—')}
                </div>
              ` : '<p class="muted">Light pollution data unavailable.</p>'}
            </div>
          ` : ''}
          <button class="action-button field--full" type="submit" ${state.busy ? 'disabled' : ''}>
            ${isEditing ? 'Save site' : 'Create site'}
          </button>
        </form>
      </div>
    </div>
  `;
}

function renderHorizonProfileEditorModal(state: AppState, site: Site | null): string {
  const points = normalizeLocalHorizonProfile(site?.horizon_profile ?? []);
  const rows = points.length
    ? points.map((point) => renderHorizonProfileEditorRow(point.azimuth_deg, point.min_altitude_deg)).join('')
    : renderHorizonProfileEditorRow('', '');

  return `
    <div class="modal-backdrop" data-close-horizon-profile-editor>
      <div class="modal-card modal-card--wide" role="dialog" aria-modal="true" aria-label="Edit horizon profile" data-horizon-profile-editor-modal-card>
        <div class="panel__header">
          <h3>Local horizon profile</h3>
          <button class="action-button action-button--secondary" type="button" data-close-horizon-profile-editor ${state.busy ? 'disabled' : ''}>
            Close
          </button>
        </div>
        <form class="form-stack horizon-profile-editor" data-form="horizon-profile-editor">
          <input name="site_id" type="hidden" value="${escapeHtml(site?.id ?? '')}" />
          <p class="muted field--full">
            Geometric horizon is 0°. Local horizon describes fixed obstructions for this Site: trees, buildings, hills or similar.
          </p>
          <div class="horizon-profile-editor__actions field--full">
            <button class="action-button action-button--secondary" type="button" data-add-horizon-profile-row>Add point</button>
            <button class="action-button action-button--secondary" type="button" data-add-horizon-profile-template>Add 8-direction template</button>
          </div>
          <div class="horizon-profile-table field--full">
            <div class="horizon-profile-table__header">
              <span>Azimuth</span>
              <span>Minimum altitude</span>
              <span></span>
            </div>
            <div data-horizon-profile-rows>
              ${rows}
            </div>
          </div>
          <button class="action-button field--full" type="submit" ${state.busy || !site ? 'disabled' : ''}>
            Save horizon profile
          </button>
        </form>
      </div>
    </div>
  `;
}

function renderHorizonProfileEditorRow(azimuthDeg: number | string, minAltitudeDeg: number | string): string {
  return `
    <div class="horizon-profile-table__row" data-horizon-profile-row>
      <input name="azimuth_deg" type="number" min="0" max="359.999" step="0.1" value="${escapeHtml(String(azimuthDeg))}" placeholder="0" />
      <input name="min_altitude_deg" type="number" min="0" max="90" step="0.1" value="${escapeHtml(String(minAltitudeDeg))}" placeholder="0" />
      <button class="action-button action-button--danger" type="button" data-delete-horizon-profile-row>Remove</button>
    </div>
  `;
}

function renderToast(state: AppState): string {
  if (!state.error && !state.message) {
    return '';
  }

  const variant = state.error ? 'error' : 'success';
  const content = state.error ?? state.message ?? '';

  return `
    <div class="toast-stack">
      <div class="toast toast--${variant}">
        ${escapeHtml(content)}
      </div>
    </div>
  `;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function getHardwareLabel(
  telescopeState: TelescopeSnapshot['telescope_state'] | null,
): string {
  if (!telescopeState) {
    return 'No device';
  }

  if (!telescopeState.connected) {
    return 'Disconnected';
  }

  if (telescopeState.is_simulated) {
    return 'Simulator';
  }

  return telescopeState.source_kind || telescopeState.adapter_id || 'Connected';
}

function getHardwareDetail(
  telescopeState: TelescopeSnapshot['telescope_state'] | null,
): string {
  if (!telescopeState) {
    return 'No telescope state is available yet.';
  }

  return [
    `adapter: ${telescopeState.adapter_id || 'unknown'}`,
    `source: ${telescopeState.source_kind || 'unknown'}`,
    `status: ${telescopeState.status || 'unknown'}`,
    `connected: ${telescopeState.connected ? 'yes' : 'no'}`,
    `mode: ${telescopeState.is_simulated ? 'simulated' : 'live'}`,
    `target: ${telescopeState.target_name || 'none'}`,
  ].join(' | ');
}

function getSkyCapabilities(
  adapter: TelescopeAdapterDescriptor | null,
  telescopeState: TelescopeSnapshot['telescope_state'] | null,
): string[] {
  if (adapter) {
    const values = ['state snapshot', 'map centering', 'fov overlay'];
    if (adapter.capabilities.can_manual_pointing) {
      values.push('manual pointing');
    }
    if (adapter.capabilities.can_stream_preview) {
      values.push('preview stream');
    }
    if (adapter.capabilities.can_start_stack) {
      values.push('stack control');
    }
    if (adapter.capabilities.can_run_observation_plans) {
      values.push('observation plans');
    }
    return values;
  }

  if (!telescopeState) {
    return ['state snapshot'];
  }

  if (telescopeState.is_simulated) {
    return ['state snapshot', 'manual pointing', 'map centering', 'fov overlay'];
  }

  return ['state snapshot', 'map centering', 'fov overlay'];
}

function formatLogForDisplay(value: string): string {
  return value
    .split(/\r?\n/)
    .filter((line) => line.length > 0)
    .reverse()
    .join('\n');
}

function formatRaDisplay(raHours: number | null | undefined): string {
  if (raHours == null) {
    return '—';
  }
  const totalSeconds = raHours * 3600;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${raHours.toFixed(6)} h · ${pad2(hours)} ${pad2(minutes)} ${seconds.toFixed(2).padStart(5, '0')}`;
}

function formatDecDisplay(decDeg: number | null | undefined): string {
  if (decDeg == null) {
    return '—';
  }
  const sign = decDeg < 0 ? '-' : '+';
  const absolute = Math.abs(decDeg);
  const totalSeconds = absolute * 3600;
  const degrees = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${decDeg.toFixed(6)}° · ${sign}${pad2(degrees)} ${pad2(minutes)} ${seconds.toFixed(1).padStart(4, '0')}`;
}

function formatTimestampDisplay(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }

  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return value;
  }

  return timestamp.toLocaleString([], {
    year: '2-digit',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function formatRunTabLabel(run: ProjectRun): string {
  if (run.started_at) {
    const date = new Date(run.started_at);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleString([], {
        year: '2-digit',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
    }
  }
  return run.id;
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMeters(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return `${value.toFixed(0)} m`;
}

function formatAngleValue(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return `${value.toFixed(4)}°`;
}

function formatSiteQuality(site: Site | null | undefined): string {
  if (!site) {
    return '—';
  }
  const parts: string[] = [];
  if (site.bortle_class != null) {
    parts.push(`B${site.bortle_class}`);
  }
  if (site.sqm_mag_arcsec2 != null) {
    parts.push(`${site.sqm_mag_arcsec2.toFixed(1)} SQM`);
  }
  return parts.join(' · ') || '—';
}

function formatHiddenNumber(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '' : String(value);
}

function formatMcdPerSquareMeter(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return '—';
  }
  return `${value.toFixed(4)} mcd/m²`;
}

function formatEstimatedMcdPerSquareMeter(value: number | null | undefined): string {
  const formatted = formatMcdPerSquareMeter(value);
  return formatted === '—' ? formatted : `${formatted} · estimated`;
}

function formatEstimatedMultiplier(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return '—';
  }
  return `${value.toFixed(2)}× · estimated`;
}

function formatWeatherValue(value: number | null | undefined, suffix: string): string {
  if (value == null) {
    return '—';
  }
  return `${value.toFixed(1)} ${suffix}`;
}

function formatDistanceMeters(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)} km`;
  }
  return `${value.toFixed(0)} m`;
}

function formatWind(
  speedKmh: number | null | undefined,
  directionDeg: number | null | undefined,
): string {
  const speed = speedKmh == null ? '—' : `${speedKmh.toFixed(1)} km/h`;
  const direction = formatWindDirection(directionDeg);
  return direction === '—' ? speed : `${speed} · ${direction}`;
}

function formatWindDirection(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  const directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  const normalized = ((value % 360) + 360) % 360;
  const index = Math.round(normalized / 45) % 8;
  return `${directions[index]} ${normalized.toFixed(0)}°`;
}

function formatDewRisk(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  return value;
}

function formatPercent(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return `${value.toFixed(0)}%`;
}

function formatSkyState(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }

  switch (value) {
    case 'day':
      return 'day';
    case 'civil_twilight':
      return 'civil twilight';
    case 'nautical_twilight':
      return 'nautical twilight';
    case 'astronomical_twilight':
      return 'astronomical twilight';
    case 'astronomical_night':
      return 'astronomical night';
    default:
      return value.replaceAll('_', ' ');
  }
}

function formatAirmass(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return value.toFixed(2);
}

function formatObservationFlag(value: boolean | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return value ? 'yes' : 'no';
}

function formatLocalHorizonAltitude(analysis: LocalHorizonAnalysis | null): string {
  if (!analysis || analysis.visibility === 'not_configured') {
    return 'Not configured';
  }
  if (analysis.localHorizonAltitudeDeg == null || !Number.isFinite(analysis.localHorizonAltitudeDeg)) {
    return '—';
  }
  return `${analysis.localHorizonAltitudeDeg.toFixed(0)}°`;
}

function formatLocalHorizonClearance(analysis: LocalHorizonAnalysis | null): string {
  if (!analysis || analysis.visibility === 'not_configured') {
    return '—';
  }
  if (analysis.clearanceDeg == null || !Number.isFinite(analysis.clearanceDeg)) {
    return '—';
  }
  const sign = analysis.clearanceDeg > 0 ? '+' : '';
  return `${sign}${analysis.clearanceDeg.toFixed(0)}°`;
}

function formatLocalHorizonVisibility(analysis: LocalHorizonAnalysis | null): string {
  switch (analysis?.visibility) {
    case 'clear':
      return 'CLEAR';
    case 'blocked':
      return 'BLOCKED';
    case 'unknown':
      return 'Unknown';
    case 'not_configured':
    default:
      return 'Not configured';
  }
}

/** Conditions timestamps: an explicit instant shown in the Site timezone (never a naive string). */
function formatConditionsTimestamp(value: string | null | undefined, timeZone: string | undefined): string {
  if (!value) {
    return '—';
  }
  const instantMs = parseInstantMs(value);
  if (instantMs == null) {
    return value;
  }
  return formatInstant(instantMs, timeZone, {
    year: '2-digit',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatTimeRange(
  start: string | null | undefined,
  end: string | null | undefined,
  timeZone: string | undefined,
): string {
  if (!start && !end) {
    return '—';
  }
  return `${formatConditionsTimestamp(start, timeZone)} → ${formatConditionsTimestamp(end, timeZone)}`;
}

function formatWindowSummary(
  start: string | null | undefined,
  end: string | null | undefined,
  status: string | null | undefined,
  timeZone: string | undefined,
): string {
  if (status === 'always_up') {
    return `all night · ${formatTimeRange(start, end, timeZone)}`;
  }
  if (status === 'always_observable') {
    return `all night above threshold · ${formatTimeRange(start, end, timeZone)}`;
  }
  if (status === 'never_up') {
    return 'not above horizon tonight';
  }
  if (status === 'not_observable') {
    return 'not above threshold tonight';
  }
  if (status === 'no_astronomical_night') {
    return 'no astronomical night for this interval';
  }
  return formatTimeRange(start, end, timeZone);
}

function formatTimelineTick(value: string | null | undefined, timeZone: string | undefined): string {
  if (!value) {
    return '—';
  }
  const instantMs = parseInstantMs(value);
  if (instantMs == null) {
    return value;
  }
  return formatInstant(instantMs, timeZone, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatObservingWindowRange(
  start: string | null | undefined,
  end: string | null | undefined,
  timeZone: string | undefined,
): string {
  if (!start || !end) {
    return 'No observing window';
  }

  const startMs = parseInstantMs(start);
  const endMs = parseInstantMs(end);
  if (startMs == null || endMs == null) {
    return `${start ?? '—'} → ${end ?? '—'}`;
  }

  const sameDay = localDateKey(startMs, timeZone) === localDateKey(endMs, timeZone);
  const timeOptions: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit' };
  if (sameDay) {
    return `${formatInstant(startMs, timeZone, timeOptions)} → ${formatInstant(endMs, timeZone, timeOptions)}`;
  }
  const dateTimeOptions: Intl.DateTimeFormatOptions = { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' };
  return `${formatInstant(startMs, timeZone, dateTimeOptions)} → ${formatInstant(endMs, timeZone, dateTimeOptions)}`;
}

function renderObservingWindowConditions(
  points: ObservingWindowConditionPoint[],
  context: ObservationIntentContext,
  noWindowReason: string | null = null,
): string {
  if (!points.length) {
    return `
      <div class="conditions-timeline__conditions">
        <span class="conditions-timeline__summary-label">Observing conditions</span>
        <p class="muted">${escapeHtml(noWindowReason ?? 'No observing window is currently available, so there is nothing to assess yet.')}</p>
      </div>
    `;
  }

  const moonAltitudes = points
    .map((point) => point.moonAltitudeDeg)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const moonAboveHorizon = moonAltitudes.length
    ? moonAltitudes.some((value) => value > 0)
    : null;
  const dewRisks = Array.from(
    new Set(
      points
        .map((point) => point.dewRisk)
        .filter((value): value is string => Boolean(value)),
    ),
  );
  const assessment = assessObservingWindowConditions(points, context);

  return `
    <div class="conditions-timeline__conditions">
      <span class="conditions-timeline__summary-label">Observing conditions</span>
      <div class="conditions-grid conditions-grid--window">
        ${renderConditionCard('Moon illumination', formatValueRange(points.map((point) => point.moonIlluminationPct), (value) => `${value.toFixed(0)}%`))}
        ${renderConditionCard('Moon altitude', `${formatValueRange(points.map((point) => point.moonAltitudeDeg), (value) => `${value.toFixed(1)}°`)}${moonAboveHorizon == null ? '' : ` · ${moonAboveHorizon ? 'above horizon' : 'below horizon'}`}`)}
        ${renderConditionCard('Moon-target separation', formatValueRange(points.map((point) => point.moonTargetSeparationDeg), (value) => `${value.toFixed(1)}°`))}
        ${renderConditionCard('Dew risk', dewRisks.length ? dewRisks.join(' → ') : '—')}
        ${renderConditionCard('Wind', formatValueRange(points.map((point) => point.windSpeedKmh), (value) => `${value.toFixed(1)} km/h`))}
        ${renderConditionCard('Gusts', formatValueRange(points.map((point) => point.windGustsKmh), (value) => `${value.toFixed(1)} km/h`))}
      </div>
      <div class="conditions-assessment">
        <div class="conditions-assessment__header">
          <span class="conditions-timeline__summary-label">Condition assessment</span>
          <span class="conditions-assessment__profile">${escapeHtml(assessment.profileLabel)}</span>
        </div>
        <p class="conditions-assessment__note">${escapeHtml(assessment.contextNote)}</p>
        <div class="conditions-assessment__grid">
          ${assessment.factors.map((factor) => renderConditionAssessmentCard(factor)).join('')}
        </div>
      </div>
    </div>
  `;
}

function renderObservingWindowObservationContext(
  state: AppState,
  astronomy: AstronomicalConditionsSnapshot | null,
): string {
  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;
  const target = astronomy?.current?.target ?? null;

  const imagingProfile = selectedMosaic?.imaging_profile_label ?? null;
  const observationType = selectedMosaic?.observation_type ?? null;
  const filterName = selectedMosaic?.filter ?? null;

  return `
    <div class="conditions-timeline__conditions">
      <span class="conditions-timeline__summary-label">Observation context</span>
      <div class="conditions-grid conditions-grid--window">
        ${renderConditionCard('Target', target?.target_name ?? 'No target context selected')}
        ${renderConditionCard('Source', target?.source_kind ?? 'site only')}
        ${renderConditionCard('Imaging profile', imagingProfile ?? 'Not available in current model')}
        ${renderConditionCard('Observation type', observationType ?? 'Not available in current model')}
        ${renderConditionCard('Filter', filterName ?? 'Not available in current model')}
      </div>
    </div>
  `;
}

function getObservationIntentContext(state: AppState): ObservationIntentContext {
  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;

  return {
    observationType: selectedMosaic?.observation_type ?? null,
    filterName: selectedMosaic?.filter ?? null,
  };
}

function renderConditionAssessmentCard(factor: ConditionAssessmentFactor): string {
  return `
    <div class="conditions-assessment-card">
      <div class="conditions-assessment-card__header">
        <span class="conditions-assessment-card__label">${escapeHtml(factor.label)}</span>
        <span class="conditions-assessment-card__status conditions-assessment-card__status--${factor.status}">
          ${escapeHtml(factor.status)}
        </span>
      </div>
      <p class="conditions-assessment-card__reason">${escapeHtml(factor.reason)}</p>
    </div>
  `;
}

function renderHorizonCompass(
  azimuthDeg: number | null | undefined,
  altitudeDeg: number | null | undefined,
  timelinePoints: ConditionsTimelinePoint[],
  observingWindow: ObservingWindowRange | null,
  localHorizonProfile: LocalHorizonPoint[] = [],
  localHorizonAnalysis: LocalHorizonAnalysis | null = null,
  timeZone: string | undefined = undefined,
): string {
  if (azimuthDeg == null || !Number.isFinite(azimuthDeg)) {
    return `
      <div class="horizon-compass horizon-compass--empty">
        <span class="conditions-timeline__summary-label">Horizon compass</span>
        <p class="muted">Azimuth is not available for this target yet.</p>
      </div>
    `;
  }

  const normalizedAzimuth = normalizeAzimuth(azimuthDeg);
  const altitude = altitudeDeg != null && Number.isFinite(altitudeDeg) ? altitudeDeg : null;
  const cx = 116;
  const cy = 116;
  const radius = 82;
  const currentProjectedPoint = altitude == null
    ? projectHorizonCompassPoint(normalizedAzimuth, 0, cx, cy, radius)
    : projectHorizonCompassPoint(normalizedAzimuth, altitude, cx, cy, radius, { includeBelowHorizon: true });
  const markerX = currentProjectedPoint.x;
  const markerY = currentProjectedPoint.y;
  const directionLabel = formatCompassDirection(normalizedAzimuth);
  const localVisibilitySuffix = localHorizonAnalysis && localHorizonAnalysis.visibility !== 'not_configured'
    ? ` · ${formatLocalHorizonVisibility(localHorizonAnalysis)}`
    : '';
  const description = `${directionLabel} · ${normalizedAzimuth.toFixed(0)}° azimuth · ${formatAltitudeAboveBelow(altitude)}${localVisibilitySuffix}`;
  const cardinalPoints = [
    { label: 'N', azimuth: 0 },
    { label: 'NE', azimuth: 45 },
    { label: 'E', azimuth: 90 },
    { label: 'SE', azimuth: 135 },
    { label: 'S', azimuth: 180 },
    { label: 'SW', azimuth: 225 },
    { label: 'W', azimuth: 270 },
    { label: 'NW', azimuth: 315 },
  ];
  const altitudeRings = [30, 60, 90];
  const normalizedLocalHorizon = localHorizonProfile.length ? normalizeLocalHorizonProfile(localHorizonProfile) : [];
  const localHorizonPoints = normalizedLocalHorizon
    .map((point) => projectHorizonCompassPoint(point.azimuth_deg, point.min_altitude_deg, cx, cy, radius))
    .concat(normalizedLocalHorizon.length ? [
      projectHorizonCompassPoint(normalizedLocalHorizon[0]!.azimuth_deg, normalizedLocalHorizon[0]!.min_altitude_deg, cx, cy, radius),
    ] : []);
  const localHorizonPath = buildCompassPath(localHorizonPoints);
  const trajectoryPoints = timelinePoints
    .filter((point) => (
      point.targetAzimuthDeg != null
      && Number.isFinite(point.targetAzimuthDeg)
      && point.targetAltitudeDeg != null
      && Number.isFinite(point.targetAltitudeDeg)
    ))
    .map((point) => {
      const projected = projectHorizonCompassPoint(point.targetAzimuthDeg as number, point.targetAltitudeDeg as number, cx, cy, radius, { includeBelowHorizon: true });
      return {
        ...point,
        ...projected,
      };
    });
  const targetAboveHorizonPoints = trajectoryPoints.filter((point) => (point.targetAltitudeDeg ?? -90) >= 0);
  const targetBelowHorizonPoints = trajectoryPoints.filter((point) => (point.targetAltitudeDeg ?? 0) < 0);
  const moonTrajectoryPoints = timelinePoints
    .filter((point) => (
      point.moonAzimuthDeg != null
      && Number.isFinite(point.moonAzimuthDeg)
      && point.moonAltitudeDeg != null
      && Number.isFinite(point.moonAltitudeDeg)
    ))
    .map((point) => {
      const projected = projectHorizonCompassPoint(point.moonAzimuthDeg as number, point.moonAltitudeDeg as number, cx, cy, radius, { includeBelowHorizon: true });
      return {
        ...point,
        ...projected,
      };
    });
  const moonAboveHorizonPoints = moonTrajectoryPoints.filter((point) => (point.moonAltitudeDeg ?? -90) >= 0);
  const moonBelowHorizonPoints = moonTrajectoryPoints.filter((point) => (point.moonAltitudeDeg ?? 0) < 0);
  const trajectoryPath = buildCompassPath(targetAboveHorizonPoints);
  const moonTrajectoryPath = buildCompassPath(moonAboveHorizonPoints);
  const observingWindowPath = buildCompassPath(
    targetAboveHorizonPoints.filter((point) => (
      observingWindow != null
      && point.index >= observingWindow.startIndex
      && point.index <= observingWindow.endIndex
    )),
  );
  const observingWindowPoints = targetAboveHorizonPoints.filter((point) => (
    observingWindow != null
    && point.index >= observingWindow.startIndex
    && point.index <= observingWindow.endIndex
  ));
  return `
    <div class="horizon-compass">
      <div class="horizon-compass__header">
        <span class="conditions-timeline__summary-label">Horizon compass</span>
        <span class="horizon-compass__description">${escapeHtml(description)}</span>
      </div>
      <svg class="horizon-compass__svg" viewBox="0 0 232 232" role="img" aria-label="${escapeHtml(description)}">
        <circle class="horizon-compass__outer" cx="${cx}" cy="${cy}" r="${radius}" />
        <circle class="horizon-compass__inner" cx="${cx}" cy="${cy}" r="${(radius - 16).toFixed(2)}" />
        ${altitudeRings.map((ringAltitude) => {
          const ringRadius = projectHorizonCompassRadius(ringAltitude, radius);
          return `
            <circle class="horizon-compass__altitude-ring" cx="${cx}" cy="${cy}" r="${ringRadius.toFixed(2)}" />
            <text class="horizon-compass__ring-label" x="${cx}" y="${(cy - ringRadius - 4).toFixed(2)}" text-anchor="middle">${ringAltitude}°</text>
          `;
        }).join('')}
        ${cardinalPoints.map((point) => renderHorizonCompassLabel(point.label, point.azimuth, cx, cy, radius + 18)).join('')}
        ${cardinalPoints.map((point) => renderHorizonCompassTick(point.azimuth, cx, cy, radius)).join('')}
        <line class="horizon-compass__crosshair" x1="${cx}" y1="${(cy - radius).toFixed(2)}" x2="${cx}" y2="${(cy + radius).toFixed(2)}" />
        <line class="horizon-compass__crosshair" x1="${(cx - radius).toFixed(2)}" y1="${cy}" x2="${(cx + radius).toFixed(2)}" y2="${cy}" />
        ${localHorizonPath ? `<path class="horizon-compass__local-horizon" d="${localHorizonPath}" />` : ''}
        ${localHorizonPoints.length ? localHorizonPoints.slice(0, -1).map((point) => `
          <circle class="horizon-compass__local-horizon-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2.2" />
        `).join('') : ''}
        ${targetBelowHorizonPoints.map((point) => `
          <circle class="horizon-compass__hour-point horizon-compass__hour-point--below" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2.7">
            <title>${escapeHtml(`Target below horizon · ${formatHorizonCompassPointTitle(timeZone, point.timeText, point.targetAltitudeDeg, point.targetAzimuthDeg)}`)}</title>
          </circle>
        `).join('')}
        ${moonBelowHorizonPoints.map((point) => `
          <circle class="horizon-compass__moon-point horizon-compass__moon-point--below" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2">
            <title>${escapeHtml(`Moon below horizon · ${formatHorizonCompassPointTitle(timeZone, point.timeText, point.moonAltitudeDeg, point.moonAzimuthDeg)}`)}</title>
          </circle>
        `).join('')}
        ${trajectoryPath ? `<path class="horizon-compass__trajectory" d="${trajectoryPath}" />` : ''}
        ${moonTrajectoryPath ? `<path class="horizon-compass__moon-trajectory" d="${moonTrajectoryPath}" />` : ''}
        ${observingWindowPath ? `<path class="horizon-compass__trajectory horizon-compass__trajectory--window" d="${observingWindowPath}" />` : ''}
        ${observingWindowPoints.length > 0 && observingWindowPath.length === 0
          ? observingWindowPoints.map((point) => `
            <circle class="horizon-compass__window-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="5.5" />
          `).join('')
          : ''}
        ${observingWindowPoints.length > 0 ? `
          <circle class="horizon-compass__window-start" cx="${observingWindowPoints[0]!.x.toFixed(2)}" cy="${observingWindowPoints[0]!.y.toFixed(2)}" r="4.2" />
          <circle class="horizon-compass__window-end" cx="${observingWindowPoints[observingWindowPoints.length - 1]!.x.toFixed(2)}" cy="${observingWindowPoints[observingWindowPoints.length - 1]!.y.toFixed(2)}" r="4.2" />
        ` : ''}
        ${targetAboveHorizonPoints.map((point) => `
          <circle class="horizon-compass__hour-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="3">
            <title>${escapeHtml(formatHorizonCompassPointTitle(timeZone, point.timeText, point.targetAltitudeDeg, point.targetAzimuthDeg))}</title>
          </circle>
        `).join('')}
        ${moonAboveHorizonPoints.map((point) => `
          <circle class="horizon-compass__moon-point" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2.15">
            <title>${escapeHtml(`Moon · ${formatHorizonCompassPointTitle(timeZone, point.timeText, point.moonAltitudeDeg, point.moonAzimuthDeg)}`)}</title>
          </circle>
        `).join('')}
        <circle class="horizon-compass__current-glow" cx="${markerX.toFixed(2)}" cy="${markerY.toFixed(2)}" r="10" />
        ${trajectoryPoints.length ? `
          <circle class="horizon-compass__start-point" cx="${trajectoryPoints[0]!.x.toFixed(2)}" cy="${trajectoryPoints[0]!.y.toFixed(2)}" r="3.5">
            <title>${escapeHtml(`Start · ${formatHorizonCompassPointTitle(timeZone, trajectoryPoints[0]!.timeText, trajectoryPoints[0]!.targetAltitudeDeg, trajectoryPoints[0]!.targetAzimuthDeg)}`)}</title>
          </circle>
          <circle class="horizon-compass__end-point" cx="${trajectoryPoints[trajectoryPoints.length - 1]!.x.toFixed(2)}" cy="${trajectoryPoints[trajectoryPoints.length - 1]!.y.toFixed(2)}" r="3.5">
            <title>${escapeHtml(`End · ${formatHorizonCompassPointTitle(timeZone, trajectoryPoints[trajectoryPoints.length - 1]!.timeText, trajectoryPoints[trajectoryPoints.length - 1]!.targetAltitudeDeg, trajectoryPoints[trajectoryPoints.length - 1]!.targetAzimuthDeg)}`)}</title>
          </circle>
        ` : ''}
        <line class="horizon-compass__pointer" x1="${cx}" y1="${cy}" x2="${markerX.toFixed(2)}" y2="${markerY.toFixed(2)}" />
        <circle class="horizon-compass__marker-ring" cx="${markerX.toFixed(2)}" cy="${markerY.toFixed(2)}" r="8" />
        <circle class="horizon-compass__marker" cx="${markerX.toFixed(2)}" cy="${markerY.toFixed(2)}" r="5.5">
          <title>${escapeHtml(`Now · ${description}`)}</title>
        </circle>
        <circle class="horizon-compass__center" cx="${cx}" cy="${cy}" r="3.6" />
      </svg>
      <div class="horizon-compass__footer">
        <span class="horizon-compass__altitude-badge">${escapeHtml(altitude == null ? 'Alt —' : `Alt ${altitude.toFixed(0)}°`)}</span>
        ${moonTrajectoryPoints.length ? '<span class="horizon-compass__moon-badge">Moon path</span>' : ''}
        ${localHorizonPath ? `<span class="horizon-compass__local-horizon-badge">Local horizon · ${escapeHtml(formatLocalHorizonVisibility(localHorizonAnalysis))}</span>` : ''}
        ${observingWindow ? '<span class="horizon-compass__window-badge">Bright path = observing window</span>' : ''}
      </div>
    </div>
  `;
}

function renderHorizonCompassLabel(
  label: string,
  azimuthDeg: number,
  cx: number,
  cy: number,
  radius: number,
): string {
  const angleRad = ((azimuthDeg - 90) * Math.PI) / 180;
  const x = cx + Math.cos(angleRad) * radius;
  const y = cy + Math.sin(angleRad) * radius;
  return `<text class="horizon-compass__label" x="${x.toFixed(2)}" y="${(y + 4).toFixed(2)}" text-anchor="middle">${escapeHtml(label)}</text>`;
}

function renderHorizonCompassTick(
  azimuthDeg: number,
  cx: number,
  cy: number,
  radius: number,
): string {
  const angleRad = ((azimuthDeg - 90) * Math.PI) / 180;
  const x1 = cx + Math.cos(angleRad) * (radius - 6);
  const y1 = cy + Math.sin(angleRad) * (radius - 6);
  const x2 = cx + Math.cos(angleRad) * radius;
  const y2 = cy + Math.sin(angleRad) * radius;
  return `<line class="horizon-compass__tick" x1="${x1.toFixed(2)}" y1="${y1.toFixed(2)}" x2="${x2.toFixed(2)}" y2="${y2.toFixed(2)}" />`;
}

function projectHorizonCompassPoint(
  azimuthDeg: number,
  altitudeDeg: number,
  cx: number,
  cy: number,
  radius: number,
  options?: { includeBelowHorizon?: boolean },
): { x: number; y: number } {
  const normalizedAzimuth = normalizeAzimuth(azimuthDeg);
  const distance = projectHorizonCompassRadius(altitudeDeg, radius, options);
  const angleRad = ((normalizedAzimuth - 90) * Math.PI) / 180;
  return {
    x: cx + Math.cos(angleRad) * distance,
    y: cy + Math.sin(angleRad) * distance,
  };
}

function projectHorizonCompassRadius(
  altitudeDeg: number,
  radius: number,
  options?: { includeBelowHorizon?: boolean },
): number {
  const minimumAltitude = options?.includeBelowHorizon ? -18 : 0;
  const normalizedAltitude = Math.max(minimumAltitude, Math.min(90, altitudeDeg));
  return radius * (1 - normalizedAltitude / 90);
}

function buildCompassPath(points: Array<{ x: number; y: number }>): string {
  if (points.length < 2) {
    return '';
  }

  return points
    .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(' ');
}

function normalizeAzimuth(value: number): number {
  const normalized = value % 360;
  return normalized < 0 ? normalized + 360 : normalized;
}

function formatCompassDirection(azimuthDeg: number): string {
  const directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  return directions[Math.round(normalizeAzimuth(azimuthDeg) / 45) % directions.length] ?? 'N';
}

function formatHorizonCompassPointTitle(
  timeZone: string | undefined,
  timeText: string | null,
  altitudeDeg: number | null | undefined,
  azimuthDeg: number | null | undefined,
): string {
  const timeLabel = formatConditionsTimestamp(timeText, timeZone);
  const altitudeLabel = altitudeDeg == null || !Number.isFinite(altitudeDeg)
    ? 'Alt —'
    : `Alt ${altitudeDeg.toFixed(0)}°`;
  const azimuthLabel = azimuthDeg == null || !Number.isFinite(azimuthDeg)
    ? 'Az —'
    : `Az ${normalizeAzimuth(azimuthDeg).toFixed(0)}°`;
  return `${timeLabel} · ${altitudeLabel} · ${azimuthLabel}`;
}

function formatAltitudeAboveBelow(altitudeDeg: number | null): string {
  if (altitudeDeg == null || !Number.isFinite(altitudeDeg)) {
    return 'altitude unavailable';
  }
  if (altitudeDeg < 0) {
    return `${Math.abs(altitudeDeg).toFixed(0)}° below horizon`;
  }
  return `${altitudeDeg.toFixed(0)}° above horizon`;
}

function formatValueRange(
  values: Array<number | null | undefined>,
  formatter: (value: number) => string,
): string {
  const filtered = values.filter((value): value is number => value != null && Number.isFinite(value));
  if (!filtered.length) {
    return '—';
  }
  const min = Math.min(...filtered);
  const max = Math.max(...filtered);
  if (Math.abs(max - min) < 0.05) {
    return formatter(min);
  }
  return `${formatter(min)} → ${formatter(max)}`;
}

function formatConditionSummary(
  conditionCode: number | null | undefined,
  isDay: number | null | undefined,
): string {
  const label = getConditionCodeLabel(conditionCode);
  const phase = isDay == null ? null : (isDay === 1 ? 'day' : 'night');
  return phase ? `${label} · ${phase}` : label;
}

function getConditionCodeLabel(conditionCode: number | null | undefined): string {
  switch (conditionCode) {
    case 0:
      return 'clear';
    case 1:
      return 'mostly clear';
    case 2:
      return 'partly cloudy';
    case 3:
      return 'overcast';
    case 45:
    case 48:
      return 'fog';
    case 51:
    case 53:
    case 55:
    case 56:
    case 57:
      return 'drizzle';
    case 61:
    case 63:
    case 65:
    case 66:
    case 67:
    case 80:
    case 81:
    case 82:
      return 'rain';
    case 71:
    case 73:
    case 75:
    case 77:
    case 85:
    case 86:
      return 'snow';
    case 95:
    case 96:
    case 99:
      return 'storm';
    default:
      return conditionCode == null ? 'unknown' : `code ${conditionCode}`;
  }
}

function renderConditionSection(title: string, items: Array<[string, string]>): string {
  return `
    <section class="conditions-section">
      <div class="panel__header panel__header--nested conditions-section__header">
        <h3>${escapeHtml(title)}</h3>
      </div>
      <div class="conditions-grid">
        ${items.map(([label, value]) => renderConditionCard(label, value)).join('')}
      </div>
    </section>
  `;
}

function renderConditionCard(label: string, value: string): string {
  return `
    <div class="conditions-card">
      <span class="conditions-card__label">${escapeHtml(label)}</span>
      <span class="conditions-card__value">${escapeHtml(value)}</span>
    </div>
  `;
}

function renderConditionsMetadata(items: Array<[string, string]>): string {
  return `
    <div class="conditions-meta">
      ${items
        .map(
          ([label, value]) => `
            <div class="conditions-meta__item">
              <span class="conditions-meta__label">${escapeHtml(label)}</span>
              <span class="conditions-meta__value">${escapeHtml(value)}</span>
            </div>
          `,
        )
        .join('')}
    </div>
  `;
}

function isPreviewableCaptureFile(fileEntry: CaptureFileEntry): boolean {
  const suffix = fileEntry.suffix.toLowerCase();
  return ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'].includes(suffix);
}

function findSelectedCaptureFolder(capture: CaptureDetails, selectedFolderName: string | null) {
  if (!capture.folders.length) {
    return null;
  }
  if (selectedFolderName) {
    return capture.folders.find((folder) => folder.name === selectedFolderName) ?? capture.folders[0] ?? null;
  }
  return capture.folders[0] ?? null;
}

function findSelectedCaptureFile(
  folder: CaptureDetails['folders'][number] | null,
  selectedRelativePath: string | null,
): CaptureFileEntry | null {
  const allFiles = folder?.files ?? [];
  if (!allFiles.length) {
    return null;
  }
  if (selectedRelativePath) {
    return allFiles.find((fileEntry) => fileEntry.relative_path === selectedRelativePath) ?? allFiles[0] ?? null;
  }
  return allFiles[0] ?? null;
}
