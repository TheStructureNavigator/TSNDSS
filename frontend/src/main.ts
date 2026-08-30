import './style.css';
import {
  clearPlannedTelescopePointing,
  createSite,
  createProject,
  createMosaicPlan,
  deleteSite,
  deleteMosaicPlan,
  fetchCoreContent,
  fetchCaptureDetails,
  fetchMosaics,
  deleteProjectRun,
  deleteProject,
  fetchHealth,
  fetchProject,
  fetchProjectRun,
  fetchProjectRuns,
  fetchProjects,
  fetchSites,
  fetchTelescopeAdapters,
  fetchTelescopeState,
  generateMosaicPanels,
  generateProjectRunPreview,
  importCapture,
  selectMosaicPanel,
  setActiveSite,
  setActiveTelescopeAdapter,
  slewToPlannedTelescopePointing,
  startProjectRun,
  type MosaicPlan,
  updateMosaicPanel,
  updatePlannedTelescopePointing,
  updateSimulatorTelescopeState,
  updateProjectSkyTarget,
  updateSite,
  type ProjectSummary,
} from './app/api';
import {
  mountObservationCenterMap,
  preserveObservationCenterMapState,
} from './app/observation_center_map';
import { mountSkyView, preserveSkyViewState } from './app/sky';
import {
  renderAppShell,
  type AppState,
  type ProcessingDetailTab,
  type ProjectDetailTab,
  type SkyDetailTab,
  type ThemeName,
  type ViewName,
} from './app/shell';

/**
 * TSN DSS frontend entrypoint.
 *
 * Responsibilities:
 * - owns the single mutable `state`
 * - coordinates API fetches and polling
 * - binds DOM events after each render
 * - hands Sky snapshots to the dedicated Aladin wrapper
 *
 * Rendering itself stays in `shell.ts`.
 */
const appElement = document.querySelector<HTMLDivElement>('#app');

if (appElement === null) {
  throw new Error('Missing #app root element.');
}

const rootElement = appElement;
let activeRunPollTimer: number | null = null;
let telescopePollTimer: number | null = null;
let toastTimer: number | null = null;
const TOAST_DURATION_MS = 3200;
const THEME_STORAGE_KEY = 'tsn_dss_theme';

// One shared application state keeps this no-framework UI predictable.
const state: AppState = {
  coreContent: null,
  health: null,
  sites: [],
  activeSiteId: null,
  projects: [],
  selectedProject: null,
  selectedProjectRunId: null,
  selectedProjectCapture: null,
  selectedProjectCaptureDetails: null,
  selectedProjectCaptureFile: null,
  selectedProjectCaptureFolder: null,
  projectRuns: [],
  mosaics: [],
  selectedMosaicId: null,
  selectedMosaicPanelId: null,
  activeRun: null,
  selectedProcessingCapture: null,
  currentView: 'core',
  theme: loadThemePreference(),
  telescopeSnapshot: null,
  telescopeAdapters: [],
  activeTelescopeAdapterId: null,
  followTelescope: false,
  message: null,
  error: null,
  busy: false,
  projectDetailTab: 'gallery',
  processingDetailTab: 'overview',
  skyDetailTab: 'telescope',
  createProjectModalOpen: false,
  createRunModalOpen: false,
  createImportCaptureModalOpen: false,
};

void bootstrap();

async function bootstrap(): Promise<void> {
  await refreshState();
}

async function refreshState(preferredSlug?: string): Promise<void> {
  const [coreContent, health, projects, sitesPayload, telescopeAdaptersPayload] = await Promise.all([
    fetchCoreContent().catch(() => state.coreContent),
    fetchHealth().catch(() => null),
    fetchProjects().catch(() => []),
    fetchSites().catch(() => null),
    fetchTelescopeAdapters().catch(() => null),
  ]);

  state.coreContent = coreContent;
  state.health = health;
  state.sites = sitesPayload?.sites ?? state.sites;
  state.activeSiteId = sitesPayload?.active_site_id ?? state.activeSiteId;
  state.projects = projects;
  state.telescopeAdapters = telescopeAdaptersPayload?.adapters ?? state.telescopeAdapters;
  state.activeTelescopeAdapterId = telescopeAdaptersPayload?.active_adapter_id ?? state.activeTelescopeAdapterId;
  state.telescopeSnapshot = await fetchTelescopeState().catch(() => state.telescopeSnapshot);

  const selectedSlug = preferredSlug ?? state.selectedProject?.slug ?? projects[0]?.slug ?? null;
  state.selectedProject = selectedSlug ? await loadProjectDetails(selectedSlug, projects) : null;
  state.projectRuns = state.selectedProject ? await fetchProjectRuns(state.selectedProject.slug).catch(() => []) : [];
  state.mosaics = state.selectedProject ? await fetchMosaics(state.selectedProject.slug).catch(() => []) : [];
  syncSelectedProjectRun();
  syncSelectedMosaic();
  await syncSelectedProjectCapture();
  syncSelectedProcessingCapture();
  if (state.activeRun !== null) {
    const matched = state.projectRuns.find((run) => run.id === state.activeRun?.id);
    if (matched) {
      state.activeRun = matched;
    }
  }
  syncActiveRunForSelectedCapture();
  syncRunPolling();
  syncTelescopePolling();
  render();
}

