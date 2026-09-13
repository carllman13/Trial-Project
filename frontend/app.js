/* Rendering and browser interactions only. See README.md for the adapter contract. */
(() => {
  'use strict';
  const app = document.querySelector('#app');
  const adapter = window.OutlookDigestAdapter;
  let data = window.OutlookDigestSample;
  const state = { tab: 'folders', folder: 'Humain', checked: new Set(data.folders.slice(0, 7)), selected: new Set(), chain: 'chain-1', message: 'message-1', chains: [], messages: [], filter: '', results: data.messages, raw: true, detail: true, showSystem: false };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const date = value => new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value)).replace(',', ' ·');
  const button = (label, action, extra = '', primary = false) => `<button class="${primary ? 'primary' : ''}" data-action="${action}" ${extra}>${label}</button>`;
  let noticeTimer;
  function notice(message) { const el = document.querySelector('#notice'); el.textContent = message; el.hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { el.hidden = true; }, 6000); }
  async function callBackend(method, payload) {
    if (!adapter?.[method]) { notice(`${method}: backend is not connected. No Outlook or database changes were made.`); return; }
    try { return await adapter[method](payload); } catch (error) { notice(`Operation failed: ${error.message}`); }
  }
  async function copy(text) {
    if (!text) return notice('Nothing is queued or selected.');
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else { const box = document.createElement('textarea'); box.value = text; box.style.position = 'fixed'; box.style.opacity = '0'; document.body.append(box); box.select(); const ok = document.execCommand('copy'); box.remove(); if (!ok) throw new Error('Clipboard unavailable'); }
      notice('Copied to clipboard.');
    } catch { notice('Clipboard access is unavailable. Select and copy the text manually.'); }
  }
  const chainRows = () => data.chains.filter(c => c.folder === state.folder && `${c.subject} ${c.from} ${c.to}`.toLowerCase().includes(state.filter.toLowerCase()));
  const queueText = kind => state[kind].map(id => data[kind].find(x => x.id === id)).filter(Boolean).map(x => `Subject: ${x.subject}\n\n${x.body}`).join('\n\n' + '='.repeat(80) + '\n\n');
  function queue(kind) {
    return `<div class="queue-box">Queued ${kind}: ${state[kind].length} ${button('Copy queued', 'copy-queue', `data-kind="${kind}"`)}<div class="queue-items">${state[kind].map(id => { const item = data[kind].find(x => x.id === id); return `<span class="chip" title="${esc(item?.subject)}">${esc(item?.subject)} ${button('×', 'remove-queue', `data-kind="${kind}" data-id="${esc(id)}" aria-label="Remove queued item"`)}</span>`; }).join('')}</div>${button('Clear', 'clear-queue', `data-kind="${kind}"`)}</div>`;
  }
  function prompts() {
    return `<section class="prompts"><div class="prompts-heading"><strong>Prompts</strong><span>Click Copy to copy prompt + queued chains.</span></div><div class="prompts-list">${data.prompts.map((p, i) => `<div class="prompt-row">${button('Copy', 'copy-prompt', `data-index="${i}"`)}<span class="triangle">▸</span><div class="prompt-fields"><input aria-label="Prompt title" data-prompt="${i}" data-field="title" value="${esc(p.title)}"><textarea aria-label="Prompt text" data-prompt="${i}" data-field="text">${esc(p.text)}</textarea></div>${button('×', 'remove-prompt', `data-index="${i}" aria-label="Remove prompt"`)}</div>`).join('')}</div></section>`;
  }
  function table(rows, kind) {
    const chains = kind === 'chains';
    const cols = chains ? [['count', '#', '4%'], ['folder', 'Folder', '22%'], ['received', 'Last reply (London)', '14%'], ['subject', 'Subject', '26%'], ['from', 'From', '17%'], ['to', 'To', '17%']] : [['received', 'Received (London) ▼', '14%'], ['from', 'From ↕', '15%'], ['to', 'To ↕', '15%'], ['subject', 'Subject ↕', '32%'], ['folder', 'Folder ↕', '22%'], ['attachments', 'Att ↕', '4%']];
    return `<div class="table-scroll"><table aria-label="${kind}"><colgroup>${cols.map(c => `<col style="width:${c[2]}">`).join('')}</colgroup><thead><tr>${cols.map(c => `<th scope="col" tabindex="0" data-sort="${c[0]}" data-kind="${kind}">${c[1]}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr draggable="true" tabindex="0" data-row="${esc(row.id)}" data-kind="${kind}" class="${state.selected.has(row.id) || (state.selected.size === 0 && (chains ? state.chain : state.message) === row.id) ? 'selected' : ''}" aria-selected="${state.selected.has(row.id)}">${cols.map(([key]) => `<td title="${esc(row[key])}" class="${key === 'count' || key === 'attachments' ? 'number' : ''}">${esc(key === 'received' ? date(row[key]) : row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table>${rows.length ? '' : '<div class="empty">No results</div>'}</div>`;
  }
  function refreshBox(title, mode, content) { return `<div class="refresh-box">${title}${content}${button('Refresh', 'refresh', `data-mode="${mode}"`, true)}</div>`; }
  function folders() {
    const chain = data.chains.find(c => c.id === state.chain);
    return `<div class="folders-layout"><aside class="sidebar"><section class="panel folder-panel"><div class="section-title">Folders ${button('Refresh folder structure', 'refresh-folders')}</div><label class="system-toggle"><input type="checkbox" id="show-system" ${state.showSystem ? 'checked' : ''}>Show system folders</label><div class="tree"><div class="tree-row"><span class="handle">≡</span>⌄ □ ${esc(data.account)}</div><div class="tree-row" style="padding-left:14px">⌄ □ Inbox</div>${data.folders.filter(f => state.showSystem || !['Sent Items', 'Conversation History'].includes(f)).map(f => `<div class="tree-row ${f === state.folder ? 'active' : ''}" style="padding-left:14px"><span class="handle">≡</span><span class="twisty">⌄</span><input aria-label="Scan ${esc(f)}" type="checkbox" data-folder-check="${esc(f)}" ${state.checked.has(f) ? 'checked' : ''}><button class="tree-name" data-folder="${esc(f)}">${esc(f)}</button></div>`).join('')}</div></section><section class="panel"><div class="section-title">Refresh</div>${refreshBox('Cutoff', 'cutoff', '<div><input id="days" type="number" min="0" value="0" aria-label="Cutoff days">days <input id="hours" type="number" min="0" value="24" aria-label="Cutoff hours">hours</div><div><input id="minutes" type="number" min="0" value="0" aria-label="Cutoff minutes">minutes</div>')}${refreshBox('Refresh from last update time', 'last', `<div class="muted">Earliest refresh among selected: ${esc(data.lastSynced)}<br>London</div>`)}${refreshBox('Refresh from start', 'start', '<div class="muted">Scans every selected (sub)folder in full — no cutoff.</div>')}<div class="refresh-note">${adapter ? 'Backend adapter supplied.' : 'Preview data — Outlook is not connected.'}</div></section></aside><aside class="panel queue-column"><div class="dropzone" data-drop="chains"><strong>Drop chains here</strong></div>${queue('chains')}</aside><section class="panel"><div class="panel-heading"><h2>${esc(state.folder)}</h2><span class="muted">${esc(data.account)} - ${esc(state.folder)}</span>${button('Copy all chains', 'copy-all', '', true)}</div><input class="chain-filter" id="chain-filter" placeholder="Filter chains…" aria-label="Filter chains" value="${esc(state.filter)}">${table(chainRows(), 'chains')}</section><aside class="panel detail-panel"><div class="panel-heading"><h2>${esc(chain?.subject ?? 'Select a chain')}</h2>${button('Copy to clipboard', 'copy-chain', '', true)}</div><pre class="reader">${esc(chain?.body)}</pre>${prompts()}</aside></div>`;
  }
  function filterField(label, name, placeholder = '', value = '', type = 'text', wide = false) { return `<label class="filter-field ${wide ? 'wide' : ''}">${label}<input name="${name}" type="${type}" placeholder="${placeholder}" value="${value}"></label>`; }
  function database() {
    const message = data.messages.find(m => m.id === state.message);
    return `<section class="panel filter-panel"><form class="filter-form" id="query-form"><label class="filter-field">Folder<select name="folder"><option value="">(all)</option>${data.folders.map(f => `<option>${esc(f)}</option>`).join('')}</select></label>${filterField('From date', 'fromDate', '', '2026-09-09', 'date')}${filterField('To date', 'toDate', '', '2026-09-13', 'date')}${filterField('Last N days', 'days', '', '4', 'number')}${filterField('Sent by', 'from', 'Pick or type a sender…', '', 'text', true)}<label class="filter-field wide">Sent to<input name="to" placeholder="Pick or type a To recipient"><span class="cc-option"><input name="includeCC" type="checkbox">or CC’d</span></label>${filterField('CC’d to', 'cc', 'Pick or type a CC recipient', '', 'text', true)}${filterField('Subject contains', 'subject', 'e.g. Universe', '', 'text', true)}${filterField('Body contains', 'body', 'e.g. follow up', '', 'text', true)}<label class="filter-field limit">Limit<input name="limit" type="number" min="1" value="500"></label><label class="evenings"><input name="evenings" type="checkbox">Evenings (18:00–22:00 UK)</label><div class="filter-buttons"><button class="primary" type="submit">Query</button><button type="reset">Clear</button></div></form><div class="filter-status" id="query-status">${state.results.length} row(s) returned.${adapter ? '' : ' (Preview data)'}</div></section><div class="database-layout ${state.detail ? '' : 'no-detail'}"><aside class="panel"><div class="section-title">Drop for messages</div><div class="drop-caption">Queues each dragged message individually.</div><div class="dropzone" data-drop="messages"><strong>Single or Multi MSG Drop</strong><span>Drag one or more selected rows from<br>the table on the right.</span></div>${queue('messages')}<div class="section-title">Drop for chains</div><div class="drop-caption">Queues the full chain containing each dragged message.</div><div class="dropzone chains" data-drop="chains"><strong>Full Latest Chain Drop</strong><span>Drag one or more selected rows;<br>chips are labelled «count | subject».</span></div>${queue('chains')}</aside><section class="panel messages-panel"><div class="panel-heading"><h2>Messages</h2><span class="muted">${state.results.length} row(s)</span>${button('Chain & copy results', 'copy-results', '', true)}</div>${table(state.results, 'messages')}</section>${state.detail ? `<aside class="panel detail-panel"><div class="panel-heading">${button('← Close', 'close-detail')}<h2>${esc(message?.subject ?? 'Select a message')}</h2><label style="white-space:nowrap"><input type="checkbox" id="show-raw" ${state.raw ? 'checked' : ''}>Show raw</label>${button('Copy body', 'copy-message', '', true)}</div><dl class="metadata"><dt>Received</dt><dd>${message ? esc(date(message.received)) : ''}</dd><dt>Attachments</dt><dd>${message?.attachments ?? ''}</dd><dt>Last seen</dt><dd>${esc(message?.lastSeen ?? '')}</dd></dl><pre class="reader">${esc(state.raw ? message?.rawBody : message?.body)}</pre>${prompts()}</aside>` : ''}</div>`;
  }
  function disclaimers() {
    return `<section class="panel disclaimers-panel"><div class="disclaimer-toolbar"><h2>Disclaimer list</h2><span class="muted">Add each disclaimer as its own entry (literal match, case-insensitive).</span><label><input id="auto-clean" type="checkbox" ${data.autoClean ? 'checked' : ''}>Auto-clean new emails</label>${button('Start backfilling', 'backfill', '', true)}</div>${data.disclaimers.map((d, i) => `<div class="disclaimer-row ${d.enabled ? '' : 'disabled'}">${button(d.enabled ? '▸' : '▹', 'toggle-disclaimer', `data-index="${i}" aria-label="Toggle disclaimer" aria-pressed="${d.enabled}"`)}<textarea data-disclaimer="${i}" aria-label="Disclaimer ${i + 1}">${esc(d.text)}</textarea>${button('×', 'remove-disclaimer', `data-index="${i}" aria-label="Remove disclaimer"`)}</div>`).join('')}<div class="disclaimer-actions">${button('Add disclaimer', 'add-disclaimer')}${button('Save disclaimers', 'save-disclaimers', '', true)}<span class="muted">${adapter ? 'Save changes before leaving.' : 'Preview edits stay in this page only.'}</span></div></section>`;
  }
  let filterValues = null;
  function render() {
    app.innerHTML = state.tab === 'folders' ? folders() : state.tab === 'database' ? database() : disclaimers();
    document.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === state.tab); b.setAttribute('aria-current', b.dataset.tab === state.tab ? 'page' : 'false'); });
    document.querySelector('#sync-status').textContent = `Last synced: ${data.lastSynced}    DB rows: ${data.totalRows}`;
    document.querySelector('#sync-status').title = adapter ? 'Backend data' : 'Synthetic preview data';
    if (state.tab === 'database' && filterValues) for (const [key, value] of Object.entries(filterValues)) { const field = app.querySelector(`[name="${key}"]`); if (field) { if (field.type === 'checkbox') field.checked = value; else field.value = value; } }
  }
  function selectRow(row, additive) {
    const id = row.dataset.row;
    if (!additive) state.selected.clear();
    if (additive && state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    if (row.dataset.kind === 'chains') state.chain = id; else { state.message = id; state.detail = true; }
    render();
  }
  document.addEventListener('click', async event => {
    const tab = event.target.closest('[data-tab]');
    if (tab) { state.tab = tab.dataset.tab; state.selected.clear(); render(); return; }
    const folder = event.target.closest('[data-folder]');
    if (folder) { state.folder = folder.dataset.folder; state.chain = data.chains.find(c => c.folder === state.folder)?.id; state.selected.clear(); render(); return; }
    const row = event.target.closest('[data-row]'); if (row) { selectRow(row, event.ctrlKey || event.metaKey); return; }
    const sort = event.target.closest('[data-sort]'); if (sort) { const rows = sort.dataset.kind === 'chains' ? data.chains : state.results; const key = sort.dataset.sort; state.sortDirection = state.sortKey === key ? -state.sortDirection : 1; state.sortKey = key; rows.sort((a, b) => (typeof a[key] === 'number' ? a[key] - b[key] : String(a[key]).localeCompare(String(b[key]))) * state.sortDirection); render(); return; }
    const el = event.target.closest('[data-action]'); if (!el) return;
    const kind = el.dataset.kind, index = Number(el.dataset.index);
    switch (el.dataset.action) {
      case 'copy-chain': await copy(data.chains.find(c => c.id === state.chain)?.body); break;
      case 'copy-message': { const m = data.messages.find(m => m.id === state.message); await copy(state.raw ? m?.rawBody : m?.body); break; }
      case 'copy-all': await copy(chainRows().map(c => c.body).join('\n\n')); break;
      case 'copy-results': { const ids = new Set(state.results.map(m => m.chainId)); await copy(data.chains.filter(c => ids.has(c.id)).map(c => c.body).join('\n\n')); break; }
      case 'copy-queue': await copy(queueText(kind)); break;
      case 'clear-queue': state[kind] = []; render(); break;
      case 'remove-queue': state[kind] = state[kind].filter(id => id !== el.dataset.id); render(); break;
      case 'copy-prompt': await copy(`${data.prompts[index].text}\n\n${queueText('chains')}`); break;
      case 'remove-prompt': data.prompts.splice(index, 1); render(); break;
      case 'close-detail': state.detail = false; render(); break;
      case 'toggle-disclaimer': data.disclaimers[index].enabled = !data.disclaimers[index].enabled; render(); break;
      case 'remove-disclaimer': data.disclaimers.splice(index, 1); render(); break;
      case 'add-disclaimer': data.disclaimers.push({ text: '', enabled: true }); render(); app.querySelector('[data-disclaimer]:last-of-type'); app.querySelectorAll('[data-disclaimer]')[data.disclaimers.length - 1].focus(); break;
      case 'save-disclaimers': await callBackend('saveDisclaimers', { disclaimers: data.disclaimers, autoClean: data.autoClean }); break;
      case 'backfill': await callBackend('backfill', { disclaimers: data.disclaimers, autoClean: data.autoClean }); break;
      case 'refresh-folders': { const result = await callBackend('refreshFolders'); if (result) { data.folders = result; render(); } break; }
      case 'refresh': { const cutoff = Object.fromEntries(['days', 'hours', 'minutes'].map(k => [k, Number(document.getElementById(k)?.value || 0)])); if (Object.values(cutoff).some(n => !Number.isFinite(n) || n < 0)) return notice('Enter a valid non-negative cutoff.'); const result = await callBackend('refresh', { mode: el.dataset.mode, folders: [...state.checked], cutoff }); if (result) { data = result; state.results = data.messages; render(); } break; }
    }
  });
  app.addEventListener('input', event => {
    const el = event.target;
    if (el.dataset.prompt !== undefined) data.prompts[Number(el.dataset.prompt)][el.dataset.field] = el.value;
    if (el.dataset.disclaimer !== undefined) data.disclaimers[Number(el.dataset.disclaimer)].text = el.value;
    if (el.id === 'chain-filter') { const pos = el.selectionStart; state.filter = el.value; render(); const next = document.querySelector('#chain-filter'); next.focus(); next.setSelectionRange(pos, pos); }
  });
  app.addEventListener('change', event => {
    const el = event.target;
    if (el.dataset.folderCheck) { if (el.checked) state.checked.add(el.dataset.folderCheck); else state.checked.delete(el.dataset.folderCheck); }
    if (el.id === 'show-system') { state.showSystem = el.checked; render(); }
    if (el.id === 'show-raw') { state.raw = el.checked; render(); }
    if (el.id === 'auto-clean') data.autoClean = el.checked;
    if (el.closest('#query-form')) filterValues = readFilters(el.form);
  });
  function readFilters(form) { return Object.fromEntries([...form.elements].filter(e => e.name).map(e => [e.name, e.type === 'checkbox' ? e.checked : e.value])); }
  app.addEventListener('submit', async event => {
    event.preventDefault(); if (event.target.id !== 'query-form') return;
    const f = filterValues = readFilters(event.target);
    if (adapter) { const result = await callBackend('queryMessages', f); if (result) { state.results = result; for (const m of result) { const i = data.messages.findIndex(x => x.id === m.id); if (i >= 0) data.messages[i] = m; else data.messages.push(m); } render(); } return; }
    const contains = (text, query) => String(text || '').toLowerCase().includes(query.toLowerCase());
    // Demo is anchored to the sample snapshot, not today's date.
    const cutoff = f.days ? new Date(data.lastSynced + 'Z').getTime() - Number(f.days) * 86400000 : -Infinity;
    state.results = data.messages.filter(m => {
      const londonDate = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(m.received));
      const hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', hourCycle: 'h23' }).format(new Date(m.received)));
      return (!f.folder || m.folder === f.folder) && (!f.fromDate || londonDate >= f.fromDate) && (!f.toDate || londonDate <= f.toDate) && new Date(m.received).getTime() >= cutoff && contains(m.from, f.from) && (contains(m.to, f.to) || (f.includeCC && contains(m.cc, f.to))) && contains(m.cc, f.cc) && contains(m.subject, f.subject) && contains(m.body, f.body) && (!f.evenings || (hour >= 18 && hour < 22));
    }).slice(0, Math.max(1, Number(f.limit) || 500)); state.selected.clear(); render();
  });
  app.addEventListener('reset', () => { filterValues = { folder: '', fromDate: '', toDate: '', days: '', from: '', to: '', cc: '', subject: '', body: '', limit: '500', includeCC: false, evenings: false }; state.results = [...data.messages]; setTimeout(render, 0); });
  app.addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('[data-row], [data-sort]')) { event.preventDefault(); event.target.click(); } });
  app.addEventListener('dragstart', event => {
    const row = event.target.closest('[data-row]'); if (!row) return;
    const ids = state.selected.has(row.dataset.row) ? [...state.selected] : [row.dataset.row];
    event.dataTransfer.setData('application/x-outlook-digest', JSON.stringify({ kind: row.dataset.kind, ids })); event.dataTransfer.effectAllowed = 'copy';
  });
  app.addEventListener('dragover', event => { const zone = event.target.closest('[data-drop]'); if (zone) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; zone.classList.add('dragover'); } });
  app.addEventListener('dragleave', event => event.target.closest('[data-drop]')?.classList.remove('dragover'));
  app.addEventListener('drop', event => {
    const zone = event.target.closest('[data-drop]'); if (!zone) return; event.preventDefault(); zone.classList.remove('dragover');
    try { const payload = JSON.parse(event.dataTransfer.getData('application/x-outlook-digest')); if (!['messages', 'chains'].includes(payload.kind) || !Array.isArray(payload.ids)) return;
      const kind = zone.dataset.drop; let ids = payload.ids;
      if (kind === 'chains' && payload.kind === 'messages') ids = ids.map(id => data.messages.find(m => m.id === id)?.chainId);
      if (kind === 'messages' && payload.kind !== 'messages') return;
      ids = ids.filter(id => data[kind].some(item => item.id === id)); state[kind] = [...new Set([...state[kind], ...ids])]; render();
    } catch { notice('Drag rows from the table. File imports are not included.'); }
  });
  async function start() {
    if (adapter) { app.textContent = 'Loading…'; try { if (!adapter.getInitialData) throw new Error('getInitialData is required'); data = await adapter.getInitialData(); state.results = data.messages; state.folder = data.folders[0] || ''; state.checked = new Set(data.folders); state.chain = data.chains[0]?.id; state.message = data.messages[0]?.id; } catch (error) { app.textContent = `Unable to load backend data: ${error.message}`; return; } }
    render();
  }
  start();
})();
