import './style.css';
import {
  createProject,
  deleteProjectRun,
  deleteProject,
  fetchHealth,
  fetchProject,
  fetchProjectRun,
  fetchProjectRuns,
  fetchProjects,
  generateProjectRunPreview,
  importCapture,
  startProjectRun,
  updateProjectSkyTarget,
  type ProjectSummary,
} from './app/api';
import { mountSkyView } from './app/sky';
import { renderAppShell, type AppState, type ProcessingDetailTab, type ProjectDetailTab, type SkyDetailTab, type ViewName } from './app/shell';

const appElement = document.querySelector<HTMLDivElement>('#app');

if (appElement === null) {
  throw new Error('Missing #app root element.');
}

const rootElement = appElement;
let activeRunPollTimer: number | null = null;
let toastTimer: number | null = null;
const TOAST_DURATION_MS = 3200;

const state: AppState = {
  health: null,
  projects: [],
  selectedProject: null,
  projectRuns: [],
  activeRun: null,
  selectedProcessingCapture: null,
  currentView: 'projects',
  message: null,
  error: null,
  busy: false,
  projectDetailTab: 'details',
  processingDetailTab: 'overview',
  skyDetailTab: 'viewer',
  createProjectModalOpen: false,
  createRunModalOpen: false,
};

void bootstrap();

async function bootstrap(): Promise<void> {
  await refreshState();
}

async function refreshState(preferredSlug?: string): Promise<void> {
  const [health, projects] = await Promise.all([
    fetchHealth().catch(() => null),
    fetchProjects().catch(() => []),
  ]);

  state.health = health;
  state.projects = projects;

  const selectedSlug = preferredSlug ?? state.selectedProject?.slug ?? projects[0]?.slug ?? null;
  state.selectedProject = selectedSlug ? await loadProjectDetails(selectedSlug, projects) : null;
  state.projectRuns = state.selectedProject ? await fetchProjectRuns(state.selectedProject.slug).catch(() => []) : [];
  syncSelectedProcessingCapture();
  if (state.activeRun !== null) {
    const matched = state.projectRuns.find((run) => run.id === state.activeRun?.id);
    if (matched) {
      state.activeRun = matched;
    }
  }
  syncActiveRunForSelectedCapture();
  syncRunPolling();
  render();
}

function render(): void {
  rootElement.innerHTML = renderAppShell(state);
  bindNavigation();
  bindProjectSelection();
  bindProjectTabs();
  bindProcessingTabs();
  bindSkyTabs();
  bindProcessingCaptureSelection();
  bindProjectActions();
  bindCreateProjectModal();
  bindCreateRunModal();
  bindRunSelection();
  bindPreviewActions();
  bindPreviewImageFallbacks();
  bindForms();
  if (state.currentView === 'sky') {
    void mountSkyView('aladin-sky-view', state.selectedProject?.sky_target ?? 'M42');
  }
}

function bindNavigation(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-view]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const nextView = button.dataset.view as ViewName | undefined;
      if (nextView === undefined) {
        return;
      }
      state.currentView = nextView;
      render();
    });
  });
}

function bindProjectSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-project-slug]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      void selectProject(button.dataset.projectSlug ?? '');
    });
  });
}

function bindProjectTabs(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-project-tab]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const nextTab = button.dataset.projectTab as ProjectDetailTab | undefined;
      if (nextTab === undefined) {
        return;
      }
      state.projectDetailTab = nextTab;
      render();
    });
  });
}

function bindProcessingTabs(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-processing-tab]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const nextTab = button.dataset.processingTab as ProcessingDetailTab | undefined;
      if (nextTab === undefined) {
        return;
      }
      state.processingDetailTab = nextTab;
      render();
    });
  });
}

function bindSkyTabs(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-sky-tab]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const nextTab = button.dataset.skyTab as SkyDetailTab | undefined;
      if (nextTab === undefined) {
        return;
      }
      state.skyDetailTab = nextTab;
      render();
    });
  });
}

function bindProcessingCaptureSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-processing-capture]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const captureName = button.dataset.processingCapture ?? '';
      if (!captureName) {
        return;
      }
      state.selectedProcessingCapture = captureName;
      state.processingDetailTab = 'overview';
      syncActiveRunForSelectedCapture();
      syncRunPolling();
      render();
    });
  });
}

function bindProjectActions(): void {
  const projectButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-delete-project-slug]');
  projectButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const slug = button.dataset.deleteProjectSlug ?? '';
      if (!slug) {
        return;
      }
      void handleDeleteProject(slug);
    });
  });

  const runButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-delete-run-id]');
  runButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const runId = button.dataset.deleteRunId ?? '';
      if (!runId) {
        return;
      }
      void handleDeleteRun(runId);
    });
  });
}

function bindCreateProjectModal(): void {
  const openButton = rootElement.querySelector<HTMLButtonElement>('[data-open-create-project-modal]');
  openButton?.addEventListener('click', () => {
    state.createProjectModalOpen = true;
    render();
  });

  const closeButtons = rootElement.querySelectorAll<HTMLElement>('[data-close-create-project-modal]');
  closeButtons.forEach((element) => {
    element.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null;
      const modalCard = rootElement.querySelector<HTMLElement>('[data-modal-card]');
      if (modalCard && target && modalCard.contains(target) && !target.hasAttribute('data-close-create-project-modal')) {
        return;
      }
      state.createProjectModalOpen = false;
      render();
    });
  });
}

function bindCreateRunModal(): void {
  const openButton = rootElement.querySelector<HTMLButtonElement>('[data-open-create-run-modal]');
  openButton?.addEventListener('click', () => {
    state.createRunModalOpen = true;
    render();
  });

  const closeButtons = rootElement.querySelectorAll<HTMLElement>('[data-close-create-run-modal]');
  closeButtons.forEach((element) => {
    element.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null;
      const modalCard = rootElement.querySelector<HTMLElement>('[data-run-modal-card]');
      if (modalCard && target && modalCard.contains(target) && !target.hasAttribute('data-close-create-run-modal')) {
        return;
      }
      state.createRunModalOpen = false;
      render();
    });
  });
}

function bindRunSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-run-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const runId = button.dataset.runId ?? '';
      if (!runId) {
        return;
      }
      const selectedRun = state.projectRuns.find((run) => run.id === runId);
      if (selectedRun === undefined) {
        return;
      }
      state.activeRun = selectedRun;
      state.selectedProcessingCapture = selectedRun.capture_name;
      state.processingDetailTab = 'overview';
      syncRunPolling();
      render();
    });
  });
}

function bindPreviewActions(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-generate-preview-run-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const runId = button.dataset.generatePreviewRunId ?? '';
      if (!runId) {
        return;
      }
      void handleGeneratePreview(runId);
    });
  });
}

function bindPreviewImageFallbacks(): void {
  const images = rootElement.querySelectorAll<HTMLImageElement>('[data-preview-image]');
  images.forEach((image) => {
    image.addEventListener('error', () => {
      const previewCard = image.closest('.preview-card');
      if (previewCard === null) {
        return;
      }
      const fallbackMessage = image.dataset.previewFallback ?? 'Preview image is not available yet.';
      previewCard.innerHTML = `<p class="muted">${escapeHtml(fallbackMessage)}</p>`;
    }, { once: true });
  });
}

function bindForms(): void {
  const createForm = rootElement.querySelector<HTMLFormElement>('[data-form="create-project"]');
  const importForm = rootElement.querySelector<HTMLFormElement>('[data-form="import-capture"]');
  const startRunForm = rootElement.querySelector<HTMLFormElement>('[data-form="start-run"]');
  const skyTargetForm = rootElement.querySelector<HTMLFormElement>('[data-form="project-sky-target"]');

  createForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleCreateProject(createForm);
  });

  importForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleImportCapture(importForm);
  });

  startRunForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleStartRun(startRunForm);
  });

  skyTargetForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleProjectSkyTarget(skyTargetForm);
  });
}

