import 'leaflet/dist/leaflet.css';
import type { Circle, Control, DivIcon, LayerGroup, LeafletMouseEvent, Map as LeafletMap, Marker, TileLayer } from 'leaflet';
import type { Site } from './api';
import { formatAccuracy, type CurrentDevicePosition } from './current_device_position';
import {
  ARTIFICIAL_SKY_BRIGHTNESS_LAYER,
  localRasterLightPollutionPointProvider,
  type LightPollutionPointResult,
} from './light_pollution';

export type LightPollutionLayerState = {
  enabled: boolean;
  opacity: number;
};

export type CandidateSiteCreateRequest = {
  latitudeDeg: number;
  longitudeDeg: number;
  lightPollution: LightPollutionPointResult;
};

let currentContainerId: string | null = null;
let leafletApi: typeof import('leaflet') | null = null;
let leafletMap: LeafletMap | null = null;
let leafletLayerGroup: LayerGroup | null = null;
let leafletLightPollutionLayer: TileLayer | null = null;
let leafletCandidateMarker: Marker | null = null;
let leafletCandidateClearControl: Control | null = null;
let leafletCurrentDeviceMarker: Marker | null = null;
let leafletCurrentDeviceAccuracyCircle: Circle | null = null;
let currentDevicePosition: CurrentDevicePosition | null = null;
let candidateRequestId = 0;
let preservedCenter: [number, number] | null = null;
let preservedZoom: number | null = null;
let currentActiveSiteId: string | null = null;
let currentSitesKey = '';

export function preserveObservationCenterMapState(): void {
  if (!leafletMap) {
    return;
  }

  const center = leafletMap.getCenter();
  preservedCenter = [center.lat, center.lng];
  preservedZoom = leafletMap.getZoom();
}

export async function mountObservationCenterMap(
  containerId: string,
  sites: Site[],
  activeSiteId: string | null,
  onSiteSelected?: (siteId: string) => void,
  lightPollutionLayer?: LightPollutionLayerState,
): Promise<void> {
  const container = document.getElementById(containerId);
  if (!container) {
    return;
  }

  const validSites = sites.filter(hasValidCoordinates);
  const nextSitesKey = getSitesKey(validSites);
  const activeSite = validSites.find((site) => site.id === activeSiteId) ?? null;
  const selectionChanged = currentActiveSiteId !== activeSiteId;
  const sitesChanged = currentSitesKey !== nextSitesKey;

  if (
    leafletMap
    && leafletLayerGroup
    && currentContainerId === containerId
    && container.childElementCount > 0
  ) {
    updateSiteMarkers(validSites, activeSiteId, onSiteSelected);
    updateLightPollutionLayer(lightPollutionLayer);
    if (selectionChanged && activeSite) {
      leafletMap.panTo([activeSite.latitude_deg!, activeSite.longitude_deg!], { animate: true, duration: 0.6 });
    } else if (sitesChanged && !preservedCenter && validSites.length > 1) {
      fitMapToSites(validSites);
    }
    leafletMap.invalidateSize();
    currentActiveSiteId = activeSiteId;
    currentSitesKey = nextSitesKey;
    return;
  }

  if (leafletMap) {
    leafletMap.remove();
    leafletMap = null;
    leafletLayerGroup = null;
    leafletLightPollutionLayer = null;
    leafletCandidateMarker = null;
    leafletCandidateClearControl = null;
    leafletCurrentDeviceMarker = null;
    leafletCurrentDeviceAccuracyCircle = null;
    currentDevicePosition = null;
    candidateRequestId += 1;
  }

  const L = await import('leaflet');
  leafletApi = L;
  container.innerHTML = '';

  const initialView = getInitialView(validSites, activeSite);
  leafletMap = L.map(container, {
    zoomControl: true,
    attributionControl: true,
  }).setView(initialView.center, initialView.zoom);
  leafletMap.on('click', handleCandidateMapClick);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(leafletMap);

  updateLightPollutionLayer(lightPollutionLayer);
  leafletLayerGroup = L.layerGroup().addTo(leafletMap);
  updateSiteMarkers(validSites, activeSiteId, onSiteSelected);
  if (!preservedCenter) {
    if (activeSite) {
      leafletMap.setView([activeSite.latitude_deg!, activeSite.longitude_deg!], Math.max(initialView.zoom, 8));
    } else if (validSites.length > 1) {
      fitMapToSites(validSites);
    }
  }

  leafletMap.invalidateSize();
  currentContainerId = containerId;
  currentActiveSiteId = activeSiteId;
  currentSitesKey = nextSitesKey;
}

