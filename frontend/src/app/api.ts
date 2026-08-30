/**
 * Browser-side HTTP client for the local TSN DSS API.
 *
 * This module is intentionally dumb: it only defines payload shapes,
 * endpoint helpers and a few URL builders used by the shell renderer.
 * Higher-level state orchestration belongs in `main.ts`.
 */
export type ApiHealth = {
  status: string;
  service: string;
  projects_root: string;
  default_siril_executable?: string;
};

export type ProjectSummary = {
  slug: string;
  project_root: string;
  captures_dir: string;
  runs_dir: string;
  capture_count: number;
  run_count: number;
  capture_names: string[];
  run_names: string[];
  sky_target: string | null;
};

export type CaptureFileEntry = {
  name: string;
  relative_path: string;
  size_bytes: number;
  suffix: string;
};

export type CaptureFolderEntry = {
  name: string;
  file_count: number;
  files: CaptureFileEntry[];
};

export type CaptureDetails = {
  project_slug: string;
  capture_name: string;
  capture_root: string;
  folders: CaptureFolderEntry[];
};

export type ProjectRun = {
  artifact_images: RunArtifactImage[];
  id: string;
  project_slug: string;
  capture_name: string;
  keep_process_dir: boolean;
  status: string;
  progress_pct: number;
  stage: string;
  command: string[];
  script_path: string;
  workspace_dir: string;
  artifacts_dir: string;
  logs_dir: string;
  stdout_log_path: string;
  stderr_log_path: string;
  status_path: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  output_path: string | null;
  preview_path: string | null;
  preview_log_path: string | null;
  preview_error: string | null;
  error_message: string | null;
  combined_log: string;
};

export type RunArtifactImage = {
  name: string;
  relative_path: string;
  size_bytes: number;
  suffix: string;
};

export type TelescopeState = {
  adapter_id: string;
  source_kind: string;
  timestamp_utc: string;
  connected: boolean;
  status: string;
  is_simulated: boolean;
  site_lat_deg: number | null;
  site_lon_deg: number | null;
  site_elevation_m: number | null;
  ra_hours: number | null;
  dec_deg: number | null;
  alt_deg: number | null;
  az_deg: number | null;
  target_name: string | null;
  position_quality: string | null;
};

export type PlannedPointing = {
  target_name: string | null;
  ra_hours: number;
  dec_deg: number;
  source_kind: string;
  source_id: string | null;
  updated_at_utc: string | null;
};

export type ImagingProfile = {
  profile_id: string;
  label: string;
  focal_length_mm: number;
  sensor_width_mm: number;
  sensor_height_mm: number;
  pixel_size_um: number | null;
  rotation_deg: number | null;
  binning: number | null;
  fov_width_deg: number | null;
  fov_height_deg: number | null;
};

export type TelescopeSnapshot = {
  telescope_state: TelescopeState;
  imaging_profile: ImagingProfile;
  planned_pointing: PlannedPointing | null;
};

export type TelescopeAdapterCapabilities = {
  can_connect: boolean;
  can_disconnect: boolean;
  can_manual_pointing: boolean;
  can_slew_to_coordinates: boolean;
  can_park: boolean;
  can_set_tracking: boolean;
  can_stream_preview: boolean;
  can_start_stack: boolean;
  can_run_observation_plans: boolean;
};

export type TelescopeAdapterDescriptor = {
  adapter_id: string;
  label: string;
  source_kind: string;
  is_simulated: boolean;
  capabilities: TelescopeAdapterCapabilities;
};

export type MosaicPanel = {
  id: string;
  mosaic_plan_id: string;
  panel_index: number;
  panel_label: string;
  center_ra_deg: number;
  center_dec_deg: number;
  fov_width_deg: number;
  fov_height_deg: number;
  rotation_deg: number;
  row_index: number | null;
  column_index: number | null;
  status: string;
  target_integration_seconds: number | null;
  acquired_integration_seconds: number | null;
};

export type MosaicPlan = {
  id: string;
  project_slug: string;
  name: string;
  target_name: string | null;
  imaging_profile_id: string;
  imaging_profile_label: string;
  fov_width_deg: number;
  fov_height_deg: number;
  center_ra_deg: number;
  center_dec_deg: number;
  region_width_deg: number;
  region_height_deg: number;
  rotation_deg: number;
  overlap_percent: number;
  status: string;
  selected_panel_id: string | null;
  panels: MosaicPanel[];
};

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8765';

export function getApiBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/+$/, '');
}

export async function fetchHealth(): Promise<ApiHealth> {
  return getJson<ApiHealth>('/api/health');
}

export async function fetchTelescopeState(): Promise<TelescopeSnapshot> {
  return getJson<TelescopeSnapshot>('/api/telescope/state');
}