async function handleCreateProject(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const slug = String(formData.get('slug') ?? '').trim();
  if (!slug) {
    setError('Project slug is required.');
    return;
  }

  setBusy(true);
  try {
    const project = await createProject(slug);
    state.createProjectModalOpen = false;
    setMessage(`Project created: ${project.slug}`);
    await refreshState(project.slug);
    form.reset();
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleImportCapture(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const projectSlug = String(formData.get('project_slug') ?? '').trim();
  const captureName = String(formData.get('capture_name') ?? '').trim();
  const sourceDir = String(formData.get('source_dir') ?? '').trim();
  const move = formData.get('move') === 'on';

  if (!projectSlug || !captureName || !sourceDir) {
    setError('Project slug, capture name and source folder are required.');
    return;
  }

  setBusy(true);
  try {
    const result = await importCapture({
      project_slug: projectSlug,
      capture_name: captureName,
      source_dir: sourceDir,
      move,
    });
    setMessage(`Capture imported to ${result.project.slug}: ${captureName}`);
    await refreshState(result.project.slug);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleStartRun(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const projectSlug = String(formData.get('project_slug') ?? '').trim();
  const captureName = String(formData.get('capture_name') ?? '').trim();
  const executable = String(formData.get('executable') ?? '').trim();
  const scriptPath = String(formData.get('script_path') ?? '').trim();
  const keepProcessDir = formData.get('keep_process_dir') === 'on';

  if (!projectSlug || !captureName) {
    setError('Project slug and capture name are required to start a run.');
    return;
  }

  setBusy(true);
  try {
    const run = await startProjectRun({
      project_slug: projectSlug,
      capture_name: captureName,
      executable: executable || undefined,
      script_path: scriptPath || undefined,
      keep_process_dir: keepProcessDir,
    });
    state.createRunModalOpen = false;
    state.activeRun = run;
    state.selectedProcessingCapture = run.capture_name;
    state.processingDetailTab = 'overview';
    state.currentView = 'processing';
    setMessage(`Run started: ${run.id}`);
    await refreshState(projectSlug);
    syncRunPolling();
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleProjectSkyTarget(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const projectSlug = String(formData.get('project_slug') ?? '').trim();
  const skyTarget = String(formData.get('sky_target') ?? '').trim();

  if (!projectSlug || !skyTarget) {
    setError('Project slug and sky target are required.');
    return;
  }

  setBusy(true);
  try {
    const project = await updateProjectSkyTarget(projectSlug, skyTarget);
    state.selectedProject = project;
    setMessage(`Sky target saved for ${project.slug}: ${project.sky_target ?? skyTarget}`);
    await refreshState(project.slug);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleGeneratePreview(runId: string): Promise<void> {
  setBusy(true);
  try {
    const run = await generateProjectRunPreview(runId);
    state.activeRun = run;
    if (state.selectedProject !== null) {
      state.projectRuns = await fetchProjectRuns(state.selectedProject.slug).catch(() => state.projectRuns);
    }
    setMessage(run.preview_path ? 'Preview generated successfully.' : (run.preview_error ?? 'Preview generation finished without JPEG output.'));
    render();
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleDeleteProject(slug: string): Promise<void> {
  const confirmed = window.confirm(`Delete project "${slug}" with all captures, runs, logs and outputs?`);
  if (!confirmed) {
    return;
  }

  setBusy(true);
  try {
    await deleteProject(slug);
    if (state.selectedProject?.slug === slug) {
      state.selectedProject = null;
      state.projectRuns = [];
      state.activeRun = null;
      syncRunPolling();
    }
    state.projectDetailTab = 'details';
    setMessage(`Project deleted: ${slug}`);
    await refreshState();
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleDeleteRun(runId: string): Promise<void> {
  const confirmed = window.confirm(`Delete run "${runId}" with all logs, outputs and workspace files?`);
  if (!confirmed) {
    return;
  }

  setBusy(true);
  try {
    await deleteProjectRun(runId);
    if (state.activeRun?.id === runId) {
      state.activeRun = null;
    }
    state.processingDetailTab = 'overview';
    setMessage(`Run deleted: ${runId}`);
    if (state.selectedProject !== null) {
      await refreshState(state.selectedProject.slug);
    } else {
      await refreshState();
    }
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function selectProject(slug: string): Promise<void> {
  if (!slug) {
    return;
  }

  state.selectedProject = await loadProjectDetails(slug, state.projects);
  state.projectDetailTab = 'details';
  state.processingDetailTab = 'overview';
  state.projectRuns = state.selectedProject ? await fetchProjectRuns(state.selectedProject.slug).catch(() => []) : [];
  syncSelectedProcessingCapture();
  syncActiveRunForSelectedCapture();
  syncRunPolling();
  render();
}

async function loadProjectDetails(slug: string, projects: ProjectSummary[]): Promise<ProjectSummary | null> {
  try {
    return await fetchProject(slug);
  } catch {
    return projects.find((project) => project.slug === slug) ?? null;
  }
}

function syncRunPolling(): void {
  if (activeRunPollTimer !== null) {
    window.clearInterval(activeRunPollTimer);
    activeRunPollTimer = null;
  }

  if (state.activeRun === null || ['completed', 'failed'].includes(state.activeRun.status)) {
    return;
  }

  activeRunPollTimer = window.setInterval(() => {
    void pollActiveRun();
  }, 1000);
}

async function pollActiveRun(): Promise<void> {
  if (state.activeRun === null) {
    return;
  }

  try {
    const run = await fetchProjectRun(state.activeRun.id);
    state.activeRun = run;
    state.selectedProcessingCapture = run.capture_name;
    if (state.selectedProject !== null) {
      state.projectRuns = await fetchProjectRuns(state.selectedProject.slug).catch(() => state.projectRuns);
    }
    syncSelectedProcessingCapture();
    syncActiveRunForSelectedCapture();
    if (['completed', 'failed'].includes(run.status)) {
      syncRunPolling();
      if (state.selectedProject !== null) {
        await refreshState(state.selectedProject.slug);
        return;
      }
    }
    render();
  } catch {
    // Keep current screen state; transient polling failures should not break the UI.
  }
}

function setBusy(nextBusy: boolean): void {
  state.busy = nextBusy;
  render();
}

function setMessage(message: string): void {
  state.message = message;
  state.error = null;
  scheduleToastDismiss();
  render();
}

function setError(message: string): void {
  state.error = message;
  state.message = null;
  scheduleToastDismiss();
  render();
}

function scheduleToastDismiss(): void {
  if (toastTimer !== null) {
    window.clearTimeout(toastTimer);
    toastTimer = null;
  }

  toastTimer = window.setTimeout(() => {
    state.message = null;
    state.error = null;
    toastTimer = null;
    render();
  }, TOAST_DURATION_MS);
}

function syncSelectedProcessingCapture(): void {
  const captureNames = state.selectedProject?.capture_names ?? [];
  if (!captureNames.length) {
    state.selectedProcessingCapture = null;
    return;
  }

  if (state.selectedProcessingCapture && captureNames.includes(state.selectedProcessingCapture)) {
    return;
  }

  const activeRunCapture = state.activeRun?.capture_name;
  if (activeRunCapture && captureNames.includes(activeRunCapture)) {
    state.selectedProcessingCapture = activeRunCapture;
    return;
  }

  state.selectedProcessingCapture = captureNames[0] ?? null;
}

function syncActiveRunForSelectedCapture(): void {
  const selectedCapture = state.selectedProcessingCapture;
  if (!selectedCapture) {
    state.activeRun = state.projectRuns[0] ?? null;
    return;
  }

  const matchingRuns = state.projectRuns.filter((run) => run.capture_name === selectedCapture);
  if (!matchingRuns.length) {
    state.activeRun = null;
    return;
  }

  if (state.activeRun && state.activeRun.capture_name === selectedCapture) {
    const refreshedActiveRun = matchingRuns.find((run) => run.id === state.activeRun?.id);
    state.activeRun = refreshedActiveRun ?? matchingRuns[0] ?? null;
    return;
  }

  state.activeRun = matchingRuns[0] ?? null;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return 'Unexpected error.';
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}