export function showCurrentDevicePosition(position: CurrentDevicePosition): void {
  if (!leafletApi || !leafletMap) {
    currentDevicePosition = position;
    return;
  }

  currentDevicePosition = position;
  const latlng: [number, number] = [position.latitudeDeg, position.longitudeDeg];
  if (!leafletCurrentDeviceMarker) {
    leafletCurrentDeviceMarker = leafletApi.marker(latlng, {
      icon: createCurrentDeviceIcon(leafletApi),
      keyboard: true,
      title: 'Current device',
    }).addTo(leafletMap);
  } else {
    leafletCurrentDeviceMarker.setLatLng(latlng);
  }

  leafletCurrentDeviceMarker
    .bindPopup(renderCurrentDevicePopup(position), {
      className: 'current-device-popup',
      maxWidth: 280,
    });
  leafletCurrentDeviceMarker.on('popupopen', bindCurrentDeviceInspectButton);

  if (position.accuracyM != null && Number.isFinite(position.accuracyM)) {
    if (!leafletCurrentDeviceAccuracyCircle) {
      leafletCurrentDeviceAccuracyCircle = leafletApi.circle(latlng, {
        radius: position.accuracyM,
        className: 'current-device-accuracy-circle',
        color: '#8dd4ff',
        fillColor: '#8dd4ff',
        fillOpacity: 0.08,
        opacity: 0.28,
        weight: 1,
        interactive: false,
      }).addTo(leafletMap);
    } else {
      leafletCurrentDeviceAccuracyCircle.setLatLng(latlng);
      leafletCurrentDeviceAccuracyCircle.setRadius(position.accuracyM);
    }
  } else if (leafletCurrentDeviceAccuracyCircle) {
    leafletMap.removeLayer(leafletCurrentDeviceAccuracyCircle);
    leafletCurrentDeviceAccuracyCircle = null;
  }

  leafletMap.setView(latlng, Math.max(leafletMap.getZoom(), 14), { animate: true });
  leafletCurrentDeviceMarker.openPopup();
}

async function handleCandidateMapClick(event: LeafletMouseEvent): Promise<void> {
  if (!leafletApi || !leafletMap) {
    return;
  }

  const latitudeDeg = event.latlng.lat;
  const longitudeDeg = event.latlng.lng;
  const requestId = candidateRequestId + 1;
  candidateRequestId = requestId;
  setCandidateSiteMarker(latitudeDeg, longitudeDeg, getLoadingLightPollutionResult());
  const pointValue = await localRasterLightPollutionPointProvider.queryPoint(latitudeDeg, longitudeDeg);
  if (requestId !== candidateRequestId) {
    return;
  }
  setCandidateSiteMarker(latitudeDeg, longitudeDeg, pointValue);
}

async function inspectLocationAsCandidate(latitudeDeg: number, longitudeDeg: number): Promise<void> {
  if (!leafletApi || !leafletMap) {
    return;
  }

  const requestId = candidateRequestId + 1;
  candidateRequestId = requestId;
  setCandidateSiteMarker(latitudeDeg, longitudeDeg, getLoadingLightPollutionResult());
  const pointValue = await localRasterLightPollutionPointProvider.queryPoint(latitudeDeg, longitudeDeg);
  if (requestId !== candidateRequestId) {
    return;
  }
  setCandidateSiteMarker(latitudeDeg, longitudeDeg, pointValue);
}

function setCandidateSiteMarker(
  latitudeDeg: number,
  longitudeDeg: number,
  pointValue: LightPollutionPointResult,
): void {
  if (!leafletApi || !leafletMap) {
    return;
  }

  if (leafletCandidateMarker) {
    leafletMap.removeLayer(leafletCandidateMarker);
    leafletCandidateMarker = null;
  }

  leafletCandidateMarker = leafletApi.marker([latitudeDeg, longitudeDeg], {
    icon: createCandidateSiteIcon(leafletApi),
    keyboard: true,
    title: 'Candidate Site',
  });
  leafletCandidateMarker
    .bindPopup(renderCandidateSitePopup(latitudeDeg, longitudeDeg, pointValue), {
      className: 'candidate-site-popup',
      maxWidth: 320,
    })
    .addTo(leafletMap)
    .openPopup();
  leafletCandidateMarker.on('popupopen', () => {
    bindCandidateCreateButton(latitudeDeg, longitudeDeg, pointValue);
  });
  bindCandidateCreateButton(latitudeDeg, longitudeDeg, pointValue);
  updateCandidateClearControl();
}

