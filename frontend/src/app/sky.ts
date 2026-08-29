import type { MosaicPanel, MosaicPlan, TelescopeSnapshot } from './api';

let currentContainerId: string | null = null;
let initialized = false;
let loadState: 'idle' | 'loading' | 'ready' | 'failed' = 'idle';
let currentTarget = 'M42';
let currentFollowTelescope = false;
let currentMosaicKey = '';
let currentPlannedPointingKey = '';
let currentMosaicPlan: MosaicPlan | null = null;
let currentSelectedPanelId: string | null = null;
let currentPanelSelectionHandler: ((panelId: string) => void) | null = null;
let currentMapClickHandler: ((event: MouseEvent) => void) | null = null;
let aladinInstance: any = null;
let aladinApi: any = null;
let telescopeCatalog: any = null;
let plannedPointingCatalog: any = null;
let footprintOverlay: any = null;
let plannedFootprintOverlay: any = null;
let mosaicOverlay: any = null;
let selectedMosaicOverlay: any = null;
let preservedFoVDeg: number | null = null;

export function preserveSkyViewState(): void {
  if (!aladinInstance) {
    return;
  }

  const currentFov =
    typeof aladinInstance.getFoV === 'function'
      ? aladinInstance.getFoV()
      : typeof aladinInstance.getFov === 'function'
        ? aladinInstance.getFov()
        : null;

  if (Array.isArray(currentFov)) {
    const [first] = currentFov;
    preservedFoVDeg = typeof first === 'number' && Number.isFinite(first) ? first : preservedFoVDeg;
    return;
  }

  if (typeof currentFov === 'number' && Number.isFinite(currentFov)) {
    preservedFoVDeg = currentFov;
  }
}

export async function mountSkyView(
  containerId: string,
  target: string,
  telescopeSnapshot: TelescopeSnapshot | null,
  followTelescope: boolean,
  mosaicPlan: MosaicPlan | null,
  selectedPanelId: string | null,
  onPanelSelected?: (panelId: string) => void,
): Promise<void> {
  const container = document.getElementById(containerId);
  if (container === null) {
    return;
  }

  currentMosaicPlan = mosaicPlan;
  currentSelectedPanelId = selectedPanelId;
  currentPanelSelectionHandler = onPanelSelected ?? null;

  const normalizedTarget = target.trim() || 'M42';
  const pointingChanged = hasPointingChanged(telescopeSnapshot);
  const followModeChanged = currentFollowTelescope !== followTelescope;
  const mosaicKey = getMosaicKey(mosaicPlan, selectedPanelId);
  const mosaicChanged = currentMosaicKey !== mosaicKey;
  const plannedPointingKey = getPlannedPointingKey(telescopeSnapshot);
  const plannedPointingChanged = currentPlannedPointingKey !== plannedPointingKey;

  if (
    initialized &&
    currentContainerId === containerId &&
    container.childElementCount > 0 &&
    currentTarget === normalizedTarget
  ) {
    if (pointingChanged || followModeChanged) {
      updateTelescopePointing(telescopeSnapshot, followTelescope);
    }
    if (plannedPointingChanged || pointingChanged) {
      updatePlannedPointing(telescopeSnapshot);
    }
    if (mosaicChanged) {
      updateMosaicFootprints(mosaicPlan, selectedPanelId);
    }
    currentFollowTelescope = followTelescope;
    currentMosaicKey = mosaicKey;
    currentPlannedPointingKey = plannedPointingKey;
    bindMapClickSelection(container);
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
    aladinApi = A;
    const initialFoVDeg = preservedFoVDeg ?? getRecommendedFoVDeg(telescopeSnapshot);
    aladinInstance = A.aladin(`#${containerId}`, {
      target: normalizedTarget,
      survey: 'P/DSS2/color',
      fov: initialFoVDeg,
      cooFrame: 'equatorial',
      projection: 'SIN',
      showCooGrid: false,
      showCooGridControl: true,
      showSimbadPointerControl: true,
      showFullscreenControl: true,
    });

    telescopeCatalog = createTelescopeCatalog(A);
    if (aladinInstance && telescopeCatalog && typeof aladinInstance.addCatalog === 'function') {
      aladinInstance.addCatalog(telescopeCatalog);
    }
    plannedPointingCatalog = createPlannedPointingCatalog(A);
    if (aladinInstance && plannedPointingCatalog && typeof aladinInstance.addCatalog === 'function') {
      aladinInstance.addCatalog(plannedPointingCatalog);
    }
    footprintOverlay = createFootprintOverlay(A);
    if (aladinInstance && footprintOverlay && typeof aladinInstance.addOverlay === 'function') {
      aladinInstance.addOverlay(footprintOverlay);
    }
    plannedFootprintOverlay = createPlannedFootprintOverlay(A);
    if (aladinInstance && plannedFootprintOverlay && typeof aladinInstance.addOverlay === 'function') {
      aladinInstance.addOverlay(plannedFootprintOverlay);
    }
    mosaicOverlay = createMosaicOverlay(A, '#c98f87', 1.2);
    if (aladinInstance && mosaicOverlay && typeof aladinInstance.addOverlay === 'function') {
      aladinInstance.addOverlay(mosaicOverlay);
    }
    selectedMosaicOverlay = createMosaicOverlay(A, '#ff6b5e', 2.4);
    if (aladinInstance && selectedMosaicOverlay && typeof aladinInstance.addOverlay === 'function') {
      aladinInstance.addOverlay(selectedMosaicOverlay);
    }
    updateTelescopePointing(telescopeSnapshot, followTelescope);
    updatePlannedPointing(telescopeSnapshot);
    updateTelescopeFootprint(telescopeSnapshot);
    updateMosaicFootprints(mosaicPlan, selectedPanelId);
    bindMapClickSelection(container);

    initialized = true;
    currentContainerId = containerId;
    currentTarget = normalizedTarget;
    currentFollowTelescope = followTelescope;
    currentMosaicKey = mosaicKey;
    currentPlannedPointingKey = plannedPointingKey;
    loadState = 'ready';
  } catch (error) {
    loadState = 'failed';
    renderSkyPlaceholder(container, getSkyErrorMessage(error));
  }
}

