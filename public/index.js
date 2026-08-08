let tries = 0;
let BASE = '';
let selectedFile = null;
let latestUploadedFile = null;
let pendingUpload = false;
let expiryTimer = null;
let toastHideTimer = null;
let nfcToastHideTimer = null;
let pageTransitionTimer = null;
let currentPageId = null;
let pendingPageId = null;
let sidebarDragState = null;
let sidebarSettleCleanup = null;
let sidebarMotionIdleTimer = null;
let activeNfcAbortController = null;
let sharePopup = null;
let qrScanStream = null;
let qrScanFrameHandle = null;
let qrScanSessionId = 0;
let qrScanLastInvalidValue = '';
let qrScanLastInvalidAt = 0;
const MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024;
const SLUG_PATTERN = /^[A-Za-z0-9_-]+$/;

function validateSlugInput() {
  const input = document.getElementById('uploadSlugInput');
  const errorEl = document.getElementById('slugError');
  if (!input || !errorEl) return true;
  const val = input.value.trim();
  if (!val) {
    input.classList.remove('error');
    errorEl.textContent = '';
    return true;
  }
  if (val.length < 2) {
    input.classList.add('error');
    errorEl.textContent = 'Slug must be at least 2 characters';
    return false;
  }
  if (!SLUG_PATTERN.test(val)) {
    input.classList.add('error');
    errorEl.textContent = 'Only letters, numbers, hyphens and underscores allowed';
    return false;
  }
  input.classList.remove('error');
  errorEl.textContent = '';
  return true;
}

const PAGE_IDS = ['main', 'scan', 'api', 'changelog', 'tos', 'privacy'];
const PAGE_TRANSITION_MS = 220;
const reduceMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
const SIDEBAR_SETTLE_VELOCITY_THRESHOLD = 0.55;
const mobileViewportQuery = window.matchMedia('(max-width: 600px)');
const DEBUG_UI_ENABLED = window.GHOSTDROP_DEBUG_UI === true;
const NFC_FUNCTIONALITY_HIDDEN = true; // set to true to hide NFC feature i would revisit it later turst me
const GHOSTDROP_PUBLIC_FILE_BASE = 'https://link.ghostdrop.qzz.io';
const GHOSTDROP_APP_FILE_BASE = window.GHOSTDROP_APP_FILE_BASE || GHOSTDROP_PUBLIC_FILE_BASE;
const GHOSTDROP_QR_HOST = 'link.ghostdrop.qzz.io';
const isApp = navigator.userAgent.toLowerCase().includes('median');
const nfcTextDecoder = new TextDecoder();
const nfcTextEncoder = new TextEncoder();
  
function logFrontend(eventName, detail) {
  if (!DEBUG_UI_ENABLED) {
    return;
  }

  if (detail === undefined) {
    console.info('[frontend]', eventName);
    return;
  }

  console.info('[frontend]', eventName, detail);
}


async function getApiUrl() {
  const apiUrlProvider = "https://raw.githubusercontent.com/SaaranshDx/GhostDrop/main/serverurl";
  logFrontend('api-url:request', { source: apiUrlProvider });

  const res = await fetch(apiUrlProvider);

  if (!res.ok) {
    logFrontend('api-url:failed', { status: res.status, statusText: res.statusText });
    throw new Error("failed to fetch");
  }

  const url = await res.text();
  logFrontend('api-url:ready', { url: url.trim() });
  return url.trim();
}

function isNfcSupported() {
  if (NFC_FUNCTIONALITY_HIDDEN) {
    return false;
  }

  return typeof window.NDEFReader === 'function';
}

function canUseNfc() {
  return isMobileDevice() && isNfcSupported();
}

function buildFileShareUrl(fileId) {
  return GHOSTDROP_PUBLIC_FILE_BASE.replace(/\/$/, '') + '/' + encodeURIComponent(fileId);
}

function buildAppFileUrl(fileId, fallbackUrl = '') {
  if (!fileId) {
    return fallbackUrl;
  }

  return GHOSTDROP_APP_FILE_BASE.replace(/\/$/, '') + '/' + encodeURIComponent(fileId);
}

function extractFileIdFromUrl(url) {
  try {
    const parsedUrl = new URL(url);
    const pathParts = parsedUrl.pathname.split('/').filter(Boolean);
    return pathParts[pathParts.length - 1] || null;
  } catch {
    return null;
  }
}

function hideNfcToast() {
  const toastNode = document.getElementById('nfctoast');
  if (!toastNode) {
    return;
  }

  toastNode.classList.remove('show', 'is-error', 'is-persistent');
  if (nfcToastHideTimer) {
    clearTimeout(nfcToastHideTimer);
    nfcToastHideTimer = null;
  }
}

function showNfcToast(message, options = {}) {
  const { error = false, persistent = false } = options;
  const toastNode = document.getElementById('nfctoast');
  if (!toastNode) {
    return;
  }

  logFrontend('nfctoast', { message, error, persistent });

  if (nfcToastHideTimer) {
    clearTimeout(nfcToastHideTimer);
    nfcToastHideTimer = null;
  }

  toastNode.textContent = message;
  toastNode.classList.toggle('is-error', error);
  toastNode.classList.toggle('is-persistent', persistent);
  toastNode.classList.remove('show');
  void toastNode.offsetWidth;
  toastNode.classList.add('show');

  if (!persistent) {
    nfcToastHideTimer = window.setTimeout(() => {
      toastNode.classList.remove('show');
      nfcToastHideTimer = null;
    }, 3200);
  }
}

function stopNfcSession() {
  if (!activeNfcAbortController) {
    return;
  }

  activeNfcAbortController.abort();
  activeNfcAbortController = null;
  logFrontend('nfc:session-stopped');
}

function updateNfcControls() {
  const shareBtn = document.getElementById('nfcShareBtn');
  const receiveBtn = document.getElementById('nfcReceiveBtn');
  const toastNode = document.getElementById('nfctoast');
  const nfcAvailable = canUseNfc();

  if (shareBtn) {
    shareBtn.hidden = !nfcAvailable;
    shareBtn.disabled = !nfcAvailable || !latestUploadedFile;
  }

  if (receiveBtn) {
    receiveBtn.hidden = !nfcAvailable;
    receiveBtn.disabled = !nfcAvailable;
  }

  if (toastNode) {
    toastNode.hidden = NFC_FUNCTIONALITY_HIDDEN;
  }

  if (NFC_FUNCTIONALITY_HIDDEN) {
    hideNfcToast();
    stopNfcSession();
  }
}

