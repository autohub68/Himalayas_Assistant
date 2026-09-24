// Full-page database dashboard: table structure, every reached member with status, member details, and cleanup.
const API = 'http://localhost:8765';
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));

// Each Chrome profile has its own account id (minted only by the service worker).
const accountReady = new Promise((resolve) => {
  try {
    chrome.runtime.sendMessage({type: 'getAccountId'}, (response) => {
      if (response && response.accountId) return resolve(response.accountId);
      chrome.storage.local.get('accountId', (stored) => resolve(stored && stored.accountId ? stored.accountId : ''));
    });
  } catch (error) { resolve(''); }
});
async function request(path, options = {}) {
  const accountId = await accountReady;
  if (!accountId) throw new Error('Open the extension popup once so this profile gets an account.');
  const response = await fetch(`${API}${path}`, {...options, headers: {'Content-Type': 'application/json', 'X-Account-Id': accountId, ...(options.headers || {})}});
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
const errorText = (error) => { try { return JSON.parse(error.message).detail || error.message; } catch (parseError) { return error.message; } };

const STATUS_LABELS = {sent: 'Sent', failed: 'Failed', scheduled: 'Queued', approved: 'Approved', queued: 'Queued', skipped: 'Skipped', received: 'Received'};
const STAGE_LABELS = {first_sent: 'Message 1 sent', intro_sent: 'Introduced', process_sent: 'Process explained', assessment_sent: 'Assessment sent', invite_pending: 'Invite pending', invited: 'GitHub invited', apply_sent: 'Application link sent', closed: 'Closed'};
const statusLabel = (status) => STATUS_LABELS[status] || status;
const stageLabel = (stage) => (stage ? (STAGE_LABELS[stage] || (/^step_(\d+)$/.test(stage) ? `Message ${stage.slice(5)} sent` : stage)) : 'Not sent yet');
const formatDate = (value) => (value ? new Date(value).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '—');
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const tag = (status, label) => `<span class="tag ${escapeHtml(status)}">${escapeHtml(label || statusLabel(status))}</span>`;

const state = {members: [], summary: {}, matched: 0, status: '', query: '', sortKey: 'last_activity', sortDir: 'desc', page: 0, pageSize: 25, openId: null};

// ---------- structure ----------
function renderTable(table, showRows = true) {
  const columns = table.columns.map((column) => `<tr><td>${escapeHtml(column.name)}</td><td>${escapeHtml(column.type)}</td><td>${column.primary_key ? '<span class="badge key">KEY</span>' : ''}${column.required ? '<span class="badge">REQUIRED</span>' : ''}</td></tr>`).join('');
  const rows = showRows && table.rows !== undefined ? `<span class="rows-badge">${table.rows} rows</span>` : '';
  return `<details class="table-card"><summary><span>${escapeHtml(table.name)}</span>${rows}</summary><p class="desc">${escapeHtml(table.description)}</p><table class="cols"><thead><tr><th>Column</th><th>Type</th><th></th></tr></thead><tbody>${columns}</tbody></table></details>`;
}
async function loadStructure() {
  const data = await request('/api/db/structure');
  $('structure-count').textContent = `${data.tables.length + 1} tables`;
  const ledger = data.ledger_status_ready === false
    ? '<p class="warning">The Supabase ledger has no status columns yet, so it only stores sent contacts. Run the updated supabase_schema.sql in the Supabase SQL Editor to store queued and failed too.</p>'
    : data.ledger_status_ready === true ? '<p class="note">Ledger statuses are active: queued, sent, failed.</p>' : '';
  $('structure').innerHTML = `<p class="group">LOCAL DATABASE (SQLITE)</p>${data.tables.map((table) => renderTable(table)).join('')}<p class="group">SHARED LEDGER (SUPABASE)</p>${ledger}${renderTable(data.external, false)}<p class="group">RELATIONSHIPS</p><ul class="links">${data.links.map((link) => `<li>${escapeHtml(link)}</li>`).join('')}</ul><p class="note">${escapeHtml(data.note)}</p>`;
}

// ---------- members ----------
function renderCards() {
  const summary = state.summary;
  const queued = (summary.queued || 0) + (summary.scheduled || 0) + (summary.approved || 0);
  const replied = state.members.filter((member) => member.replies > 0).length;
  const card = (label, value, cls = '') => `<div class="card ${cls}"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`;
  $('cards').innerHTML = card('Members reached', summary.total || 0) + card('Sent', summary.sent || 0, 'sent') + card('Failed', summary.failed || 0, 'failed') + card('Queued', queued, 'queued') + card('Skipped', summary.skipped || 0) + card('Replied', replied);
}
function renderChips() {
  const statuses = Object.keys(state.summary).filter((key) => key !== 'total');
  const chip = (status, label, count) => `<button class="chip${state.status === status ? ' active' : ''}" data-status="${escapeHtml(status)}">${escapeHtml(label)}<b>${count}</b></button>`;
  $('chips').innerHTML = chip('', 'All', state.summary.total || 0) + statuses.map((status) => chip(status, statusLabel(status), state.summary[status])).join('');
  $('chips').querySelectorAll('.chip').forEach((button) => { button.onclick = () => { state.status = button.dataset.status; state.page = 0; renderMembers(); }; });
}
function visibleMembers() {
  const query = state.query.toLowerCase();
  const items = state.members.filter((member) => (!state.status || member.outreach_status === state.status) && (!query || member.name.toLowerCase().includes(query)));
  const key = state.sortKey, direction = state.sortDir === 'asc' ? 1 : -1;
  return items.sort((a, b) => {
    const left = a[key] ?? '', right = b[key] ?? '';
    return (typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right), undefined, {sensitivity: 'base'})) * direction;
  });
}
function githubCell(member) {
  if (!member.github_username) return '<span class="sub">—</span>';
  const failed = member.github_invited_at && String(member.github_invited_at).startsWith('failed');
  const note = failed ? '<span class="sub">invite failed</span>' : member.github_invited_at ? '<span class="sub">invited</span>' : '<span class="sub">not invited</span>';
  return `${escapeHtml(member.github_username)}<br>${note}`;
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
  return `<tr data-id="${member.id}"><td><span class="name">${escapeHtml(member.name)}</span><span class="sub">${escapeHtml(member.category === 'developer' ? 'Developer' : 'Business')}</span>${problem ? `<span class="err">${escapeHtml(problem)}</span>` : ''}</td>` +
    `<td>${escapeHtml(member.role || '—')}</td><td>${tag(member.outreach_status)}${followUpFailed ? ' ' + tag('failed', 'Reply failed') : ''}</td><td>${himalayasCell(member)}</td>` +
    `<td>${escapeHtml(stageLabel(member.stage))}</td><td class="num">${member.replies}</td><td>${githubCell(member)}</td><td>${escapeHtml(formatDate(member.last_activity))}</td>` +
    `<td>${retryId ? `<button class="retry" data-retry="${retryId}">Retry</button>` : ''}</td></tr>`;
}
function renderMembers() {
  renderChips();
  const items = visibleMembers();
  const pages = Math.max(1, Math.ceil(items.length / state.pageSize));
  state.page = Math.min(state.page, pages - 1);
  const slice = items.slice(state.page * state.pageSize, (state.page + 1) * state.pageSize);
  $('rows').innerHTML = slice.length ? slice.map(rowHtml).join('') : `<tr><td colspan="9" class="empty">${state.members.length ? 'No members match this filter.' : 'No members yet. Members appear here after their first message is queued.'}</td></tr>`;
  document.querySelectorAll('#members th[data-sort]').forEach((header) => { header.classList.toggle('sorted', header.dataset.sort === state.sortKey); header.classList.toggle('desc', header.dataset.sort === state.sortKey && state.sortDir === 'desc'); });
  $('pager-info').textContent = `${plural(items.length, 'member')} · page ${state.page + 1} of ${pages}${state.matched > state.members.length ? ` · showing the newest ${state.members.length} of ${state.matched}` : ''}`;
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page >= pages - 1;
  $('rows').querySelectorAll('tr[data-id]').forEach((row) => { row.onclick = () => openDetail(Number(row.dataset.id)); });
  $('rows').querySelectorAll('.retry').forEach((button) => { button.onclick = (event) => { event.stopPropagation(); retryMessage(button); }; });
}
async function retryMessage(button) {
  button.disabled = true; button.textContent = 'Queued';
  try { await request(`/api/messages/${button.dataset.retry}/retry`, {method: 'POST'}); await loadMembers(); if (state.openId) await openDetail(state.openId, true); }
  catch (error) { button.textContent = 'Retry'; button.disabled = false; }
}
async function loadMembers() {
  const data = await request('/api/db/members?limit=1000');
  state.members = data.members; state.summary = data.summary; state.matched = data.matched;
  renderCards();
  renderMembers();
}

