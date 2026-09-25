const API = 'http://127.0.0.1:8765';
const $ = (id) => document.getElementById(id);
let currentPage = 1;
let refreshing = false;
let selectedConversation = null;
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));
// Every Chrome profile has its own extension storage, so each profile gets its own account id.
// The service worker is the only place that creates it (avoids a race that registered two profiles).
const accountReady = new Promise((resolve, reject) => {
  try {
    chrome.runtime.sendMessage({type: 'getAccountId'}, (response) => {
      if (chrome.runtime.lastError) {
        chrome.storage.local.get('accountId', (stored) => {
          if (stored && stored.accountId) resolve(stored.accountId);
          else reject(new Error(chrome.runtime.lastError.message));
        });
        return;
      }
      if (response && response.accountId) resolve(response.accountId);
      else reject(new Error('No account id from extension service worker'));
    });
  } catch (error) { reject(error); }
});
async function request(path, options = {}) {
  const accountId = await accountReady;
  const response = await fetch(`${API}${path}`, {...options, headers: {'Content-Type': 'application/json', 'X-Account-Id': accountId, ...(options.headers || {})}});
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
const settingFields = ['openrouter_api_key', 'supabase_url', 'supabase_key', 'github_token', 'github_owner', 'github_repo', 'github_ai_repo', 'min_message_delay_seconds', 'max_message_delay_seconds', 'daily_dm_limit'];
async function loadSettings() {
  const [values, account] = await Promise.all([request('/api/settings'), request('/api/account')]);
  $('account_label').value = account.label || '';
  $('account-id').textContent = `Account id: ${account.id}`;
  const secretFields = ['openrouter_api_key', 'supabase_key', 'github_token'];
  settingFields.forEach((field) => {
    if (secretFields.includes(field)) {
      $(field).value = '';
      $(field).placeholder = values[field] || '';
    } else {
      $(field).value = values[field] ?? '';
    }
  });
}
const TABS = ['dashboard', 'chats', 'settings'];
function showTab(tab) {
  TABS.forEach((name) => { $(`${name}-panel`).hidden = name !== tab; $(`${name}-tab`).classList.toggle('active', name === tab); });
  if (tab === 'chats') loadConversations().catch(() => { $('conversation-list').innerHTML = '<p class="chat-empty">Could not load conversations.</p>'; });
  if (tab === 'settings') loadSettings().catch(() => { $('settings-result').textContent = 'Could not load settings.'; });
}
function formatTime(value) {
  if (!value) return 'pending';
  return new Date(value).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
function candidatePage(item) {
  try { return JSON.parse(item.source_json || '{}').page; } catch (error) { return null; }
}
function renderActivity(items) {
  $('activity-count').textContent = items.length;
  $('activity-log').innerHTML = items.length ? items.map((item) => `<div class="log-row ${escapeHtml(item.status)}"><span class="log-dot"></span><div><div class="log-name">${escapeHtml(item.name)}</div><div class="log-detail">${escapeHtml(item.direction === 'inbound' ? 'Candidate reply received' : item.error ? 'Delivery completed with warning' : 'Outreach message')}</div></div><div><div class="log-status">${escapeHtml(item.status)}</div><div class="log-time">${formatTime(item.sent_at || item.send_after || item.created_at)}</div></div></div>`).join('') : '<p class="log-empty">No message activity yet.</p>';
}
function renderProgress(progress, delivery) {
  const total = Math.max(progress.total, 1);
  const percent = Math.min(100, Math.round((progress.sent / total) * 100));
  $('progress-sent').textContent = progress.sent;
  $('progress-queued').textContent = progress.queued;
  $('progress-failed').textContent = progress.failed;
  $('progress-bar').style.width = `${percent}%`;
  $('progress-state').textContent = delivery && delivery.status === 'sending' ? 'SENDING' : progress.queued ? 'SCHEDULED' : progress.sent ? 'COMPLETE' : 'IDLE';
  $('current-recipient').textContent = delivery && delivery.current_name ? delivery.current_name : (progress.next_recipient || 'No message waiting');
  $('next-delivery').textContent = progress.next_send_at ? formatTime(progress.next_send_at) : 'No scheduled message';
  const daily = progress.daily;
  $('daily-count').textContent = !daily ? '—' : daily.limit ? `${daily.sent_today} / ${daily.limit}${daily.reached ? ' · limit reached' : ''}` : `${daily.sent_today} (no limit)`;
  $('latest-result').textContent = progress.latest_recipient ? `${progress.latest_recipient}: ${progress.latest_status}${progress.latest_error ? ' · ledger warning retained' : ''}` : 'Waiting for delivery activity.';
}
function updateMetrics(allCandidates, pageCandidates, progress) {
  const sent = progress.sent;
  const queued = progress.queued;
  $('metric-profiles').textContent = allCandidates.length;
  $('metric-ready').textContent = queued;
  $('metric-sent').textContent = sent;
  $('metric-profiles-detail').textContent = allCandidates.length === 1 ? 'profile indexed' : 'profiles indexed';
  $('metric-ready-detail').textContent = queued ? `${pageCandidates.length} on current page` : 'queue is clear';
  $('metric-sent-detail').textContent = progress.total ? `${progress.total} tracked messages` : 'no deliveries yet';
  $('outreach-step').classList.toggle('active', sent > 0 || progress.total > 0);
  $('outreach-step-detail').textContent = queued ? `${queued} queued` : sent > 0 ? `${sent} delivered` : 'Awaiting launch';
}
function updateAutomation(automation) {
  const running = Boolean(automation && automation.running);
  $('launch').disabled = running;
  $('launch').hidden = running;
  $('stop').hidden = !running;
  if (running) $('status').textContent = `Automation ${automation.status} · page ${automation.page}`;
}
function renderConversations(items) {
  $('chat-count').textContent = items.length;
  const unread = items.reduce((total, item) => total + Number(item.unread_count || 0), 0);
  $('unread-badge').hidden = unread === 0;
  $('unread-badge').textContent = unread;
  $('conversation-list').innerHTML = items.length ? items.map((item) => `<button class="conversation-row ${selectedConversation === item.id ? 'active' : ''} ${item.unread_count ? 'unread' : ''}" data-conversation-id="${item.id}"><span><strong>${escapeHtml(item.name)}</strong><small>${item.message_count} message${item.message_count === 1 ? '' : 's'} · ${formatTime(item.last_reply_at || item.last_activity)}</small>${item.last_reply ? `<small class="reply-preview">“${escapeHtml(item.last_reply)}”</small>` : ''}</span><span><span class="tag">${escapeHtml(item.conversation_status)}</span>${item.unread_count ? `<b class="row-unread">${item.unread_count}</b>` : ''}</span></button>`).join('') : '<p class="chat-empty">No conversations yet.</p>';
  document.querySelectorAll('[data-conversation-id]').forEach((button) => { button.onclick = () => loadConversation(Number(button.dataset.conversationId)); });
}
function renderConversationDetail(data) {
  $('conversation-name').textContent = data.candidate.name;
  $('conversation-status').textContent = data.messages.length ? data.messages[data.messages.length - 1].status : 'NO ACTIVITY';
  const github = data.candidate.github_invited_at ? `GitHub: ${data.candidate.github_username || data.candidate.github_email || 'recorded'} · ${data.candidate.github_invited_at.startsWith('failed:') ? 'invitation failed' : 'invited'}` : data.candidate.github_username || data.candidate.github_email ? 'GitHub contact recorded · invitation pending' : 'GitHub contact not provided';
  $('conversation-summary').textContent = `${data.candidate.category} · ${data.messages.length} messages · ${github}`;
  $('conversation-messages').innerHTML = data.messages.length ? data.messages.map((item) => `<div class="chat-bubble ${item.direction}">${escapeHtml(item.body).replace(/\n/g, '<br>')}<small>${item.direction === 'inbound' ? 'MEMBER' : 'OCEANPARKASSET'} · ${escapeHtml(item.status)} · ${formatTime(item.sent_at || item.send_after || item.created_at)}</small></div>`).join('') : '<p class="chat-empty">No messages recorded.</p>';
}
async function loadConversation(candidateId) { selectedConversation = candidateId; await request(`/api/conversations/${candidateId}/read`, {method: 'POST'}); const data = await request(`/api/conversations/${candidateId}`); renderConversationDetail(data); renderConversations(await request('/api/conversations')); }
async function loadConversations() { const items = await request('/api/conversations'); renderConversations(items); if (selectedConversation) renderConversationDetail(await request(`/api/conversations/${selectedConversation}`)); }
async function pingServer() {
  // Online/offline is only about reachability. Do not require account data to succeed.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch(`${API}/api/health`, {signal: controller.signal});
    return response.ok;
  } catch (error) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const online = await pingServer();
    if (!online) {
      $('service-dot').className = 'service-dot error';
      $('status').textContent = 'Server offline · click Start server';
      setServerButton(false);
      $('auth').textContent = 'Connect Himalayas';
      return;
    }
    setServerButton(true);
    try {
      const [health, allCandidates, progress, activity] = await Promise.all([
        request('/api/health'), request('/api/candidates'), request('/api/progress'), request('/api/activity'),
      ]);
      const candidates = allCandidates.filter((item) => candidatePage(item) === currentPage);
      $('service-dot').className = 'service-dot online';
      $('status').textContent = health.hold
        ? `Sending on hold: ${health.hold}`
        : `Service online · auto-send ${health.auto_send ? 'on' : 'off'}`;
      $('auth').textContent = health.himalayas_authorized
        ? 'Himalayas connected'
        : health.himalayas_login === 'expired'
          ? 'Login expired · Reconnect'
          : 'Connect Himalayas';
      $('page-title').textContent = `Page ${currentPage}`;
      $('previous').disabled = currentPage === 1;
      $('page-count').textContent = candidates.length;
      $('last-updated').textContent = new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
      renderActivity(activity);
      renderProgress(progress, health.delivery);
      updateMetrics(allCandidates, candidates, progress);
      updateAutomation(health.automation);
      await loadConversations();
    } catch (error) {
      // Server is up; this profile's data call failed (account / API). Do not claim the server is offline.
      $('service-dot').className = 'service-dot online';
      let detail = error.message || 'unknown error';
      try { detail = JSON.parse(error.message).detail || detail; } catch (parseError) { /* plain text */ }
      $('status').textContent = `Server online · could not load this profile: ${String(detail).slice(0, 120)}`;
    }
  } finally {
    refreshing = false;
  }
}
async function syncPage() { $('sync').disabled = true; $('sync').textContent = 'Syncing...'; $('status').textContent = `Loading page ${currentPage} from MCP...`; try { const result = await request(`/api/candidates/sync?page=${currentPage}`, {method: 'POST'}); await refresh(); $('status').textContent = `Page ${currentPage} synced · ${result.imported} profiles`; } catch (error) { $('status').textContent = `Sync failed: ${error.message}`; throw error; } finally { $('sync').disabled = false; $('sync').textContent = 'Sync page'; } }
$('sync').onclick = syncPage;
$('refresh').onclick = refresh;
$('launch').onclick = async () => { $('launch').disabled = true; try { await request('/api/automation/start', {method: 'POST'}); await refresh(); } catch (error) { $('status').textContent = `Automation failed: ${error.message}`; $('launch').disabled = false; } };
$('stop').onclick = async () => { $('stop').disabled = true; try { await request('/api/automation/stop', {method: 'POST'}); await refresh(); } finally { $('stop').disabled = false; } };
$('previous').onclick = async () => { if (currentPage > 1) { $('previous').disabled = true; currentPage -= 1; try { await refresh(); } catch (error) { $('status').textContent = `Could not load page ${currentPage}: ${error.message}`; } finally { $('previous').disabled = false; } } };
$('next').onclick = async () => { $('next').disabled = true; currentPage += 1; try { await refresh(); $('status').textContent = `Page ${currentPage} ready · click Sync page to load profiles`; } catch (error) { $('status').textContent = `Could not load page ${currentPage}: ${error.message}`; } finally { $('next').disabled = false; } };
const NATIVE_HOST = 'com.himalayas.hiring_assistant';
let serverRunning = false;
function setServerButton(running) { serverRunning = running; $('server-toggle').textContent = running ? 'Stop server' : 'Start server'; }
function callNativeHost(action) {
  return new Promise((resolve, reject) => chrome.runtime.sendNativeMessage(NATIVE_HOST, {action}, (reply) => chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve(reply || {})));
}
$('server-toggle').onclick = async () => {
  const button = $('server-toggle');
  const action = serverRunning ? 'stop' : 'start';
  button.disabled = true;
  $('status').textContent = action === 'start' ? 'Starting server...' : 'Stopping server...';
  try {
    const reply = await callNativeHost(action);
    $('status').textContent = reply.message || (action === 'start' ? 'Server started' : 'Server stopped');
    // Always re-check health — native host and this Chrome profile must agree.
    const online = await pingServer();
    setServerButton(online);
    $('service-dot').className = online ? 'service-dot online' : 'service-dot error';
    if (online) await refresh();
    else $('status').textContent = reply.message || 'Server offline · click Start server';
  } catch (error) {
    const online = await pingServer();
    setServerButton(online);
    $('service-dot').className = online ? 'service-dot online' : 'service-dot error';
    if (online) {
      $('status').textContent = 'Server is running (helper could not control it). Reloaded status.';
      await refresh();
    } else if (/not found|forbidden|Specified native messaging host not found/i.test(error.message)) {
      $('status').textContent = 'Helper not installed. Run scripts/install-native-host.sh, then reload the extension.';
    } else {
      $('status').textContent = `Server control failed: ${error.message}`;
    }
  } finally {
    button.disabled = false;
  }
};
$('auth').onclick = async () => { window.open(`${API}/api/auth/start?account_id=${encodeURIComponent(await accountReady)}`, '_blank'); };
$('dashboard-tab').onclick = () => showTab('dashboard');
$('chats-tab').onclick = () => showTab('chats');
// One dashboard for every extension, served by the backend. It belongs to no Chrome profile.
$('admin-link').onclick = () => { window.open('http://localhost:8765/admin/', 'hiring-admin'); };
$('settings-tab').onclick = () => showTab('settings');
$('save-settings').onclick = async () => { const button = $('save-settings'); button.disabled = true; $('settings-result').textContent = 'Saving...'; const values = {}; settingFields.forEach((field) => { const value = $(field).value.trim(); if (value !== '') values[field] = (field.endsWith('_delay_seconds') || field === 'daily_dm_limit') ? Number(value) : value; }); try { const saved = await request('/api/settings', {method: 'PUT', body: JSON.stringify(values)}); await request('/api/account', {method: 'PUT', body: JSON.stringify({label: $('account_label').value.trim()})}); const warnings = (saved && saved.warnings) || [], errors = (saved && saved.errors) || []; $('settings-result').textContent = errors.length ? `${saved.saved && saved.saved.length ? 'Saved. ' : ''}${errors.join(' ')}` : warnings.length ? `Settings saved. Note: ${warnings.join(' ')}` : 'Settings saved.'; await loadSettings(); } catch (error) { let detail = error.message; try { detail = JSON.parse(error.message).detail || detail; } catch (parseError) { /* plain text */ } $('settings-result').textContent = `Not saved: ${detail}`; } finally { button.disabled = false; } };
async function connectDashboardEvents() {
  const source = new EventSource(`${API}/api/events?account_id=${encodeURIComponent(await accountReady)}`);
  source.addEventListener('dashboard-update', refresh);
  source.onerror = () => { source.close(); setTimeout(connectDashboardEvents, 5000); };
}
connectDashboardEvents();
setInterval(refresh, 60000);
refresh();