function parseNfcJsonText(text) {
  try {
    const payload = JSON.parse(text);
    if (payload && typeof payload === 'object') {
      return payload;
    }
  } catch {}

  return null;
}

function parseNfcRecord(record) {
  const rawText = record.data ? nfcTextDecoder.decode(record.data) : '';

  if (record.recordType === 'url') {
    const url = rawText.trim();
    return url ? { url } : null;
  }

  if (record.recordType === 'text' || record.recordType === 'mime' || record.mediaType === 'application/json') {
    const parsedPayload = parseNfcJsonText(rawText.trim());
    if (parsedPayload) {
      return parsedPayload;
    }

    if (/^https?:\/\//i.test(rawText.trim())) {
      return { url: rawText.trim() };
    }
  }

  return null;
}

function extractGhostDropPayload(message) {
  for (const record of message.records) {
    const payload = parseNfcRecord(record);
    if (!payload) {
      continue;
    }

    if (payload.type === 'ghostdrop' || payload.file_id || payload.url) {
      return payload;
    }
  }

  return null;
}

function redirectToNfcPayload(payload) {
  const fileId = payload.file_id || extractFileIdFromUrl(payload.url || '');
  const publicUrl = payload.url || (fileId ? buildFileShareUrl(fileId) : '');
  const targetUrl = fileId ? buildAppFileUrl(fileId, publicUrl) : publicUrl;

  if (!targetUrl) {
    throw new Error('GhostDrop NFC payload did not contain a usable file link');
  }

  logFrontend('nfc:redirect', {
    fileId,
    targetUrl,
    isApp,
  });

  window.location.assign(targetUrl);
}

function getScanElements() {
  return {
    page: document.getElementById('page-scan'),
    video: document.getElementById('scanVideo'),
    status: document.getElementById('scanStatus'),
    startBtn: document.getElementById('scanStartBtn'),
    stopBtn: document.getElementById('scanStopBtn'),
  };
}

function setScanStatus(message, options = {}) {
  const { error = false, live = false } = options;
  const { status } = getScanElements();
  if (!status) {
    return;
  }

  status.textContent = message;
  status.classList.toggle('is-error', error);
  status.classList.toggle('is-live', live);
}

function updateScanButtons(isScanning) {
  const { startBtn, stopBtn } = getScanElements();
  if (startBtn) {
    startBtn.disabled = isScanning;
  }

  if (stopBtn) {
    stopBtn.disabled = !isScanning;
  }
}

function normalizeGhostDropScanUrl(rawValue) {
  const rawText = rawValue?.trim();
  if (!rawText) {
    return null;
  }

  const candidateUrl = /^[a-z][a-z0-9+.-]*:/i.test(rawText)
    ? rawText
    : 'https://' + rawText.replace(/^\/+/, '');

  try {
    const parsedUrl = new URL(candidateUrl);
    if (!/^https?:$/.test(parsedUrl.protocol)) {
      return null;
    }

    if (parsedUrl.hostname.toLowerCase() !== GHOSTDROP_QR_HOST) {
      return null;
    }

    const pathParts = parsedUrl.pathname.split('/').filter(Boolean);
    if (pathParts.length !== 1) {
      return null;
    }

    return buildFileShareUrl(pathParts[0]);
  } catch {
    return null;
  }
}

function stopQrScanner(options = {}) {
  const { resetStatus = true, clearPreview = true } = options;
  qrScanSessionId += 1;
  qrScanLastInvalidValue = '';
  qrScanLastInvalidAt = 0;

  if (qrScanFrameHandle) {
    window.cancelAnimationFrame(qrScanFrameHandle);
    qrScanFrameHandle = null;
  }

  if (qrScanStream) {
    qrScanStream.getTracks().forEach((track) => track.stop());
    qrScanStream = null;
  }

  const { video } = getScanElements();
  if (video) {
    try {
      video.pause();
    } catch {}

    if (clearPreview) {
      video.srcObject = null;
    }
  }

  updateScanButtons(false);

  if (resetStatus) {
    setScanStatus('Allow camera access or tap start camera to begin scanning.');
  }
}

async function scanQrFrame(sessionId, detector) {
  if (sessionId !== qrScanSessionId) {
    return;
  }

  const { video } = getScanElements();
  if (!video || !qrScanStream) {
    return;
  }

  try {
    if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
      const detections = await detector.detect(video);
      if (sessionId !== qrScanSessionId) {
        return;
      }

      if (detections.length > 0) {
        const rawValue = detections[0].rawValue?.trim();
        if (rawValue) {
          const targetUrl = normalizeGhostDropScanUrl(rawValue);
          if (targetUrl) {
            logFrontend('scan:success', { rawValue, targetUrl });
            setScanStatus('GhostDrop file found. Opening it now…', { live: true });
            stopQrScanner({ resetStatus: false, clearPreview: false });
            window.setTimeout(() => window.location.assign(targetUrl), 180);
            return;
          }

          const now = Date.now();
          if (rawValue !== qrScanLastInvalidValue || now - qrScanLastInvalidAt > 1400) {
            qrScanLastInvalidValue = rawValue;
            qrScanLastInvalidAt = now;
            logFrontend('scan:invalid-qr', { rawValue });
            setScanStatus('invalid qr', { error: true });
          }
        }
      }
    }
  } catch (error) {
    console.error(error);
    logFrontend('scan:failed', { message: error.message });
    stopQrScanner({ resetStatus: false });
    setScanStatus('camera scan failed: ' + error.message, { error: true });
    return;
  }

  qrScanFrameHandle = window.requestAnimationFrame(() => {
    void scanQrFrame(sessionId, detector);
  });
}

async function startQrScanner() {
  const { page, video } = getScanElements();
  if (!page || !video) {
    return;
  }

  if (currentPageId !== 'scan' && pendingPageId !== 'scan' && !page.classList.contains('active')) {
    return;
  }

  if (!window.isSecureContext) {
    setScanStatus('camera access requires a secure context', { error: true });
    updateScanButtons(false);
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia) {
    setScanStatus('camera access is not supported on this device', { error: true });
    updateScanButtons(false);
    return;
  }

  if (typeof window.BarcodeDetector !== 'function') {
    setScanStatus('qr scanning is not supported on this device', { error: true });
    updateScanButtons(false);
    return;
  }

  try {
    const supportedFormats = await window.BarcodeDetector.getSupportedFormats?.();
    if (Array.isArray(supportedFormats) && !supportedFormats.includes('qr_code')) {
      setScanStatus('qr scanning is not supported on this device', { error: true });
      updateScanButtons(false);
      return;
    }
  } catch {}

  stopQrScanner({ resetStatus: false });
  const sessionId = qrScanSessionId + 1;
  qrScanSessionId = sessionId;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
      },
    });

    if (sessionId !== qrScanSessionId) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }

    qrScanStream = stream;
    video.srcObject = stream;
    await video.play().catch(() => {});
    updateScanButtons(true);
    setScanStatus('point the camera at a GhostDrop QR code', { live: true });
    logFrontend('scan:start', { sessionId });

    const detector = new window.BarcodeDetector({ formats: ['qr_code'] });
    qrScanFrameHandle = window.requestAnimationFrame(() => {
      void scanQrFrame(sessionId, detector);
    });
  } catch (error) {
    console.error(error);
    logFrontend('scan:start-failed', { message: error.message });
    stopQrScanner({ resetStatus: false });
    setScanStatus('camera access failed: ' + error.message, { error: true });
  }
}