// ---------- member details ----------
function closeDetail() { state.openId = null; $('detail').hidden = true; $('scrim').hidden = true; }
function inviteText(candidate) {
  if (!candidate.github_username) return '—';
  if (!candidate.github_invited_at) return `${candidate.github_username} (not invited yet)`;
  if (String(candidate.github_invited_at).startsWith('failed')) return `${candidate.github_username} — ${candidate.github_invited_at}`;
  return `${candidate.github_username} (invited ${formatDate(candidate.github_invited_at)})`;
}
function bubbleHtml(message) {
  const outbound = message.direction === 'outbound';
  const when = formatDate(message.sent_at || message.send_after || message.created_at);
  const retry = outbound && message.status === 'failed' ? `<button class="retry" data-retry="${message.id}">Retry</button>` : '';
  const due = outbound && ['scheduled', 'approved'].includes(message.status) && message.send_after ? `<span>sends ${escapeHtml(formatDate(message.send_after))}</span>` : `<span>${escapeHtml(when)}</span>`;
  return `<div class="bubble ${outbound ? 'outbound' : 'inbound'}"><div class="who">${outbound ? 'Us' : 'Member'}</div>${escapeHtml(message.body)}<div class="meta">${tag(message.status)}${due}${message.error ? `<span class="err">${escapeHtml(message.error)}</span>` : ''}${retry}</div></div>`;
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
    const live = await request(`/api/conversations/${id}/himalayas`);
    if (state.openId !== id) return;
    if (liveMatches(live, local)) return;  // the same as the bot's record: nothing to show. The status under each message is enough
    document.getElementById('live-section').hidden = false;
    box.innerHTML = '<p class="note">Himalayas shows something different from the bot\'s record:</p>' + ([...live.messages].reverse().map(liveBubble).join('') || '<p class="muted">Himalayas holds no conversation with this member.</p>') + `<p class="note">Room: ${escapeHtml(live.room || '')}</p>`;
  } catch (error) { /* a failed live check is not shown: it says nothing about this member */ }
}
async function openDetail(id, keepOpen = false) {
  state.openId = id;
  $('detail').hidden = false; $('scrim').hidden = false;
  if (!keepOpen) { $('detail-name').textContent = 'Loading...'; $('detail-sub').textContent = ''; $('detail-body').innerHTML = ''; }
  try {
    const data = await request(`/api/conversations/${id}`);
    const c = data.candidate;
    $('detail-name').textContent = c.name;
    $('detail-sub').textContent = `${c.suggested_role || 'No role yet'} · ${stageLabel(c.stage)}`;
    const profile = c.profile_url ? `<a href="${escapeHtml(c.profile_url)}" target="_blank" rel="noopener">${escapeHtml(c.profile_url)}</a>` : '—';
    $('detail-body').innerHTML =
      `<dl class="facts"><dt>Category</dt><dd>${escapeHtml(c.category === 'developer' ? 'Developer' : 'Business')}</dd><dt>Profile</dt><dd>${profile}</dd><dt>GitHub</dt><dd>${escapeHtml(inviteText(c))}</dd>${c.github_email ? `<dt>Email</dt><dd>${escapeHtml(c.github_email)}</dd>` : ''}<dt>Messages</dt><dd>${data.messages.length}</dd></dl>` +
      (c.summary ? `<div><p class="group">PROFILE</p><div class="profile">${escapeHtml(c.summary.slice(0, 1500))}</div></div>` : '') +
      `<div><p class="group">CONVERSATION</p><div class="thread">${data.messages.length ? data.messages.map(bubbleHtml).join('') : '<p class="muted">No messages.</p>'}</div></div>` +
      `<div id="live-section" hidden><p class="group">CHECK ON HIMALAYAS (LIVE)</p><div id="live-thread" class="thread"></div></div>`;
    loadLive(id, data.messages);
    $('detail-body').querySelectorAll('.retry').forEach((button) => { button.onclick = () => retryMessage(button); });
  } catch (error) { $('detail-name').textContent = 'Could not load this member'; $('detail-sub').textContent = errorText(error); }
}
$('detail-close').onclick = closeDetail;
$('scrim').onclick = closeDetail;
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });

