import 'leaflet/dist/leaflet.css';
import type { Control, DivIcon, LayerGroup, LeafletMouseEvent, Map as LeafletMap, Marker, TileLayer } from 'leaflet';
import type { Site } from './api';
import {
  ARTIFICIAL_SKY_BRIGHTNESS_LAYER,
  localRasterLightPollutionPointProvider,
  type LightPollutionPointResult,
} from './light_pollution';

export type LightPollutionLayerState = {
  enabled: boolean;
  opacity: number;
};

let currentContainerId: string | null = null;
let leafletApi: typeof import('leaflet') | null = null;
let leafletMap: LeafletMap | null = null;
let leafletLayerGroup: LayerGroup | null = null;
let leafletLightPollutionLayer: TileLayer | null = null;
let leafletCandidateMarker: Marker | null = null;
let leafletCandidateClearControl: Control | null = null;
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
      <span class="candidate-site-popup__coordinate">${latitudeDeg.toFixed(6)}°</span>
      <span class="candidate-site-popup__coordinate">${longitudeDeg.toFixed(6)}°</span>
      <span class="candidate-site-popup__section-title">Light Pollution</span>
      <dl class="candidate-site-popup__data">
        <div>
          <dt>Artificial brightness</dt>
          <dd>${measurement ? `${measurement.artificialBrightnessMcdM2.toFixed(4)} mcd/m² <small>source</small>` : escapeHtml(pointValue.message)}</dd>
        </div>
        <div>
          <dt>Natural sky ratio</dt>
          <dd>${measurement ? `${measurement.naturalSkyRatio.toFixed(2)}× <small>estimated</small>` : '—'}</dd>
        </div>
        <div>
          <dt>Total brightness</dt>
          <dd>${measurement ? `${measurement.estimatedTotalBrightnessMcdM2.toFixed(4)} mcd/m² <small>estimated</small>` : '—'}</dd>
        </div>
        <div>
          <dt>Estimated SQM</dt>
          <dd>${measurement ? `${measurement.estimatedSqmMagArcsec2.toFixed(2)} mag/arcsec² <small>estimated</small>` : '—'}</dd>
        </div>
        <div>
          <dt>Estimated Bortle</dt>
          <dd>${measurement ? `Class ${measurement.estimatedBortleClass} <small>estimated</small>` : '—'}</dd>
        </div>
      </dl>
      <span>Dataset: ${escapeHtml(pointValue.datasetName)}</span>
      <span>Source: ${escapeHtml(pointValue.source)}</span>
      ${isLoading ? '<span class="candidate-site-popup__loading">Loading light pollution data…</span>' : ''}
      <button class="candidate-site-popup__button" type="button" disabled title="Site creation will be added in next step">
        Create Site here
      </button>
      <small>Site creation will be added in next step.</small>
    </section>
  `;
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