function bindMapClickSelection(container: HTMLElement): void {
  if (currentMapClickHandler) {
    container.removeEventListener('click', currentMapClickHandler);
  }

  currentMapClickHandler = (event: MouseEvent) => {
    if (!currentMosaicPlan?.panels.length || !currentPanelSelectionHandler) {
      return;
    }

    const target = event.target as HTMLElement | null;
    if (shouldIgnoreMapClick(target)) {
      return;
    }

    const worldCoordinates = getWorldCoordinatesFromClick(container, event);
    if (!worldCoordinates) {
      return;
    }

    const selectedPanel = findPanelAtCoordinates(currentMosaicPlan.panels, worldCoordinates.raDeg, worldCoordinates.decDeg);
    if (!selectedPanel) {
      return;
    }

    if (selectedPanel.id === currentSelectedPanelId) {
      return;
    }

    currentPanelSelectionHandler(selectedPanel.id);
  };

  container.addEventListener('click', currentMapClickHandler);
}

function shouldIgnoreMapClick(target: HTMLElement | null): boolean {
  if (!target) {
    return false;
  }

  return Boolean(
    target.closest(
      'button, input, select, textarea, a, .aladin-popup-container, .aladin-popup, .aladin-location, .aladin-zoom, .aladin-cooFrame, .aladin-projection-control, .aladin-fullScreen-control, .aladin-status-bar, .aladin-widgets-toolbar',
    ),
  );
}

function getWorldCoordinatesFromClick(
  container: HTMLElement,
  event: MouseEvent,
): { raDeg: number; decDeg: number } | null {
  if (!aladinInstance) {
    return null;
  }

  const rect = container.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const candidateMethods = [
    () => (typeof aladinInstance.pix2world === 'function' ? aladinInstance.pix2world(x, y) : null),
    () => (typeof aladinInstance.viewToWorld === 'function' ? aladinInstance.viewToWorld(x, y) : null),
  ];

  for (const getCandidate of candidateMethods) {
    const coordinates = normalizeWorldCoordinates(getCandidate());
    if (coordinates) {
      return coordinates;
    }
  }

  return null;
}

function normalizeWorldCoordinates(value: unknown): { raDeg: number; decDeg: number } | null {
  if (Array.isArray(value) && value.length >= 2) {
    const raDeg = Number(value[0]);
    const decDeg = Number(value[1]);
    return Number.isFinite(raDeg) && Number.isFinite(decDeg) ? { raDeg, decDeg } : null;
  }

  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const raDeg = Number(record.ra ?? record.lon ?? record.lng);
    const decDeg = Number(record.dec ?? record.lat);
    return Number.isFinite(raDeg) && Number.isFinite(decDeg) ? { raDeg, decDeg } : null;
  }

  return null;
}