function handlePageActivation(nextPageId) {
  if (nextPageId === 'scan') {
    void startQrScanner();
    return;
  }

  stopQrScanner();
}

async function shareLatestFileOverNfc() {
  if (NFC_FUNCTIONALITY_HIDDEN) {
    return;
  }

  if (!latestUploadedFile) {
    toast('upload a file first', true);
    return;
  }

  if (!canUseNfc()) {
    toast('nfc is not available on this device', true);
    return;
  }

  stopNfcSession();
  showNfcToast('Hold an NFC tag near this device to write the GhostDrop link', { persistent: true });

  try {
    const ndef = new NDEFReader();
    const payload = {
      v: 1,
      type: 'ghostdrop',
      file_id: latestUploadedFile.id,
      filename: latestUploadedFile.originalName,
      expires_in_hours: latestUploadedFile.expiresInHours,
      url: latestUploadedFile.url,
    };

    logFrontend('nfc:write-start', payload);
    await ndef.write({
      records: [{
        recordType: 'mime',
        mediaType: 'application/json',
        data: nfcTextEncoder.encode(JSON.stringify(payload)),
      }],
    });

    navigator.vibrate?.([90, 45, 90]);
    showNfcToast('GhostDrop link written. Tap the tag with the receiving device.', { persistent: false });
    logFrontend('nfc:write-success', { id: latestUploadedFile.id });
  } catch (error) {
    console.error(error);
    showNfcToast('NFC share failed: ' + error.message, { error: true });
    logFrontend('nfc:write-failed', { message: error.message });
  }
}

async function startNfcReceive() {
  if (NFC_FUNCTIONALITY_HIDDEN) {
    return;
  }

  if (!canUseNfc()) {
    toast('nfc is not available on this device', true);
    return;
  }

  stopNfcSession();
  activeNfcAbortController = new AbortController();

  try {
    const ndef = new NDEFReader();
    showNfcToast('Bring a GhostDrop NFC tag near this device to open the file', { persistent: true });
    logFrontend('nfc:scan-start');

    ndef.addEventListener('readingerror', () => {
      showNfcToast('NFC tag detected, but the payload could not be read', { error: true });
      logFrontend('nfc:scan-read-error');
    }, { signal: activeNfcAbortController.signal });

    ndef.addEventListener('reading', (event) => {
      try {
        const payload = extractGhostDropPayload(event.message);
        if (!payload) {
          throw new Error('no GhostDrop payload found on the NFC tag');
        }

        showNfcToast('GhostDrop file found. Opening it now…');
        navigator.vibrate?.([75, 40, 75]);
        logFrontend('nfc:scan-success', payload);
        stopNfcSession();
        window.setTimeout(() => redirectToNfcPayload(payload), 240);
      } catch (error) {
        console.error(error);
        showNfcToast('NFC receive failed: ' + error.message, { error: true });
        logFrontend('nfc:scan-parse-failed', { message: error.message });
      }
    }, { signal: activeNfcAbortController.signal });

    await ndef.scan({ signal: activeNfcAbortController.signal });
  } catch (error) {
    activeNfcAbortController = null;
    console.error(error);
    showNfcToast('NFC receive failed: ' + error.message, { error: true });
    logFrontend('nfc:scan-failed', { message: error.message });
  }
}

function setSelectedFile(file) {
  const pill = document.getElementById('filePill');
  const uploadBtn = document.getElementById('uploadBtn');

  if (!pill || !uploadBtn) {
    return;
  }

  if (!file) {
    selectedFile = null;
    pill.style.display = 'none';
    pill.textContent = '';
    uploadBtn.disabled = true;
    logFrontend('file:cleared');
    return;
  }

  if (file.size > MAX_FILE_SIZE_BYTES) {
    selectedFile = null;
    pill.style.display = 'none';
    pill.textContent = '';
    uploadBtn.disabled = true;
    toast('file too large (max 100MB)', true);
    logFrontend('file:rejected', { name: file.name, size: file.size, reason: 'too-large' });
    return;
  }

  selectedFile = file;

  pill.style.display = 'block';
  pill.textContent = file.name + '  ·  ' + fmtBytes(file.size);
  uploadBtn.disabled = false;
  logFrontend('file:selected', { name: file.name, size: file.size, type: file.type || 'unknown' });
}

function normalizePageId(id) {
  if (id === 'client') {
    return 'scan';
  }

  return PAGE_IDS.includes(id) ? id : 'main';
}

function getPageDirection(fromId, toId) {
  if (!PAGE_IDS.includes(fromId) || !PAGE_IDS.includes(toId)) {
    return 'page-forward';
  }

  return PAGE_IDS.indexOf(toId) >= PAGE_IDS.indexOf(fromId) ? 'page-forward' : 'page-backward';
}

function setActiveSidebarLink(id, animate = true) {
  document.querySelectorAll('.sidebar a').forEach((link) => link.classList.remove('active'));
  const link = document.getElementById('snav-' + id);
  if (link) {
    link.classList.add('active');
  }
  moveSidebarIndicator(link, animate && !isMobileNavLayout());
}

function moveSidebarIndicator(link, animate = true) {
  const indicator = document.getElementById('sidebarIndicator');
  const sidebar = document.querySelector('.sidebar');
  if (!indicator || !sidebar || !link) {
    return;
  }

  const inset = isMobileNavLayout() ? 2 : 0;
  indicator.classList.toggle('no-animate', !animate);
  indicator.style.width = Math.max(0, link.offsetWidth - (inset * 2)) + 'px';
  indicator.style.height = Math.max(0, link.offsetHeight - (inset * 2)) + 'px';
  indicator.style.transform = 'translate3d(' + (link.offsetLeft + inset) + 'px, ' + (link.offsetTop + inset) + 'px, 0)';
  indicator.style.opacity = '1';
}

