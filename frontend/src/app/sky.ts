let currentContainerId: string | null = null;
let initialized = false;
let loadState: 'idle' | 'loading' | 'ready' | 'failed' = 'idle';
let currentTarget = 'M42';

export async function mountSkyView(containerId: string, target: string): Promise<void> {
  const container = document.getElementById(containerId);
  if (container === null) {
    return;
  }

  const normalizedTarget = target.trim() || 'M42';

  if (
    initialized &&
    currentContainerId === containerId &&
    container.childElementCount > 0 &&
    currentTarget === normalizedTarget
  ) {
    return;
  }

  if (loadState === 'loading') {
    return;
  }

  loadState = 'loading';
  renderSkyPlaceholder(container, 'Loading Aladin Lite...');

  try {
    const { default: A } = await import('aladin-lite');
    container.innerHTML = '';

    await A.init;
    A.aladin(`#${containerId}`, {
      target: normalizedTarget,
      survey: 'P/DSS2/color',
      fov: 2.5,
      cooFrame: 'equatorial',
      projection: 'SIN',
      showCooGrid: true,
      showCooGridControl: true,
      showSimbadPointerControl: true,
      showFullscreenControl: true,
    });

    initialized = true;
    currentContainerId = containerId;
    currentTarget = normalizedTarget;
    loadState = 'ready';
  } catch (error) {
    loadState = 'failed';
    renderSkyPlaceholder(container, getSkyErrorMessage(error));
  }
}

function renderSkyPlaceholder(container: HTMLElement, message: string): void {
  container.innerHTML = `<div class="aladin-placeholder">${escapeHtml(message)}</div>`;
}

function getSkyErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return `Failed to load Aladin Lite: ${error.message}`;
  }
  return 'Failed to load Aladin Lite.';
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}
