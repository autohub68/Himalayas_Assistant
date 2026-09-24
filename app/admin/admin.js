// Control center: every extension (one account per Chrome profile) in one dashboard.
// This page belongs to no profile. It calls the same API as the extensions and names the account it acts on.
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));

async function api(path, options = {}, accountId = null) {
  const headers = {'Content-Type': 'application/json', ...(accountId ? {'X-Account-Id': accountId, 'X-Admin': '1'} : {}), ...(options.headers || {})};
  const response = await fetch(path, {...options, headers});
  if (response.status === 401) { location.href = '/login'; throw new Error(JSON.stringify({detail: 'Signed out. Sign in again.'})); }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
const errorText = (error) => { try { return JSON.parse(error.message).detail || error.message; } catch (parseError) { return error.message; } };
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const formatDate = (value) => (value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '—');
function timeAgo(value) {
  if (!value) return 'never';
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}
function timeUntil(value) {
  if (!value) return '—';
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  if (seconds <= 0) return 'due now';
  if (seconds < 60) return `in ${seconds}s`;
  if (seconds < 3600) return `in ${Math.round(seconds / 60)} min`;
  return `in ${(seconds / 3600).toFixed(1)} h`;
}
const STATUS_LABELS = {sent: 'Sent', failed: 'Failed', scheduled: 'Queued', approved: 'Approved', queued: 'Queued', skipped: 'Skipped', received: 'Received'};
const STAGE_LABELS = {first_sent: 'Message 1 sent', intro_sent: 'Introduced', experience_sent: 'Experience asked', process_sent: 'Process explained', assessment_sent: 'Assessment sent', invite_pending: 'Invite pending', invited: 'GitHub invited', apply_sent: 'Application link sent', closed: 'Closed'};
const statusLabel = (status) => STATUS_LABELS[status] || status;
const stageLabel = (stage) => (stage ? (STAGE_LABELS[stage] || (/^step_(\d+)$/.test(stage) ? `Message ${stage.slice(5)} sent` : stage)) : 'Not sent yet');
const tag = (status, label) => `<span class="tag ${escapeHtml(status)}">${escapeHtml(label || statusLabel(status))}</span>`;

const state = {overview: null, view: 'accounts', accountId: null, tab: 'overview', busy: false,
  mem: {items: [], summary: {}, matched: 0, status: '', query: '', sortKey: 'last_activity', sortDir: 'desc', page: 0, pageSize: 25, openId: null},
  ledger: {items: [], total: 0, status: '', query: '', offset: 0, pageSize: 50, busy: false}};
const currentAccount = () => (state.overview ? state.overview.accounts.find((account) => account.id === state.accountId) : null);

function toast(message, isError = false) {
  const box = $('toast');
  box.hidden = !message; box.textContent = message || ''; box.style.borderColor = isError ? '#6b2f2b' : '';
  clearTimeout(toast.timer);
  if (message) toast.timer = setTimeout(() => { box.hidden = true; }, 7000);
}

// ---------- header ----------
function renderHeader(overview) {
  const pill = $('server-pill');
  pill.className = 'pill ok'; pill.textContent = 'Server online';
  const c = overview.checks;
  const check = (ok, label, hint, warn = false) => `<span class="pill ${ok ? 'ok' : warn ? 'warn' : 'bad'}" title="${escapeHtml(hint)}">${ok ? '✓' : '✗'} ${escapeHtml(label)}</span>`;
  $('checks').innerHTML =
    check(c.openrouter_key, 'AI key', c.openrouter_key ? 'OpenRouter key is set' : 'Set the OpenRouter API key in Settings') +
    check(c.supabase, 'Contact ledger', c.supabase ? 'Supabase is configured and accepts the key' : (c.supabase_error || 'Set the Supabase URL and key in Settings')) +
    (c.ledger_status_ready === false ? check(false, 'Ledger statuses off', 'Run the updated supabase_schema.sql in Supabase to store queued and failed statuses', true) : c.ledger_status_ready === true ? check(true, 'Ledger statuses on', 'Queued and sent claims block other profiles') : '') +
    check(c.github, 'GitHub access', c.github ? 'GitHub token, owner and repository are set' : 'Set the GitHub token, owner and repository in Settings, or developer invitations will be held', true);
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString([], {timeStyle: 'short'})}`;
  updateGlobalUnread((overview.totals && overview.totals.unread) || 0);
}
function updateGlobalUnread(unread) {
  const total = Number(unread) || 0;
  [['global-unread', total], ['accounts-unread-badge', total]].forEach(([id, count]) => {
    const el = $(id);
    if (!el) return;
    el.hidden = !count;
    el.textContent = count;
  });
}
function alertClass(text) {
  if (/unread/i.test(text)) return ' unread';
  if (/failed|not connected|expired/i.test(text)) return ' bad';
  return '';
}
function markOffline(error) {
  const pill = $('server-pill');
  pill.className = 'pill bad'; pill.textContent = 'Server not reachable';
  toast(/Failed to fetch|NetworkError|Load failed/i.test(error.message) ? 'The server is not reachable. Start it from any extension popup (Start server), then press Refresh.' : errorText(error), true);
}

// ---------- accounts overview ----------
function card(label, value, cls = '') { return `<div class="card ${cls}"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`; }
function renderTotals(t) {
  $('totals').innerHTML = card('Accounts', t.accounts) + card('Connected', `${t.connected}/${t.accounts}`) + card('Automations running', t.automation_running) + card('Sending paused', t.paused) +
    card('Members reached', t.members) + card('Sent', t.sent, 'sent') + card('Failed', t.failed, 'failed') + card('Queued', t.queued, 'queued') +
    (t.unread ? card('Unread replies', t.unread, 'failed') : '');
}
const shortStatus = (text) => { const line = String(text || '').split('\n')[0]; return line.length > 60 ? `${line.slice(0, 57)}…` : line; };
function automationText(account) {
  const a = account.automation;
  if (a.running) return `Running · page ${a.page ?? '?'} · ${a.status}`;
  return a.status && a.status !== 'idle' ? shortStatus(a.status) : 'Idle';
}
function sendingText(account) {
  if (account.paused) return 'Paused';
  const d = account.delivery;
  return d.status === 'sending' && d.current_name ? `Sending to ${d.current_name}` : d.status === 'idle' ? 'Idle' : d.status;
}
function dailyText(daily) {
  if (!daily || !daily.limit) return 'No limit';
  const hours = Math.floor(daily.resets_in_seconds / 3600), minutes = Math.floor((daily.resets_in_seconds % 3600) / 60);
  return `${daily.sent_today} of ${daily.limit} today${daily.reached ? ' · reached' : ''} · resets in ${hours}h ${minutes}m`;
}
const loginExpired = (account) => account.himalayas_login === 'expired';
function accountCard(account) {
  const b = account.by_status;
  const queued = (b.queued || 0) + (b.scheduled || 0) + (b.approved || 0);
  const wait = Math.max(0, (new Date(account.next_send_at).getTime() - Date.now()) / 1000, account.next_send_in || 0);
  const next = account.next_recipient ? `${escapeHtml(account.next_recipient)} · ${wait < 3 ? 'now' : `in ${Math.round(wait)}s`}` : '—';
  const unread = account.unread || 0;
  return `<article class="acct${unread ? ' attention' : ''}" data-id="${escapeHtml(account.id)}">
    <div class="acct-head"><div><div class="acct-name" data-open="${escapeHtml(account.id)}"><span class="dot on"></span>${escapeHtml(account.label || 'Unnamed profile')}</div><div class="acct-id">${escapeHtml(account.id.slice(0, 10))}… · seen ${escapeHtml(timeAgo(account.last_seen))}</div></div><div class="acct-head-right">${unread ? `<span class="acct-unread" title="${unread} unread ${unread === 1 ? 'reply' : 'replies'}">${unread}</span>` : ''}${account.paused ? '<span class="pill warn">Paused</span>' : account.automation.running ? '<span class="pill ok">Running</span>' : '<span class="pill ok">Extension online</span>'}</div></div>
    <div class="nums"><div><b>${account.members}</b><span>Members</span></div><div class="sent"><b>${b.sent || 0}</b><span>Sent</span></div><div class="failed"><b>${b.failed || 0}</b><span>Failed</span></div><div class="queued"><b>${queued}</b><span>Queued</span></div></div>
    <div class="lines"><div><span>Extension</span><span>Online</span></div><div><span>Himalayas</span><span>${account.himalayas_login === 'none' ? 'Not connected' : loginExpired(account) ? 'Login expired' : 'Connected'}</span></div><div><span>Automation</span><span>${escapeHtml(automationText(account))}</span></div><div><span>Sending</span><span>${escapeHtml(sendingText(account))}</span></div><div><span>Today</span><span>${escapeHtml(account.daily && account.daily.limit ? `${account.daily.sent_today} / ${account.daily.limit}` : `${account.daily ? account.daily.sent_today : 0} (no limit)`)}</span></div><div><span>Replies</span><span>${account.replied} replied${unread ? ` · ${unread} unread` : ''} · ${escapeHtml(shortStatus(account.reply_monitor.status))}</span></div><div><span>Next message</span><span>${next}</span></div></div>
    <div class="acct-actions">
      <button data-open="${escapeHtml(account.id)}" ${unread ? 'data-open-members="1"' : ''}>${unread ? `Open members (${unread} unread)` : 'Open'}</button>
      <button class="secondary" data-act="automation" data-id="${escapeHtml(account.id)}" ${!account.himalayas_authorized && !account.automation.running ? 'disabled title="Connect Himalayas first"' : ''}>${account.automation.running ? 'Stop automation' : 'Start automation'}</button>
      <button class="secondary" data-act="pause" data-id="${escapeHtml(account.id)}">${account.paused ? 'Resume sending' : 'Pause sending'}</button>
      ${account.himalayas_authorized ? '' : `<a class="button-link attention" target="_blank" rel="noopener" href="/api/auth/start?account_id=${encodeURIComponent(account.id)}">${loginExpired(account) ? 'Reconnect Himalayas' : 'Connect Himalayas'}</a>`}
      <button class="danger" data-act="remove" data-id="${escapeHtml(account.id)}" title="Stop automation and delete this profile from the Control center">Remove</button>
    </div></article>`;
}
function renderAccounts(overview) {
  renderTotals(overview.totals);
  $('accounts').innerHTML = overview.accounts.map(accountCard).join('');
  $('accounts-empty').hidden = overview.accounts.length > 0;
  const note = $('accounts-offline-note');
  if (note) note.hidden = true;
  $('accounts').querySelectorAll('[data-open]').forEach((element) => {
    element.onclick = () => openAccount(element.dataset.open, element.dataset.openMembers === '1' ? 'members' : 'overview');
  });
  $('accounts').querySelectorAll('[data-act]').forEach((button) => { button.onclick = () => accountAction(button.dataset.id, button.dataset.act); });
}

async function accountAction(id, action) {
  const account = state.overview.accounts.find((item) => item.id === id);
  if (!account) return;
  try {
    if (action === 'remove') {
      const name = account.label || account.id.slice(0, 8);
      if (!confirm(`Remove “${name}” from the Control center?\n\nThis stops its automation and deletes its local members, messages and Himalayas login on this server. The shared contact ledger is not changed.`)) return;
      await api(`/api/admin/accounts/${encodeURIComponent(id)}`, {method: 'DELETE'});
      if (state.accountId === id) { state.accountId = null; showView('accounts'); }
      toast(`${name}: removed.`);
      await loadOverview();
      return;
    }
    if (action === 'automation') await api(account.automation.running ? '/api/automation/stop' : '/api/automation/start', {method: 'POST'}, id);
    if (action === 'pause') await api(`/api/admin/accounts/${encodeURIComponent(id)}/pause`, {method: 'POST', body: JSON.stringify({paused: !account.paused})});
    toast(`${account.label || 'Profile'}: ${action === 'pause' ? (account.paused ? 'sending resumed' : 'sending paused') : (account.automation.running ? 'automation stopping' : 'automation starting')}.`);
  } catch (error) { toast(`Could not do that: ${errorText(error)}`, true); }
  await loadOverview();
}
async function bulk(action, question) {
  if (question && !confirm(question)) return;
  try {
    const data = await api('/api/admin/bulk', {method: 'POST', body: JSON.stringify({action})});
    const entries = Object.entries(data.results);
    const skipped = entries.filter(([, result]) => result.startsWith('skipped'));
    const names = (list) => list.map(([id]) => (state.overview.accounts.find((a) => a.id === id) || {}).label || id.slice(0, 8)).join(', ');
    toast(`${entries.length - skipped.length} of ${entries.length} accounts: ${action.replace('_', ' ')}.${skipped.length ? ` Skipped ${names(skipped)} (${[...new Set(skipped.map(([, r]) => r.replace('skipped: ', '')))].join('; ')}).` : ''}`);
  } catch (error) { toast(`Could not do that: ${errorText(error)}`, true); }
  await loadOverview();
}
$('all-start').onclick = () => bulk('start_automation', 'Start the automatic campaign for every connected account?');
$('all-stop').onclick = () => bulk('stop_automation');
$('all-pause').onclick = () => bulk('pause');
$('all-resume').onclick = () => bulk('resume');

async function loadOverview() {
  try {
    state.overview = await api('/api/admin/overview');
    renderHeader(state.overview);
    renderAccounts(state.overview);
    if (state.view === 'account') { renderAccountHead(); loadAccountTab(); }
  } catch (error) { markOffline(error); }
}

// ---------- navigation ----------
function showView(view) {
  state.view = view;
  ['accounts', 'account', 'ledger', 'playbook', 'settings'].forEach((name) => { $(`view-${name}`).hidden = name !== view; });
  document.querySelectorAll('#main-tabs button').forEach((button) => button.classList.toggle('active', button.dataset.view === (view === 'account' ? 'accounts' : view)));
  if (view === 'settings') loadSettings();
  if (view === 'playbook') loadPlaybook();
  if (view === 'ledger') loadLedger();
}
document.querySelectorAll('#main-tabs button').forEach((button) => { button.onclick = () => { closeDetail(); showView(button.dataset.view); }; });
$('back').onclick = () => { closeDetail(); showView('accounts'); };

// ---------- one account ----------
function openAccount(id, tab = 'overview') {
  state.accountId = id; state.tab = tab;
  Object.assign(state.mem, {items: [], status: '', query: '', page: 0, openId: null}); $('search').value = '';
  showView('account'); renderAccountHead(); setTab(tab);
}
function renderAccountHead() {
  const account = currentAccount();
  if (!account) return;
  if (document.activeElement !== $('account-label')) $('account-label').value = account.label || '';
  $('account-meta').textContent = `Account ${account.id} · extension ${account.extension_online ? 'online' : 'offline'} · last seen ${timeAgo(account.last_seen)} · ${account.himalayas_login === 'connected' ? 'Himalayas connected' : account.himalayas_login === 'expired' ? 'Himalayas login expired' : 'Himalayas not connected'}`;
  $('a-automation').textContent = account.automation.running ? 'Stop automation' : 'Start automation';
  $('a-automation').className = account.automation.running ? 'warn' : '';
  $('a-automation').disabled = !account.himalayas_authorized && !account.automation.running;
  $('a-pause').textContent = account.paused ? 'Resume sending' : 'Pause sending';
  $('a-connect').href = `/api/auth/start?account_id=${encodeURIComponent(account.id)}`;
  $('a-connect').textContent = account.himalayas_login === 'none' ? 'Connect Himalayas' : 'Reconnect Himalayas';
  $('a-connect').className = account.himalayas_authorized ? 'button-link' : 'button-link attention';
  // Only unread alerts here. Failure/outage notes stay in the Members table and Failed count.
  const unreadAlerts = (account.alerts || []).filter((text) => /unread/i.test(text));
  $('account-alerts').innerHTML = unreadAlerts.map((text) => `<span class="alert${alertClass(text)}">${escapeHtml(text)}</span>`).join('');
  updateUnreadBadge(account.unread || 0);
}
function updateUnreadBadge(unread) {
  const badge = $('unread-badge');
  badge.hidden = !unread;
  badge.textContent = unread;
}
$('account-save').onclick = async () => {
  try { await api('/api/account', {method: 'PUT', body: JSON.stringify({label: $('account-label').value.trim()})}, state.accountId); toast('Name saved.'); } catch (error) { toast(`Could not save: ${errorText(error)}`, true); }
  await loadOverview();
};
$('a-automation').onclick = () => accountAction(state.accountId, 'automation');
$('a-pause').onclick = () => accountAction(state.accountId, 'pause');

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll('#account-tabs button').forEach((button) => button.classList.toggle('active', button.dataset.tab === tab));
  ['overview', 'members', 'clean'].forEach((name) => { $(`tab-${name}`).hidden = name !== tab; });
  loadAccountTab();
}
document.querySelectorAll('#account-tabs button').forEach((button) => { button.onclick = () => setTab(button.dataset.tab); });
async function loadAccountTab() {
  if (!state.accountId) return;
  try {
    if (state.tab === 'overview') await loadAccountOverview();
    else if (state.tab === 'members') await loadMembers();
    else await loadCleanup();
  } catch (error) { toast(`Could not load: ${errorText(error)}`, true); }
}

async function loadAccountOverview() {
  const id = state.accountId;
  const [progress, activity, people] = await Promise.all([api('/api/progress', {}, id), api('/api/activity?limit=40', {}, id), api('/api/db/members?limit=1', {}, id)]);
  const account = currentAccount(), by = people.summary;
  // One count for each member (the status of the first message), so Sent + Queued + Failed + Skipped = Members.
  const queuedMembers = (by.scheduled || 0) + (by.approved || 0) + (by.queued || 0);
  $('progress-cards').innerHTML = card('Sent', by.sent || 0, 'sent') + card('Queued', queuedMembers, 'queued') + card('Failed', by.failed || 0, 'failed') + card('Skipped', by.skipped || 0) + card('Members', by.total) + card('Replied', account ? account.replied : '—') + (account && account.unread ? card('Unread', account.unread, 'failed') : '') +
    `<p class="note" style="grid-column:1/-1">Each member is counted once, by the status of the first message. ${plural(progress.total, 'message')} in total including follow-ups and retries (${progress.sent} sent, ${progress.failed} failed).</p>`;
  const live = account ? [
    ['Automation', automationText(account)], ['Sending', sendingText(account)], ['Daily limit', dailyText(account.daily)], ['Reply monitor', `${shortStatus(account.reply_monitor.status)} (${account.reply_monitor.detected} detected)`],
    ['Next message', account.next_recipient ? `${account.next_recipient} · ${timeUntil(account.next_send_at)}` : '—'], ['Latest result', progress.latest_recipient ? `${progress.latest_recipient} · ${progress.latest_status}${progress.latest_error ? ` · ${progress.latest_error}` : ''}` : '—'],
    ['Imported', `${plural(account.imported, 'candidate')}`],
  ] : [];
  $('live').innerHTML = live.map(([term, value]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd>`).join('');
  $('activity').innerHTML = activity.length ? activity.map((row) => `<div class="act-row"><div>${escapeHtml(row.name)}<br><small>${escapeHtml(row.direction)} · ${escapeHtml(formatDate(row.sent_at || row.send_after || row.created_at))}</small></div>${tag(row.status)}<span></span></div>`).join('') : '<p class="muted">No activity yet.</p>';
}