function clearCandidateSiteMarker(): void {
  candidateRequestId += 1;
  if (!leafletMap || !leafletCandidateMarker) {
    return;
  }

  leafletMap.removeLayer(leafletCandidateMarker);
  leafletCandidateMarker = null;
  updateCandidateClearControl();
}

function updateCandidateClearControl(): void {
  if (!leafletApi || !leafletMap) {
    return;
  }

  if (!leafletCandidateMarker) {
    if (leafletCandidateClearControl) {
      leafletMap.removeControl(leafletCandidateClearControl);
      leafletCandidateClearControl = null;
    }
    return;
  }

  if (leafletCandidateClearControl) {
    return;
  }

  const L = leafletApi;
  const CandidateClearControl = L.Control.extend({
    options: {
      position: 'topright',
    },
    onAdd() {
      const container = L.DomUtil.create('div', 'leaflet-bar candidate-site-control');
      const button = L.DomUtil.create('button', 'candidate-site-control__button', container);
      button.type = 'button';
      button.textContent = 'Clear candidate';
      button.title = 'Clear temporary candidate site marker';
      L.DomEvent.disableClickPropagation(container);
      L.DomEvent.on(button, 'click', (event) => {
        L.DomEvent.stop(event);
        clearCandidateSiteMarker();
      });
      return container;
    },
  });

  leafletCandidateClearControl = new CandidateClearControl();
  leafletCandidateClearControl.addTo(leafletMap);
}

function renderCandidateSitePopup(
  latitudeDeg: number,
  longitudeDeg: number,
  pointValue: LightPollutionPointResult,
): string {
  const measurement = pointValue.measurement;
  const isLoading = pointValue.status === 'loading';
  return `
    <section class="candidate-site-popup__content">
      <strong>Candidate Site</strong>
      <div class="candidate-site-popup__coordinates">
        <span class="candidate-site-popup__coordinate">Lat ${latitudeDeg.toFixed(6)}°</span>
        <span class="candidate-site-popup__coordinate">Lon ${longitudeDeg.toFixed(6)}°</span>
      </div>
      <span class="candidate-site-popup__section-title">Light Pollution</span>
      ${measurement
        ? `
          <div class="candidate-site-popup__primary">
            <div>
              <span>Estimated SQM</span>
              <strong>${measurement.estimatedSqmMagArcsec2.toFixed(2)}</strong>
            </div>
            <div>
              <span>Estimated Bortle</span>
              <strong>Class ${measurement.estimatedBortleClass}</strong>
            </div>
          </div>
          <dl class="candidate-site-popup__data">
            <div>
              <dt>Artificial brightness</dt>
              <dd>${measurement.artificialBrightnessMcdM2.toFixed(4)} mcd/m²</dd>
            </div>
            <div>
              <dt>Artificial / natural ratio</dt>
              <dd>${measurement.naturalSkyRatio.toFixed(2)}× <small>estimated</small></dd>
            </div>
            <div>
              <dt>Estimated total brightness</dt>
              <dd>${measurement.estimatedTotalBrightnessMcdM2.toFixed(4)} mcd/m² <small>estimated</small></dd>
            </div>
            <div>
              <dt>Dataset</dt>
              <dd>${escapeHtml(pointValue.datasetName)}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>${escapeHtml(pointValue.source)}</dd>
            </div>
          </dl>
        `
        : `
          <p class="candidate-site-popup__unavailable">Light pollution data unavailable for this location.</p>
          <dl class="candidate-site-popup__data">
            <div>
              <dt>Dataset</dt>
              <dd>${escapeHtml(pointValue.datasetName)}</dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>${escapeHtml(pointValue.source)}</dd>
            </div>
          </dl>
        `}
      ${isLoading ? '<span class="candidate-site-popup__loading">Loading light pollution data…</span>' : ''}
      <button
        class="candidate-site-popup__button"
        type="button"
        data-create-site-from-candidate
        ${isLoading ? 'disabled' : ''}
        title="${isLoading ? 'Light pollution lookup is still loading.' : 'Create a saved observation site from this candidate.'}"
      >
        Create Site here
      </button>
      <small>${isLoading ? 'Waiting for light pollution lookup…' : 'Opens the normal Site form with this candidate prefilled.'}</small>
    </section>
  `;
}