function findPanelAtCoordinates(
  panels: MosaicPanel[],
  raDeg: number,
  decDeg: number,
): MosaicPanel | null {
  const containingPanel = panels.find((panel) => isPointInsidePanel(panel, raDeg, decDeg)) ?? null;
  if (containingPanel) {
    return containingPanel;
  }

  return null;
}

function isPointInsidePanel(panel: MosaicPanel, raDeg: number, decDeg: number): boolean {
  const deltaDec = decDeg - panel.center_dec_deg;
  const deltaRa = shortestRaDeltaDeg(raDeg, panel.center_ra_deg);
  const cosDec = Math.max(Math.cos((panel.center_dec_deg * Math.PI) / 180.0), 1e-6);
  const localX = deltaRa * cosDec;
  const localY = deltaDec;

  const rotationRad = (-panel.rotation_deg * Math.PI) / 180.0;
  const rotatedX = localX * Math.cos(rotationRad) - localY * Math.sin(rotationRad);
  const rotatedY = localX * Math.sin(rotationRad) + localY * Math.cos(rotationRad);

  return (
    Math.abs(rotatedX) <= panel.fov_width_deg / 2.0
    && Math.abs(rotatedY) <= panel.fov_height_deg / 2.0
  );
}

function shortestRaDeltaDeg(targetRaDeg: number, centerRaDeg: number): number {
  let delta = targetRaDeg - centerRaDeg;
  while (delta > 180) {
    delta -= 360;
  }
  while (delta < -180) {
    delta += 360;
  }
  return delta;
}

function createTelescopeCatalog(A: any): any {
  if (typeof A.catalog !== 'function') {
    return null;
  }
  return A.catalog({
    name: 'Telescope',
    sourceSize: 20,
    shape: 'plus',
    color: '#ff6b5e',
    onClick: 'showPopup',
  });
}

function createPlannedPointingCatalog(A: any): any {
  if (typeof A.catalog !== 'function') {
    return null;
  }
  return A.catalog({
    name: 'Planned target',
    sourceSize: 18,
    shape: 'rhomb',
    color: '#ffd166',
    onClick: 'showPopup',
  });
}

function updateTelescopePointing(telescopeSnapshot: TelescopeSnapshot | null, followTelescope: boolean): void {
  if (!aladinApi || !telescopeCatalog) {
    return;
  }

  const raHours = telescopeSnapshot?.telescope_state.ra_hours;
  const decDeg = telescopeSnapshot?.telescope_state.dec_deg;
  if (raHours == null || decDeg == null) {
    clearCatalogSources(telescopeCatalog);
    return;
  }

  const raDeg = raHours * 15.0;
  const markerLabel = telescopeSnapshot?.telescope_state.target_name || 'Telescope';
  const status = telescopeSnapshot?.telescope_state.status || 'unknown';
  const sourceKind = telescopeSnapshot?.telescope_state.source_kind || 'unknown';

  const marker =
    typeof aladinApi.marker === 'function'
      ? aladinApi.marker(raDeg, decDeg, {
          popupTitle: markerLabel,
          popupDesc: `Source: ${sourceKind} | Status: ${status}`,
        })
      : typeof aladinApi.source === 'function'
        ? aladinApi.source(raDeg, decDeg, {
            popupTitle: markerLabel,
            popupDesc: `Source: ${sourceKind} | Status: ${status}`,
          })
        : null;

  clearCatalogSources(telescopeCatalog);
  if (marker && typeof telescopeCatalog.addSources === 'function') {
    telescopeCatalog.addSources([marker]);
  }
  if (followTelescope && aladinInstance && typeof aladinInstance.gotoRaDec === 'function') {
    aladinInstance.gotoRaDec(raDeg, decDeg);
  }
  updateTelescopeFootprint(telescopeSnapshot);
}

function clearCatalogSources(catalog: any): void {
  if (!catalog) {
    return;
  }
  if (typeof catalog.clear === 'function') {
    catalog.clear();
    return;
  }
  if (Array.isArray(catalog.sources)) {
    catalog.sources.length = 0;
  }
}

function createFootprintOverlay(A: any): any {
  if (typeof A.graphicOverlay !== 'function') {
    return null;
  }
  return A.graphicOverlay({
    color: '#ff9a8f',
    lineWidth: 2,
  });
}

