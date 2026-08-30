import 'leaflet/dist/leaflet.css';
import type { DivIcon, LayerGroup, Map as LeafletMap, Marker } from 'leaflet';
import type { Site } from './api';

let currentContainerId: string | null = null;
let leafletApi: typeof import('leaflet') | null = null;
let leafletMap: LeafletMap | null = null;
let leafletLayerGroup: LayerGroup | null = null;
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
  }

  const L = await import('leaflet');
  leafletApi = L;
  container.innerHTML = '';

  const initialView = getInitialView(validSites, activeSite);
  leafletMap = L.map(container, {
    zoomControl: true,
    attributionControl: true,
  }).setView(initialView.center, initialView.zoom);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(leafletMap);

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