function bindCandidateCreateButton(
  latitudeDeg: number,
  longitudeDeg: number,
  pointValue: LightPollutionPointResult,
): void {
  if (pointValue.status === 'loading') {
    return;
  }
  const popupElement = leafletCandidateMarker?.getPopup()?.getElement();
  const button = popupElement?.querySelector<HTMLButtonElement>('[data-create-site-from-candidate]');
  if (!button) {
    return;
  }
  button.addEventListener('click', () => {
    window.dispatchEvent(new CustomEvent<CandidateSiteCreateRequest>('tsn-dss:create-site-from-candidate', {
      detail: {
        latitudeDeg,
        longitudeDeg,
        lightPollution: pointValue,
      },
    }));
    clearCandidateSiteMarker();
  }, { once: true });
}

function renderCurrentDevicePopup(position: CurrentDevicePosition): string {
  return `
    <section class="current-device-popup__content">
      <strong>Current device</strong>
      <span class="candidate-site-popup__coordinate">Lat ${position.latitudeDeg.toFixed(6)}°</span>
      <span class="candidate-site-popup__coordinate">Lon ${position.longitudeDeg.toFixed(6)}°</span>
      <span>${escapeHtml(formatAccuracy(position.accuracyM))}</span>
      <span>Position timestamp: ${escapeHtml(formatDeviceTimestamp(position.timestampMs))}</span>
      <button class="candidate-site-popup__button" type="button" data-inspect-current-device>
        Inspect this location
      </button>
      <small>Creates a Candidate Site here and runs the existing Light Pollution lookup.</small>
    </section>
  `;
}

function bindCurrentDeviceInspectButton(): void {
  const popupElement = leafletCurrentDeviceMarker?.getPopup()?.getElement();
  const button = popupElement?.querySelector<HTMLButtonElement>('[data-inspect-current-device]');
  if (!button || !currentDevicePosition) {
    return;
  }
  const position = currentDevicePosition;
  button.addEventListener('click', () => {
    void inspectLocationAsCandidate(position.latitudeDeg, position.longitudeDeg);
  }, { once: true });
}

function formatDeviceTimestamp(timestampMs: number): string {
  const date = new Date(timestampMs);
  if (Number.isNaN(date.getTime())) {
    return 'unavailable';
  }
  return date.toLocaleString();
}

function getLoadingLightPollutionResult(): LightPollutionPointResult {
  return {
    status: 'loading',
    providerName: localRasterLightPollutionPointProvider.name,
    datasetName: localRasterLightPollutionPointProvider.datasetName,
    source: 'Falchi et al. 2016',
    sourceValue: null,
    sourceUnit: null,
    measurement: null,
    message: 'Loading…',
  };
}

function updateLightPollutionLayer(lightPollutionLayer?: LightPollutionLayerState): void {
  if (!leafletApi || !leafletMap) {
    return;
  }

  const shouldShow = lightPollutionLayer?.enabled ?? false;
  if (!shouldShow) {
    if (leafletLightPollutionLayer) {
      leafletMap.removeLayer(leafletLightPollutionLayer);
      leafletLightPollutionLayer = null;
    }
    return;
  }

  const opacity = clampOpacity(lightPollutionLayer?.opacity ?? 0.55);
  if (!leafletLightPollutionLayer) {
    leafletLightPollutionLayer = leafletApi.tileLayer(ARTIFICIAL_SKY_BRIGHTNESS_LAYER.tileUrl, {
      attribution: ARTIFICIAL_SKY_BRIGHTNESS_LAYER.attribution,
      maxNativeZoom: ARTIFICIAL_SKY_BRIGHTNESS_LAYER.maxNativeZoom,
      maxZoom: 19,
      opacity,
      zIndex: 350,
      className: 'observation-map-light-pollution-layer',
    }).addTo(leafletMap);
    return;
  }

  leafletLightPollutionLayer.setOpacity(opacity);
  if (!leafletMap.hasLayer(leafletLightPollutionLayer)) {
    leafletLightPollutionLayer.addTo(leafletMap);
  }
}