export async function fetchTelescopeAdapters(): Promise<{
  active_adapter_id: string;
  adapters: TelescopeAdapterDescriptor[];
}> {
  return getJson<{ active_adapter_id: string; adapters: TelescopeAdapterDescriptor[] }>('/api/telescope/adapters');
}

export async function setActiveTelescopeAdapter(adapterId: string): Promise<{
  active_adapter_id: string;
  snapshot: TelescopeSnapshot;
  capabilities: TelescopeAdapterCapabilities;
}> {
  return sendJson<{
    active_adapter_id: string;
    snapshot: TelescopeSnapshot;
    capabilities: TelescopeAdapterCapabilities;
  }>('/api/telescope/active-adapter', { adapter_id: adapterId });
}

export async function updateSimulatorTelescopeState(input: {
  ra_hours?: number;
  dec_deg?: number;
  alt_deg?: number;
  az_deg?: number;
  target_name?: string;
  status?: string;
}): Promise<TelescopeSnapshot> {
  return sendJson<TelescopeSnapshot>('/api/telescope/simulator/state', input);
}

export async function updatePlannedTelescopePointing(input: {
  ra_hours: number;
  dec_deg: number;
  target_name?: string;
  source_kind?: string;
  source_id?: string;
}): Promise<TelescopeSnapshot> {
  return sendJson<TelescopeSnapshot>('/api/telescope/planned-pointing', input);
}

