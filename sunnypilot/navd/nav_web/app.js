'use strict';

let map;
let gpsMarker;
let destMarker;
let routeLoaded = false;
let debounceTimer = null;
let currentResults = [];
let mapAvailable = false;
let lastRouteKey = '';
let routeFetchInFlight = false;

const ROUTE_SOURCE_ID = 'active-route-source';
const ROUTE_LAYER_GLOW_ID = 'active-route-glow';
const ROUTE_LAYER_MAIN_ID = 'active-route-main';

function fetchJson(url, options) {
  return fetch(url, options).then((r) => {
    if (!r.ok) throw new Error('Request failed');
    return r.json();
  });
}

function markerElement(color, ringColor) {
  const el = document.createElement('div');
  el.className = 'map-marker';
  el.style.background = color;
  el.style.borderColor = ringColor;
  return el;
}

function ensureRouteLayers() {
  if (!map || routeLoaded) return;
  if (!map.getSource(ROUTE_SOURCE_ID)) {
    map.addSource(ROUTE_SOURCE_ID, {
      type: 'geojson',
      data: {
        type: 'Feature',
        properties: {},
        geometry: { type: 'LineString', coordinates: [] },
      },
    });
  }

  if (!map.getLayer(ROUTE_LAYER_GLOW_ID)) {
    map.addLayer({
      id: ROUTE_LAYER_GLOW_ID,
      type: 'line',
      source: ROUTE_SOURCE_ID,
      paint: {
        'line-color': '#00d4ff',
        'line-width': 10,
        'line-opacity': 0.2,
      },
    });
  }

  if (!map.getLayer(ROUTE_LAYER_MAIN_ID)) {
    map.addLayer({
      id: ROUTE_LAYER_MAIN_ID,
      type: 'line',
      source: ROUTE_SOURCE_ID,
      paint: {
        'line-color': '#49f4ff',
        'line-width': 4,
        'line-opacity': 0.95,
      },
    });
  }

  routeLoaded = true;
}

function setRouteGeoJson(coordinates) {
  if (!mapAvailable || !map) return;
  ensureRouteLayers();
  const src = map.getSource(ROUTE_SOURCE_ID);
  if (!src) return;
  src.setData({
    type: 'Feature',
    properties: {},
    geometry: {
      type: 'LineString',
      coordinates: Array.isArray(coordinates) ? coordinates : [],
    },
  });
}

function clearRoute() {
  setRouteGeoJson([]);
  lastRouteKey = '';
}

function setMarkerPosition(marker, lon, lat) {
  if (!mapAvailable || !marker) return;
  marker.setLngLat([lon, lat]);
}

function maybeFitRoute(gpsLon, gpsLat, destLon, destLat) {
  if (!map) return;
  const bounds = new mapboxgl.LngLatBounds();
  bounds.extend([gpsLon, gpsLat]);
  bounds.extend([destLon, destLat]);
  map.fitBounds(bounds, { padding: 56, duration: 900, maxZoom: 14 });
}

async function initMap() {
  let config = {};
  try {
    config = await fetchJson('/api/config');
  } catch (_) {
    config = {};
  }

  if (typeof window.mapboxgl === 'undefined' || !config.mapboxPublicToken) {
    mapAvailable = false;
    const mapEl = document.getElementById('map');
    if (mapEl) {
      mapEl.innerHTML = '<div style="padding:18px;color:#d0d0d0">Map unavailable (missing Mapbox token or offline). Search and saved Home/Work still work.</div>';
    }
    return;
  }

  mapboxgl.accessToken = config.mapboxPublicToken;
  mapAvailable = true;
  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/dark-v11',
    projection: 'globe',
    center: [-122.0, 37.4],
    zoom: 12,
    attributionControl: true,
    antialias: true,
  });

  map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), 'top-right');

  map.on('style.load', () => {
    map.setFog({
      color: 'rgb(12, 18, 24)',
      'high-color': 'rgb(26, 36, 44)',
      'horizon-blend': 0.12,
      'space-color': 'rgb(1, 2, 4)',
      'star-intensity': 0.12,
    });
    ensureRouteLayers();
  });

  gpsMarker = new mapboxgl.Marker({ element: markerElement('#e94560', '#ffffff'), anchor: 'center' })
    .setLngLat([-122.0, 37.4])
    .addTo(map);

  destMarker = new mapboxgl.Marker({ element: markerElement('#49f4ff', '#0d1a24'), anchor: 'center' })
    .setLngLat([-122.0, 37.4])
    .addTo(map);
  destMarker.getElement().style.display = 'none';
}