function createPlannedFootprintOverlay(A: any): any {
  if (typeof A.graphicOverlay !== 'function') {
    return null;
  }
  return A.graphicOverlay({
    color: '#ffd166',
    lineWidth: 1.6,
  });
}

function createMosaicOverlay(A: any, color: string, lineWidth: number): any {
  if (typeof A.graphicOverlay !== 'function') {
    return null;
  }
  return A.graphicOverlay({
    color,
    lineWidth,
  });
}

function updateTelescopeFootprint(telescopeSnapshot: TelescopeSnapshot | null): void {
  if (!aladinApi || !footprintOverlay) {
    return;
  }

  if (typeof footprintOverlay.removeAll === 'function') {
    footprintOverlay.removeAll();
  }

  const state = telescopeSnapshot?.telescope_state;
  const profile = telescopeSnapshot?.imaging_profile;
  if (
    state?.ra_hours == null
    || state.dec_deg == null
    || profile?.fov_width_deg == null
    || profile.fov_height_deg == null
  ) {
    return;
  }

  const corners = computeFootprintCorners(
    state.ra_hours * 15.0,
    state.dec_deg,
    profile.fov_width_deg,
    profile.fov_height_deg,
  );

  const polygon =
    typeof aladinApi.polygon === 'function'
      ? aladinApi.polygon(corners)
      : null;

  if (polygon && typeof footprintOverlay.addFootprints === 'function') {
    footprintOverlay.addFootprints([polygon]);
  }
}

function updatePlannedPointing(telescopeSnapshot: TelescopeSnapshot | null): void {
  if (!aladinApi || !plannedPointingCatalog || !plannedFootprintOverlay) {
    return;
  }

  clearCatalogSources(plannedPointingCatalog);
  if (typeof plannedFootprintOverlay.removeAll === 'function') {
    plannedFootprintOverlay.removeAll();
  }

  const plannedPointing = telescopeSnapshot?.planned_pointing;
  const profile = telescopeSnapshot?.imaging_profile;
  if (
    !plannedPointing
    || profile?.fov_width_deg == null
    || profile.fov_height_deg == null
  ) {
    return;
  }

  const raDeg = plannedPointing.ra_hours * 15.0;
  const marker =
    typeof aladinApi.marker === 'function'
      ? aladinApi.marker(raDeg, plannedPointing.dec_deg, {
          popupTitle: plannedPointing.target_name || 'Planned target',
          popupDesc: `Source: ${plannedPointing.source_kind}${plannedPointing.source_id ? ` | ${plannedPointing.source_id}` : ''}`,
        })
      : typeof aladinApi.source === 'function'
        ? aladinApi.source(raDeg, plannedPointing.dec_deg, {
            popupTitle: plannedPointing.target_name || 'Planned target',
            popupDesc: `Source: ${plannedPointing.source_kind}${plannedPointing.source_id ? ` | ${plannedPointing.source_id}` : ''}`,
          })
        : null;

  if (marker && typeof plannedPointingCatalog.addSources === 'function') {
    plannedPointingCatalog.addSources([marker]);
  }

  const corners = computeFootprintCorners(
    raDeg,
    plannedPointing.dec_deg,
    profile.fov_width_deg,
    profile.fov_height_deg,
    profile.rotation_deg ?? 0,
  );
  const polygon = typeof aladinApi.polygon === 'function' ? aladinApi.polygon(corners) : null;
  if (polygon && typeof plannedFootprintOverlay.addFootprints === 'function') {
    plannedFootprintOverlay.addFootprints([polygon]);
  }
}

function updateMosaicFootprints(mosaicPlan: MosaicPlan | null, selectedPanelId: string | null): void {
  if (!aladinApi || !mosaicOverlay || !selectedMosaicOverlay) {
    return;
  }

  if (typeof mosaicOverlay.removeAll === 'function') {
    mosaicOverlay.removeAll();
  }
  if (typeof selectedMosaicOverlay.removeAll === 'function') {
    selectedMosaicOverlay.removeAll();
  }

  if (!mosaicPlan?.panels.length) {
    return;
  }

  const allPolygons: any[] = [];
  const selectedPolygons: any[] = [];
  for (const panel of mosaicPlan.panels) {
    const corners = computeFootprintCorners(
      panel.center_ra_deg,
      panel.center_dec_deg,
      panel.fov_width_deg,
      panel.fov_height_deg,
      panel.rotation_deg,
    );
    const polygon = typeof aladinApi.polygon === 'function' ? aladinApi.polygon(corners) : null;
    if (!polygon) {
      continue;
    }
    if (panel.id === selectedPanelId || (!selectedPanelId && panel.id === mosaicPlan.selected_panel_id)) {
      selectedPolygons.push(polygon);
    } else {
      allPolygons.push(polygon);
    }
  }

  if (allPolygons.length && typeof mosaicOverlay.addFootprints === 'function') {
    mosaicOverlay.addFootprints(allPolygons);
  }
  if (selectedPolygons.length && typeof selectedMosaicOverlay.addFootprints === 'function') {
    selectedMosaicOverlay.addFootprints(selectedPolygons);
  }
}