// Render is intentionally followed by explicit event rebinding because the app
// replaces the workspace HTML on each state change.
function render(): void {
  const workspaceScrollTop = getWorkspaceScrollTop();
  const preservedObservationCenterMapElement = state.currentView === 'observationcenter'
    ? rootElement.querySelector<HTMLElement>('#observation-center-map')
    : null;
  const preservedSkyViewElement = state.currentView === 'sky'
    ? rootElement.querySelector<HTMLElement>('#aladin-sky-view')
    : null;
  if (state.currentView === 'observationcenter') {
    preserveObservationCenterMapState();
  }
  if (state.currentView === 'sky') {
    preserveSkyViewState();
  }
  rootElement.innerHTML = renderAppShell(state);
  if (preservedObservationCenterMapElement) {
    const nextObservationCenterMapElement = rootElement.querySelector<HTMLElement>('#observation-center-map');
    nextObservationCenterMapElement?.replaceWith(preservedObservationCenterMapElement);
  }
  if (preservedSkyViewElement) {
    const nextSkyViewElement = rootElement.querySelector<HTMLElement>('#aladin-sky-view');
    nextSkyViewElement?.replaceWith(preservedSkyViewElement);
  }
  bindNavigation();
  bindThemeToggle();
  bindProjectSelection();
  bindProjectTabs();
  bindProjectRunSelection();
  bindProjectCaptureSelection();
  bindProjectCaptureFolderSelection();
  bindProjectCaptureFileSelection();
  bindProcessingTabs();
  bindSkyTabs();
  bindObservationSiteSelection();
  bindMosaicSelection();
  bindMosaicPanelSelection();
  bindMosaicActions();
  bindProcessingCaptureSelection();
  bindProjectActions();
  bindCreateProjectModal();
  bindCreateRunModal();
  bindImportCaptureModal();
  bindRunSelection();
  bindPreviewActions();
  bindPreviewImageFallbacks();
  bindForms();
  bindInlineProjectSkyTarget();
  bindSkyFollowToggle();
  bindTelescopeAdapterForm();
  bindActiveSiteForm();
  bindSiteEditorForm();
  bindSiteActions();
  hydrateSkySimulatorPanel();
  if (state.currentView === 'observationcenter') {
    void refreshObservationCenterMapLive();
  }
  if (state.currentView === 'sky') {
    void refreshSkyViewLive();
  }
  if (workspaceScrollTop > 0) {
    scheduleWorkspaceScrollRestore(workspaceScrollTop);
  }
}

function bindThemeToggle(): void {
  const button = rootElement.querySelector<HTMLButtonElement>('[data-theme-toggle]');
  button?.addEventListener('click', () => {
    state.theme = state.theme === 'dark' ? 'observation' : 'dark';
    persistThemePreference(state.theme);
    render();
  });
}

function bindProjectRunSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-project-run-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const runId = button.dataset.projectRunId ?? '';
      if (!runId) {
        return;
      }
      const selectedRun = state.projectRuns.find((run) => run.id === runId);
      if (selectedRun === undefined) {
        return;
      }
      state.selectedProjectRunId = selectedRun.id;
      render();
    });
  });
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

function bindProjectCaptureSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-project-capture]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const captureName = button.dataset.projectCapture ?? '';
      if (!captureName) {
        return;
      }
      void selectProjectCapture(captureName);
    });
  });
}

function bindProjectCaptureFolderSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-capture-folder]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const folderName = button.dataset.captureFolder ?? '';
      if (!folderName) {
        return;
      }
      const workspaceScrollTop = getWorkspaceScrollTop();
      state.selectedProjectCaptureFolder = folderName;
      syncSelectedProjectCaptureFile();
      render();
      scheduleWorkspaceScrollRestore(workspaceScrollTop);
    });
  });
}

function bindProjectCaptureFileSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-capture-file]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const relativePath = button.dataset.captureFile ?? '';
      if (!relativePath) {
        return;
      }
      const workspaceScrollTop = getWorkspaceScrollTop();
      state.selectedProjectCaptureFile = relativePath;
      render();
      scheduleWorkspaceScrollRestore(workspaceScrollTop);
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

function bindObservationSiteSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-observation-site-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const siteId = button.dataset.observationSiteId ?? '';
      if (!siteId) {
        return;
      }
      void handleSetActiveSiteById(siteId);
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

