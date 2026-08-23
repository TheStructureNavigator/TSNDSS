import iconUrl from '../assets/tsn_dss_icon.png';
import {
  getApiBaseUrl,
  getProjectRunOutputUrl,
  getProjectRunPreviewLogUrl,
  getProjectRunPreviewUrl,
  type ApiHealth,
  type ProjectRun,
  type ProjectSummary,
} from './api';

const APP_VERSION = '0.1.0';

export type ViewName = 'projects' | 'processing' | 'sky';

export type AppState = {
  health: ApiHealth | null;
  projects: ProjectSummary[];
  selectedProject: ProjectSummary | null;
  projectRuns: ProjectRun[];
  activeRun: ProjectRun | null;
  selectedProcessingCapture: string | null;
  currentView: ViewName;
  message: string | null;
  error: string | null;
  busy: boolean;
  projectDetailTab: ProjectDetailTab;
  processingDetailTab: ProcessingDetailTab;
  createProjectModalOpen: boolean;
  createRunModalOpen: boolean;
};

export type ProjectDetailTab = 'details' | 'import' | 'target' | 'settings';
export type ProcessingDetailTab = 'overview' | 'logs' | 'settings';

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
            ${renderNavItem('projects', 'Projects', state.currentView)}
            ${renderNavItem('processing', 'Processing', state.currentView)}
            ${renderNavItem('sky', 'Sky', state.currentView)}
          </nav>

          <div class="sidebar-watermark" aria-hidden="true">
            <img class="sidebar-watermark__icon" src="${iconUrl}" alt="" />
          </div>
        </aside>

        <main class="workspace">
          ${state.currentView === 'sky' ? renderWorkspaceHeader(state.currentView) : ''}
          ${renderView(state, primaryProject, latestRun)}
        </main>
      </div>

      <footer class="app-footer">
        <span>TSN DSS | TheStructureNavigator | ${APP_VERSION}</span>
      </footer>

      ${state.createProjectModalOpen ? renderCreateProjectModal(state.busy) : ''}
      ${state.createRunModalOpen ? renderCreateRunModal(state, primaryProject, state.selectedProcessingCapture) : ''}
      ${renderToast(state)}
    </div>
  `;
}

function renderNavItem(view: ViewName, label: string, currentView: ViewName): string {
  const selectedClass = currentView === view ? ' nav__item--active' : '';
  return `<button class="nav__item${selectedClass}" type="button" data-view="${view}">${label}</button>`;
}

function renderView(state: AppState, primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  if (state.currentView === 'processing') {
    return renderProcessingView(state, primaryProject, latestRun);
  }

  if (state.currentView === 'sky') {
    return renderSkyView(primaryProject, latestRun);
  }

  return renderProjectsView(state, primaryProject);
}

function renderProjectsView(state: AppState, primaryProject: ProjectSummary | null): string {
  return `
    <section class="projects-workspace">
      <article class="panel project-tabs-panel">
        <div class="panel__header">
          <h3>Projects</h3>
          <button class="action-button" type="button" data-open-create-project-modal ${state.busy ? 'disabled' : ''}>
            Create project
          </button>
        </div>
        ${renderProjectTabs(state.projects, primaryProject?.slug ?? null)}
      </article>

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
    <article class="panel project-detail-panel">
      <div class="panel__header">
        <h3>${project.slug}</h3>
      </div>

      <nav class="tab-strip" aria-label="Project details">
        ${renderProjectTab('details', 'Project details', state.projectDetailTab)}
        ${renderProjectTab('import', 'Import capture', state.projectDetailTab)}
        ${renderProjectTab('target', 'Sky target', state.projectDetailTab)}
        ${renderProjectTab('settings', 'Project settings', state.projectDetailTab)}
      </nav>

      <div class="project-detail-stack">
        ${renderSelectedProjectTab(state, project)}
      </div>
    </article>
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

  if (state.projectDetailTab === 'target') {
    return `
      <section class="detail-stack">
        <div class="panel__header panel__header--nested">
          <h3>Sky target</h3>
          <span>${project.sky_target ?? 'not set'}</span>
        </div>
        <form class="form-stack" data-form="project-sky-target">
          <label class="field">
            <span>Project slug</span>
            <input name="project_slug" type="text" value="${project.slug}" placeholder="m42_rebuild" required />
          </label>
          <label class="field">
            <span>Sky target</span>
            <input name="sky_target" type="text" value="${project.sky_target ?? ''}" placeholder="M42 / NGC 1976 / Orion Nebula" required />
          </label>
          <button class="action-button" type="submit" ${state.busy ? 'disabled' : ''}>Save target</button>
        </form>
      </section>
    `;
  }

  if (state.projectDetailTab === 'details') {
    const latestProjectRun = state.projectRuns[0] ?? null;
    return `
      <section class="detail-stack">
        ${renderProjectDetails(project, latestProjectRun)}
      </section>
    `;
  }

  return `
    <section class="detail-stack">
      <div class="panel__header panel__header--nested">
        <h3>Import capture</h3>
        <span>${project.slug}</span>
      </div>
      <section class="detail-stack">
        <div>
          <h4>Imported captures</h4>
          ${renderTagList(project.capture_names, 'No captures imported yet')}
        </div>
      </section>
      <form class="form-stack" data-form="import-capture">
        <label class="field">
          <span>Project slug</span>
          <input name="project_slug" type="text" value="${project.slug}" placeholder="m42_rebuild" required />
        </label>
        <label class="field">
          <span>Capture name</span>
          <input name="capture_name" type="text" value="" placeholder="OrionNebula" required />
        </label>
        <label class="field">
          <span>Source folder</span>
          <input name="source_dir" type="text" placeholder="C:\\path\\to\\OrionNebula" required />
        </label>
        <label class="checkbox-field">
          <input name="move" type="checkbox" />
          <span>Move instead of copy</span>
        </label>
        <button class="action-button" type="submit" ${state.busy ? 'disabled' : ''}>Import capture</button>
      </form>
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
      <article class="panel project-tabs-panel">
        <div class="panel__header">
          <h3>Captures</h3>
          <button class="action-button" type="button" data-open-create-run-modal ${state.busy ? 'disabled' : ''}>
            Create run
          </button>
        </div>
        ${renderProcessingCaptureTabs(primaryProject.capture_names, selectedCapture)}
        <div class="panel__header panel__header--nested">
          <h3>Runs</h3>
          <span>${selectedCapture ?? 'none'}</span>
        </div>
        ${renderRunTabs(runsForCapture, activeProcessingRun?.id ?? null)}
      </article>

      <article class="panel processing-detail-panel">
        <div class="panel__header">
          <h3>${activeProcessingRun?.id ?? 'none'}</h3>
          <span>Processing: ${activeProcessingRun?.status ?? 'idle'}</span>
        </div>

        <section class="detail-stack">
          ${renderRunDetailTabs(state.processingDetailTab, activeProcessingRun)}
          ${renderRunDetailPanel(state.processingDetailTab, activeProcessingRun)}
        </section>
      </article>
    </section>
  `;
}