// ---------- members ----------
function renderChips() {
  const summary = state.mem.summary;
  const statuses = Object.keys(summary).filter((key) => !['total', 'unread'].includes(key));
  const chip = (status, label, count) => `<button class="chip${state.mem.status === status ? ' active' : ''}" data-status="${escapeHtml(status)}">${escapeHtml(label)}<b>${count}</b></button>`;
  $('chips').innerHTML =
    chip('', 'All', summary.total || 0) +
    (summary.unread ? chip('unread', 'Unread', summary.unread) : '') +
    statuses.map((status) => chip(status, statusLabel(status), summary[status])).join('');
  $('chips').querySelectorAll('.chip').forEach((button) => { button.onclick = () => { state.mem.status = button.dataset.status; state.mem.page = 0; renderMembers(); }; });
}
function visibleMembers() {
  const m = state.mem, query = m.query.toLowerCase(), direction = m.sortDir === 'asc' ? 1 : -1;
  const filtered = m.items.filter((item) => {
    if (m.status === 'unread') return Number(item.unread_count || 0) > 0;
    if (m.status && item.outreach_status !== m.status) return false;
    if (query && !(
      item.name.toLowerCase().includes(query)
      || (item.country || '').toLowerCase().includes(query)
      || (item.role || '').toLowerCase().includes(query)
    )) return false;
    return true;
  });
  // Keep API unread-first order unless the user chose a column sort other than the default.
  if (m.sortKey === 'last_activity' && m.sortDir === 'desc' && !m.status) return filtered;
  return filtered.sort((a, b) => {
    if (m.status !== 'unread' && (a.unread_count || 0) !== (b.unread_count || 0)) return (b.unread_count || 0) - (a.unread_count || 0);
    const left = a[m.sortKey] ?? '', right = b[m.sortKey] ?? '';
    return (typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right), undefined, {sensitivity: 'base'})) * direction;
  });
}
function githubCell(member) {
  if (!member.github_username) return '<span class="sub">—</span>';
  const note = member.github_invited_at && String(member.github_invited_at).startsWith('failed') ? 'invite failed' : member.github_invited_at ? 'invited' : 'not invited';
  return `${escapeHtml(member.github_username)}<br><span class="sub">${note}</span>`;
}
function himalayasCell(member) {
  const h = member.himalayas;
  if (!h) return '<span class="sub">not checked</span>';
  if (!h.ok) return `<span class="himal bad">⚠ ${escapeHtml(h.note || 'differs')}</span>`;
  return `<span class="himal ok">✓ ${h.total} message${h.total === 1 ? '' : 's'}</span><span class="sub">${h.read ? 'read by member' : 'not read yet'}${h.theirs ? ` · ${h.theirs} repl${h.theirs === 1 ? 'y' : 'ies'}` : ''}</span>`;
}
function liveBubble(message) {
  const ours = message.who === 'company';
  return `<div class="bubble ${ours ? 'outbound' : 'inbound'}"><div class="who">${ours ? 'Us' : 'Member'} · on Himalayas</div>${escapeHtml(message.body)}<div class="meta"><span>${escapeHtml(message.when || '')}</span>${ours ? `<span>${message.read ? '✓ read' : 'not read yet'}</span>` : ''}</div></div>`;
}
function reportCsv(items) {
  const lines = items.map((m) => { const h = m.himalayas || {}; return [m.name, m.profile_url, h.room, m.outreach_status, h.total, h.ours, h.theirs, h.read, h.checked_at ? (h.ok ? 'yes' : 'no') : 'not checked', h.note].map(csvCell).join(','); });
  return [['member', 'profile_url', 'himalayas_room', 'bot_status', 'messages_on_himalayas', 'ours', 'member_replies', 'ours_read', 'matches_bot', 'note'].join(','), ...lines].join('\n');
}
function rowHtml(member) {
  const retryId = member.outreach_status === 'failed' ? member.outreach_message_id : member.failed_message_id;
  const followUpFailed = member.failed_message_id && member.outreach_status !== 'failed';
  const problem = member.outreach_status === 'failed' || member.outreach_status === 'skipped' ? member.outreach_error : followUpFailed ? member.failed_error : null;
  const unread = Number(member.unread_count || 0);
  const replyPreview = unread && member.last_reply ? `<span class="reply-preview">“${escapeHtml(member.last_reply)}”</span>` : '';
  return `<tr data-id="${member.id}" class="${unread ? 'unread' : ''}"><td><span class="name">${escapeHtml(member.name)}${unread ? `<b class="row-unread">${unread}</b>` : ''}</span><span class="sub">${member.category === 'developer' ? 'Developer' : 'Business'}</span>${replyPreview}${problem ? `<span class="err">${escapeHtml(problem)}</span>` : ''}</td><td>${escapeHtml(member.country || '—')}</td><td>${escapeHtml(member.role || '—')}</td><td>${tag(member.outreach_status)}${followUpFailed ? ' ' + tag('failed', 'Reply failed') : ''}${unread ? ' ' + tag('received', 'New reply') : ''}</td><td>${himalayasCell(member)}</td><td>${escapeHtml(stageLabel(member.stage))}</td><td class="num">${member.replies}${unread ? `<br><span class="sub">${unread} unread</span>` : ''}</td><td>${githubCell(member)}</td><td>${escapeHtml(formatDate(member.last_reply_at || member.last_activity))}</td><td>${retryId ? `<button class="retry" data-retry="${retryId}">Retry</button>` : ''}</td></tr>`;
}
function renderMembers() {
  renderChips();
  const m = state.mem, items = visibleMembers(), pages = Math.max(1, Math.ceil(items.length / m.pageSize));
  m.page = Math.min(m.page, pages - 1);
  const slice = items.slice(m.page * m.pageSize, (m.page + 1) * m.pageSize);
  const unread = m.items.reduce((total, item) => total + Number(item.unread_count || 0), 0);
  updateUnreadBadge(unread);
  $('rows').innerHTML = slice.length ? slice.map(rowHtml).join('') : `<tr><td colspan="10" class="empty">${m.items.length ? 'No members match this filter.' : 'No members yet.'}</td></tr>`;
  document.querySelectorAll('#members th[data-sort]').forEach((header) => { header.classList.toggle('sorted', header.dataset.sort === m.sortKey); header.classList.toggle('desc', header.dataset.sort === m.sortKey && m.sortDir === 'desc'); });
  $('pager-info').textContent = `${plural(items.length, 'member')} · page ${m.page + 1} of ${pages}${m.matched > m.items.length ? ` · showing the newest ${m.items.length} of ${m.matched}` : ''}${unread ? ` · ${plural(unread, 'unread reply')}` : ''}`;
  $('prev').disabled = m.page === 0; $('next').disabled = m.page >= pages - 1;
  $('rows').querySelectorAll('tr[data-id]').forEach((row) => { row.onclick = () => openDetail(Number(row.dataset.id)); });
  $('rows').querySelectorAll('.retry').forEach((button) => { button.onclick = (event) => { event.stopPropagation(); retryMessage(button); }; });
}
async function retryMessage(button) {
  button.disabled = true; button.textContent = 'Queued';
  try { await api(`/api/messages/${button.dataset.retry}/retry`, {method: 'POST'}, state.accountId); await loadMembers(); if (state.mem.openId) await openDetail(state.mem.openId, true); }
  catch (error) { button.textContent = 'Retry'; button.disabled = false; toast(`Could not retry: ${errorText(error)}`, true); }
}
async function loadMembers() {
  const data = await api('/api/db/members?limit=1000', {}, state.accountId);
  Object.assign(state.mem, {items: data.members, summary: data.summary, matched: data.matched});
  const account = currentAccount();
  if (account) {
    account.unread = data.summary.unread || 0;
    account.alerts = (account.alerts || []).filter((text) => !/unread/i.test(text));
    if (account.unread) account.alerts.unshift(`${account.unread} unread ${account.unread === 1 ? 'reply' : 'replies'}`);
    const unreadAlerts = account.alerts.filter((text) => /unread/i.test(text));
    $('account-alerts').innerHTML = unreadAlerts.map((text) => `<span class="alert${alertClass(text)}">${escapeHtml(text)}</span>`).join('');
  }
  renderMembers();
}
document.querySelectorAll('#members th[data-sort]').forEach((header) => {
  header.onclick = () => { const key = header.dataset.sort, m = state.mem; m.sortDir = m.sortKey === key && m.sortDir === 'asc' ? 'desc' : 'asc'; m.sortKey = key; m.page = 0; renderMembers(); };
});
let searchTimer = null;
$('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.mem.query = $('search').value.trim(); state.mem.page = 0; renderMembers(); }, 200); };
$('prev').onclick = () => { state.mem.page -= 1; renderMembers(); };
$('next').onclick = () => { state.mem.page += 1; renderMembers(); };
const csvCell = (value) => { const text = String(value ?? ''); return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text; };
function membersCsv(items) {
  const lines = items.map((m) => [m.name, m.country, m.role, m.category, m.outreach_status, stageLabel(m.stage), m.replies, m.github_username, m.last_activity, m.outreach_error || m.failed_error].map(csvCell).join(','));
  return [['name', 'country', 'suggested_role', 'category', 'outreach_status', 'chat_step', 'replies', 'github_username', 'last_activity', 'error'].join(','), ...lines].join('\n');
}
$('verify').onclick = async () => {
  const button = $('verify'), out = $('verify-result');
  button.disabled = true; out.textContent = 'Reading every conversation from Himalayas… this can take a minute.';
  try {
    const r = await api('/api/db/verify-himalayas', {method: 'POST'}, state.accountId);
    out.textContent = `Himalayas holds ${r.conversations_on_himalayas} conversations. ${r.match} of ${r.checked} members match this bot.` + (r.missing_on_himalayas.length ? ` ${r.missing_on_himalayas.length} have a message the bot recorded but Himalayas does not show.` : '') + (r.replies_imported ? ` ${r.replies_imported} missed repl${r.replies_imported === 1 ? 'y was' : 'ies were'} added.` : '');
    await loadMembers();
  } catch (error) { out.textContent = `Could not check: ${errorText(error)}`; }
  button.disabled = false;
};
$('report').onclick = () => {
  const url = URL.createObjectURL(new Blob([reportCsv(state.mem.items)], {type: 'text/csv'}));
  const link = document.createElement('a');
  link.href = url; link.download = `himalayas-report-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('export').onclick = () => {
  const account = currentAccount(), url = URL.createObjectURL(new Blob([membersCsv(visibleMembers())], {type: 'text/csv'}));
  const link = document.createElement('a');
  link.href = url; link.download = `members-${(account && account.label ? account.label : 'profile').replace(/\W+/g, '-')}-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$('retry-failed').onclick = async () => {
  try {
    const preview = await api('/api/db/retry-failed', {method: 'POST', body: JSON.stringify({dry_run: true, rewrite: true})}, state.accountId);
    const total = preview.safe + preview.waiting;
    if (!total) { toast(preview.other ? `Nothing to retry safely. ${plural(preview.other, 'failed message')} failed after a send was attempted, so retrying could send twice. Retry those one by one.` : 'There are no failed or waiting messages.'); return; }
    const other = preview.other ? ` ${plural(preview.other, 'other failed message')} will stay failed, because retrying them could send twice.` : '';
    if (!confirm(`Rewrite ${plural(preview.rewrite, 'first message')} in the current short, neutral style and queue ${plural(total, 'message')} at the normal pace? Nothing is sent until sending is resumed.${other}`)) return;
    toast('Rewriting the messages…');
    const done = await api('/api/db/retry-failed', {method: 'POST', body: JSON.stringify({dry_run: false, rewrite: true})}, state.accountId);
    toast(`${plural(done.rewritten, 'message')} rewritten and ${plural(done.retried + done.waiting, 'message')} queued. Review them in the Members tab, then resume sending.`);
    await loadOverview(); await loadMembers();
  } catch (error) { toast(`Could not do that: ${errorText(error)}`, true); }
};

// ---------- member details ----------
function closeDetail() { state.mem.openId = null; $('detail').hidden = true; $('scrim').hidden = true; }
function inviteText(c) {
  if (!c.github_username) return '—';
  if (!c.github_invited_at) return `${c.github_username} (not invited yet)`;
  if (String(c.github_invited_at).startsWith('failed')) return `${c.github_username} — ${c.github_invited_at}`;
  return `${c.github_username} (invited ${formatDate(c.github_invited_at)})`;
}
function bubbleHtml(message) {
  const outbound = message.direction === 'outbound';
  const due = outbound && ['scheduled', 'approved'].includes(message.status) && message.send_after ? `sends ${formatDate(message.send_after)}` : formatDate(message.sent_at || message.created_at);
  const retry = outbound && message.status === 'failed' ? `<button class="retry" data-retry="${message.id}">Retry</button>` : '';
  return `<div class="bubble ${outbound ? 'outbound' : 'inbound'}"><div class="who">${outbound ? 'Us' : 'Member'}</div>${escapeHtml(message.body)}<div class="meta">${tag(message.status)}<span>${escapeHtml(due)}</span>${message.error ? `<span class="err">${escapeHtml(message.error)}</span>` : ''}${retry}</div></div>`;
}
function liveMatches(live, local) {
  // The bot's own messages that really left (or arrived), in the same words as the live copy from Himalayas.
  const key = (text) => String(text || '').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 40);
  const mine = local.filter((m) => (m.direction === 'outbound' && m.status === 'sent') || m.direction === 'inbound');
  const liveKeys = live.messages.map((m) => key(m.body));
  return mine.length === live.messages.length && mine.every((m) => liveKeys.includes(key(m.body)));
}
async function loadLive(id, local = []) {
  const box = document.getElementById('live-thread');
  if (!box) return;
  try {
    const live = await api(`/api/conversations/${id}/himalayas`, {}, state.accountId);
    if (state.mem.openId !== id) return;
    if (liveMatches(live, local)) return;  // the same as the bot's record: nothing to show. The status under each message is enough
    document.getElementById('live-section').hidden = false;
    box.innerHTML = '<p class="note">Himalayas shows something different from the bot\'s record:</p>' + ([...live.messages].reverse().map(liveBubble).join('') || '<p class="muted">Himalayas holds no conversation with this member.</p>') + `<p class="note">Room: ${escapeHtml(live.room || '')}</p>`;
  } catch (error) { /* a failed live check is not shown: it says nothing about this member */ }
}
async function openDetail(id, keepOpen = false) {
  state.mem.openId = id; $('detail').hidden = false; $('scrim').hidden = false;
  if (!keepOpen) { $('detail-name').textContent = 'Loading...'; $('detail-sub').textContent = ''; $('detail-body').innerHTML = ''; }
  try {
    await api(`/api/conversations/${id}/read`, {method: 'POST'}, state.accountId);
    const data = await api(`/api/conversations/${id}`, {}, state.accountId), c = data.candidate;
    $('detail-name').textContent = c.name;
    $('detail-sub').textContent = `${[c.country, c.suggested_role || 'No role yet', stageLabel(c.stage)].filter(Boolean).join(' · ')}`;
    const profile = c.profile_url ? `<a href="${escapeHtml(c.profile_url)}" target="_blank" rel="noopener">${escapeHtml(c.profile_url)}</a>` : '—';
    $('detail-body').innerHTML =
      `<dl class="facts"><dt>Country</dt><dd>${escapeHtml(c.country || '—')}</dd><dt>Category</dt><dd>${c.category === 'developer' ? 'Developer' : 'Business'}</dd><dt>Profile</dt><dd>${profile}</dd><dt>GitHub</dt><dd>${escapeHtml(inviteText(c))}</dd>${c.github_email ? `<dt>Email</dt><dd>${escapeHtml(c.github_email)}</dd>` : ''}<dt>Messages</dt><dd>${data.messages.length}</dd></dl>` +
      (c.summary ? `<div><p class="group">PROFILE</p><div class="profile">${escapeHtml(c.summary.slice(0, 1500))}</div></div>` : '') +
      `<div><p class="group">CONVERSATION</p><div class="thread">${data.messages.length ? data.messages.map(bubbleHtml).join('') : '<p class="muted">No messages.</p>'}</div></div>` +
      `<div id="live-section" hidden><p class="group">CHECK ON HIMALAYAS (LIVE)</p><div id="live-thread" class="thread"></div></div>`;
    loadLive(id, data.messages);
    $('detail-body').querySelectorAll('.retry').forEach((button) => { button.onclick = () => retryMessage(button); });
    if (state.tab === 'members') await loadMembers();
    else {
      const account = currentAccount();
      if (account) {
        const people = await api('/api/db/members?limit=1', {}, state.accountId);
        const unread = (people.summary && people.summary.unread) || 0;
        account.unread = unread;
        account.alerts = (account.alerts || []).filter((text) => !/unread/i.test(text));
        if (unread) account.alerts.unshift(`${unread} unread ${unread === 1 ? 'reply' : 'replies'}`);
        updateUnreadBadge(unread);
        const unreadAlerts = account.alerts.filter((text) => /unread/i.test(text));
        $('account-alerts').innerHTML = unreadAlerts.map((text) => `<span class="alert${alertClass(text)}">${escapeHtml(text)}</span>`).join('');
      }
    }
  } catch (error) { $('detail-name').textContent = 'Could not load this member'; $('detail-sub').textContent = errorText(error); }
}
$('detail-close').onclick = closeDetail;
$('scrim').onclick = closeDetail;
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });

// ---------- clean up (this account) ----------
function describeSelection(data, done = false) {
  return `${plural(data.members, 'member')} and ${plural(data.messages, 'message')}.${data.queued_cancelled ? ` ${plural(data.queued_cancelled, 'queued message')} ${done ? 'were' : 'will be'} cancelled.` : ''}`;
}
function localDayStart(value, addDays = 0) { const [year, month, day] = value.split('-').map(Number); return new Date(year, month - 1, day + addDays).toISOString(); }
function cleanRange() { const from = $('clean-from').value, to = $('clean-to').value; return {mode: 'range', from_time: from ? localDayStart(from) : null, before_time: to ? localDayStart(to, 1) : null}; }
function resetCleanPreview() { $('clean-summary').hidden = true; $('clean-confirm').hidden = true; }
async function loadCleanup() {
  const account = currentAccount();
  const data = await api('/api/db/clean', {method: 'POST', body: JSON.stringify({mode: 'all', dry_run: true})}, state.accountId);
  $('clean-current').textContent = `${account && account.label ? account.label : 'This profile'} currently holds ${describeSelection(data)}`;
}
$('clean-from').oninput = resetCleanPreview;
$('clean-to').oninput = resetCleanPreview;
$('clean-preview').onclick = async () => {
  $('clean-result').textContent = '';
  try {
    const data = await api('/api/db/clean', {method: 'POST', body: JSON.stringify({...cleanRange(), dry_run: true})}, state.accountId);
    $('clean-summary').hidden = false;
    $('clean-summary').textContent = data.members ? `Will delete ${describeSelection(data)}` : 'No members match these dates.';
    $('clean-confirm').hidden = !data.members;
  } catch (error) { $('clean-summary').hidden = false; $('clean-summary').textContent = errorText(error); $('clean-confirm').hidden = true; }
};
async function runCleanup(body, button) {
  button.disabled = true; $('clean-result').textContent = 'Deleting...';
  try {
    const data = await api('/api/db/clean', {method: 'POST', body: JSON.stringify({...body, dry_run: false})}, state.accountId);
    $('clean-result').textContent = data.members ? `Deleted ${describeSelection(data, true)} Backup saved as ${data.backup}.` : 'Nothing to delete.';
    resetCleanPreview(); $('clean-all-text').value = ''; closeDetail();
    await loadOverview();
  } catch (error) { $('clean-result').textContent = `Could not delete: ${errorText(error)}`; }
  finally { button.disabled = button.id === 'clean-all'; }
}
$('clean-confirm').onclick = () => runCleanup(cleanRange(), $('clean-confirm'));
$('clean-all-text').oninput = () => { $('clean-all').disabled = $('clean-all-text').value !== 'DELETE'; };
$('clean-all').onclick = () => runCleanup({mode: 'all', confirm: 'DELETE'}, $('clean-all'));