function bindImportCaptureModal(): void {
  const openButton = rootElement.querySelector<HTMLButtonElement>('[data-open-import-capture-modal]');
  openButton?.addEventListener('click', () => {
    state.createImportCaptureModalOpen = true;
    render();
  });

  const closeButtons = rootElement.querySelectorAll<HTMLElement>('[data-close-import-capture-modal]');
  closeButtons.forEach((element) => {
    element.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null;
      const modalCard = rootElement.querySelector<HTMLElement>('[data-import-capture-modal-card]');
      if (modalCard && target && modalCard.contains(target) && !target.hasAttribute('data-close-import-capture-modal')) {
        return;
      }
      state.createImportCaptureModalOpen = false;
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
  const skySimulatorForm = rootElement.querySelector<HTMLFormElement>('[data-form="sky-simulator"]');
  const telescopeAdapterForm = rootElement.querySelector<HTMLFormElement>('[data-form="telescope-adapter"]');
  const activeSiteForm = rootElement.querySelector<HTMLFormElement>('[data-form="active-site"]');
  const siteEditorForm = rootElement.querySelector<HTMLFormElement>('[data-form="site-editor"]');
  const createMosaicForm = rootElement.querySelector<HTMLFormElement>('[data-form="create-mosaic"]');
  const updateMosaicPanelStatusForm = rootElement.querySelector<HTMLFormElement>('[data-form="update-mosaic-panel-status"]');

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

  skySimulatorForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleSkySimulatorUpdate(skySimulatorForm);
  });

  telescopeAdapterForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleTelescopeAdapterSwitch(telescopeAdapterForm);
  });

  activeSiteForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleActiveSiteChange(activeSiteForm);
  });

  siteEditorForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleSiteEditorSubmit(siteEditorForm);
  });

  createMosaicForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleCreateMosaic(createMosaicForm);
  });

  updateMosaicPanelStatusForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    void handleUpdateMosaicPanelStatus(updateMosaicPanelStatusForm);
  });
}

function bindActiveSiteForm(): void {
  const form = rootElement.querySelector<HTMLFormElement>('[data-form="active-site"]');
  if (!form) {
    return;
  }

  const select = form.elements.namedItem('site_id');
  if (select instanceof HTMLSelectElement) {
    select.value = state.activeSiteId ?? '';
  }
}

function bindSiteEditorForm(): void {
  const clearButton = rootElement.querySelector<HTMLButtonElement>('[data-clear-site-editor]');
  clearButton?.addEventListener('click', () => {
    const form = rootElement.querySelector<HTMLFormElement>('[data-form="site-editor"]');
    if (!form) {
      return;
    }

    setFormFieldValue(form, 'site_id', '');
    setFormFieldValue(form, 'name', '');
    setFormFieldValue(form, 'latitude_deg', '');
    setFormFieldValue(form, 'longitude_deg', '');
    setFormFieldValue(form, 'elevation_m', '');
    setFormFieldValue(form, 'sqm_mag_arcsec2', '');
    setFormFieldValue(form, 'bortle_class', '');
    setFormFieldValue(form, 'notes', '');

    const southHorizonField = form.elements.namedItem('south_horizon_open');
    if (southHorizonField instanceof HTMLInputElement) {
      southHorizonField.checked = false;
    }
  });
}

function bindSiteActions(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-delete-site-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const siteId = button.dataset.deleteSiteId ?? '';
      if (!siteId) {
        return;
      }
      void handleDeleteSite(siteId);
    });
  });
}

function bindMosaicSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-mosaic-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const mosaicId = button.dataset.mosaicId ?? '';
      if (!mosaicId) {
        return;
      }
      state.selectedMosaicId = mosaicId;
      syncSelectedMosaic();
      render();
    });
  });
}

function bindMosaicPanelSelection(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-mosaic-panel-id]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const panelId = button.dataset.mosaicPanelId ?? '';
      if (!panelId || !state.selectedMosaicId) {
        return;
      }
      void handleSelectMosaicPanel(state.selectedMosaicId, panelId);
    });
  });
}

function bindMosaicActions(): void {
  const buttons = rootElement.querySelectorAll<HTMLButtonElement>('[data-generate-mosaic-panels]');
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const mosaicId = button.dataset.generateMosaicPanels ?? '';
      if (!mosaicId) {
        return;
      }
      void handleGenerateMosaicPanels(mosaicId);
    });
  });

  const planButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-plan-mosaic-panel]');
  planButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const panelId = button.dataset.planMosaicPanel ?? '';
      if (!panelId) {
        return;
      }
      void handlePlanMosaicPanel(panelId);
    });
  });

  const clearButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-clear-planned-pointing]');
  clearButtons.forEach((button) => {
    button.addEventListener('click', () => {
      void handleClearPlannedPointing();
    });
  });

  const slewButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-slew-to-planned-pointing]');
  slewButtons.forEach((button) => {
    button.addEventListener('click', () => {
      void handleSlewToPlannedPointing();
    });
  });

  const deleteButtons = rootElement.querySelectorAll<HTMLButtonElement>('[data-delete-mosaic-id]');
  deleteButtons.forEach((button) => {
    button.addEventListener('click', () => {
      const mosaicId = button.dataset.deleteMosaicId ?? '';
      if (!mosaicId) {
        return;
      }
      void handleDeleteMosaic(mosaicId);
    });
  });
}

function bindSkyFollowToggle(): void {
  const input = rootElement.querySelector<HTMLInputElement>('[data-follow-telescope-toggle]');
  input?.addEventListener('change', () => {
    state.followTelescope = input.checked;
    render();
  });
}