function clampSidebarPosition(left, top, width, height) {
  const margin = 12;
  return {
    left: Math.min(Math.max(left, margin), Math.max(margin, window.innerWidth - width - margin)),
    top: Math.min(Math.max(top, margin), Math.max(margin, window.innerHeight - height - margin)),
  };
}

function setSidebarPosition(left, top) {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) {
    return;
  }

  const bounds = clampSidebarPosition(left, top, sidebar.offsetWidth, sidebar.offsetHeight);
  sidebar.style.left = bounds.left + 'px';
  sidebar.style.top = bounds.top + 'px';
  sidebar.style.transform = 'none';
  sidebar.dataset.dragged = 'true';
}

function triggerSidebarSettle(velocityX) {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar || reduceMotionQuery.matches) {
    return;
  }

  const speed = Math.abs(velocityX);
  if (speed < SIDEBAR_SETTLE_VELOCITY_THRESHOLD) {
    return;
  }

  if (sidebarSettleCleanup) {
    sidebar.removeEventListener('animationend', sidebarSettleCleanup);
    sidebarSettleCleanup = null;
  }

  const direction = velocityX > 0 ? 1 : -1;
  const settleAngle = Math.min(8, Math.max(3.4, speed * 5.5));

  sidebar.classList.remove('is-settling');
  sidebar.style.setProperty('--sidebar-settle-angle', settleAngle + 'deg');
  sidebar.style.setProperty('--sidebar-settle-direction', String(direction));
  void sidebar.offsetWidth;
  sidebar.classList.add('is-settling');

  sidebarSettleCleanup = () => {
    sidebar.classList.remove('is-settling');
    sidebar.style.removeProperty('--sidebar-settle-angle');
    sidebar.style.removeProperty('--sidebar-settle-direction');
    sidebar.removeEventListener('animationend', sidebarSettleCleanup);
    sidebarSettleCleanup = null;
  };

  sidebar.addEventListener('animationend', sidebarSettleCleanup);
}

function setSidebarMotionActive(active) {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) {
    return;
  }

  sidebar.classList.toggle('is-drag-moving', active);
}

function setSidebarIdleActive(active) {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) {
    return;
  }

  sidebar.classList.toggle('is-drag-idle', active);
}

function setSidebarDragDirection(direction) {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) {
    return;
  }

  sidebar.dataset.dragDirection = direction > 0 ? 'right' : 'left';
}

function isMobileDevice() {
  if (typeof navigator.userAgentData?.mobile === 'boolean') {
    return navigator.userAgentData.mobile;
  }

  const ua = navigator.userAgent || '';
  if (/Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(ua)) {
    return true;
  }

  return /Macintosh/i.test(ua)
    && navigator.maxTouchPoints > 1
    && Math.max(window.screen.width, window.screen.height) <= 1366;
}

function isMobileNavLayout() {
  return isMobileDevice() && mobileViewportQuery.matches;
}

function resetSidebarForMobile() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) {
    return;
  }

  sidebarDragState = null;
  if (sidebarMotionIdleTimer) {
    clearTimeout(sidebarMotionIdleTimer);
    sidebarMotionIdleTimer = null;
  }
  if (sidebarSettleCleanup) {
    sidebar.removeEventListener('animationend', sidebarSettleCleanup);
    sidebarSettleCleanup = null;
  }

  sidebar.classList.remove('is-dragging', 'is-drag-moving', 'is-drag-idle', 'is-settling');
  sidebar.style.removeProperty('left');
  sidebar.style.removeProperty('top');
  sidebar.style.removeProperty('transform');
  sidebar.style.removeProperty('--sidebar-settle-angle');
  sidebar.style.removeProperty('--sidebar-settle-direction');
  delete sidebar.dataset.dragged;
}

function setupSidebarDrag() {
  const sidebar = document.querySelector('.sidebar');
  const handle = document.getElementById('sidebarDragHandle');
  if (!sidebar || !handle) {
    return;
  }

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || isMobileNavLayout()) {
      return;
    }

    const rect = sidebar.getBoundingClientRect();
    sidebarDragState = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
      lastX: event.clientX,
      lastY: event.clientY,
      lastTime: event.timeStamp,
      velocityX: 0,
      directionX: 1,
    };

    if (sidebarSettleCleanup) {
      sidebar.removeEventListener('animationend', sidebarSettleCleanup);
      sidebarSettleCleanup = null;
    }

    sidebar.classList.remove('is-settling');
    sidebar.style.removeProperty('--sidebar-settle-angle');
    sidebar.style.removeProperty('--sidebar-settle-direction');
    sidebar.classList.add('is-dragging');
    setSidebarMotionActive(false);
    setSidebarIdleActive(false);
    setSidebarDragDirection(1);
    setSidebarPosition(rect.left, rect.top);
    logFrontend('sidebar:drag-start', { left: rect.left, top: rect.top });
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });

  handle.addEventListener('pointermove', (event) => {
    if (!sidebarDragState || event.pointerId !== sidebarDragState.pointerId || isMobileNavLayout()) {
      return;
    }

    const deltaX = event.clientX - sidebarDragState.lastX;
    const deltaY = event.clientY - sidebarDragState.lastY;
    const deltaTime = Math.max(1, event.timeStamp - sidebarDragState.lastTime);
    const nextVelocityX = deltaX / deltaTime;
    sidebarDragState.velocityX = (sidebarDragState.velocityX * 0.35) + (nextVelocityX * 0.65);
    sidebarDragState.lastX = event.clientX;
    sidebarDragState.lastY = event.clientY;
    sidebarDragState.lastTime = event.timeStamp;

    if (Math.abs(deltaX) > 0.2 || Math.abs(deltaY) > 0.2) {
      if (Math.abs(deltaX) > 0.2) {
        sidebarDragState.directionX = deltaX > 0 ? 1 : -1;
        setSidebarDragDirection(sidebarDragState.directionX);
      }

      setSidebarMotionActive(true);
      setSidebarIdleActive(false);
      if (sidebarMotionIdleTimer) {
        clearTimeout(sidebarMotionIdleTimer);
      }
      sidebarMotionIdleTimer = window.setTimeout(() => {
        setSidebarMotionActive(false);
        setSidebarIdleActive(true);
        sidebarMotionIdleTimer = null;
      }, 90);
    }

    setSidebarPosition(event.clientX - sidebarDragState.offsetX, event.clientY - sidebarDragState.offsetY);
  });

  function endSidebarDrag(event) {
    if (!sidebarDragState || event.pointerId !== sidebarDragState.pointerId) {
      return;
    }

    const releaseVelocityX = sidebarDragState.velocityX;
    sidebarDragState = null;
    if (sidebarMotionIdleTimer) {
      clearTimeout(sidebarMotionIdleTimer);
      sidebarMotionIdleTimer = null;
    }
    setSidebarMotionActive(false);
    setSidebarIdleActive(false);
    sidebar.classList.remove('is-dragging');
    triggerSidebarSettle(releaseVelocityX);
    logFrontend('sidebar:drag-end', { velocityX: releaseVelocityX });
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  handle.addEventListener('pointerup', endSidebarDrag);
  handle.addEventListener('pointercancel', endSidebarDrag);
}