export async function clearPlannedTelescopePointing(): Promise<TelescopeSnapshot> {
  const response = await fetch(`${getApiBaseUrl()}/api/telescope/planned-pointing`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status} ${response.statusText}`);
  }

  await response.json();

  return fetchTelescopeState();
}

export async function slewToPlannedTelescopePointing(): Promise<TelescopeSnapshot> {
  return sendJson<TelescopeSnapshot>('/api/telescope/slew-to-planned', {});
}

export async function fetchMosaics(projectSlug?: string): Promise<MosaicPlan[]> {
  const suffix = projectSlug ? `?project_slug=${encodeURIComponent(projectSlug)}` : '';
  const payload = await getJson<{ mosaics: MosaicPlan[] }>(`/api/mosaics${suffix}`);
  return payload.mosaics;
}

export async function createMosaicPlan(input: {
  id?: string;
  project_slug: string;
  name: string;
  target_name?: string;
  imaging_profile_id?: string;
  imaging_profile_label?: string;
  fov_width_deg?: number;
  fov_height_deg?: number;
  center_ra_deg: number;
  center_dec_deg: number;
  region_width_deg: number;
  region_height_deg: number;
  rotation_deg?: number;
  overlap_percent?: number;
  status?: string;
}): Promise<MosaicPlan> {
  const payload = await sendJson<{ mosaic: MosaicPlan }>('/api/mosaics', input);
  return payload.mosaic;
}

export async function generateMosaicPanels(mosaicId: string): Promise<MosaicPlan> {
  const payload = await sendJson<{ mosaic: MosaicPlan }>(
    `/api/mosaics/${encodeURIComponent(mosaicId)}/generate-panels`,
    {},
  );
  return payload.mosaic;
}

export async function selectMosaicPanel(mosaicId: string, panelId: string): Promise<MosaicPlan> {
  const payload = await sendJson<{ mosaic: MosaicPlan }>(
    `/api/mosaics/${encodeURIComponent(mosaicId)}/select-panel`,
    { panel_id: panelId },
  );
  return payload.mosaic;
}

export async function updateMosaicPanel(panelId: string, input: {
  panel_label?: string;
  status?: string;
  target_integration_seconds?: number | null;
  acquired_integration_seconds?: number | null;
}): Promise<MosaicPanel> {
  const payload = await sendJson<{ panel: MosaicPanel }>(
    `/api/mosaic-panels/${encodeURIComponent(panelId)}`,
    input,
  );
  return payload.panel;
}

export async function deleteMosaicPlan(mosaicId: string): Promise<void> {
  await sendDelete(`/api/mosaics/${encodeURIComponent(mosaicId)}`);
}

export async function fetchProjects(): Promise<ProjectSummary[]> {
  const payload = await getJson<{ projects: ProjectSummary[] }>('/api/projects');
  return payload.projects;
}

export async function fetchProject(slug: string): Promise<ProjectSummary> {
  const payload = await getJson<{ project: ProjectSummary }>(`/api/projects/${encodeURIComponent(slug)}`);
  return payload.project;
}

export async function createProject(slug: string): Promise<ProjectSummary> {
  const payload = await sendJson<{ project: ProjectSummary }>('/api/projects', { slug });
  return payload.project;
}

export async function deleteProject(slug: string): Promise<void> {
  await sendDelete(`/api/projects/${encodeURIComponent(slug)}`);
}

export async function updateProjectSkyTarget(slug: string, skyTarget: string): Promise<ProjectSummary> {
  const payload = await sendJson<{ project: ProjectSummary }>(
    `/api/projects/${encodeURIComponent(slug)}/sky-target`,
    { sky_target: skyTarget },
  );
  return payload.project;
}

export async function importCapture(input: {
  project_slug: string;
  capture_name: string;
  source_dir: string;
  move?: boolean;
}): Promise<{ project: ProjectSummary; capture_root: string }> {
  return sendJson<{ project: ProjectSummary; capture_root: string }>('/api/import-capture', input);
}

export async function fetchCaptureDetails(projectSlug: string, captureName: string): Promise<CaptureDetails> {
  const payload = await getJson<{ capture: CaptureDetails }>(
    `/api/projects/${encodeURIComponent(projectSlug)}/captures/${encodeURIComponent(captureName)}`,
  );
  return payload.capture;
}

export function getCaptureFileUrl(projectSlug: string, captureName: string, relativePath: string): string {
  const encodedRelativePath = encodeCaptureRelativePath(relativePath);
  return `${getApiBaseUrl()}/api/projects/${encodeURIComponent(projectSlug)}/captures/${encodeURIComponent(captureName)}/files/${encodedRelativePath}`;
}

export function getCaptureThumbnailUrl(
  projectSlug: string,
  captureName: string,
  relativePath: string,
  size = 384,
): string {
  const encodedRelativePath = encodeCaptureRelativePath(relativePath);
  return `${getApiBaseUrl()}/api/projects/${encodeURIComponent(projectSlug)}/captures/${encodeURIComponent(captureName)}/thumbnails/${encodedRelativePath}?size=${encodeURIComponent(String(size))}`;
}

function encodeCaptureRelativePath(relativePath: string): string {
  const encodedParts = relativePath
    .split(/[\\/]+/)
    .filter((part) => part.length > 0)
    .map((part) => encodeURIComponent(part));
  return encodedParts.join('/');
}

export async function startProjectRun(input: {
  project_slug: string;
  capture_name: string;
  executable?: string;
  script_path?: string;
  keep_process_dir?: boolean;
}): Promise<ProjectRun> {
  const payload = await sendJson<{ run: ProjectRun }>('/api/project-runs', input);
  return payload.run;
}

export async function fetchProjectRun(runId: string): Promise<ProjectRun> {
  const payload = await getJson<{ run: ProjectRun }>(`/api/project-runs/${encodeURIComponent(runId)}`);
  return payload.run;
}

export async function fetchProjectRuns(projectSlug: string, captureName?: string): Promise<ProjectRun[]> {
  const query = new URLSearchParams({ project_slug: projectSlug });
  if (captureName) {
    query.set('capture_name', captureName);
  }
  const payload = await getJson<{ runs: ProjectRun[] }>(`/api/project-runs?${query.toString()}`);
  return payload.runs;
}

export async function generateProjectRunPreview(runId: string): Promise<ProjectRun> {
  const payload = await sendJson<{ run: ProjectRun }>(
    `/api/project-runs/${encodeURIComponent(runId)}/generate-preview`,
    {},
  );
  return payload.run;
}

export async function deleteProjectRun(runId: string): Promise<void> {
  await sendDelete(`/api/project-runs/${encodeURIComponent(runId)}`);
}

export function getProjectRunPreviewUrl(runId: string): string {
  return `${getApiBaseUrl()}/api/project-runs/${encodeURIComponent(runId)}/preview`;
}

export function getProjectRunOutputUrl(runId: string): string {
  return `${getApiBaseUrl()}/api/project-runs/${encodeURIComponent(runId)}/output`;
}

export function getProjectRunPreviewLogUrl(runId: string): string {
  return `${getApiBaseUrl()}/api/project-runs/${encodeURIComponent(runId)}/preview-log`;
}

export function getProjectRunArtifactUrl(runId: string, relativePath: string): string {
  return `${getApiBaseUrl()}/api/project-runs/${encodeURIComponent(runId)}/artifacts/${encodeCaptureRelativePath(relativePath)}`;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`);
  if (!response.ok) {
    throw new Error(`API request failed: ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

async function sendJson<T>(path: string, payload: object): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });
  const responseBody = (await response.json()) as T & { message?: string };
  if (!response.ok) {
    throw new Error(responseBody.message ?? `API request failed: ${response.status} ${response.statusText}`);
  }
  return responseBody;
}

async function sendDelete(path: string): Promise<void> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    method: 'DELETE',
  });

  let responseBody: { message?: string } | null = null;
  try {
    responseBody = (await response.json()) as { message?: string };
  } catch {
    responseBody = null;
  }

  if (!response.ok) {
    throw new Error(responseBody?.message ?? `API request failed: ${response.status} ${response.statusText}`);
  }
}
