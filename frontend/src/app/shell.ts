import iconUrl from '../assets/tsn_dss_icon.png';
import {
  type AstronomicalConditionsSnapshot,
  type CaptureDetails,
  type CaptureFileEntry,
  type CoreContent,
  getCaptureFileUrl,
  getCaptureThumbnailUrl,
  getApiBaseUrl,
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
  type SiteForecastSnapshot,
  type TelescopeAdapterDescriptor,
  type TelescopeSnapshot,
} from './api';

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
  siteForecast: SiteForecastSnapshot | null;
  astronomicalConditions: AstronomicalConditionsSnapshot | null;
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
] as const;

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
    <section class="projects-workspace">
      <div class="projects-toolbar">
        <span class="projects-toolbar__label">Observation Center</span>
      </div>

      <nav class="tab-strip project-detail-tabs" aria-label="Observation Center">
        ${renderObservationCenterTab('sites', 'Sites', state.observationCenterTab)}
        ${renderObservationCenterTab('conditions', 'Conditions', state.observationCenterTab)}
      </nav>

      <section class="project-gallery-layout">
        <aside class="project-gallery-sidebar">
          <div class="project-gallery-sidebar__action">
            <button class="action-button" type="button" data-clear-site-editor>
              Create site
            </button>
          </div>

          <section class="project-gallery-group">
            <div class="panel__header panel__header--nested">
              <h3>Sites</h3>
            </div>
            ${renderObservationSiteTabs(state.sites, state.activeSiteId)}
          </section>
        </aside>

        <section class="project-gallery-main">
          <div class="project-detail-stack">
            ${state.observationCenterTab === 'conditions'
              ? renderObservationConditionsPanel(state, activeSite)
              : renderObservationSitesPanel(state, activeSite)}
          </div>
        </section>
      </section>
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
    <article class="panel telescope-panel">
      <div class="panel__header">
        <h3>Observation site</h3>
        <span>${escapeHtml(activeSite?.name ?? 'new')}</span>
      </div>
      <form class="telescope-adapter-form" data-form="active-site">
        <label class="field">
          <span>Active site</span>
          <select name="site_id">
            <option value="" ${state.activeSiteId ? '' : 'selected'}>No active site</option>
            ${state.sites.map((site) => `
              <option value="${escapeHtml(site.id)}" ${site.id === state.activeSiteId ? 'selected' : ''}>
                ${escapeHtml(site.name)}
              </option>
            `).join('')}
          </select>
        </label>
        <button class="action-button telescope-panel__submit" type="submit">
          Apply site
        </button>
      </form>

      <div class="project-summary-strip project-summary-strip--sky">
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Latitude</span>
          <span class="project-summary-strip__value">${formatAngleValue(activeSite?.latitude_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Longitude</span>
          <span class="project-summary-strip__value">${formatAngleValue(activeSite?.longitude_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Elevation</span>
          <span class="project-summary-strip__value">${formatMeters(activeSite?.elevation_m)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Sky quality</span>
          <span class="project-summary-strip__value">${formatSiteQuality(activeSite)}</span>
        </div>
      </div>

      <form class="sky-simulator-form" data-form="site-editor">
        <input name="site_id" type="hidden" value="${escapeHtml(activeSite?.id ?? '')}" />
        <label class="field">
          <span>Site name</span>
          <input name="name" type="text" value="${escapeHtml(activeSite?.name ?? '')}" placeholder="Backyard / Bieszczady / Remote site" required />
        </label>
        <label class="field">
          <span>Latitude (deg)</span>
          <input name="latitude_deg" type="text" value="${activeSite?.latitude_deg ?? ''}" placeholder="50.1234" />
        </label>
        <label class="field">
          <span>Longitude (deg)</span>
          <input name="longitude_deg" type="text" value="${activeSite?.longitude_deg ?? ''}" placeholder="19.1234" />
        </label>
        <label class="field">
          <span>Elevation (m)</span>
          <input name="elevation_m" type="text" value="${activeSite?.elevation_m ?? ''}" placeholder="optional" />
        </label>
        <label class="field">
          <span>SQM</span>
          <input name="sqm_mag_arcsec2" type="text" value="${activeSite?.sqm_mag_arcsec2 ?? ''}" placeholder="optional" />
        </label>
        <label class="field">
          <span>Bortle</span>
          <input name="bortle_class" type="text" value="${activeSite?.bortle_class ?? ''}" placeholder="1-9" />
        </label>
        <label class="checkbox-field">
          <input name="south_horizon_open" type="checkbox" ${activeSite?.south_horizon_open ? 'checked' : ''} />
          <span>South horizon open</span>
        </label>
        <label class="field field--full">
          <span>Notes</span>
          <textarea name="notes" rows="3" placeholder="Optional site notes">${escapeHtml(activeSite?.notes ?? '')}</textarea>
        </label>
        <div class="telescope-panel__action-row">
          <button class="action-button telescope-panel__submit" type="submit">${activeSite ? 'Update site' : 'Create site'}</button>
          ${activeSite ? `<button class="action-button action-button--secondary" type="button" data-clear-site-editor>New site</button>` : ''}
          ${activeSite ? `<button class="action-button action-button--danger" type="button" data-delete-site-id="${escapeHtml(activeSite.id)}">Delete site</button>` : ''}
        </div>
      </form>
    </article>

    <article class="panel sky-panel sky-panel--full">
      <div class="panel__header">
        <h3>Site map</h3>
        <span>${state.sites.filter((site) => site.latitude_deg != null && site.longitude_deg != null).length} mapped</span>
      </div>
      <div id="observation-center-map" class="observation-map-container"></div>
      <p class="muted">
        Saved sites are shown on the map. The active site is highlighted, and clicking a marker selects it in Observation Center.
      </p>
    </article>
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
  if (!forecast && !astronomy) {
    return `
      <article class="panel">
        <p class="muted">Observation conditions are not available right now for this site.</p>
      </article>
    `;
  }

  return `
    <article class="panel telescope-panel">
      <div class="panel__header">
        <h3>Conditions</h3>
        <span>${escapeHtml(activeSite.name)}</span>
      </div>
      ${renderConditionsMetadata([
        ['Site', activeSite.name],
        ['Provider', [forecast?.provider, astronomy?.provider].filter(Boolean).join(' + ') || 'pending'],
        ['Timezone', forecast?.timezone ?? 'UTC / browser local view'],
        ['Updated', formatTimestampDisplay(currentAstronomy?.time_utc ?? currentWeather?.time ?? forecast?.generated_at ?? null)],
      ])}
      <div class="conditions-grid conditions-grid--summary">
        ${renderConditionCard('Current sky', formatSkyState(currentAstronomy?.sky_state))}
        ${renderConditionCard('Target', currentAstronomy?.target?.target_name ?? 'Site only')}
        ${renderConditionCard('Dew risk', formatDewRisk(currentWeather?.dew_risk))}
        ${renderConditionCard('Clouds', formatWeatherValue(currentWeather?.cloud_cover_pct, '%'))}
        ${renderConditionCard('Wind', formatWind(currentWeather?.wind_speed_kmh, currentWeather?.wind_direction_deg))}
        ${renderConditionCard('Weather status', formatConditionSummary(currentWeather?.condition_code, currentWeather?.is_day))}
      </div>
      <div class="conditions-sections">
        ${renderConditionSection(
          'Sky darkness',
          [
            ['Sky state', formatSkyState(currentAstronomy?.sky_state)],
            ['Sun altitude', formatAngleValue(currentAstronomy?.sun_altitude_deg)],
            ['Astronomical night', formatTimeRange(currentAstronomy?.astronomical_night_start_utc, currentAstronomy?.astronomical_night_end_utc)],
            ['Sun set / rise', formatTimeRange(currentAstronomy?.sunset_utc, currentAstronomy?.sunrise_utc)],
            ['Civil twilight', formatTimeRange(currentAstronomy?.civil_twilight_evening_end_utc, currentAstronomy?.civil_twilight_morning_start_utc)],
            ['Nautical twilight', formatTimeRange(currentAstronomy?.nautical_twilight_evening_end_utc, currentAstronomy?.nautical_twilight_morning_start_utc)],
          ],
        )}
        ${renderAstronomicalTargetSummary(currentAstronomy?.target ?? null, astronomy?.min_target_altitude_deg ?? 30)}
        ${renderConditionSection(
          'Moon',
          [
            ['Phase', formatMoonPhase(currentAstronomy?.moon_phase_label, currentAstronomy?.moon_illumination_pct)],
            ['Moon altitude', formatAngleValue(currentAstronomy?.moon_altitude_deg)],
            ['Moon rise / set', formatTimeRange(currentAstronomy?.moonrise_utc, currentAstronomy?.moonset_utc)],
          ],
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
            ['Wind', formatWind(currentWeather?.wind_speed_kmh, currentWeather?.wind_direction_deg)],
            ['Gusts', formatWeatherValue(currentWeather?.wind_gusts_kmh, 'km/h')],
            ['Precipitation', formatWeatherValue(currentWeather?.precipitation_mm, 'mm')],
            ['Precip chance', formatWeatherValue(currentWeather?.precipitation_probability_pct, '%')],
          ],
        )}
        ${renderConditionSection(
          'Cloud layers',
          [
            ['Total', formatWeatherValue(currentWeather?.cloud_cover_pct, '%')],
            ['Low', formatWeatherValue(currentWeather?.cloud_cover_low_pct, '%')],
            ['Mid', formatWeatherValue(currentWeather?.cloud_cover_mid_pct, '%')],
            ['High', formatWeatherValue(currentWeather?.cloud_cover_high_pct, '%')],
          ],
        )}
      </div>
    </article>

    <article class="panel">
      <div class="panel__header">
        <h3>Next 24 hours</h3>
        <span>${Math.max(forecast?.hourly.length ?? 0, astronomy?.hourly.length ?? 0)} points</span>
      </div>
      ${renderConditionsTimeline(state, forecast, astronomy)}
      ${renderConditionsHourlyTable(forecast, astronomy)}
    </article>
  `;
}

function renderAstronomicalTargetSummary(
  target: AstronomicalConditionsSnapshot['current']['target'] | null,
  minTargetAltitudeDeg: number,
): string {
  if (!target) {
    return renderConditionSection('Target visibility', [
      ['Target', 'Select a planned pointing, mosaic panel, or project sky target to see target-specific conditions.'],
    ]);
  }

  return renderConditionSection('Target visibility', [
    ['Target', target.target_name ?? 'Target'],
    ['Source', target.source_kind ?? 'target'],
    ['Alt / az', `${formatAngleValue(target.altitude_deg)} / ${formatAngleValue(target.azimuth_deg)}`],
    ['Airmass', formatAirmass(target.airmass)],
    ['Transit', formatTimestampDisplay(target.transit_time_utc)],
    ['Max altitude', formatAngleValue(target.max_altitude_deg)],
    ['Moon separation', formatAngleValue(target.moon_separation_deg)],
    ['Above horizon', formatObservationFlag(target.above_horizon)],
    [`Above ${minTargetAltitudeDeg.toFixed(0)}°`, formatObservationFlag(target.above_observation_threshold)],
    ['Night horizon window', formatWindowSummary(target.above_horizon_window_start_utc, target.above_horizon_window_end_utc, target.above_horizon_window_status)],
    ['Night observe window', formatWindowSummary(target.observation_window_start_utc, target.observation_window_end_utc, target.observation_window_status)],
  ]);
}

function renderConditionsTimeline(
  state: AppState,
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): string {
  const rowCount = Math.max(forecast?.hourly.length ?? 0, astronomy?.hourly.length ?? 0);
  if (rowCount < 2) {
    return '';
  }

  const currentAstronomy = astronomy?.current ?? null;
  const minTargetAltitudeDeg = astronomy?.min_target_altitude_deg ?? 30;
  const points = Array.from({ length: rowCount }, (_, index) => {
    const weatherHour = forecast?.hourly[index] ?? null;
    const astronomyHour = astronomy?.hourly[index] ?? null;
    return {
      index,
      timeText: astronomyHour?.time_utc ?? weatherHour?.time ?? null,
      timeMs: toTimestampMillis(astronomyHour?.time_utc ?? weatherHour?.time ?? null),
      skyState: astronomyHour?.sky_state ?? null,
      targetAltitudeDeg: astronomyHour?.target_altitude_deg ?? null,
      moonAltitudeDeg: astronomyHour?.moon_altitude_deg ?? null,
      cloudCoverPct: weatherHour?.cloud_cover_pct ?? null,
      moonIlluminationPct: astronomyHour?.moon_illumination_pct ?? null,
      moonTargetSeparationDeg: astronomyHour?.moon_target_separation_deg ?? null,
      dewRisk: weatherHour?.dew_risk ?? null,
      windSpeedKmh: weatherHour?.wind_speed_kmh ?? null,
      windGustsKmh: weatherHour?.wind_gusts_kmh ?? null,
    };
  });

  const validTimes = points
    .map((point) => point.timeMs)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const startMs = validTimes[0] ?? 0;
  const endMs = validTimes[validTimes.length - 1] ?? Math.max(1, rowCount - 1);
  const spanMs = Math.max(endMs - startMs, 1);

  const width = 960;
  const height = 364;
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
    const ms = toTimestampMillis(value);
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

  const observingFlags = points.map((point) => (
    point.skyState === 'astronomical_night'
    && point.targetAltitudeDeg != null
    && Number.isFinite(point.targetAltitudeDeg)
    && point.targetAltitudeDeg >= minTargetAltitudeDeg
    && point.cloudCoverPct != null
    && Number.isFinite(point.cloudCoverPct)
    && point.cloudCoverPct <= OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT
  ));

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
    if (
      point.cloudCoverPct == null
      || !Number.isFinite(point.cloudCoverPct)
      || point.cloudCoverPct > OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT
    ) {
      reasons.push('Cloud cover above threshold');
    }
    return reasons;
  });

  let activeWindowStartIndex: number | null = null;
  const observingWindows: Array<{ startIndex: number; endIndex: number; startMs: number; endMs: number }> = [];
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
  const observingWindow = observingWindows
    .slice()
    .sort((leftWindow, rightWindow) => {
      const leftDuration = leftWindow.endMs - leftWindow.startMs;
      const rightDuration = rightWindow.endMs - rightWindow.startMs;
      if (rightDuration !== leftDuration) {
        return rightDuration - leftDuration;
      }
      return leftWindow.startMs - rightWindow.startMs;
    })[0] ?? null;
  const observingWindowPoints = observingWindow
    ? points.slice(observingWindow.startIndex, observingWindow.endIndex + 1)
    : [];

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
        <text class="conditions-timeline__tick-label" x="${x.toFixed(2)}" y="${labelY.toFixed(2)}" text-anchor="middle">${escapeHtml(formatTimelineTick(point.timeText))}</text>
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
        <svg class="conditions-timeline__svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Timeline of darkness, target altitude, moon altitude, cloud cover and observing window for the next 24 hours.">
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
                )
              : 'No observing window',
          )}
        </strong>
        <span class="conditions-timeline__summary-hint">
          target ≥ ${minTargetAltitudeDeg.toFixed(0)}° · clouds ≤ ${OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT}%
        </span>
      </div>
      ${renderObservingWindowObservationContext(state, astronomy)}
      ${renderObservingWindowConditions(observingWindowPoints)}
      <div class="conditions-timeline__factors">
        <span class="conditions-timeline__factors-label">Limiting factors</span>
        ${rejectionSummary || '<span class="conditions-timeline__factors-empty">None inside the visible range.</span>'}
      </div>
    </section>
  `;
}

function renderConditionsHourlyTable(
  forecast: SiteForecastSnapshot | null,
  astronomy: AstronomicalConditionsSnapshot | null,
): string {
  const rowCount = Math.max(forecast?.hourly.length ?? 0, astronomy?.hourly.length ?? 0);
  if (rowCount === 0) {
    return '<p class="muted">No hourly observation points are available for this site yet.</p>';
  }

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
            <th>Airmass</th>
            <th>Moon sep</th>
          </tr>
        </thead>
        <tbody>
          ${Array.from({ length: rowCount }, (_, index) => {
            const weatherHour = forecast?.hourly[index] ?? null;
            const astronomyHour = astronomy?.hourly[index] ?? null;
            return `
            <tr>
              <td>${escapeHtml(formatTimestampDisplay(astronomyHour?.time_utc ?? weatherHour?.time ?? null))}</td>
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
    return '<p class="muted">No observation sites yet.</p>';
  }

  return `
    <div class="run-tab-list">
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
    .replaceAll('>', '&gt;');
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

function formatMoonPhase(label: string | null | undefined, illuminationPct: number | null | undefined): string {
  if (!label && illuminationPct == null) {
    return '—';
  }
  if (!label) {
    return formatPercent(illuminationPct);
  }
  if (illuminationPct == null) {
    return label;
  }
  return `${label} · ${formatPercent(illuminationPct)}`;
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

function formatTimeRange(start: string | null | undefined, end: string | null | undefined): string {
  if (!start && !end) {
    return '—';
  }
  return `${formatTimestampDisplay(start)} → ${formatTimestampDisplay(end)}`;
}

function formatWindowSummary(
  start: string | null | undefined,
  end: string | null | undefined,
  status: string | null | undefined,
): string {
  if (status === 'always_up') {
    return `all night · ${formatTimeRange(start, end)}`;
  }
  if (status === 'always_observable') {
    return `all night above threshold · ${formatTimeRange(start, end)}`;
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
  return formatTimeRange(start, end);
}

function formatTimelineTick(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return value;
  }
  return timestamp.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function toTimestampMillis(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatObservingWindowRange(start: string | null | undefined, end: string | null | undefined): string {
  if (!start || !end) {
    return 'No observing window';
  }

  const startDate = new Date(start);
  const endDate = new Date(end);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) {
    return `${start ?? '—'} → ${end ?? '—'}`;
  }

  const sameDay = startDate.toDateString() === endDate.toDateString();
  const startLabel = startDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const endLabel = endDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay) {
    return `${startLabel} → ${endLabel}`;
  }
  return `${startDate.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} → ${endDate.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
}

function renderObservingWindowConditions(
  points: Array<{
    moonIlluminationPct: number | null;
    moonAltitudeDeg: number | null;
    moonTargetSeparationDeg: number | null;
    dewRisk: string | null;
    windSpeedKmh: number | null;
    windGustsKmh: number | null;
  }>,
): string {
  if (!points.length) {
    return '';
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