// ---------- shared settings ----------
const settingFields = ['openrouter_api_key', 'supabase_url', 'supabase_key', 'github_token', 'github_owner', 'github_repo', 'github_ai_repo', 'min_message_delay_seconds', 'max_message_delay_seconds', 'daily_dm_limit'];
const secretFields = ['openrouter_api_key', 'supabase_key', 'github_token'];
async function loadSettings() {
  try {
    const values = await api('/api/settings');
    settingFields.forEach((field) => { $(field).value = secretFields.includes(field) ? '' : (values[field] ?? ''); if (secretFields.includes(field)) $(field).placeholder = values[field] || ''; });
  } catch (error) { $('settings-result').textContent = `Could not load settings: ${errorText(error)}`; }
}
$('save-settings').onclick = async () => {
  const button = $('save-settings'), values = {};
  settingFields.forEach((field) => { const value = $(field).value.trim(); if (value !== '') values[field] = (field.endsWith('_delay_seconds') || field === 'daily_dm_limit') ? Number(value) : value; });
  button.disabled = true; $('settings-result').textContent = 'Saving...';
  try { const saved = await api('/api/settings', {method: 'PUT', body: JSON.stringify(values)}); const warnings = saved.warnings || [], errors = saved.errors || []; $('settings-result').textContent = errors.length ? `${saved.saved && saved.saved.length ? 'Saved. ' : ''}${errors.join(' ')}` : warnings.length ? `Settings saved. Note: ${warnings.join(' ')}` : 'Settings saved.'; await loadOverview(); await loadSettings(); }
  catch (error) { $('settings-result').textContent = `Not saved: ${errorText(error)}`; }
  finally { button.disabled = false; }
};