function bindTelescopeAdapterForm(): void {
  const form = rootElement.querySelector<HTMLFormElement>('[data-form="telescope-adapter"]');
  if (!form) {
    return;
  }

  const select = form.elements.namedItem('adapter_id');
  if (select instanceof HTMLSelectElement) {
    select.value = state.activeTelescopeAdapterId ?? state.telescopeSnapshot?.telescope_state.adapter_id ?? select.value;
  }
}

function hydrateSkySimulatorPanel(): void {
  const snapshot = state.telescopeSnapshot;
  const telescopeState = snapshot?.telescope_state;
  const imagingProfile = snapshot?.imaging_profile;
  const stateContainer = rootElement.querySelector<HTMLElement>('[data-sky-simulator-state]');
  if (stateContainer) {
    stateContainer.innerHTML = `
      <div class="project-summary-strip project-summary-strip--sky project-summary-strip--sky-current">
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Target</span>
          <span class="project-summary-strip__value">${escapeHtml(telescopeState?.target_name ?? 'none')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Status</span>
          <span class="project-summary-strip__value">${escapeHtml(telescopeState?.status ?? 'unknown')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Source</span>
          <span class="project-summary-strip__value">${escapeHtml(telescopeState?.source_kind ?? 'unknown')}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Mode</span>
          <span class="project-summary-strip__value">${telescopeState?.is_simulated ? 'simulated' : 'live'}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">RA</span>
          <span class="project-summary-strip__value">${formatRaDisplay(telescopeState?.ra_hours)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Dec</span>
          <span class="project-summary-strip__value">${formatDecDisplay(telescopeState?.dec_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Alt</span>
          <span class="project-summary-strip__value">${formatAngleDisplay(telescopeState?.alt_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Az</span>
          <span class="project-summary-strip__value">${formatAngleDisplay(telescopeState?.az_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">FOV</span>
          <span class="project-summary-strip__value">${formatFov(imagingProfile?.fov_width_deg, imagingProfile?.fov_height_deg)}</span>
        </div>
        <div class="project-summary-strip__item">
          <span class="project-summary-strip__label">Updated</span>
          <span class="project-summary-strip__value">${formatTimestampDisplay(telescopeState?.timestamp_utc)}</span>
        </div>
      </div>
    `;
  }

  const form = rootElement.querySelector<HTMLFormElement>('[data-form="sky-simulator"]');
  if (!form || !telescopeState) {
    return;
  }

  const activeElement = document.activeElement;
  if (activeElement instanceof HTMLElement && form.contains(activeElement)) {
    return;
  }

  setFormFieldValue(form, 'target_name', telescopeState.target_name ?? '');
  setFormFieldValue(form, 'ra_hours', telescopeState.ra_hours != null ? String(telescopeState.ra_hours) : '');
  setFormFieldValue(form, 'dec_deg', telescopeState.dec_deg != null ? String(telescopeState.dec_deg) : '');
  setFormFieldValue(form, 'alt_deg', telescopeState.alt_deg != null ? String(telescopeState.alt_deg) : '');
  setFormFieldValue(form, 'az_deg', telescopeState.az_deg != null ? String(telescopeState.az_deg) : '');
  setFormFieldValue(form, 'status', telescopeState.status ?? '');
  const followField = form.elements.namedItem('follow_telescope');
  if (followField instanceof HTMLInputElement) {
    followField.checked = state.followTelescope;
  }
}