function clampOpacity(value: number): number {
  if (!Number.isFinite(value)) {
    return 0.55;
  }
  return Math.max(0, Math.min(1, value));
}

function updateSiteMarkers(
  sites: Site[],
  activeSiteId: string | null,
  onSiteSelected?: (siteId: string) => void,
): void {
  if (!leafletApi || !leafletMap || !leafletLayerGroup) {
    return;
  }

  const layerGroup = leafletLayerGroup;
  leafletLayerGroup.clearLayers();

  if (!sites.length) {
    return;
  }

  const markers: Marker[] = sites.map((site) => {
    const isActive = site.id === activeSiteId;
    const marker = leafletApi!.marker([site.latitude_deg!, site.longitude_deg!], {
      icon: createSiteIcon(leafletApi!, isActive),
      keyboard: true,
      title: site.name,
    });
    const popupParts = [
      `<strong>${escapeHtml(site.name)}</strong>`,
      `${site.latitude_deg!.toFixed(4)}°, ${site.longitude_deg!.toFixed(4)}°`,
    ];
    if (site.elevation_m != null) {
      popupParts.push(`${site.elevation_m.toFixed(0)} m`);
    }
    marker.bindPopup(popupParts.join('<br />'));
    marker.on('click', () => {
      onSiteSelected?.(site.id);
    });
    return marker;
  });

  markers.forEach((marker) => marker.addTo(layerGroup));
}

function createSiteIcon(L: typeof import('leaflet'), isActive: boolean): DivIcon {
  return L.divIcon({
    className: `observation-site-marker${isActive ? ' observation-site-marker--active' : ''}`,
    html: '<span class="observation-site-marker__dot"></span>',
    iconSize: [18, 18],
    iconAnchor: [9, 9],
    popupAnchor: [0, -10],
  });
}

function createCandidateSiteIcon(L: typeof import('leaflet')): DivIcon {
  return L.divIcon({
    className: 'candidate-site-marker',
    html: '<span class="candidate-site-marker__ring"></span><span class="candidate-site-marker__cross"></span>',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    popupAnchor: [0, -12],
  });
}

function createCurrentDeviceIcon(L: typeof import('leaflet')): DivIcon {
  return L.divIcon({
    className: 'current-device-marker',
    html: '<span class="current-device-marker__pulse"></span><span class="current-device-marker__dot"></span>',
    iconSize: [24, 24],
    iconAnchor: [12, 12],
    popupAnchor: [0, -12],
  });
}

function fitMapToSites(sites: Site[]): void {
  if (!leafletMap || sites.length < 2) {
    return;
  }

  const bounds = sites.map((site) => [site.latitude_deg!, site.longitude_deg!] as [number, number]);
  leafletMap.fitBounds(bounds, {
    padding: [24, 24],
    maxZoom: 10,
  });
}

function getInitialView(
  sites: Site[],
  activeSite: Site | null,
): { center: [number, number]; zoom: number } {
  if (preservedCenter && preservedZoom != null) {
    return {
      center: preservedCenter,
      zoom: preservedZoom,
    };
  }

  if (activeSite) {
    return {
      center: [activeSite.latitude_deg!, activeSite.longitude_deg!],
      zoom: 8,
    };
  }

  if (sites.length === 1) {
    return {
      center: [sites[0]!.latitude_deg!, sites[0]!.longitude_deg!],
      zoom: 8,
    };
  }

  if (sites.length > 1) {
    return {
      center: getAverageCenter(sites),
      zoom: 5,
    };
  }

  return {
    center: [52.0, 19.0],
    zoom: 5,
  };
}

function getAverageCenter(sites: Site[]): [number, number] {
  const total = sites.reduce(
    (accumulator, site) => {
      accumulator.latitude += site.latitude_deg!;
      accumulator.longitude += site.longitude_deg!;
      return accumulator;
    },
    { latitude: 0, longitude: 0 },
  );
  return [total.latitude / sites.length, total.longitude / sites.length];
}

function getSitesKey(sites: Site[]): string {
  return sites
    .map((site) => `${site.id}:${site.latitude_deg}:${site.longitude_deg}:${site.name}`)
    .join('|');
}

function hasValidCoordinates(site: Site): site is Site & { latitude_deg: number; longitude_deg: number } {
  return Number.isFinite(site.latitude_deg) && Number.isFinite(site.longitude_deg);
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}