// ---------- start ----------
$('refresh').onclick = loadOverview;
loadOverview();
setInterval(() => { if (!document.hidden) loadOverview(); }, 10000);

function connectAdminEvents() {
  const source = new EventSource('/api/admin/events');
  source.addEventListener('dashboard-update', () => {
    if (!document.hidden) loadOverview();
  });
  source.onerror = () => {
    source.close();
    setTimeout(connectAdminEvents, 5000);
  };
}
connectAdminEvents();


// ---------- shared Supabase ledger ----------
function ledgerChip(status, label) {
  const active = state.ledger.status === status;
  return `<button type="button" class="chip${active ? ' active' : ''}" data-ledger-status="${escapeHtml(status)}">${escapeHtml(label)}</button>`;
}
function renderLedgerChips() {
  $('ledger-chips').innerHTML = ledgerChip('', 'All') + ledgerChip('sent', 'Sent') + ledgerChip('queued', 'Queued') + ledgerChip('failed', 'Failed');
  $('ledger-chips').querySelectorAll('button').forEach((button) => {
    button.onclick = () => { state.ledger.status = button.dataset.ledgerStatus; state.ledger.offset = 0; loadLedger(); };
  });
}
function ledgerRow(contact) {
  const profile = contact.profile_url
    ? `<a href="${escapeHtml(contact.profile_url)}" target="_blank" rel="noopener">${escapeHtml(contact.talent_slug || 'profile')}</a>`
    : escapeHtml(contact.talent_slug || '—');
  const when = contact.updated_at || contact.sent_at || contact.created_at || '';
  return `<tr data-slug="${escapeHtml(contact.talent_slug || '')}"><td><span class="name">${escapeHtml(contact.candidate_name || '—')}</span><span class="sub">${escapeHtml(contact.account_label || contact.account_id || '—')}</span></td><td>${escapeHtml(contact.country || '—')}</td><td>${tag(contact.status || 'sent')}</td><td>${profile}</td><td>${escapeHtml(contact.category || '—')}</td><td>${escapeHtml(formatDate(when))}</td></tr>`;
}
function renderLedger() {
  renderLedgerChips();
  const L = state.ledger;
  $('ledger-rows').innerHTML = L.items.length ? L.items.map(ledgerRow).join('') : `<tr><td colspan="6" class="empty">${L.busy ? 'Loading…' : 'No ledger rows match.'}</td></tr>`;
  const page = Math.floor(L.offset / L.pageSize) + 1;
  const pages = Math.max(1, Math.ceil((L.total || 0) / L.pageSize));
  $('ledger-pager-info').textContent = `${Number(L.total || 0).toLocaleString()} member${L.total === 1 ? '' : 's'} · page ${page} of ${pages}`;
  $('ledger-prev').disabled = L.offset <= 0 || L.busy;
  $('ledger-next').disabled = L.offset + L.pageSize >= L.total || L.busy;
  $('ledger-rows').querySelectorAll('tr[data-slug]').forEach((row) => {
    row.onclick = () => openLedgerDetail(row.dataset.slug);
  });
}
async function loadLedger() {
  const L = state.ledger;
  L.busy = true; renderLedger();
  const params = new URLSearchParams({offset: String(L.offset), limit: String(L.pageSize)});
  if (L.status) params.set('status', L.status);
  if (L.query) params.set('q', L.query);
  try {
    const data = await api(`/api/admin/ledger?${params}`);
    L.items = data.contacts || [];
    L.total = data.total || 0;
    $('ledger-note').textContent = `Shared Supabase ledger · ${Number(L.total).toLocaleString()} stored member${L.total === 1 ? '' : 's'}. Click a row for details.`;
  } catch (error) {
    L.items = []; L.total = 0;
    $('ledger-note').textContent = `Could not load ledger: ${errorText(error)}`;
  }
  L.busy = false; renderLedger();
}
async function openLedgerDetail(slug) {
  if (!slug) return;
  state.mem.openId = null;
  $('detail').hidden = false; $('scrim').hidden = false;
  $('detail-name').textContent = 'Loading…'; $('detail-sub').textContent = slug; $('detail-body').innerHTML = '';
  try {
    const data = await api(`/api/admin/ledger/${encodeURIComponent(slug)}`);
    const c = data.contact || {};
    const local = data.local || [];
    $('detail-name').textContent = c.candidate_name || slug;
    $('detail-sub').textContent = [c.country, c.status, c.account_label || c.account_id].filter(Boolean).join(' · ');
    const profile = c.profile_url ? `<a href="${escapeHtml(c.profile_url)}" target="_blank" rel="noopener">${escapeHtml(c.profile_url)}</a>` : '—';
    const stack = Array.isArray(c.stack) ? c.stack.join(', ') : (typeof c.stack === 'string' ? c.stack : '');
    const localHtml = local.length
      ? `<div class="table-wrap"><table class="plain"><thead><tr><th>Local profile</th><th>Country</th><th>Role</th><th>Sent</th><th>Replies</th></tr></thead><tbody>${local.map((row) => `<tr><td>${escapeHtml(row.account_label || row.account_id || '—')}</td><td>${escapeHtml(row.country || '—')}</td><td>${escapeHtml(row.suggested_role || '—')}</td><td>${row.sent || 0}</td><td>${row.replies || 0}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="muted">No matching local member rows on this server (they may have been cleaned after uninstall).</p>';
    $('detail-body').innerHTML =
      `<dl class="facts"><dt>Slug</dt><dd>${escapeHtml(c.talent_slug || slug)}</dd><dt>Country</dt><dd>${escapeHtml(c.country || '—')}</dd><dt>Status</dt><dd>${tag(c.status || 'sent')}</dd><dt>Suggested role</dt><dd>${escapeHtml(c.category || '—')}</dd><dt>Profile</dt><dd>${profile}</dd><dt>Account</dt><dd>${escapeHtml(c.account_label || c.account_id || '—')}</dd><dt>Sent at</dt><dd>${escapeHtml(formatDate(c.sent_at))}</dd><dt>Updated</dt><dd>${escapeHtml(formatDate(c.updated_at || c.created_at))}</dd>${c.error ? `<dt>Error</dt><dd class="err">${escapeHtml(c.error)}</dd>` : ''}${stack ? `<dt>Stack</dt><dd>${escapeHtml(stack)}</dd>` : ''}</dl>` +
      (c.message_body ? `<div><p class="group">LEDGER MESSAGE</p><div class="profile">${escapeHtml(c.message_body.slice(0, 2000))}</div></div>` : '') +
      (c.summary ? `<div><p class="group">SUMMARY</p><div class="profile">${escapeHtml(String(c.summary).slice(0, 1500))}</div></div>` : '') +
      `<div><p class="group">LOCAL COPIES ON THIS SERVER</p>${localHtml}</div>`;
  } catch (error) {
    $('detail-name').textContent = 'Could not load ledger member';
    $('detail-sub').textContent = errorText(error);
  }
}
$('ledger-refresh').onclick = () => loadLedger();
$('ledger-prev').onclick = () => { state.ledger.offset = Math.max(0, state.ledger.offset - state.ledger.pageSize); loadLedger(); };
$('ledger-next').onclick = () => { state.ledger.offset += state.ledger.pageSize; loadLedger(); };
let ledgerSearchTimer = null;
$('ledger-search').oninput = () => {
  clearTimeout(ledgerSearchTimer);
  ledgerSearchTimer = setTimeout(() => { state.ledger.query = $('ledger-search').value.trim(); state.ledger.offset = 0; loadLedger(); }, 250);
};


