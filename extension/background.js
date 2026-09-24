// Single owner of the Chrome-profile account id. Popup and database pages ask for
// it; they must not mint their own, or two IDs race into the Control center.
const API = 'http://127.0.0.1:8765';
const ALARM = 'him-heartbeat';

let accountIdPromise = null;

function createAccountId() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9]/g, '');
}

function ensureAccountId() {
  if (accountIdPromise) return accountIdPromise;
  accountIdPromise = (async () => {
    const stored = await chrome.storage.local.get('accountId');
    if (stored.accountId) return stored.accountId;
    const id = createAccountId();
    await chrome.storage.local.set({accountId: id});
    // Another context cannot mint IDs anymore; re-read in case of an older leftover write.
    const again = await chrome.storage.local.get('accountId');
    return again.accountId || id;
  })();
  accountIdPromise.finally(() => { accountIdPromise = null; });
  return accountIdPromise;
}

function setUninstallHook(accountId) {
  // Chrome opens this URL when the extension is uninstalled, so the Control center
  // can mark the profile offline immediately instead of waiting for a heartbeat timeout.
  const url = `${API}/api/extension/uninstalled?account_id=${encodeURIComponent(accountId)}`;
  if (url.length > 255) return;
  try {
    chrome.runtime.setUninstallURL(url, () => {
      void chrome.runtime.lastError;
    });
  } catch (error) {
    // Older Chrome builds may reject; heartbeat timeout remains as fallback.
  }
}

async function heartbeat() {
  try {
    const id = await ensureAccountId();
    setUninstallHook(id);
    await fetch(`${API}/api/health`, {headers: {'X-Account-Id': id}});
  } catch (error) {
    // Backend may be down; try again on the next alarm.
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === 'getAccountId') {
    ensureAccountId().then((accountId) => {
      setUninstallHook(accountId);
      sendResponse({accountId});
    }).catch(() => sendResponse({accountId: ''}));
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, {periodInMinutes: 0.5});
  heartbeat();
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM, {periodInMinutes: 0.5});
  heartbeat();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) heartbeat();
});
chrome.alarms.create(ALARM, {periodInMinutes: 0.5});
heartbeat();
