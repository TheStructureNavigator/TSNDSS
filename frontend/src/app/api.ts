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

export type ProjectRun = {
  id: string;
  project_slug: string;
  capture_name: string;
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

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8765';

export function getApiBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/+$/, '');
}

export async function fetchHealth(): Promise<ApiHealth> {
  return getJson<ApiHealth>('/api/health');
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

export async function startProjectRun(input: {
  project_slug: string;
  capture_name: string;
  executable?: string;
  script_path?: string;
}): Promise<ProjectRun> {
  const payload = await sendJson<{ run: ProjectRun }>('/api/project-runs', input);
  return payload.run;
}

export async function fetchProjectRun(runId: string): Promise<ProjectRun> {
  const payload = await getJson<{ run: ProjectRun }>(`/api/project-runs/${encodeURIComponent(runId)}`);
  return payload.run;
}

export async function fetchProjectRuns(projectSlug: string): Promise<ProjectRun[]> {
  const payload = await getJson<{ runs: ProjectRun[] }>(
    `/api/project-runs?project_slug=${encodeURIComponent(projectSlug)}`,
  );
  return payload.runs;
}

export async function generateProjectRunPreview(runId: string): Promise<ProjectRun> {
  const payload = await sendJson<{ run: ProjectRun }>(
    `/api/project-runs/${encodeURIComponent(runId)}/generate-preview`,
    {},
  );
  return payload.run;
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