// ---------- playbook (.md) ----------
const pbState = {checked: null};
function pbSummary(status) {
  if (status.mode === 'prompt') {
    const p = status.prompts || {}, size = (key) => (p[key] ? `${p[key].toLocaleString()} characters` : 'not set');
    const source = status.is_default ? 'Built-in' : !status.imported_at ? 'Not applied yet' : `Imported from ${escapeHtml(status.filename || 'a file')} · ${escapeHtml(formatDate(status.imported_at))}`;
    return `<dl class="facts"><dt>Playbook</dt><dd><b>${escapeHtml(status.name)}</b> · ${source}</dd><dt>Mode</dt><dd>Prompts: the AI writes message 1 and every reply</dd><dt>Platform</dt><dd>${escapeHtml(status.platform || '—')}</dd><dt>Company and roles</dt><dd>${size('knowledge')}</dd><dt>First message prompt</dt><dd>${size('first')}</dd><dt>Chat logic prompt</dt><dd>${size('chat')}</dd><dt>Style rules</dt><dd>${size('style')}</dd></dl>`;
  }
  const roles = status.roles.map((role) => `<tr><td>${escapeHtml(role.name)}</td><td>${role.type === 'developer' ? 'Developer (assessment)' : 'Business (application link)'}</td><td>${escapeHtml(role.rate || '—')}</td></tr>`).join('');
  const source = status.is_default ? 'Built-in' : !status.imported_at ? 'Not applied yet' : `Imported from ${escapeHtml(status.filename || 'a file')} · ${escapeHtml(formatDate(status.imported_at))}`;
  return `<dl class="facts"><dt>Playbook</dt><dd><b>${escapeHtml(status.name)}</b> · ${source}</dd><dt>Platform</dt><dd>${escapeHtml(status.platform)}</dd><dt>Company</dt><dd>${escapeHtml(status.company)} · ${escapeHtml(status.website)}</dd><dt>Roles</dt><dd>${status.roles.length}</dd></dl>` +
    `<div class="table-wrap"><table class="plain"><thead><tr><th>Role</th><th>Type</th><th>Rate text</th></tr></thead><tbody>${roles}</tbody></table></div>`;
}
async function loadPlaybook() {
  try { $('pb-active').innerHTML = pbSummary(await api('/api/admin/playbook')); }
  catch (error) { $('pb-active').innerHTML = `<p class="err">Could not load: ${escapeHtml(errorText(error))}</p>`; }
}
function saveFile(url, name) { const link = document.createElement('a'); link.href = url; link.download = name; link.click(); }
$('pb-download').onclick = () => saveFile('/api/admin/playbook/download', 'playbook.md');
$('pb-template').onclick = () => saveFile('/api/admin/playbook/download?builtin=true', 'playbook-fixed-text-template.md');
$('pb-template-prompts').onclick = () => saveFile('/api/admin/playbook/download?kind=prompts', 'playbook-prompts-template.md');
$('pb-reset').onclick = async () => {
  if (!confirm('Go back to the built-in playbook (Ocean Park Asset on Himalayas)? The imported file is removed. Queued messages keep their wording.')) return;
  try { await api('/api/admin/playbook', {method: 'DELETE'}); toast('The built-in playbook is active.'); pbState.checked = null; $('pb-report').innerHTML = ''; $('pb-apply').disabled = true; await loadPlaybook(); }
  catch (error) { toast(`Could not reset: ${errorText(error)}`, true); }
};
$('pb-file').onchange = async () => {
  const file = $('pb-file').files[0];
  if (!file) return;
  if (file.size > 200000) { toast('That file is too large for a playbook.', true); return; }
  $('pb-text').value = await file.text(); $('pb-text').dataset.filename = file.name;
  pbState.checked = null; $('pb-apply').disabled = true; $('pb-report').innerHTML = '';
  await checkPlaybook();
};
$('pb-text').oninput = () => { pbState.checked = null; $('pb-apply').disabled = true; };
function reportHtml(report) {
  const list = (items, cls, title) => (items.length ? `<div class="pb-list ${cls}"><b>${title}</b><ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></div>` : '');
  let html = report.ok ? '<p class="pb-ok">The file is valid.</p>' : '<p class="pb-bad">The file has errors. Fix them and check again.</p>';
  html += list(report.errors, 'bad', 'Errors (must fix)') + list(report.warnings, 'warn', 'Warnings') + list(report.notes, 'info', 'Notes');
  if (report.ok) {
    html += `<h3>What will be used</h3>${pbSummary({...report.summary, is_default: false, filename: $('pb-text').dataset.filename, imported_at: null})}`;
    html += '<h3>Sample messages (fixed parts only)</h3>' + report.samples.map((sample) => `<details class="pb-sample"><summary>${escapeHtml(sample.role)}</summary>${sample.steps.map((step) => `<p class="group">${escapeHtml(step.step)}</p><div class="bubble outbound">${escapeHtml(step.text)}</div>`).join('')}</details>`).join('');
  }
  return html;
}
async function checkPlaybook() {
  const text = $('pb-text').value;
  if (!text.trim()) { toast('Choose a file or paste the playbook text first.', true); return; }
  $('pb-check').disabled = true; $('pb-report').innerHTML = '<p class="muted">Checking…</p>';
  try {
    const report = await api('/api/admin/playbook/check', {method: 'POST', body: JSON.stringify({md: text})});
    $('pb-report').innerHTML = reportHtml(report);
    pbState.checked = report.ok ? text : null; $('pb-apply').disabled = !report.ok;
    $('pb-try').hidden = !(report.ok && report.summary && report.summary.mode === 'prompt'); $('pb-try-out').innerHTML = '';
  } catch (error) { $('pb-report').innerHTML = `<p class="pb-bad">Could not check: ${escapeHtml(errorText(error))}</p>`; }
  $('pb-check').disabled = false;
}
$('pb-check').onclick = checkPlaybook;
$('pb-apply').onclick = async () => {
  if (pbState.checked === null || pbState.checked !== $('pb-text').value) { toast('Check the file first.', true); return; }
  const status = await api('/api/admin/playbook').catch(() => null);
  const queued = status && status.queued_messages ? ` ${plural(status.queued_messages, 'message')} already in the queue keep their old wording.` : '';
  if (!confirm(`Use this playbook for every profile from now on?${queued}`)) return;
  $('pb-apply').disabled = true;
  try {
    await api('/api/admin/playbook', {method: 'PUT', body: JSON.stringify({md: pbState.checked, filename: $('pb-text').dataset.filename || 'pasted text'})});
    toast('The playbook is active. New messages use it now.'); await loadPlaybook(); await loadOverview();
  } catch (error) { toast(`Not applied: ${errorText(error)}`, true); $('pb-apply').disabled = false; }
};