function renderSkyView(primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
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
      <article class="panel">
        <div class="panel__header">
          <h3>Sky</h3>
          <span>${primaryProject.slug}</span>
        </div>
        <div class="detail-columns">
          <section>
            <dl class="health-list">
              <div>
                <dt>Active project</dt>
                <dd>${primaryProject.slug}</dd>
              </div>
              <div>
                <dt>Sky target</dt>
                <dd>${primaryProject.sky_target ?? 'not set'}</dd>
              </div>
            </dl>
          </section>
          <section>
            <dl class="health-list">
              <div>
                <dt>Captures</dt>
                <dd>${primaryProject.capture_count}</dd>
              </div>
              <div>
                <dt>Latest run</dt>
                <dd>${latestRun?.status ?? 'idle'}</dd>
              </div>
            </dl>
          </section>
        </div>
      </article>

      <section class="sky-layout">
        <article class="panel sky-panel sky-panel--full">
          <div class="panel__header">
            <h3>Sky viewer</h3>
            <span>${primaryProject.sky_target ?? 'M42'}</span>
          </div>
          <div id="aladin-sky-view" class="aladin-container"></div>
        </article>
      </section>

      <section class="grid sky-support-grid">
        <article class="panel">
          <div class="panel__header">
            <h3>Sky context</h3>
            <span>${latestRun?.status ?? 'idle'}</span>
          </div>
          <div class="detail-stack">
            <p class="muted">
              Aladin Lite is centered on the current project target when available, otherwise it falls back to M42.
            </p>
            <dl class="health-list">
              <div>
                <dt>Project root</dt>
                <dd>${primaryProject.project_root}</dd>
              </div>
              <div>
                <dt>Latest output</dt>
                <dd>${latestRun?.output_path ?? 'No output yet'}</dd>
              </div>
            </dl>
          </div>
        </article>

        <article class="panel">
          <div class="panel__header">
            <h3>What comes next</h3>
            <span>Planned</span>
          </div>
          <ul class="sidebar-list">
            <li>project target centering</li>
            <li>frame footprint overlays</li>
            <li>dataset highlight / selection</li>
            <li>processing result preview hooks</li>
          </ul>
        </article>
      </section>
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
        <h4>Live logs</h4>
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

function renderProjectDetails(project: ProjectSummary | null, latestProjectRun: ProjectRun | null): string {
  if (project === null) {
    return `<p class="muted">No project selected.</p>`;
  }

  return `
    <div class="details-split-layout project-details-layout">
      <div class="detail-stack">
        <dl class="health-list">
          <div>
            <dt>Project root</dt>
            <dd>${project.project_root}</dd>
          </div>
          <div>
            <dt>Captures dir</dt>
            <dd>${project.captures_dir}</dd>
          </div>
          <div>
            <dt>Runs dir</dt>
            <dd>${project.runs_dir}</dd>
          </div>
        </dl>

        <div class="detail-columns">
          <section>
            <h4>Captures</h4>
            ${renderTagList(project.capture_names, 'No captures')}
          </section>
          <section>
            <h4>Run folders</h4>
            ${renderTagList(project.run_names, 'No run folders yet')}
          </section>
        </div>
      </div>

      <section class="detail-stack details-preview-panel project-preview-panel">
        ${renderProjectPreviewPanel(latestProjectRun)}
      </section>
    </div>
  `;
}

function renderProjectPreviewPanel(run: ProjectRun | null): string {
  if (run === null) {
    return '<p class="muted">No processing output is available for this project yet.</p>';
  }

  if (run.preview_path) {
    return `
      <div class="preview-card">
        <a class="preview-card__link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">
          <img
            class="preview-card__image preview-card__image--project"
            src="${getProjectRunPreviewUrl(run.id)}"
            alt="Latest preview for project run ${run.id}"
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

function renderWorkspaceHeader(view: ViewName): string {
  return `
    <header class="workspace__header">
      <div>
        <p class="workspace__eyebrow">${eyebrowForView(view)}</p>
        <h2>${titleForView(view)}</h2>
      </div>
    </header>
  `;
}

function titleForView(view: ViewName): string {
  if (view === 'processing') {
    return 'Processing workspace';
  }
  if (view === 'sky') {
    return 'Sky workspace';
  }
  return 'Projects workspace';
}

function eyebrowForView(view: ViewName): string {
  if (view === 'processing') {
    return 'Siril runs and logs';
  }
  if (view === 'sky') {
    return 'Aladin Lite sky view';
  }
  return 'Projects and captures';
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
