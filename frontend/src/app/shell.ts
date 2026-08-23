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

const APP_VERSION = '0.0.0';

export type ViewName = 'projects' | 'processing' | 'sky';

export type AppState = {
  health: ApiHealth | null;
  projects: ProjectSummary[];
  selectedProject: ProjectSummary | null;
  projectRuns: ProjectRun[];
  activeRun: ProjectRun | null;
  currentView: ViewName;
  message: string | null;
  error: string | null;
  busy: boolean;
  projectDetailTab: ProjectDetailTab;
};

export type ProjectDetailTab = 'import' | 'target' | 'details';

export function renderAppShell(state: AppState): string {
  const primaryProject = state.selectedProject ?? state.projects[0] ?? null;
  const latestRun = state.activeRun ?? state.projectRuns[0] ?? null;

  return `
    <div class="app-frame">
      <header class="topbar">
        <div class="topbar__brand">
          <img class="topbar__icon" src="${iconUrl}" alt="TSN DSS icon" />
          <div>
            <h1>TSN | DSS</h1>
          </div>
        </div>
        <div class="status-pill">${state.health?.status === 'ok' ? 'API online' : 'API offline'}</div>
      </header>

      <div class="app-shell">
        <aside class="sidebar">
          <p class="sidebar__intro">Projects, processing and sky view are now split into separate workspaces.</p>

          <nav class="nav">
            ${renderNavItem('projects', 'Projects', state.currentView)}
            ${renderNavItem('processing', 'Processing', state.currentView)}
            ${renderNavItem('sky', 'Sky', state.currentView)}
          </nav>

          <section class="sidebar-section">
            <h2>Current focus</h2>
            <ul class="sidebar-list">
              <li>${primaryProject?.slug ?? 'Waiting for project data'}</li>
              <li>${latestRun?.status ?? 'No processing run yet'}</li>
              <li>${state.currentView === 'sky' ? 'Aladin Lite mount ready' : latestRun?.stage ?? 'Ready'}</li>
            </ul>
          </section>
        </aside>

        <main class="workspace">
          <header class="workspace__header">
            <div>
              <p class="workspace__eyebrow">${eyebrowForView(state.currentView)}</p>
              <h2>${titleForView(state.currentView)}</h2>
            </div>
          </header>

          ${renderBanner(state)}
          ${renderView(state, primaryProject, latestRun)}
        </main>
      </div>

      <footer class="app-footer">
        <span>TSN DSS | TheStructureNavigator | ${APP_VERSION}</span>
      </footer>
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
    <section class="hero-panel">
      <div>
        <h3>Project management is separated now</h3>
        <p>
          This workspace focuses only on creating projects, importing captures and browsing project structure.
        </p>
      </div>
      <div class="hero-metrics">
        <article class="metric-card">
          <span class="metric-card__label">Projects</span>
          <strong>${state.projects.length}</strong>
          <span>${primaryProject ? `${primaryProject.capture_count} captures in focus` : 'No local projects yet'}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">Selected project</span>
          <strong>${primaryProject?.slug ?? 'None'}</strong>
          <span>${primaryProject?.sky_target ?? (primaryProject ? 'Set sky target next' : 'Create a project to begin')}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">API endpoint</span>
          <strong>${state.health?.service ?? 'Unavailable'}</strong>
          <span>${getApiBaseUrl()}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">API health</span>
          <strong>${state.health?.status ?? 'offline'}</strong>
          <span>${state.health?.default_siril_executable ?? 'siril-cli'}</span>
        </article>
      </div>
    </section>

    <section class="projects-layout">
      <article class="panel projects-sidebar-panel">
        <div class="panel__header">
          <h3>Create project</h3>
          <span>${state.busy ? 'working...' : 'ready'}</span>
        </div>
        <form class="form-stack" data-form="create-project">
          <label class="field">
            <span>Project slug</span>
            <input name="slug" type="text" placeholder="m42_rebuild" required />
          </label>
          <button class="action-button" type="submit" ${state.busy ? 'disabled' : ''}>Create project</button>
        </form>

        <div class="panel__header projects-sidebar-panel__header">
          <h3>Projects</h3>
          <span>${state.projects.length} loaded</span>
        </div>
        ${renderProjects(state.projects, primaryProject?.slug ?? null)}
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
        <span>${project.capture_count} captures</span>
      </div>

      <nav class="tab-strip" aria-label="Project details">
        ${renderProjectTab('import', 'Import capture', state.projectDetailTab)}
        ${renderProjectTab('target', 'Sky target', state.projectDetailTab)}
        ${renderProjectTab('details', 'Project details', state.projectDetailTab)}
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
            <input name="sky_target" type="text" value="${project.sky_target ?? 'M42'}" placeholder="M42 / NGC 1976 / Orion Nebula" required />
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
        <div class="panel__header panel__header--nested">
          <h3>Project details</h3>
          <span>${project.run_count} runs</span>
        </div>
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
      <form class="form-stack" data-form="import-capture">
        <label class="field">
          <span>Project slug</span>
          <input name="project_slug" type="text" value="${project.slug}" placeholder="m42_rebuild" required />
        </label>
        <label class="field">
          <span>Capture name</span>
          <input name="capture_name" type="text" value="OrionNebula" placeholder="OrionNebula" required />
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
  return `
    <section class="hero-panel">
      <div>
        <h3>Processing has its own workspace now</h3>
        <p>
          Launch Siril runs here and keep all status, progress and logs away from project management.
        </p>
      </div>
      <div class="hero-metrics">
        <article class="metric-card">
          <span class="metric-card__label">Latest run</span>
          <strong>${latestRun?.status ?? 'Idle'}</strong>
          <span>${latestRun?.stage ?? 'Ready to launch Siril'}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">Selected project</span>
          <strong>${primaryProject?.slug ?? 'None'}</strong>
          <span>${primaryProject?.capture_names[0] ?? 'Import a capture first'}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">Siril executable</span>
          <strong>${state.health?.default_siril_executable ?? 'siril-cli'}</strong>
          <span>Default run target</span>
        </article>
      </div>
    </section>

    <section class="grid">
      <article class="panel">
        <div class="panel__header">
          <h3>Run project processing</h3>
          <span>${latestRun?.status ?? 'idle'}</span>
        </div>
        <form class="form-stack" data-form="start-run">
          <label class="field">
            <span>Project slug</span>
            <input name="project_slug" type="text" value="${primaryProject?.slug ?? ''}" placeholder="m42_rebuild" required />
          </label>
          <label class="field">
            <span>Capture name</span>
            <input name="capture_name" type="text" value="${primaryProject?.capture_names[0] ?? 'OrionNebula'}" placeholder="OrionNebula" required />
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
          <button class="action-button" type="submit" ${state.busy ? 'disabled' : ''}>Run OSC_Preprocessing</button>
        </form>
      </article>

      <article class="panel">
        <div class="panel__header">
          <h3>Run folders</h3>
          <span>${primaryProject?.run_count ?? 0} folders</span>
        </div>
        ${renderTagList(primaryProject?.run_names ?? [], 'No run folders yet')}
      </article>

      <article class="panel">
        <div class="panel__header">
          <h3>Recent runs</h3>
          <span>${state.projectRuns.length}</span>
        </div>
        ${renderRunList(state.projectRuns, latestRun?.id ?? null)}
      </article>

      <article class="panel panel--wide">
        <div class="panel__header">
          <h3>Run monitor</h3>
          <span>${latestRun?.id ?? 'no run selected'}</span>
        </div>
        ${renderRunMonitor(latestRun)}
      </article>
    </section>
  `;
}

function renderSkyView(primaryProject: ProjectSummary | null, latestRun: ProjectRun | null): string {
  return `
    <section class="hero-panel">
      <div>
        <h3>Sky view is prepared for Aladin Lite</h3>
        <p>
          This workspace is now reserved for sky navigation, framing and later target / dataset overlays.
        </p>
      </div>
      <div class="hero-metrics">
        <article class="metric-card">
          <span class="metric-card__label">Current target</span>
          <strong>${primaryProject?.sky_target ?? 'M42'}</strong>
          <span>${primaryProject?.sky_target ? 'Project-centered sky focus' : 'Bootstrap sky focus'}</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">Selected project</span>
          <strong>${primaryProject?.slug ?? 'None'}</strong>
          <span>${primaryProject?.capture_count ?? 0} captures</span>
        </article>
        <article class="metric-card">
          <span class="metric-card__label">Latest processing</span>
          <strong>${latestRun?.status ?? 'Idle'}</strong>
          <span>${latestRun?.output_path ?? 'No output yet'}</span>
        </article>
      </div>
    </section>

    <section class="grid sky-grid">
      <article class="panel panel--wide sky-panel">
        <div class="panel__header">
          <h3>Sky viewer</h3>
          <span>Aladin Lite</span>
        </div>
        <div id="aladin-sky-view" class="aladin-container"></div>
      </article>

      <article class="panel">
        <div class="panel__header">
          <h3>Sky context</h3>
          <span>${primaryProject?.sky_target ?? 'M42'}</span>
        </div>
        <div class="detail-stack">
          <p class="muted">
            Aladin Lite is centered on the current project target when available, otherwise it falls back to M42.
          </p>
          ${primaryProject ? `<code class="command">${primaryProject.project_root}</code>` : '<p class="muted">No selected project.</p>'}
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
  `;
}

function renderBanner(state: AppState): string {
  if (state.error) {
    return `<div class="banner banner--error">${state.error}</div>`;
  }
  if (state.message) {
    return `<div class="banner banner--success">${state.message}</div>`;
  }
  return '';
}

function renderProjects(projects: ProjectSummary[], selectedSlug: string | null): string {
  if (!projects.length) {
    return `
      <p class="muted">
        No projects were found under the local <code>projects/</code> directory yet.
      </p>
    `;
  }

  return `
    <div class="project-list">
      ${projects
        .map((project) => {
          const selectedClass = project.slug === selectedSlug ? ' project-card--selected' : '';
          return `
            <button class="project-card${selectedClass}" type="button" data-project-slug="${project.slug}">
              <div class="project-card__header">
                <h4>${project.slug}</h4>
                <span>${project.run_count} run folders</span>
              </div>
              <dl class="project-meta">
                <div>
                  <dt>Captures</dt>
                  <dd>${project.capture_count}</dd>
                </div>
                <div>
                  <dt>Runs</dt>
                  <dd>${project.run_count}</dd>
                </div>
              </dl>
              <p class="project-path">${project.project_root}</p>
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderRunMonitor(run: ProjectRun | null): string {
  if (run === null) {
    return `<p class="muted">No processing run yet. Import a capture and launch Siril.</p>`;
  }

  const outputPath = run.output_path ? `<code class="command">${run.output_path}</code>` : '<p class="muted">No output yet.</p>';
  const errorBlock = run.error_message ? `<div class="banner banner--error">${run.error_message}</div>` : '';
  const previewBlock = renderRunPreview(run);
  const outputActions = run.output_path
    ? `
        <div class="inline-actions">
          <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">Open output FITS</a>
        </div>
      `
    : '';

  return `
    <div class="run-monitor">
      <div class="run-monitor__summary">
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

      <div class="detail-columns">
        <section>
          <h4>Output</h4>
          ${outputPath}
          ${outputActions}
        </section>
        <section>
          <h4>Command</h4>
          <code class="command">${run.command.join(' ')}</code>
        </section>
      </div>

      <section>
        <h4>Preview</h4>
        ${previewBlock}
      </section>

      <section>
        <h4>Live logs</h4>
        <pre class="log-view">${escapeHtml(run.combined_log || 'Waiting for process output...')}</pre>
      </section>
    </div>
  `;
}

function renderRunList(runs: ProjectRun[], selectedRunId: string | null): string {
  if (!runs.length) {
    return '<p class="muted">No runs recorded for this project yet.</p>';
  }

  return `
    <div class="run-list">
      ${runs
        .map((run) => {
          const selectedClass = run.id === selectedRunId ? ' run-list__item--selected' : '';
          return `
            <button class="run-list__item${selectedClass}" type="button" data-run-id="${run.id}">
              <strong>${run.capture_name}</strong>
              <span>${run.status}</span>
              <small>${run.stage}</small>
            </button>
          `;
        })
        .join('')}
    </div>
  `;
}

function renderRunPreview(run: ProjectRun): string {
  if (run.preview_path) {
    return `
      <div class="preview-card">
        <img
          class="preview-card__image"
          src="${getProjectRunPreviewUrl(run.id)}"
          alt="Preview for run ${run.id}"
        />
        <div class="inline-actions">
          <a class="secondary-link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">Open preview image</a>
          <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">Open FITS</a>
        </div>
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
    <div class="project-details-layout">
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
          <div>
            <dt>Latest output</dt>
            <dd>${renderProjectOutputInline(latestProjectRun)}</dd>
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

      <section class="detail-stack">
        <h4>Latest preview</h4>
        ${renderProjectPreviewPanel(latestProjectRun)}
      </section>
    </div>
  `;
}

function renderProjectOutputInline(run: ProjectRun | null): string {
  if (run === null) {
    return '<span class="muted">No output yet</span>';
  }

  if (run.preview_path) {
    return `
      <span class="inline-output-links">
        <a class="secondary-link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">Preview</a>
        <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">FITS</a>
      </span>
    `;
  }

  if (run.output_path) {
    return `
      <span class="inline-output-links">
        <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">FITS only</a>
      </span>
    `;
  }

  return `<span class="muted">${escapeHtml(run.status)}</span>`;
}

function renderProjectPreviewPanel(run: ProjectRun | null): string {
  if (run === null) {
    return '<p class="muted">No processing output is available for this project yet.</p>';
  }

  if (run.preview_path) {
    return `
      <div class="preview-card">
        <img
          class="preview-card__image preview-card__image--project"
          src="${getProjectRunPreviewUrl(run.id)}"
          alt="Latest preview for project run ${run.id}"
        />
        <div class="inline-actions">
          <a class="secondary-link" href="${getProjectRunPreviewUrl(run.id)}" target="_blank" rel="noreferrer">Open preview image</a>
          <a class="secondary-link" href="${getProjectRunOutputUrl(run.id)}" target="_blank" rel="noreferrer">Open FITS</a>
        </div>
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
