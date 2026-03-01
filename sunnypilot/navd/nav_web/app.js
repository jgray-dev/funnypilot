'use strict';

let map, posMarker, destMarker, routeLayer;
let debounceTimer = null;
let currentResults = [];
let mapAvailable = false;

function initMap() {
  if (typeof window.L === 'undefined') {
    mapAvailable = false;
    const mapEl = document.getElementById('map');
    if (mapEl) {
      mapEl.innerHTML = '<div style="padding:18px;color:#d0d0d0">Map unavailable (offline). Search and saved Home/Work still work when data is available.</div>';
    }
    return;
  }

  mapAvailable = true;
  map = L.map('map', { zoomControl: true }).setView([37.4, -122.0], 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(map);

  const posIcon = L.divIcon({
    className: '',
    html: '<div style="width:14px;height:14px;background:#e94560;border:3px solid white;border-radius:50%;box-shadow:0 0 6px rgba(0,0,0,0.5)"></div>',
    iconAnchor: [7, 7],
  });
  posMarker = L.marker([37.4, -122.0], { icon: posIcon }).addTo(map);
  posMarker.setOpacity(0);
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

function getLat(obj) {
  if (hasNumber(obj?.lat)) return obj.lat;
  if (hasNumber(obj?.latitude)) return obj.latitude;
  return null;
}

function getLon(obj) {
  if (hasNumber(obj?.lon)) return obj.lon;
  if (hasNumber(obj?.longitude)) return obj.longitude;
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
      const latlng = [data.gps_lat, data.gps_lon];
      posMarker.setLatLng(latlng);
      posMarker.setOpacity(1);
    }

    if (mapAvailable && data.active && hasNumber(data.dest_lat) && hasNumber(data.dest_lon)) {
      const destLatLng = [data.dest_lat, data.dest_lon];
      if (!destMarker) {
        destMarker = L.marker(destLatLng, {
          icon: L.divIcon({
            className: '',
            html: '<div style="color:#e94560; margin-top:-14px; drop-shadow: 0 4px 6px rgba(0,0,0,0.5);"><svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3" fill="white"></circle></svg></div>',
            iconAnchor: [14, 28],
          }),
        }).addTo(map);
      } else {
        destMarker.setLatLng(destLatLng);
      }
    } else if (mapAvailable && destMarker) {
      destMarker.remove();
      destMarker = null;
    }
  } catch (e) {
    // silently ignore network errors during polling
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
      alert('Failed to set destination');
      return;
    }
    await pollStatus();
    if (mapAvailable && map) {
      map.setView([lat, lon], 14);
    }
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function cancelNav() {
  await fetch('/api/destination', { method: 'DELETE' });
  document.getElementById('search-input').value = '';
  if (mapAvailable && routeLayer) { routeLayer.remove(); routeLayer = null; }
  if (mapAvailable && destMarker) { destMarker.remove(); destMarker = null; }
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

document.addEventListener('DOMContentLoaded', () => {
  initMap();
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