function fmtDist(meters) {
  if (meters < 1000) return Math.round(meters) + ' m';
  return (meters / 1000).toFixed(1) + ' km';
}

function fmtTime(seconds) {
  if (seconds < 60) return Math.round(seconds) + ' sec';
  const m = Math.round(seconds / 60);
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return h + 'h ' + rem + 'm';
}

function hasNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function asFiniteNumber(v) {
  if (hasNumber(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const parsed = Number(v);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function getLat(obj) {
  const lat = asFiniteNumber(obj?.lat);
  if (lat !== null) return lat;
  const latitude = asFiniteNumber(obj?.latitude);
  if (latitude !== null) return latitude;
  return null;
}

function getLon(obj) {
  const lon = asFiniteNumber(obj?.lon);
  if (lon !== null) return lon;
  const longitude = asFiniteNumber(obj?.longitude);
  if (longitude !== null) return longitude;
  return null;
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) return;
    const data = await r.json();

    const strip = document.getElementById('status-strip');
    const stripText = document.getElementById('status-text');
    const destPanel = document.getElementById('dest-panel');

    if (data.active) {
      strip.classList.add('active');
      stripText.textContent = '● Navigating to ' + (data.dest_name || 'destination');

      destPanel.classList.add('visible');
      document.getElementById('dest-name').textContent = data.dest_name || 'Destination';
      document.getElementById('dest-addr').textContent = data.dest_addr || '';

      document.getElementById('save-btns').style.display = 'flex';
    } else {
      strip.classList.remove('active');
      stripText.textContent = 'No active navigation';
      destPanel.classList.remove('visible');
    }

    if (mapAvailable && hasNumber(data.gps_lat) && hasNumber(data.gps_lon)) {
      setMarkerPosition(gpsMarker, data.gps_lon, data.gps_lat);
      gpsMarker.getElement().style.display = 'block';
    }

    if (mapAvailable && data.active && hasNumber(data.dest_lat) && hasNumber(data.dest_lon)) {
      setMarkerPosition(destMarker, data.dest_lon, data.dest_lat);
      destMarker.getElement().style.display = 'block';

      if (hasNumber(data.gps_lat) && hasNumber(data.gps_lon)) {
        await updateRoutePreview(data.gps_lat, data.gps_lon, data.dest_lat, data.dest_lon);
      }
    } else if (mapAvailable) {
      destMarker.getElement().style.display = 'none';
      clearRoute();
    }
  } catch (e) {
    // silently ignore network errors during polling
  }
}

async function updateRoutePreview(startLat, startLon, endLat, endLon) {
  if (!mapAvailable || routeFetchInFlight) return;

  const routeKey = [
    Number(startLat).toFixed(3),
    Number(startLon).toFixed(3),
    Number(endLat).toFixed(5),
    Number(endLon).toFixed(5),
  ].join('|');
  if (routeKey === lastRouteKey) return;

  routeFetchInFlight = true;
  try {
    const q =
      '/api/route_preview?start_lat=' + encodeURIComponent(startLat)
      + '&start_lon=' + encodeURIComponent(startLon)
      + '&end_lat=' + encodeURIComponent(endLat)
      + '&end_lon=' + encodeURIComponent(endLon);

    const data = await fetchJson(q);
    const coords = data?.geometry?.coordinates;
    if (Array.isArray(coords) && coords.length > 1) {
      setRouteGeoJson(coords);
      maybeFitRoute(startLon, startLat, endLon, endLat);
      lastRouteKey = routeKey;
    }
  } catch (_) {
    // keep UI responsive; fallback behavior is no route overlay
  } finally {
    routeFetchInFlight = false;
  }
}

async function autocomplete(q) {
  if (!q || q.length < 2) {
    closeDropdown();
    return;
  }
  let url = '/api/autocomplete?q=' + encodeURIComponent(q);
  try {
    const gps = await fetch('/api/gps').then(r => r.json()).catch(() => ({}));
    if (hasNumber(gps.latitude) && hasNumber(gps.longitude)) {
      url += '&lat=' + gps.latitude + '&lon=' + gps.longitude;
    }
  } catch (_) {}

  try {
    const r = await fetch(url);
    if (!r.ok) return;
    currentResults = await r.json();
    if (!Array.isArray(currentResults) || !currentResults.length) {
      closeDropdown();
      return;
    }
    renderDropdown(currentResults);
  } catch (_) {
    closeDropdown();
  }
}

function renderDropdown(results) {
  const list = document.getElementById('autocomplete-list');
  list.innerHTML = '';
  results.forEach((item, i) => {
    const div = document.createElement('div');
    div.className = 'autocomplete-item';
    div.innerHTML = `<div class="item-name">${escHtml(item.name)}</div><div class="item-addr">${escHtml(item.address)}</div>`;
    div.addEventListener('click', () => selectResult(i));
    list.appendChild(div);
  });
  list.classList.add('open');
}

function closeDropdown() {
  document.getElementById('autocomplete-list').classList.remove('open');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function selectResult(idx) {
  const item = currentResults[idx];
  if (!item) return;
  closeDropdown();
  document.getElementById('search-input').value = item.name + (item.address ? ', ' + item.address : '');
  await setDestination(item);
}

async function setDestination(item) {
  const lat = getLat(item);
  const lon = getLon(item);
  if (!hasNumber(lat) || !hasNumber(lon)) {
    alert('Invalid destination coordinates');
    return;
  }

  try {
    const r = await fetch('/api/destination', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lat, lon, name: item.name, address: item.address }),
    });
    if (!r.ok) {
      let errMsg = 'Failed to set destination';
      try {
        const err = await r.json();
        if (err && err.error) errMsg = 'Failed to set destination: ' + err.error;
      } catch (_) {}
      alert(errMsg);
      return;
    }
    await pollStatus();
    if (mapAvailable && map) {
      map.flyTo({ center: [lon, lat], zoom: 14, essential: true, speed: 0.8 });
    }
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function cancelNav() {
  await fetch('/api/destination', { method: 'DELETE' });
  document.getElementById('search-input').value = '';
  clearRoute();
  if (mapAvailable && destMarker) destMarker.getElement().style.display = 'none';
  await pollStatus();
}

async function goHome() {
  const r = await fetch('/api/home');
  const data = await r.json();
  if (!hasNumber(getLat(data)) || !hasNumber(getLon(data))) {
    alert('No home location saved. Search for a location, then use "Save as Home".');
    return;
  }
  await setDestination(data);
}

async function goWork() {
  const r = await fetch('/api/work');
  const data = await r.json();
  if (!hasNumber(getLat(data)) || !hasNumber(getLon(data))) {
    alert('No work location saved. Search for a location, then use "Save as Work".');
    return;
  }
  await setDestination(data);
}

async function saveAsHome() {
  const r = await fetch('/api/status');
  const status = await r.json();
  if (!status.active) return;
  await fetch('/api/home', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat: status.dest_lat, lon: status.dest_lon, name: status.dest_name, address: status.dest_addr }),
  });
  alert('Saved as Home');
}

async function saveAsWork() {
  const r = await fetch('/api/status');
  const status = await r.json();
  if (!status.active) return;
  await fetch('/api/work', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat: status.dest_lat, lon: status.dest_lon, name: status.dest_name, address: status.dest_addr }),
  });
  alert('Saved as Work');
}

document.addEventListener('DOMContentLoaded', async () => {
  await initMap();
  pollStatus();
  setInterval(pollStatus, 3000);

  const input = document.getElementById('search-input');
  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => autocomplete(input.value.trim()), 300);
  });
  input.addEventListener('blur', () => {
    setTimeout(closeDropdown, 200);
  });

  document.getElementById('btn-home').addEventListener('click', goHome);
  document.getElementById('btn-work').addEventListener('click', goWork);
  document.getElementById('btn-cancel').addEventListener('click', cancelNav);
  document.getElementById('btn-save-home').addEventListener('click', saveAsHome);
  document.getElementById('btn-save-work').addEventListener('click', saveAsWork);
});