function computeFootprintCorners(
  centerRaDeg: number,
  centerDecDeg: number,
  fovWidthDeg: number,
  fovHeightDeg: number,
  rotationDeg = 0,
): Array<[number, number]> {
  const halfWidth = fovWidthDeg / 2.0;
  const halfHeight = fovHeightDeg / 2.0;
  const rotationRad = (rotationDeg * Math.PI) / 180.0;
  const baseCorners: Array<[number, number]> = [
    [-halfWidth, -halfHeight],
    [halfWidth, -halfHeight],
    [halfWidth, halfHeight],
    [-halfWidth, halfHeight],
  ];

  return baseCorners.map(([xDeg, yDeg]) => {
    const rotatedXDeg = xDeg * Math.cos(rotationRad) - yDeg * Math.sin(rotationRad);
    const rotatedYDeg = xDeg * Math.sin(rotationRad) + yDeg * Math.cos(rotationRad);
    const cosDec = Math.max(Math.cos(((centerDecDeg + rotatedYDeg) * Math.PI) / 180.0), 1e-6);
    return normalizeRaDec(centerRaDeg + (rotatedXDeg / cosDec), centerDecDeg + rotatedYDeg);
  });
}

function normalizeRaDec(raDeg: number, decDeg: number): [number, number] {
  let normalizedRa = raDeg % 360;
  if (normalizedRa < 0) {
    normalizedRa += 360;
  }
  const normalizedDec = Math.max(-90, Math.min(90, decDeg));
  return [normalizedRa, normalizedDec];
}

function getRecommendedFoVDeg(snapshot: TelescopeSnapshot | null): number {
  const width = snapshot?.imaging_profile.fov_width_deg;
  const height = snapshot?.imaging_profile.fov_height_deg;
  if (width == null || height == null) {
    return 2.5;
  }
  const dominantFov = Math.max(width, height);
  const paddedFov = dominantFov * 2.0;
  return Math.max(0.2, Math.min(180, paddedFov));
}

function hasPointingChanged(telescopeSnapshot: TelescopeSnapshot | null): boolean {
  const nextPointingKey = getPointingKey(telescopeSnapshot);
  const currentPointingKey = getPointingKey((globalThis as { __tsnSkySnapshot?: TelescopeSnapshot | null }).__tsnSkySnapshot ?? null);
  (globalThis as { __tsnSkySnapshot?: TelescopeSnapshot | null }).__tsnSkySnapshot = telescopeSnapshot;
  return nextPointingKey !== currentPointingKey;
}

function getPointingKey(snapshot: TelescopeSnapshot | null): string {
  const state = snapshot?.telescope_state;
  if (!state) {
    return 'none';
  }
  return [
    state.ra_hours ?? 'na',
    state.dec_deg ?? 'na',
    state.status ?? 'na',
    state.target_name ?? 'na',
  ].join('|');
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

function getMosaicKey(mosaicPlan: MosaicPlan | null, selectedPanelId: string | null): string {
  if (!mosaicPlan) {
    return 'none';
  }
  return [
    mosaicPlan.id,
    selectedPanelId ?? mosaicPlan.selected_panel_id ?? 'none',
    mosaicPlan.panels.length,
    ...mosaicPlan.panels.map((panel) => `${panel.id}:${panel.center_ra_deg}:${panel.center_dec_deg}:${panel.rotation_deg}:${panel.status}`),
  ].join('|');
}

function getPlannedPointingKey(snapshot: TelescopeSnapshot | null): string {
  const plannedPointing = snapshot?.planned_pointing;
  if (!plannedPointing) {
    return 'none';
  }
  return [
    plannedPointing.target_name ?? 'none',
    plannedPointing.ra_hours,
    plannedPointing.dec_deg,
    plannedPointing.source_kind,
    plannedPointing.source_id ?? 'none',
  ].join('|');
}