function clearPageTransitionClasses() {
  document.querySelectorAll('.page').forEach((page) => {
    page.classList.remove('page-enter', 'page-leave', 'page-forward', 'page-backward');
  });
}

function activatePage(id, direction, animate = true) {
  const nextPage = document.getElementById('page-' + id);
  if (!nextPage) {
    return;
  }

  document.querySelectorAll('.page').forEach((page) => {
    page.classList.remove('active', 'page-enter', 'page-leave', 'page-forward', 'page-backward');
  });

  nextPage.classList.add('active', direction);

  if (animate) {
    void nextPage.offsetWidth;
    nextPage.classList.add('page-enter');
  }

  currentPageId = id;
  pendingPageId = null;
  setActiveSidebarLink(id, animate);
  window.scrollTo(0, 0);
  handlePageActivation(id);
  logFrontend('page:activated', { id, direction, animate });
}

function getPageIdFromHash() {
  return normalizePageId(window.location.hash.replace(/^#/, ''));
}

function showPage(id, options = {}) {
  const { updateHash = true, immediate = false } = options;
  const nextId = normalizePageId(id);
  const nextPage = document.getElementById('page-' + nextId);
  const previousPageId = currentPageId;
  if (!nextPage) {
    return;
  }

  if (pageTransitionTimer) {
    clearTimeout(pageTransitionTimer);
    pageTransitionTimer = null;
  }

  clearPageTransitionClasses();

  const activePage = currentPageId ? document.getElementById('page-' + currentPageId) : document.querySelector('.page.active');
  if (currentPageId === nextId && activePage?.classList.contains('active')) {
    pendingPageId = null;
    setActiveSidebarLink(nextId, false);
    if (updateHash && window.location.hash !== '#' + nextId) {
      window.location.hash = nextId;
    }
    logFrontend('page:noop', { id: nextId });
    return;
  }

  const shouldAnimate = !immediate
    && !reduceMotionQuery.matches
    && currentPageId
    && currentPageId !== nextId
    && activePage
    && activePage.classList.contains('active');
  const direction = getPageDirection(currentPageId, nextId);

  pendingPageId = nextId;

  if (shouldAnimate) {
    activePage.classList.add('page-leave', direction);
    setActiveSidebarLink(nextId);
    pageTransitionTimer = window.setTimeout(() => {
      activatePage(nextId, direction, true);
      pageTransitionTimer = null;
    }, PAGE_TRANSITION_MS);
  } else {
    activatePage(nextId, direction, !immediate && !reduceMotionQuery.matches);
  }

  if (updateHash && window.location.hash !== '#' + nextId) {
    window.location.hash = nextId;
  }

  logFrontend('page:transition', {
    from: previousPageId,
    to: nextId,
    immediate,
    animate: shouldAnimate,
    updateHash,
  });
}

function onFileSelect() {
  const f = document.getElementById('fileInput').files[0];
  logFrontend('file:input-change', { hasFile: Boolean(f) });
  setSelectedFile(f);
}

function fmtBytes(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

function fmtTime(s) {
  if (s <= 0) return 'expired';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + sec + 's';
  return sec + 's';
}

async function uploadFile(event) {
  event?.preventDefault();
  if (!selectedFile) {
    logFrontend('upload:skipped', { reason: 'no-file' });
    return;
  }
  if (selectedFile.size > MAX_FILE_SIZE_BYTES) {
    const oversizedFile = selectedFile;
    toast('file too large (max 100MB)', true);
    setSelectedFile(null);
    logFrontend('upload:blocked', { reason: 'too-large', size: oversizedFile.size, name: oversizedFile.name });
    return;
  }
  if (!BASE) {
    toast('server url not ready yet', true);
    logFrontend('upload:blocked', { reason: 'base-url-missing' });
    return;
  }
  
  // Show password popup before uploading
  pendingUpload = true;
  showPasswordPopup();
}

async function proceedWithUpload(password = '') {
  if (!selectedFile || !BASE) {
    return;
  }
  
  const btn = document.getElementById('uploadBtn');
  if (!btn) {
    return;
  }
  const slugInput = document.getElementById('uploadSlugInput');
  const slug = slugInput?.value ?? '';
  if (slug && !validateSlugInput()) {
    btn.disabled = false;
    btn.textContent = 'upload';
    toast('fix the slug before uploading', true);
    return;
  }
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>uploading…';
  const fd = new FormData();
  fd.append('file', selectedFile);
  if (slug) {
    fd.append('slug', slug);
  }
  if (password) {
    fd.append('password', password);
  }
  logFrontend('upload:start', {
    name: selectedFile.name,
    size: selectedFile.size,
    hasSlug: Boolean(slug),
    hasPassword: Boolean(password),
  });
  try {
    const res = await fetch(BASE + '/upload/', {
      method: 'POST',
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.detail || data.error || 'upload failed';
      if (res.status === 409) {
        toast('slug "' + slug + '" is already in use', true);
        if (slugInput) slugInput.classList.add('error');
        document.getElementById('slugError').textContent = 'This slug is already taken';
      } else if (res.status === 400 && slug && msg.toLowerCase().includes('slug')) {
        toast(msg, true);
        if (slugInput) slugInput.classList.add('error');
        document.getElementById('slugError').textContent = msg;
      } else {
        toast(msg, true);
      }
      logFrontend('upload:server-rejected', { status: res.status, message: msg });
      return;
    }
    const url = 'https://link.ghostdrop.qzz.io' + '/' + data.id;
    const urlNode = document.getElementById('res-url');
    urlNode.textContent = url;
    urlNode.href = url;
    latestUploadedFile = {
      id: data.id,
      originalName: data.original_name,
      expiresInHours: data.expires_in_hours,
      url,
    };
    updateShareButtonVisibility();
    updateNfcControls();
    document.getElementById('resultCard').style.display = 'block';
    document.getElementById('fileInput').value = '';
    document.getElementById('filePill').style.display = 'none';
    document.getElementById('filePill').textContent = '';
    if (slugInput) {
      slugInput.value = '';
      slugInput.classList.remove('error');
    }
    const slugErrorEl = document.getElementById('slugError');
    if (slugErrorEl) slugErrorEl.textContent = '';
    selectedFile = null;
    shareFile(url);
    startExpiry(6 * 3600);
    navigator.clipboard?.writeText(url).catch(() => {});
    toast('uploaded — link copied');
    logFrontend('upload:success', {
      id: data.id,
      status: res.status,
      originalName: data.original_name,
      expiresInHours: data.expires_in_hours,
    });

  } catch(e) {
    toast('error: ' + e.message, true);
    logFrontend('upload:error', { message: e.message });
  } finally {
    btn.disabled = !selectedFile;
    btn.textContent = 'upload';
    logFrontend('upload:complete', { pendingFile: Boolean(selectedFile) });
  }
}

async function reupload() {
  if (tries >= 3) {
    toast('upload failed after multiple attempts', true);
    tries = 0;
    logFrontend('upload:retry-aborted');
    return;
  }

  logFrontend('upload:retry', { tries });
  await uploadFile();

}

async function removeFile(id, password) {
  logFrontend('delete:start', { id, hasPassword: Boolean(password) });
  try {
    const res = await fetch(BASE + '/delete/' + id, {
      method: 'DELETE',
      headers: {
        'password': String(password)
      }
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Delete failed');
    }

    toast('File deleted');
    logFrontend('delete:success', { id });
  } catch (e) {
    toast('error: ' + e.message, true);
    logFrontend('delete:error', { id, message: e.message });
  }
}

function startExpiry(total) {
  if (expiryTimer) clearInterval(expiryTimer);
  let rem = total;
  const fill = document.getElementById('expiryFill');
  const lbl = document.getElementById('expiryTime');
  if (!fill || !lbl) {
    return;
  }
  fill.style.width = '100%';
  lbl.textContent = fmtTime(rem);
  logFrontend('expiry:start', { totalSeconds: total });
  expiryTimer = setInterval(() => {
    rem--;
    fill.style.width = Math.max(0, rem / total * 100) + '%';
    lbl.textContent = fmtTime(rem);
    if (rem <= 0) {
      clearInterval(expiryTimer);
      logFrontend('expiry:ended');
    }
  }, 1000);
}

function copyLink(event, btn) {
  event?.preventDefault();
  const text = document.getElementById('res-url').href;
  logFrontend('clipboard:copy-link', { text });
  navigator.clipboard?.writeText(text).then(() => {
    btn.textContent = 'copied'; setTimeout(() => btn.textContent = 'copy', 1400);
    logFrontend('clipboard:copy-link-success');
  });
}

function getGoNativeShare() {
  return window.gonative?.share?.sharePage || window.median?.share?.sharePage || null;
}

function isMobileShareEnvironment() {
  return isMobileDevice();
}

function canShareFile() {
  return true;
}

function updateShareButtonVisibility() {
  const shareBtn = document.getElementById('shareBtn');
  if (!shareBtn) {
    return;
  }

  if (!canShareFile()) {
    shareBtn.style.display = 'none';
    shareBtn.disabled = true;
    return;
  }

  shareBtn.style.display = '';
  shareBtn.disabled = !document.getElementById('res-url')?.href;
  logFrontend('share:button-state', {
    visible: true,
    disabled: shareBtn.disabled,
  });
}

function getShareQrImageUrl(url) {
  return 'https://api.qrserver.com/v1/create-qr-code/?size=320x320&margin=0&data=' + encodeURIComponent(url);
}

function hideSharePopup() {
  if (!sharePopup) {
    return;
  }

  sharePopup.classList.remove('is-visible');
  document.body.classList.remove('modal-open');
}

function ensureSharePopup() {
  if (sharePopup) {
    return sharePopup;
  }

  const style = document.createElement('style');
  style.textContent = `
    .share-popup {
      position: fixed;
      inset: 0;
      z-index: 1400;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
      background: rgba(8, 8, 8, 0.82);
      backdrop-filter: blur(14px);
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
      transition: opacity 0.22s ease, visibility 0.22s ease;
    }

    .share-popup.is-visible {
      opacity: 1;
      visibility: visible;
      pointer-events: auto;
    }

    .share-popup__card {
      width: min(100%, 420px);
      border: 1px solid var(--border2);
      border-radius: 18px;
      background:
        radial-gradient(circle at top right, rgba(200,255,110,0.14), transparent 32%),
        linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)),
        #0a0a0a;
      box-shadow: 0 28px 90px rgba(0, 0, 0, 0.45);
      overflow: hidden;
      opacity: 0;
      transform: translateY(26px) scale(0.96);
      transition:
        opacity 0.28s ease,
        transform 0.34s cubic-bezier(0.22, 1, 0.36, 1);
    }

    .share-popup.is-visible .share-popup__card {
      opacity: 1;
      transform: translateY(0) scale(1);
    }

    .share-popup__content {
      padding: 1.4rem;
      text-align: center;
    }

    .share-popup__eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      margin-bottom: 0.75rem;
      color: var(--green);
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 1.2px;
      text-transform: uppercase;
    }

    .share-popup__eyebrow::before {
      content: '';
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 12px rgba(200,255,110,0.45);
    }

    .share-popup__title {
      margin: 0 0 0.6rem;
      color: var(--text);
      font-family: var(--display);
      font-size: clamp(1.7rem, 5vw, 2.2rem);
      line-height: 0.96;
      letter-spacing: -0.04em;
    }

    .share-popup__lead {
      margin: 0 0 1.15rem;
      color: rgba(239,239,239,0.82);
      font-size: 14px;
      line-height: 1.7;
    }

    .share-popup__qr-shell {
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1rem;
      margin-bottom: 1rem;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: rgba(255,255,255,0.04);
    }

    .share-popup__qr {
      width: min(100%, 280px);
      aspect-ratio: 1;
      display: block;
      border-radius: 12px;
      background: #fff;
      object-fit: contain;
    }

    .share-popup__link {
      display: block;
      margin: 0 0 1rem;
      color: rgba(239,239,239,0.72);
      font-size: 12px;
      line-height: 1.5;
      word-break: break-all;
      text-decoration: none;
    }

    .share-popup__actions {
      display: grid;
      gap: 0.7rem;
    }

    .share-popup__button {
      width: 100%;
      padding: 0.95rem 1rem;
      border: 1px solid transparent;
      border-radius: 12px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease, color 0.18s ease, opacity 0.18s ease;
    }

    .share-popup__button:hover:not(:disabled) {
      transform: translateY(-1px);
    }

    .share-popup__button--primary {
      background: var(--green);
      color: #070707;
    }

    .share-popup__button--secondary {
      background: rgba(255,255,255,0.04);
      border-color: var(--border);
      color: var(--text);
    }

    .share-popup__button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    @media (prefers-reduced-motion: reduce) {
      .share-popup,
      .share-popup__card,
      .share-popup__button {
        transition: none;
      }
    }
  `;
  document.head.appendChild(style);

  const popup = document.createElement('div');
  popup.className = 'share-popup';
  popup.id = 'sharePopup';
  popup.setAttribute('role', 'dialog');
  popup.setAttribute('aria-modal', 'true');
  popup.setAttribute('aria-labelledby', 'sharePopupTitle');
  popup.innerHTML = `
    <div class="share-popup__card">
      <div class="share-popup__content">
        <span class="share-popup__eyebrow">Share file</span>
        <h2 class="share-popup__title" id="sharePopupTitle">Scan to open</h2>
        <p class="share-popup__lead">Scan this QR code on another device, or use the app share drawer below.</p>
        <div class="share-popup__qr-shell">
          <img class="share-popup__qr" id="sharePopupQr" alt="QR code for shared file link">
        </div>
        <a class="share-popup__link" id="sharePopupLink" target="_blank" rel="noopener"></a>
        <div class="share-popup__actions">
          <button type="button" class="share-popup__button share-popup__button--primary" id="sharePopupNativeBtn">share via other apps</button>
          <button type="button" class="share-popup__button share-popup__button--secondary" id="sharePopupCloseBtn">close</button>
        </div>
      </div>
    </div>
  `;

  popup.addEventListener('click', (event) => {
    if (event.target === popup) {
      hideSharePopup();
    }
  });

  popup.querySelector('#sharePopupCloseBtn')?.addEventListener('click', hideSharePopup);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && popup.classList.contains('is-visible')) {
      hideSharePopup();
    }
  });

  document.body.appendChild(popup);
  sharePopup = popup;
  return popup;
}

async function shareFileViaOtherApps(url) {
  if (!url) {
    console.info('share skipped: missing file url');
    return;
  }

  hideSharePopup();

  const goNativeShare = getGoNativeShare();
  if (goNativeShare) {
    try {
      logFrontend('share:native-start', { provider: 'gonative', url });
      goNativeShare({
        url,
        text: "Here’s your file",
      });
      logFrontend('share:native-success', { provider: 'gonative' });
    } catch (error) {
      console.info('GoNative share failed', error);
      logFrontend('share:native-failed', { provider: 'gonative', message: error.message });
    }
    return;
  }

  if (typeof navigator.share === 'function') {
    try {
      logFrontend('share:native-start', { provider: 'web-share', url });
      await navigator.share({
        title: 'GhostDrop file',
        text: "Here’s your file",
        url,
      });
      logFrontend('share:native-success', { provider: 'web-share' });
    } catch (error) {
      console.info('share cancelled or failed', error);
      logFrontend('share:native-failed', { provider: 'web-share', message: error.message });
    }
    return;
  }

  updateShareButtonVisibility();
  console.info('share unavailable: no native share provider');
}

function showSharePopup(url) {
  const popup = ensureSharePopup();
  const qrNode = popup.querySelector('#sharePopupQr');
  const linkNode = popup.querySelector('#sharePopupLink');
  const nativeShareBtn = popup.querySelector('#sharePopupNativeBtn');

  qrNode.src = getShareQrImageUrl(url);
  linkNode.href = url;
  linkNode.textContent = url;
  nativeShareBtn.disabled = !getGoNativeShare() && typeof navigator.share !== 'function';
  nativeShareBtn.onclick = () => shareFileViaOtherApps(url);

  popup.classList.remove('is-visible');
  void popup.offsetWidth;
  requestAnimationFrame(() => {
    popup.classList.add('is-visible');
  });
  document.body.classList.add('modal-open');
  logFrontend('share:popup-open', {
    url,
    nativeShareAvailable: !nativeShareBtn.disabled,
  });
}

async function shareFile(url) {
  if (!url) {
    console.info('share skipped: missing file url');
    return;
  }

  showSharePopup(url);
}

function showPasswordPopup() {
  const popup = document.getElementById('passwordPopup');
  if (popup) {
    popup.classList.add('is-visible');
    document.body.classList.add('modal-open');
    const input = document.getElementById('passwordPopupInput');
    if (input) {
      input.focus();
    }
  }
}

function hidePasswordPopup() {
  const popup = document.getElementById('passwordPopup');
  if (popup) {
    popup.classList.remove('is-visible');
    document.body.classList.remove('modal-open');
  }
  const input = document.getElementById('passwordPopupInput');
  if (input) {
    input.value = '';
  }
  pendingUpload = false;
}

function skipPasswordPopup() {
  const shouldProceedWithUpload = pendingUpload && selectedFile;
  hidePasswordPopup();
  
  // Proceed with upload without password
  if (shouldProceedWithUpload) {
    proceedWithUpload('');
  }
}

function submitPasswordPopup() {
  const input = document.getElementById('passwordPopupInput');
  const password = input?.value ?? '';
  logFrontend('password-popup:submit', { hasPassword: Boolean(password) });
  
  // Check if we were waiting for password input before uploading
  const shouldProceedWithUpload = pendingUpload && selectedFile;
  
  hidePasswordPopup();
  
  // Proceed with upload after hiding the popup
  if (shouldProceedWithUpload) {
    proceedWithUpload(password);
  }
}

function cc(btn) {
  const txt = btn.parentElement.innerText.replace(/^copy\n/, '').trim();
  navigator.clipboard?.writeText(txt).then(() => {
    btn.textContent = 'copied'; setTimeout(() => btn.textContent = 'copy', 1400);
  });
}

function toggleEp(head) {
  const body = head.nextElementSibling;
  const chev = head.querySelector('.chev');
  const open = !body.classList.contains('open');
  logFrontend('accordion:toggle', {
    path: head.querySelector('.ep-path')?.textContent?.trim() || 'unknown',
    open,
  });

  if (body._epTransitionCleanup) {
    body.removeEventListener('transitionend', body._epTransitionCleanup);
    body._epTransitionCleanup = null;
  }

  if (reduceMotionQuery.matches) {
    body.classList.toggle('open', open);
    body.style.height = open ? 'auto' : '0px';
  } else if (open) {
    body.style.height = '0px';
    body.classList.add('open');
    void body.offsetHeight;
    body.style.height = body.scrollHeight + 'px';

    body._epTransitionCleanup = (event) => {
      if (event.propertyName !== 'height') {
        return;
      }

      body.style.height = 'auto';
      body.removeEventListener('transitionend', body._epTransitionCleanup);
      body._epTransitionCleanup = null;
    };
    body.addEventListener('transitionend', body._epTransitionCleanup);
  } else {
    body.style.height = body.scrollHeight + 'px';
    void body.offsetHeight;
    body.classList.remove('open');
    body.style.height = '0px';

    body._epTransitionCleanup = (event) => {
      if (event.propertyName !== 'height') {
        return;
      }

      body.removeEventListener('transitionend', body._epTransitionCleanup);
      body._epTransitionCleanup = null;
    };
    body.addEventListener('transitionend', body._epTransitionCleanup);
  }

  head.classList.toggle('open', open);
  chev.classList.toggle('open', open);
}

function toast(msg, err = false) {
  const t = document.getElementById('toast');
  if (!t) {
    return;
  }
  logFrontend('toast', { message: msg, error: err });
  if (toastHideTimer) {
    clearTimeout(toastHideTimer);
    toastHideTimer = null;
  }

  t.textContent = msg;
  t.classList.toggle('is-error', err);
  t.classList.remove('show');
  void t.offsetWidth;
  t.classList.add('show');
  toastHideTimer = window.setTimeout(() => {
    t.classList.remove('show');
    toastHideTimer = null;
  }, 2800);
}

async function initializePage() {
  logFrontend('init:start');
  currentPageId = normalizePageId(document.querySelector('.page.active')?.id?.replace('page-', ''));
  showPage(getPageIdFromHash(), { updateHash: false, immediate: true });
  setupSidebarDrag();
  if (isMobileNavLayout()) {
    resetSidebarForMobile();
  }

  updateShareButtonVisibility();
  updateNfcControls();

  document.querySelectorAll('[data-copy-link="true"]').forEach((btn) => {
    btn.addEventListener('click', (event) => copyLink(event, btn));
  });

  const shareBtn = document.getElementById('shareBtn');
  if (shareBtn) {
    shareBtn.addEventListener('click', () => {
      const url = document.getElementById('res-url')?.href;
      shareFile(url);
    });
  }

  const nfcShareBtn = document.getElementById('nfcShareBtn');
  if (nfcShareBtn) {
    nfcShareBtn.addEventListener('click', shareLatestFileOverNfc);
  }

/*
  const nfcReceiveBtn = document.getElementById('nfcReceiveBtn');
  if (nfcReceiveBtn) {
    nfcReceiveBtn.addEventListener('click', startNfcReceive);
  }
*/
  const scanStartBtn = document.getElementById('scanStartBtn');
  if (scanStartBtn) {
    scanStartBtn.addEventListener('click', () => {
      void startQrScanner();
    });
  }

  const scanStopBtn = document.getElementById('scanStopBtn');
  if (scanStopBtn) {
    scanStopBtn.addEventListener('click', () => stopQrScanner());
  }

  const uploadBtn = document.getElementById('uploadBtn');
  if (uploadBtn) {
    uploadBtn.addEventListener('click', uploadFile);
  }

  const slugInput = document.getElementById('uploadSlugInput');
  if (slugInput) {
    slugInput.addEventListener('input', validateSlugInput);
  }

  // Password popup event listeners
  const passwordPopupSkip = document.getElementById('passwordPopupSkip');
  if (passwordPopupSkip) {
    passwordPopupSkip.addEventListener('click', skipPasswordPopup);
  }

  const passwordPopupSubmit = document.getElementById('passwordPopupSubmit');
  if (passwordPopupSubmit) {
    passwordPopupSubmit.addEventListener('click', submitPasswordPopup);
  }

  const passwordPopupInput = document.getElementById('passwordPopupInput');
  if (passwordPopupInput) {
    passwordPopupInput.addEventListener('keypress', (event) => {
      if (event.key === 'Enter') {
        submitPasswordPopup();
      }
    });
  }

  const dz = document.getElementById('dropZone');
  if (dz) {
    dz.addEventListener('dragover', (e) => {
      e.preventDefault();
      dz.classList.add('dragover');
    });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      const f = e.dataTransfer.files[0];
      logFrontend('dropzone:drop', { hasFile: Boolean(f) });
      setSelectedFile(f);
    });
  }

  try {
    BASE = await getApiUrl();
    logFrontend('init:base-ready', { base: BASE });
  } catch (error) {
    toast('failed to load server url', true);
    console.error(error);
    logFrontend('init:base-failed', { message: error.message });
  }

  logFrontend('init:complete');
}