$('pb-try-run').onclick = async () => {
  const button = $('pb-try-run'), out = $('pb-try-out');
  button.disabled = true; out.innerHTML = '<p class="muted">Writing…</p>';
  try {
    const r = await api('/api/admin/playbook/try', {method: 'POST', body: JSON.stringify({md: $('pb-text').value, profile: $('pb-profile').value, reply: $('pb-reply').value})});
    const bubble = (who, text) => `<p class="group">${who}</p><div class="bubble outbound">${escapeHtml(text)}</div>`;
    let html = bubble(`Message 1 · role: ${escapeHtml(r.role)}`, r.first_message);
    if (r.reply) {
      const d = r.reply, action = {reply: 'Reply', no_reply: 'No reply (stays silent)', close: 'Close the chat', invite_github: `Send a GitHub invitation to ${d.github_username || '?'}`}[d.action] || d.action;
      html += `<p class="group">Next step · action: ${escapeHtml(action)}</p>` + (d.message ? `<div class="bubble outbound">${escapeHtml(d.message)}</div>` : '');
    }
    out.innerHTML = html;
  } catch (error) { out.innerHTML = `<p class="pb-bad">${escapeHtml(errorText(error))}</p>`; }
  button.disabled = false;
};

// Log out is shown when the dashboard is opened from another machine (a login is needed there).
if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) {
  $('logout').hidden = false;
  $('logout').onclick = async () => { try { await fetch('/api/logout', {method: 'POST'}); } finally { location.href = '/login'; } };
}