// ---------- controls ----------
document.querySelectorAll('#members th[data-sort]').forEach((header) => {
  header.onclick = () => { const key = header.dataset.sort; state.sortDir = state.sortKey === key && state.sortDir === 'asc' ? 'desc' : 'asc'; state.sortKey = key; state.page = 0; renderMembers(); };
});
let searchTimer = null;
$('search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.query = $('search').value.trim(); state.page = 0; renderMembers(); }, 200); };
$('prev').onclick = () => { state.page -= 1; renderMembers(); };
$('next').onclick = () => { state.page += 1; renderMembers(); };

function csvCell(value) { const text = String(value ?? ''); return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text; }
function membersCsv(items) {
  const header = ['name', 'suggested_role', 'category', 'outreach_status', 'chat_step', 'replies', 'github_username', 'last_activity', 'error'];
  const lines = items.map((m) => [m.name, m.role, m.category, m.outreach_status, stageLabel(m.stage), m.replies, m.github_username, m.last_activity, m.outreach_error || m.failed_error].map(csvCell).join(','));
  return [header.join(','), ...lines].join('\n');
}
$('export').onclick = () => {
  const url = URL.createObjectURL(new Blob([membersCsv(visibleMembers())], {type: 'text/csv'}));
  const link = document.createElement('a');
  link.href = url; link.download = `members-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$('verify').onclick = async () => {
  const button = $('verify'), out = $('verify-result');
  button.disabled = true; out.textContent = 'Reading every conversation from Himalayas… this can take a minute.';
  try {
    const r = await request('/api/db/verify-himalayas', {method: 'POST'});
    out.textContent = `Himalayas holds ${r.conversations_on_himalayas} conversations. ${r.match} of ${r.checked} members match this bot.` + (r.missing_on_himalayas.length ? ` ${r.missing_on_himalayas.length} have a message the bot recorded but Himalayas does not show.` : '') + (r.replies_imported ? ` ${r.replies_imported} missed repl${r.replies_imported === 1 ? 'y was' : 'ies were'} added.` : '');
    await loadMembers();
  } catch (error) { out.textContent = `Could not check: ${errorText(error)}`; }
  button.disabled = false;
};
$('report').onclick = () => {
  const url = URL.createObjectURL(new Blob([reportCsv(state.members)], {type: 'text/csv'}));
  const link = document.createElement('a');
  link.href = url; link.download = `himalayas-report-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

// ---------- clean up ----------
function describeSelection(data, done = false) {
  const queued = data.queued_cancelled ? ` ${plural(data.queued_cancelled, 'queued message')} ${done ? 'were' : 'will be'} cancelled.` : '';
  return `${plural(data.members, 'member')} and ${plural(data.messages, 'message')}.${queued}`;
}
function localDayStart(value, addDays = 0) {
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day + addDays).toISOString();
}
function cleanRange() {
  const from = $('clean-from').value, to = $('clean-to').value;
  return {mode: 'range', from_time: from ? localDayStart(from) : null, before_time: to ? localDayStart(to, 1) : null};
}
function resetCleanPreview() { $('clean-summary').hidden = true; $('clean-confirm').hidden = true; }
async function loadCleanup() {
  const data = await request('/api/db/clean', {method: 'POST', body: JSON.stringify({mode: 'all', dry_run: true})});
  $('clean-current').textContent = `This profile currently holds ${describeSelection(data)}`;
}
$('clean-from').oninput = resetCleanPreview;
$('clean-to').oninput = resetCleanPreview;
$('clean-preview').onclick = async () => {
  $('clean-result').textContent = '';
  try {
    const data = await request('/api/db/clean', {method: 'POST', body: JSON.stringify({...cleanRange(), dry_run: true})});
    $('clean-summary').hidden = false;
    $('clean-summary').textContent = data.members ? `Will delete ${describeSelection(data)}` : 'No members match these dates.';
    $('clean-confirm').hidden = !data.members;
  } catch (error) { $('clean-summary').hidden = false; $('clean-summary').textContent = errorText(error); $('clean-confirm').hidden = true; }
};
async function runCleanup(body, button) {
  button.disabled = true; $('clean-result').textContent = 'Deleting...';
  try {
    const data = await request('/api/db/clean', {method: 'POST', body: JSON.stringify({...body, dry_run: false})});
    $('clean-result').textContent = data.members ? `Deleted ${describeSelection(data, true)} Backup saved as ${data.backup}.` : 'Nothing to delete.';
    resetCleanPreview(); $('clean-all-text').value = '';
    closeDetail();
    await refreshAll();
  } catch (error) { $('clean-result').textContent = `Could not delete: ${errorText(error)}`; }
  finally { button.disabled = button.id === 'clean-all'; }
}
$('clean-confirm').onclick = () => runCleanup(cleanRange(), $('clean-confirm'));
$('clean-all-text').oninput = () => { $('clean-all').disabled = $('clean-all-text').value !== 'DELETE'; };
$('clean-all').onclick = () => runCleanup({mode: 'all', confirm: 'DELETE'}, $('clean-all'));

// ---------- load ----------
async function refreshAll() {
  try {
    const [account] = await Promise.all([request('/api/account'), loadStructure(), loadMembers(), loadCleanup()]);
    $('account-name').textContent = account.label ? `Profile: ${account.label}` : '';
    $('offline').hidden = true;
    $('updated').textContent = `Updated ${new Date().toLocaleTimeString([], {timeStyle: 'short'})}`;
  } catch (error) {
    $('offline').hidden = false;
    $('offline').textContent = /Failed to fetch|NetworkError/i.test(error.message) ? 'The server is not reachable. Start it from the extension popup (Start server), then press Refresh.' : errorText(error);
  }
}
$('refresh').onclick = refreshAll;
refreshAll();
setInterval(() => { if (!document.hidden) loadMembers().then(() => { $('updated').textContent = `Updated ${new Date().toLocaleTimeString([], {timeStyle: 'short'})}`; }).catch(() => {}); }, 15000);