function bindInlineProjectSkyTarget(): void {
  const input = rootElement.querySelector<HTMLInputElement>('[data-inline-sky-target]');
  input?.addEventListener('change', () => {
    const projectSlug = input.dataset.projectSlugInline ?? '';
    const skyTarget = input.value.trim();
    if (!projectSlug || !skyTarget || skyTarget === (state.selectedProject?.sky_target ?? '')) {
      return;
    }
    void handleProjectSkyTarget(projectSlug, skyTarget);
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
    state.createImportCaptureModalOpen = false;
    state.selectedProjectCapture = captureName;
    state.selectedProjectCaptureFolder = null;
    state.selectedProjectCaptureFile = null;
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

async function handleProjectSkyTarget(projectSlug: string, skyTarget: string): Promise<void> {
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

async function handleSkySimulatorUpdate(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const input = {
    target_name: normalizeOptionalText(formData.get('target_name')),
    status: normalizeOptionalText(formData.get('status')),
    ra_hours: normalizeOptionalRa(formData.get('ra_hours')),
    dec_deg: normalizeOptionalDec(formData.get('dec_deg')),
    alt_deg: normalizeOptionalNumber(formData.get('alt_deg')),
    az_deg: normalizeOptionalNumber(formData.get('az_deg')),
  };

  if (input.ra_hours == null || input.dec_deg == null) {
    setError('RA and Dec are required for simulator pointing.');
    return;
  }

  setBusy(true);
  try {
    const snapshot = await updateSimulatorTelescopeState(input);
    state.telescopeSnapshot = snapshot;
    setMessage(`Telescope updated: ${snapshot.telescope_state.target_name ?? 'simulator pointing'}`);
    render();
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleTelescopeAdapterSwitch(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const adapterId = String(formData.get('adapter_id') ?? '').trim();
  if (!adapterId) {
    setError('Select a telescope adapter first.');
    return;
  }

  setBusy(true);
  try {
    const payload = await setActiveTelescopeAdapter(adapterId);
    state.activeTelescopeAdapterId = payload.active_adapter_id;
    state.telescopeSnapshot = payload.snapshot;
    setMessage(`Active telescope adapter: ${payload.active_adapter_id}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleCreateMosaic(form: HTMLFormElement): Promise<void> {
  if (!state.selectedProject) {
    setError('Select a project first.');
    return;
  }

  const formData = new FormData(form);
  const centerRaDeg = normalizeOptionalNumber(formData.get('center_ra_deg'));
  const centerDecDeg = normalizeOptionalNumber(formData.get('center_dec_deg'));
  const regionWidthDeg = normalizeOptionalNumber(formData.get('region_width_deg'));
  const regionHeightDeg = normalizeOptionalNumber(formData.get('region_height_deg'));
  const overlapPercent = normalizeOptionalNumber(formData.get('overlap_percent'));
  const rotationDeg = normalizeOptionalNumber(formData.get('rotation_deg'));
  const profile = state.telescopeSnapshot?.imaging_profile;

  if (
    centerRaDeg == null
    || centerDecDeg == null
    || regionWidthDeg == null
    || regionHeightDeg == null
  ) {
    setError('Center RA, center Dec, region width and region height are required.');
    return;
  }

  setBusy(true);
  try {
    const mosaic = await createMosaicPlan({
      project_slug: state.selectedProject.slug,
      name: String(formData.get('name') ?? '').trim() || 'New mosaic',
      target_name: normalizeOptionalText(formData.get('target_name')),
      imaging_profile_id: profile?.profile_id,
      imaging_profile_label: profile?.label,
      fov_width_deg: profile?.fov_width_deg ?? undefined,
      fov_height_deg: profile?.fov_height_deg ?? undefined,
      center_ra_deg: centerRaDeg,
      center_dec_deg: centerDecDeg,
      region_width_deg: regionWidthDeg,
      region_height_deg: regionHeightDeg,
      overlap_percent: overlapPercent ?? 25,
      rotation_deg: rotationDeg ?? profile?.rotation_deg ?? 0,
    });
    state.mosaics = await fetchMosaics(state.selectedProject.slug).catch(() => [mosaic]);
    state.selectedMosaicId = mosaic.id;
    syncSelectedMosaic();
    setMessage(`Mosaic created: ${mosaic.name}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleGenerateMosaicPanels(mosaicId: string): Promise<void> {
  setBusy(true);
  try {
    const mosaic = await generateMosaicPanels(mosaicId);
    replaceMosaicInState(mosaic);
    state.selectedMosaicId = mosaic.id;
    state.selectedMosaicPanelId = mosaic.selected_panel_id ?? mosaic.panels[0]?.id ?? null;
    setMessage(`Panels generated: ${mosaic.panels.length}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleSelectMosaicPanel(mosaicId: string, panelId: string): Promise<void> {
  try {
    const mosaic = await selectMosaicPanel(mosaicId, panelId);
    replaceMosaicInState(mosaic);
    state.selectedMosaicId = mosaic.id;
    state.selectedMosaicPanelId = panelId;
    render();
  } catch (error) {
    setError(getErrorMessage(error));
  }
}

async function handleUpdateMosaicPanelStatus(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const panelId = String(formData.get('panel_id') ?? '').trim();
  if (!panelId || !state.selectedMosaicId) {
    setError('Select a mosaic panel first.');
    return;
  }

  setBusy(true);
  try {
    const updatedPanel = await updateMosaicPanel(panelId, {
      status: normalizeOptionalText(formData.get('status')),
      target_integration_seconds: normalizeNullableNumber(formData.get('target_integration_seconds')),
      acquired_integration_seconds: normalizeNullableNumber(formData.get('acquired_integration_seconds')),
    });
    const refreshedMosaic = await selectMosaicPanel(state.selectedMosaicId, updatedPanel.id);
    replaceMosaicInState(refreshedMosaic);
    state.selectedMosaicPanelId = updatedPanel.id;
    setMessage(`Panel updated: ${updatedPanel.panel_label}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handlePlanMosaicPanel(panelId: string): Promise<void> {
  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;
  const selectedPanel =
    selectedMosaic?.panels.find((panel) => panel.id === panelId)
    ?? null;

  if (!selectedMosaic || !selectedPanel) {
    setError('Select a mosaic panel first.');
    return;
  }

  setBusy(true);
  try {
    const snapshot = await updatePlannedTelescopePointing({
      ra_hours: selectedPanel.center_ra_deg / 15.0,
      dec_deg: selectedPanel.center_dec_deg,
      target_name: `${selectedMosaic.name} · ${selectedPanel.panel_label}`,
      source_kind: 'mosaic_panel',
      source_id: selectedPanel.id,
    });
    state.telescopeSnapshot = snapshot;
    setMessage(`Planned target set: ${selectedPanel.panel_label}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleClearPlannedPointing(): Promise<void> {
  setBusy(true);
  try {
    state.telescopeSnapshot = await clearPlannedTelescopePointing();
    setMessage('Planned target cleared.');
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleSlewToPlannedPointing(): Promise<void> {
  setBusy(true);
  try {
    state.telescopeSnapshot = await slewToPlannedTelescopePointing();
    setMessage(`Slew started: ${state.telescopeSnapshot.planned_pointing?.target_name ?? 'planned target'}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleActiveSiteChange(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const siteId = normalizeOptionalText(formData.get('site_id')) ?? null;

  setBusy(true);
  try {
    const payload = await setActiveSite(siteId);
    state.activeSiteId = payload.active_site_id;
    state.telescopeSnapshot = payload.snapshot;
    setMessage(siteId ? `Active site set: ${payload.snapshot.active_site?.name ?? siteId}` : 'Active site cleared.');
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleSetActiveSiteById(siteId: string): Promise<void> {
  setBusy(true);
  try {
    const payload = await setActiveSite(siteId);
    state.activeSiteId = payload.active_site_id;
    state.telescopeSnapshot = payload.snapshot;
    setMessage(`Active site set: ${payload.snapshot.active_site?.name ?? siteId}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleSiteEditorSubmit(form: HTMLFormElement): Promise<void> {
  const formData = new FormData(form);
  const siteId = normalizeOptionalText(formData.get('site_id'));
  const sitePayload = {
    name: String(formData.get('name') ?? '').trim(),
    latitude_deg: normalizeNullableNumber(formData.get('latitude_deg')),
    longitude_deg: normalizeNullableNumber(formData.get('longitude_deg')),
    elevation_m: normalizeNullableNumber(formData.get('elevation_m')),
    sqm_mag_arcsec2: normalizeNullableNumber(formData.get('sqm_mag_arcsec2')),
    bortle_class: normalizeNullableNumber(formData.get('bortle_class')),
    south_horizon_open: formData.get('south_horizon_open') === 'on',
    notes: normalizeNullableText(formData.get('notes')),
  };

  if (!sitePayload.name) {
    setError('Site name is required.');
    return;
  }

  setBusy(true);
  try {
    const savedSite = siteId
      ? await updateSite(siteId, sitePayload)
      : await createSite(sitePayload);

    const sitesPayload = await fetchSites().catch(() => null);
    state.sites = sitesPayload?.sites ?? state.sites;

    const activePayload = await setActiveSite(savedSite.id);
    state.activeSiteId = activePayload.active_site_id;
    state.telescopeSnapshot = activePayload.snapshot;
    setMessage(siteId ? `Site updated: ${savedSite.name}` : `Site created: ${savedSite.name}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleDeleteSite(siteId: string): Promise<void> {
  const site = state.sites.find((entry) => entry.id === siteId);
  const confirmed = window.confirm(`Delete observation site "${site?.name ?? siteId}"?`);
  if (!confirmed) {
    return;
  }

  setBusy(true);
  try {
    await deleteSite(siteId);
    const sitesPayload = await fetchSites().catch(() => null);
    state.sites = sitesPayload?.sites ?? [];
    state.activeSiteId = sitesPayload?.active_site_id ?? null;
    state.telescopeSnapshot = await fetchTelescopeState().catch(() => state.telescopeSnapshot);
    setMessage(`Site deleted: ${site?.name ?? siteId}`);
  } catch (error) {
    setError(getErrorMessage(error));
  } finally {
    setBusy(false);
  }
}

async function handleDeleteMosaic(mosaicId: string): Promise<void> {
  const mosaic = state.mosaics.find((entry) => entry.id === mosaicId);
  const confirmed = window.confirm(`Delete mosaic "${mosaic?.name ?? mosaicId}" with all generated panels?`);
  if (!confirmed) {
    return;
  }

  setBusy(true);
  try {
    await deleteMosaicPlan(mosaicId);
    state.mosaics = state.mosaics.filter((entry) => entry.id !== mosaicId);
    syncSelectedMosaic();
    setMessage(`Mosaic deleted: ${mosaic?.name ?? mosaicId}`);
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
      state.selectedProjectRunId = null;
      state.selectedProjectCapture = null;
      state.selectedProjectCaptureDetails = null;
      state.selectedProjectCaptureFolder = null;
      state.selectedProjectCaptureFile = null;
      state.mosaics = [];
      state.selectedMosaicId = null;
      state.selectedMosaicPanelId = null;
      state.projectRuns = [];
      state.activeRun = null;
      syncRunPolling();
    }
    state.projectDetailTab = 'gallery';
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
  state.selectedProjectRunId = null;
  state.projectDetailTab = 'gallery';
  state.processingDetailTab = 'overview';
  state.projectRuns = state.selectedProject ? await fetchProjectRuns(state.selectedProject.slug).catch(() => []) : [];
  state.mosaics = state.selectedProject ? await fetchMosaics(state.selectedProject.slug).catch(() => []) : [];
  syncSelectedProjectRun();
  syncSelectedMosaic();
  await syncSelectedProjectCapture();
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

async function selectProjectCapture(captureName: string): Promise<void> {
  if (!state.selectedProject) {
    return;
  }

  state.selectedProjectCapture = captureName;
  const matchingRuns = state.projectRuns.filter((run) => run.capture_name === captureName);
  state.selectedProjectRunId = matchingRuns[0]?.id ?? null;
  state.selectedProjectCaptureFolder = null;
  state.selectedProjectCaptureFile = null;
  state.selectedProjectCaptureDetails = await fetchCaptureDetails(state.selectedProject.slug, captureName).catch(() => null);
  syncSelectedProjectCaptureFile();
  render();
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

// Active run polling is short-interval because users expect near-live logs.
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
      syncSelectedProjectRun();
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

function syncTelescopePolling(): void {
  if (telescopePollTimer !== null) {
    window.clearInterval(telescopePollTimer);
    telescopePollTimer = null;
  }

  telescopePollTimer = window.setInterval(() => {
    void pollTelescopeState();
  }, 3000);
}

// Telescope polling is gentler because Sky updates are informational, not logs.
async function pollTelescopeState(): Promise<void> {
  try {
    const snapshot = await fetchTelescopeState();
    state.telescopeSnapshot = snapshot;
    state.activeTelescopeAdapterId = snapshot.telescope_state.adapter_id;
    if (state.currentView === 'sky') {
      hydrateSkySimulatorPanel();
      void refreshSkyViewLive();
    }
  } catch {
    // Ignore transient telescope polling failures for now.
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

async function syncSelectedProjectCapture(): Promise<void> {
  const captureNames = state.selectedProject?.capture_names ?? [];
  if (!captureNames.length) {
    state.selectedProjectCapture = null;
    state.selectedProjectCaptureDetails = null;
    state.selectedProjectCaptureFolder = null;
    state.selectedProjectCaptureFile = null;
    return;
  }

  if (!state.selectedProjectCapture || !captureNames.includes(state.selectedProjectCapture)) {
    state.selectedProjectCapture = captureNames[0] ?? null;
  }

  if (!state.selectedProjectCapture || !state.selectedProject) {
    state.selectedProjectCaptureDetails = null;
    state.selectedProjectCaptureFolder = null;
    state.selectedProjectCaptureFile = null;
    return;
  }

  state.selectedProjectCaptureDetails = await fetchCaptureDetails(
    state.selectedProject.slug,
    state.selectedProjectCapture,
  ).catch(() => null);
  syncSelectedProjectCaptureFile();
}

function syncSelectedProjectRun(): void {
  if (!state.projectRuns.length) {
    state.selectedProjectRunId = null;
    return;
  }

  if (state.selectedProjectRunId && state.projectRuns.some((run) => run.id === state.selectedProjectRunId)) {
    return;
  }

  state.selectedProjectRunId = state.projectRuns[0]?.id ?? null;
}

function syncSelectedMosaic(): void {
  if (!state.mosaics.length) {
    state.selectedMosaicId = null;
    state.selectedMosaicPanelId = null;
    return;
  }

  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;

  state.selectedMosaicId = selectedMosaic?.id ?? null;
  if (!selectedMosaic) {
    state.selectedMosaicPanelId = null;
    return;
  }

  const selectedPanel =
    (state.selectedMosaicPanelId
      ? selectedMosaic.panels.find((panel) => panel.id === state.selectedMosaicPanelId)
      : null)
    ?? (selectedMosaic.selected_panel_id
      ? selectedMosaic.panels.find((panel) => panel.id === selectedMosaic.selected_panel_id)
      : null)
    ?? selectedMosaic.panels[0]
    ?? null;

  state.selectedMosaicPanelId = selectedPanel?.id ?? null;
}

function replaceMosaicInState(nextMosaic: MosaicPlan): void {
  const nextList = [...state.mosaics];
  const index = nextList.findIndex((mosaic) => mosaic.id === nextMosaic.id);
  if (index >= 0) {
    nextList[index] = nextMosaic;
  } else {
    nextList.unshift(nextMosaic);
  }
  state.mosaics = nextList;
}

function syncSelectedProjectCaptureFile(): void {
  const folders = state.selectedProjectCaptureDetails?.folders ?? [];
  if (!folders.length) {
    state.selectedProjectCaptureFolder = null;
    state.selectedProjectCaptureFile = null;
    return;
  }

  const selectedFolder =
    (state.selectedProjectCaptureFolder
      ? folders.find((folder) => folder.name === state.selectedProjectCaptureFolder)
      : null)
    ?? folders[0]
    ?? null;

  state.selectedProjectCaptureFolder = selectedFolder?.name ?? null;

  const folderFiles = selectedFolder?.files ?? [];
  if (!folderFiles.length) {
    state.selectedProjectCaptureFile = null;
    return;
  }

  if (
    state.selectedProjectCaptureFile
    && folderFiles.some((fileEntry) => fileEntry.relative_path === state.selectedProjectCaptureFile)
  ) {
    return;
  }

  state.selectedProjectCaptureFile = folderFiles[0]?.relative_path ?? null;
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

function normalizeOptionalNumber(value: FormDataEntryValue | null): number | undefined {
  const text = String(value ?? '').trim();
  if (!text) {
    return undefined;
  }
  const parsed = Number(text);
  if (Number.isNaN(parsed)) {
    return undefined;
  }
  return parsed;
}

function normalizeNullableNumber(value: FormDataEntryValue | null): number | null | undefined {
  const text = String(value ?? '').trim();
  if (!text) {
    return null;
  }
  const parsed = Number(text);
  if (Number.isNaN(parsed)) {
    return undefined;
  }
  return parsed;
}

function normalizeOptionalRa(value: FormDataEntryValue | null): number | undefined {
  const text = String(value ?? '').trim();
  if (!text) {
    return undefined;
  }

  const decimal = Number(text);
  if (!Number.isNaN(decimal)) {
    return decimal;
  }

  const parts = text.split(/[\s:]+/).filter(Boolean);
  if (parts.length !== 3) {
    return undefined;
  }

  const hours = Number(parts[0]);
  const minutes = Number(parts[1]);
  const seconds = Number(parts[2]);
  if ([hours, minutes, seconds].some((part) => Number.isNaN(part))) {
    return undefined;
  }

  return hours + minutes / 60 + seconds / 3600;
}

function normalizeOptionalDec(value: FormDataEntryValue | null): number | undefined {
  const text = String(value ?? '').trim();
  if (!text) {
    return undefined;
  }

  const decimal = Number(text);
  if (!Number.isNaN(decimal)) {
    return decimal;
  }

  const parts = text.split(/[\s:]+/).filter(Boolean);
  if (parts.length !== 3) {
    return undefined;
  }

  const degrees = Number(parts[0]);
  const minutes = Number(parts[1]);
  const seconds = Number(parts[2]);
  if ([degrees, minutes, seconds].some((part) => Number.isNaN(part))) {
    return undefined;
  }

  const sign = degrees < 0 || text.startsWith('-') ? -1 : 1;
  const absoluteDegrees = Math.abs(degrees) + minutes / 60 + seconds / 3600;
  return sign * absoluteDegrees;
}

function normalizeOptionalText(value: FormDataEntryValue | null): string | undefined {
  const text = String(value ?? '').trim();
  return text || undefined;
}

function normalizeNullableText(value: FormDataEntryValue | null): string | null {
  const text = String(value ?? '').trim();
  return text || null;
}

function setFormFieldValue(form: HTMLFormElement, fieldName: string, value: string): void {
  const field = form.elements.namedItem(fieldName);
  if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement || field instanceof HTMLTextAreaElement) {
    field.value = value;
  }
}

function formatFov(widthDeg: number | null | undefined, heightDeg: number | null | undefined): string {
  if (widthDeg == null || heightDeg == null) {
    return '—';
  }
  return `${widthDeg.toFixed(2)}° × ${heightDeg.toFixed(2)}°`;
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

function formatAngleDisplay(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  return `${value.toFixed(3)}°`;
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

function loadThemePreference(): ThemeName {
  const savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (savedTheme === 'dark' || savedTheme === 'observation') {
    return savedTheme;
  }
  return 'dark';
}

function persistThemePreference(theme: ThemeName): void {
  window.localStorage.setItem(THEME_STORAGE_KEY, theme);
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function getWorkspaceScrollTop(): number {
  return rootElement.querySelector<HTMLElement>('.workspace')?.scrollTop ?? 0;
}

function restoreWorkspaceScrollTop(value: number): void {
  const workspace = rootElement.querySelector<HTMLElement>('.workspace');
  if (workspace) {
    workspace.scrollTop = value;
  }
}

function scheduleWorkspaceScrollRestore(value: number): void {
  window.requestAnimationFrame(() => {
    restoreWorkspaceScrollTop(value);
    window.requestAnimationFrame(() => {
      restoreWorkspaceScrollTop(value);
    });
  });
}

async function refreshSkyViewLive(): Promise<void> {
  const selectedMosaic =
    (state.selectedMosaicId
      ? state.mosaics.find((mosaic) => mosaic.id === state.selectedMosaicId)
      : null)
    ?? state.mosaics[0]
    ?? null;
  await mountSkyView(
    'aladin-sky-view',
    state.selectedProject?.sky_target ?? 'M42',
    state.telescopeSnapshot,
    state.followTelescope,
    selectedMosaic ?? null,
    state.selectedMosaicPanelId,
    (panelId) => {
      if (!state.selectedMosaicId) {
        return;
      }
      void handleSelectMosaicPanel(state.selectedMosaicId, panelId);
    },
  );
}

async function refreshObservationCenterMapLive(): Promise<void> {
  await mountObservationCenterMap(
    'observation-center-map',
    state.sites,
    state.activeSiteId,
    (siteId) => {
      void handleSetActiveSiteById(siteId);
    },
  );
}