initializePage();

window.addEventListener('hashchange', () => {
  const nextId = getPageIdFromHash();
  logFrontend('hashchange', { nextId, currentPageId, pendingPageId });
  if (nextId === currentPageId || nextId === pendingPageId) {
    return;
  }

  showPage(nextId, { updateHash: false });
});

window.addEventListener('resize', () => {
  logFrontend('resize', {
    width: window.innerWidth,
    height: window.innerHeight,
    mobileDevice: isMobileDevice(),
    mobileLayout: isMobileNavLayout(),
  });
  updateNfcControls();
  if (isMobileNavLayout()) {
    resetSidebarForMobile();
  }

  const activeId = pendingPageId || currentPageId;
  if (!activeId) {
    return;
  }

  const activeLink = document.getElementById('snav-' + activeId);
  moveSidebarIndicator(activeLink, false);

  const sidebar = document.querySelector('.sidebar');
  if (sidebar?.dataset.dragged === 'true') {
    setSidebarPosition(sidebar.offsetLeft, sidebar.offsetTop);
  }
});

window.addEventListener('pagehide', () => {
  stopQrScanner({ resetStatus: false });
});

function refreshNativeCapabilities() {
  updateShareButtonVisibility();
  updateNfcControls();
}

window.median_library_ready = refreshNativeCapabilities;
window.gonative_library_ready = refreshNativeCapabilities;

window.showPage = showPage;
window.showpage = showPage;
window.onFileSelect = onFileSelect;
window.toggleEp = toggleEp;
window.cc = cc;
window.removeFile = removeFile;
window.shareLatestFileOverNfc = shareLatestFileOverNfc;
window.startNfcReceive = startNfcReceive;
