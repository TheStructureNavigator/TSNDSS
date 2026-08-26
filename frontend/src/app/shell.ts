import iconUrl from '../assets/tsn_dss_icon.png';
import coreContent from '../content/core-content.json';
import {
  type CaptureDetails,
  type CaptureFileEntry,
  getCaptureFileUrl,
  getCaptureThumbnailUrl,
  getApiBaseUrl,
  getProjectRunArtifactUrl,
  getProjectRunOutputUrl,
  getProjectRunPreviewLogUrl,
  getProjectRunPreviewUrl,
  type ApiHealth,
  type ProjectRun,
  type ProjectSummary,
  type RunArtifactImage,
} from './api';

type CoreContent = {
  current_version: string;
  releases: Array<{
    version: string;
    date?: string;
    changelog: string[];
  }>;
  todo: string[];
};

const typedCoreContent = coreContent as CoreContent;
const currentRelease =
  typedCoreContent.releases.find((release) => release.version === typedCoreContent.current_version)
  ?? typedCoreContent.releases[0]
  ?? { version: typedCoreContent.current_version, changelog: [] };
const APP_VERSION = typedCoreContent.current_version;
const historicalReleases = typedCoreContent.releases.filter((release) => release.version !== currentRelease.version);

export type ViewName = 'core' | 'projects' | 'processing' | 'sky';

export type AppState = {
  health: ApiHealth | null;
  projects: ProjectSummary[];
  selectedProject: ProjectSummary | null;
  selectedProjectRunId: string | null;
  selectedProjectCapture: string | null;
  selectedProjectCaptureDetails: CaptureDetails | null;
  selectedProjectCaptureFile: string | null;
  selectedProjectCaptureFolder: string | null;
  projectRuns: ProjectRun[];
  activeRun: ProjectRun | null;
  selectedProcessingCapture: string | null;
  currentView: ViewName;
  message: string | null;
  error: string | null;
  busy: boolean;
  projectDetailTab: ProjectDetailTab;
  processingDetailTab: ProcessingDetailTab;
  skyDetailTab: SkyDetailTab;
  createProjectModalOpen: boolean;
  createRunModalOpen: boolean;
  createImportCaptureModalOpen: boolean;
};

export type ProjectDetailTab = 'gallery' | 'import' | 'settings';
export type ProcessingDetailTab = 'overview' | 'logs' | 'settings';
export type SkyDetailTab = 'viewer';

export function renderAppShell(state: AppState): string {
  const primaryProject = state.selectedProject ?? state.projects[0] ?? null;
  const latestRun = state.activeRun ?? state.projectRuns[0] ?? null;
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

  return `
    <div class="app-frame">
      <header class="topbar">
        <div class="topbar__brand">
          <img class="topbar__icon" src="${iconUrl}" alt="TSN DSS icon" />
          <div>
            <h1>TSN | DSS</h1>
          </div>
        </div>
        <div class="topbar__status-group">
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
        <span>TSN DSS | TheStructureNavigator | ${APP_VERSION}</span>
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

  if (state.currentView === 'sky') {
    return renderSkyView(state, primaryProject, latestRun);
  }

  return renderProjectsView(state, primaryProject);
}

function renderCoreView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  return `
    <section class="core-workspace">
      <section class="project-detail-stack">
        <div class="project-summary-strip">
          <div class="project-summary-strip__item">
            <span class="project-summary-strip__label">Version</span>
            <span class="project-summary-strip__value">${APP_VERSION}</span>
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

        <article class="panel">
          <p class="core-lead">
            TheStructureNavigator Deep Space System is a local workspace for projects, captures, preprocessing, and downstream image review.
          </p>
        </article>
      </section>

      <div class="core-columns">
        <article class="panel">
          <div class="panel__header">
            <h3>Changelog</h3>
            <span>v${escapeHtml(APP_VERSION)}</span>
          </div>
          <div class="release-stack">
            <section class="release-card release-card--current">
              <div class="panel__header panel__header--nested">
                <h3>v${escapeHtml(currentRelease.version)}</h3>
                <span>${escapeHtml(currentRelease.date ?? 'Current')}</span>
              </div>
              <ul class="core-list">
                ${currentRelease.changelog.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
              </ul>
            </section>

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
            ${typedCoreContent.todo.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}
          </ul>
        </article>
      </div>
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

function renderSkyView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
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

  return `
    <section class="sky-workspace">
      <section class="processing-main">
        <div class="project-detail-stack">
          ${renderSkyDetailPanel('viewer', primaryProject, latestRun)}
        </div>
      </section>
    </section>
  `;
}

function renderSkyDetailPanel(activeTab: SkyDetailTab, primaryProject: ProjectSummary, latestRun: ProjectRun | null): string {
  return `
    <section class="detail-stack">
      <article class="panel sky-panel sky-panel--full">
        <div id="aladin-sky-view" class="aladin-container"></div>
      </article>
    </section>
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

function renderRunDetailTabs(activeTab: ProcessingDetailTab, run: ProjectRun | null): string {
  if (run === null) {
    return '';
  }

  return `
    <nav class="tab-strip" aria-label="Run details">
      ${renderProcessingTab('overview', 'Overview', activeTab)}
      ${renderProcessingTab('logs', 'Logs', activeTab)}
      ${renderProcessingTab('settings', 'Run settings', activeTab)}
    </nav>
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

function renderProjectGeneratedPreviewPanel(run: ProjectRun | null): string {
  if (run === null) {
    return '<p class="muted">No processing output is available for this project yet.</p>';
  }

  if (run.preview_path) {
    return `
      <div class="preview-card project-preview-card">
        <a class="preview-card__link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">
          <img
            class="preview-card__image preview-card__image--project project-preview-card__image"
            src="${getProjectRunPreviewUrl(run.id)}"
            alt="Latest preview for project run ${run.id}"
            data-preview-image
            data-preview-fallback="Preview image is not available yet."
          />
        </a>
      </div>
    `;
  }

  if (run.output_path) {
    const previewLogLink = run.preview_log_path
      ? `<a class="secondary-link" href="${getProjectRunPreviewLogUrl(run.id)}" target="_blank" rel="noreferrer">Open preview export log</a>`
      : '';
    return `
      <div class="detail-stack">
        <p class="muted">Output FITS exists, but preview image is not available yet.</p>
        <div class="inline-actions">
          <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">Open FITS</a>
          ${previewLogLink}
        </div>
      </div>
    `;
  }

  return `<p class="muted">The latest run has not produced an output file yet.</p>`;
}

function renderProjectEditedGallery(run: ProjectRun | null): string {
  if (run === null) {
    return '';
  }

  return `
    <article class="panel project-edited-panel">
      <div class="panel__header">
        <h3>Edited</h3>
        <span>${run.artifact_images.length} image${run.artifact_images.length === 1 ? '' : 's'}</span>
      </div>
      ${renderProjectEditedPanel(run)}
    </article>
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

function renderTagList(values: string[], emptyLabel: string): string {
  if (!values.length) {
    return `<p class="muted">${emptyLabel}</p>`;
  }

  return `<div class="project-tags">${values.map((value) => `<span>${value}</span>`).join('')}</div>`;
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

function formatLogForDisplay(value: string): string {
  return value
    .split(/\r?\n/)
    .filter((line) => line.length > 0)
    .reverse()
    .join('\n');
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
