/* Rendering and browser interactions only. See README.md for the adapter contract. */
(() => {
  'use strict';
  const app = document.querySelector('#app');
  const adapter = window.OutlookDigestAdapter;
  // Folders-tab preferences. mail.db (via the adapter) is the master copy;
  // browser storage is a fallback when the server is unreachable.
  const folderOrderKey = 'outlook-digest-folder-order-v1';
  const checkedKey = 'outlook-digest-refresh-checked-v1';
  const collapsedKey = 'outlook-digest-folder-collapsed-v1';
  const dbCollapsedKey = 'outlook-digest-db-folder-collapsed-v1';
  const layoutKey = 'outlook-digest-layout-widths-v1';
  const queueSplitKey = 'outlook-digest-queue-splits-v1';
  const tableColumnsKey = 'outlook-digest-table-columns-v1';
  const prefName = { [folderOrderKey]: 'folderOrder', [checkedKey]: 'refreshChecked' };
  // Per account, so one mailbox never overwrites another mailbox's settings.
  const storageKey = base => `${base}:${data.account}`;
  const prefCache = {};
  function readPref(base) {
    if (base in prefCache) return prefCache[base];
    try {
      const legacy = base === folderOrderKey ? localStorage.getItem(base) : null;
      const value = JSON.parse(localStorage.getItem(storageKey(base)) || legacy || 'null');
      return Array.isArray(value) ? value : null;
    } catch { return null; }
  }
  let pendingPrefs = {}, prefTimer;
  function writePref(base, value) {
    prefCache[base] = value;
    try { localStorage.setItem(storageKey(base), JSON.stringify(value)); } catch { /* Database copy is still saved. */ }
    if (!adapter?.savePreferences) return;
    pendingPrefs[prefName[base]] = value;
    clearTimeout(prefTimer);
    prefTimer = setTimeout(async () => {
      const prefs = pendingPrefs; pendingPrefs = {};
      try { await adapter.savePreferences(prefs); }
      catch (error) { notice(`Saved in this browser only; the database copy failed: ${error.message}`); }
    }, 400);
  }
  /** Server values win; an empty database adopts what this browser remembered. */
  async function loadServerPreferences() {
    if (!adapter?.getPreferences) return;
    let prefs;
    try { prefs = await adapter.getPreferences(); }
    catch (error) { notice(`Saved folder settings could not be read from the database: ${error.message}`); return; }
    for (const base of [folderOrderKey, checkedKey]) {
      const value = prefs?.[prefName[base]];
      if (Array.isArray(value)) prefCache[base] = value;
      else { const local = readPref(base); if (local) writePref(base, local); }
    }
  }
  function orderedFolders(folders) {
    const saved = readPref(folderOrderKey);
    if (!saved) return [...folders].reverse();
    const available = new Set(folders);
    return [...saved.filter(folder => available.delete(folder)), ...folders.filter(folder => available.has(folder))];
  }
  function saveFolderOrder() { writePref(folderOrderKey, data.folders); }
  /** Previously ticked Folders-tab boxes that still exist; null when never saved. */
  function loadChecked(folders) {
    const saved = readPref(checkedKey);
    if (!saved) return null;
    const available = new Set(folders);
    return new Set(saved.filter(folder => available.has(folder)));
  }
  function saveChecked() { writePref(checkedKey, [...state.checked]); }
  function loadCollapsed(base) {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey(base)) || '[]');
      return new Set(Array.isArray(value) ? value : []);
    } catch { return new Set(); }
  }
  function saveCollapsed(scope) {
    const base = scope === 'db' ? dbCollapsedKey : collapsedKey;
    try { localStorage.setItem(storageKey(base), JSON.stringify([...scopeCollapsed(scope)])); } catch { /* UI state can remain session-only. */ }
  }
  const layoutDefaults = { folders: [213, 162, 600, 590], database: [175, 700, 327] };
  const layoutMinimums = { folders: [160, 100, 260, 220], database: [130, 320, 220] };
  const tableColumnDefaults = {
    chains: [
      { key: 'count', label: '#', width: 4 }, { key: 'folder', label: 'Folder', width: 22 },
      { key: 'received', label: 'Last reply (London)', width: 14 }, { key: 'subject', label: 'Subject', width: 26 },
      { key: 'from', label: 'From', width: 17 }, { key: 'to', label: 'To', width: 17 },
    ],
    messages: [
      { key: 'received', label: 'Received (London)', width: 14 }, { key: 'from', label: 'From', width: 15 },
      { key: 'to', label: 'To', width: 15 }, { key: 'subject', label: 'Subject', width: 30 },
      { key: 'folder', label: 'Folder', width: 22 }, { key: 'attachments', label: 'Att', width: 4 },
    ],
  };
  function loadLayoutWidths() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey(layoutKey)) || '{}');
      for (const name of Object.keys(layoutDefaults)) {
        if (Array.isArray(value[name]) && value[name].every(width => Number.isFinite(width) && width > 0)) state.layoutWidths[name] = value[name];
      }
    } catch { /* Default widths remain in use. */ }
  }
  function saveLayoutWidths() {
    try { localStorage.setItem(storageKey(layoutKey), JSON.stringify(state.layoutWidths)); } catch { /* UI state can remain session-only. */ }
  }
  function loadQueueSplits() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey(queueSplitKey)) || '{}');
      for (const name of ['folders', 'database']) if (Number.isFinite(value[name]) && value[name] > 0 && value[name] < 100) state.queueSplits[name] = value[name];
    } catch { /* Equal sections remain in use. */ }
  }
  function saveQueueSplits() {
    try { localStorage.setItem(storageKey(queueSplitKey), JSON.stringify(state.queueSplits)); } catch { /* UI state can remain session-only. */ }
  }
  function loadTableColumns() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey(tableColumnsKey)) || '{}');
      for (const kind of Object.keys(tableColumnDefaults)) {
        if (!Array.isArray(saved[kind])) continue;
        const defaults = new Map(tableColumnDefaults[kind].map(column => [column.key, column]));
        if (saved[kind].length !== defaults.size || saved[kind].some(column => !defaults.has(column.key) || !Number.isFinite(column.width) || column.width <= 0)) continue;
        state.tableColumns[kind] = saved[kind].map(column => ({ ...defaults.get(column.key), width: column.width }));
      }
    } catch { /* Default column order and widths remain in use. */ }
  }
  function saveTableColumns() {
    try {
      const value = Object.fromEntries(Object.entries(state.tableColumns).map(([kind, columns]) => [kind, columns.map(({ key, width }) => ({ key, width }))]));
      localStorage.setItem(storageKey(tableColumnsKey), JSON.stringify(value));
    } catch { /* UI state can remain session-only. */ }
  }
  function layoutTemplate(name, widths, count) {
    const minimums = layoutMinimums[name];
    return widths.slice(0, count).map((width, index) => `${index ? '10px ' : ''}minmax(${minimums[index]}px, ${width}fr)`).join(' ');
  }
  function installResizeHandles() {
    const layout = app.querySelector('.folders-layout, .database-layout');
    if (!layout) return;
    const name = layout.classList.contains('folders-layout') ? 'folders' : 'database';
    const panels = [...layout.children];
    const saved = state.layoutWidths[name];
    if (saved) {
      const widths = layoutDefaults[name].map((fallback, index) => saved[index] || fallback);
      layout.style.gridTemplateColumns = layoutTemplate(name, widths, panels.length);
    }
    panels.slice(0, -1).forEach((panel, index) => {
      const handle = document.createElement('div');
      handle.className = 'resize-handle';
      handle.dataset.resizeLayout = name;
      handle.dataset.resizeIndex = index;
      handle.tabIndex = 0;
      handle.setAttribute('role', 'separator');
      handle.setAttribute('aria-orientation', 'vertical');
      handle.setAttribute('aria-label', 'Resize adjacent sections');
      handle.title = 'Drag to resize sections';
      panel.after(handle);
    });
  }
  const parentOf = path => String(path).split('/').slice(0, -1).join('/');
  const inBranch = (path, root) => path === root || path.startsWith(root + '/');
  /** Reorder among siblings only; a folder always moves with its subfolders. */
  function moveFolder(source, target, after = false) {
    if (!source || !target || source === target) return;
    if (parentOf(source) !== parentOf(target)) return notice('Folders can only be reordered within the same parent folder.');
    const moving = data.folders.filter(path => inBranch(path, source));
    const rest = data.folders.filter(path => !inBranch(path, source));
    const targetRows = rest.flatMap((path, i) => inBranch(path, target) ? [i] : []);
    if (!targetRows.length) return;
    rest.splice(after ? Math.max(...targetRows) + 1 : Math.min(...targetRows), 0, ...moving);
    data.folders = rest;
    saveFolderOrder();
    render();
  }
  const SYSTEM_FOLDERS = new Set([
    'Sent Items', 'Conversation History',
    '已发送邮件', '对话历史记录',
  ]);
  function isSystemFolder(path) {
    const name = String(path).split(/[/\\]/).pop();
    return SYSTEM_FOLDERS.has(path) || SYSTEM_FOLDERS.has(name);
  }
  /** Build a nestable tree from flat folder paths like Inbox/UKVI. */
  function treeFromPaths(folders) {
    const roots = [];
    const byPath = new Map();
    for (const path of folders) {
      const parts = String(path).split(/[/\\]/).filter(Boolean);
      let parent = '';
      let siblings = roots;
      for (const name of parts) {
        const full = parent ? `${parent}/${name}` : name;
        let node = byPath.get(full);
        if (!node) {
          node = { path: full, name, children: [] };
          byPath.set(full, node);
          siblings.push(node);
        }
        siblings = node.children;
        parent = full;
      }
    }
    return roots;
  }
  function visibleTree(nodes) {
    return (nodes || [])
      .filter(node => state.showSystem || !isSystemFolder(node.path))
      .map(node => ({ ...node, children: visibleTree(node.children) }));
  }
  const ROOT_KEY = '__account__';
  // Two independent trees: 'folders' (refresh selection) and 'db' (query filter).
  const scopeSet = scope => scope === 'db' ? state.dbFolders : state.checked;
  const scopeCollapsed = scope => scope === 'db' ? state.dbCollapsed : state.collapsed;
  const currentTree = () => visibleTree(treeFromPaths(data.folders));
  function pruneTree(nodes, text) {
    if (!text) return nodes;
    const needle = text.toLowerCase();
    return (nodes || []).flatMap(node => {
      const children = pruneTree(node.children, text);
      return node.name.toLowerCase().includes(needle) || children.length ? [{ ...node, children }] : [];
    });
  }
  const pickerTree = () => pruneTree(treeFromPaths(data.folders), state.dbFolderFilter);
  const scopeTree = scope => scope === 'db' ? pickerTree() : currentTree();
  // A filtered picker shows every match, so its branches are always open.
  const isOpen = (scope, key) => (scope === 'db' && state.dbFolderFilter) || !scopeCollapsed(scope).has(key);
  const branchOf = node => [node.path, ...(node.children || []).flatMap(branchOf)];
  function findNode(nodes, path) {
    for (const node of nodes || []) {
      if (node.path === path) return node;
      const hit = findNode(node.children, path);
      if (hit) return hit;
    }
    return null;
  }
  /** Every folder path the branch checkbox for `key` controls, including itself. */
  function branchPaths(key, scope = 'folders') {
    const tree = scopeTree(scope);
    if (key === ROOT_KEY) return tree.flatMap(branchOf);
    const node = findNode(tree, key);
    return node ? branchOf(node) : [];
  }
  function branchBox(scope, key, paths, label) {
    const set = scopeSet(scope);
    const ticked = paths.filter(path => set.has(path)).length;
    const all = paths.length > 0 && ticked === paths.length;
    const some = ticked > 0 && !all;
    return `<input type="checkbox" class="branch-check" data-scope="${scope}" data-branch-check="${esc(key)}" ${all ? 'checked' : ''} data-indeterminate="${some ? '1' : ''}" aria-label="Select ${esc(label)} and all subfolders" title="Select this folder and all subfolders">`;
  }
  function twisty(scope, key, hasKids) {
    if (!hasKids) return '<span class="twisty"></span>';
    const open = isOpen(scope, key);
    return `<button type="button" class="twisty" data-scope="${scope}" data-twisty="${esc(key)}" aria-expanded="${open}" aria-label="${open ? 'Collapse' : 'Expand'}">${open ? '▾' : '▸'}</button>`;
  }
  function treeRows(nodes, depth = 0) {
    return (nodes || []).map(node => {
      const pad = 14 + depth * 14;
      const hasKids = (node.children || []).length > 0;
      const kids = hasKids && isOpen('folders', node.path) ? treeRows(node.children, depth + 1) : '';
      const branch = hasKids ? branchBox('folders', node.path, branchOf(node), node.path) : '';
      return `<div class="tree-row folder-sortable ${node.path === state.folder ? 'active' : ''}" draggable="true" data-folder-row="${esc(node.path)}" style="padding-left:${pad}px"><span class="handle" title="Drag to reorder">≡</span>${twisty('folders', node.path, hasKids)}${branch}<input aria-label="Include ${esc(node.path)} in refresh" title="Include this folder only" type="checkbox" data-scope="folders" data-folder-check="${esc(node.path)}" ${state.checked.has(node.path) ? 'checked' : ''}><button class="tree-name" data-folder="${esc(node.path)}" title="Drag to reorder; Alt+Up/Down also moves this folder">${esc(node.name)}</button></div>${kids}`;
    }).join('');
  }
  function pickerRows(nodes, depth = 0) {
    return (nodes || []).map(node => {
      const hasKids = (node.children || []).length > 0;
      const kids = hasKids && isOpen('db', node.path) ? pickerRows(node.children, depth + 1) : '';
      const branch = hasKids ? branchBox('db', node.path, branchOf(node), node.path) : '';
      return `<div class="tree-row" style="padding-left:${14 + depth * 14}px">${twisty('db', node.path, hasKids)}${branch}<label class="picker-name" title="${esc(node.path)}"><input type="checkbox" data-scope="db" data-folder-check="${esc(node.path)}" ${state.dbFolders.has(node.path) ? 'checked' : ''}>${esc(node.name)}</label></div>${kids}`;
    }).join('');
  }
  function folderPicker() {
    const picked = [...state.dbFolders];
    const label = !picked.length ? '(all)' : picked.length === 1 ? picked[0].split('/').pop() : `${picked.length} folders`;
    const tree = pickerTree();
    const popup = `<div class="picker-popup"><div class="picker-bar"><input id="db-folder-filter" placeholder="Filter folders…" aria-label="Filter folders" value="${esc(state.dbFolderFilter)}">${button('Clear', 'db-folders-clear', 'type="button"')}${button('Done', 'db-folders-done', 'type="button"')}</div><div class="picker-tree">${tree.length ? pickerRows(tree, -1) : '<div class="empty">No matching folders</div>'}</div></div>`;
    return `<div class="filter-field folder-picker">Folder<button type="button" class="picker-toggle" data-action="toggle-db-folders" aria-expanded="${state.dbFolderOpen}" title="${esc(picked.join('\n'))}">${esc(label)}<span>▾</span></button>${state.dbFolderOpen ? popup : ''}</div>`;
  }
  // Sample data has no people list; derive one from the preview messages.
  function samplePeople() {
    const collect = key => [...new Set(data.messages.flatMap(m => String(m[key] || '').split(';').map(s => s.trim()).filter(Boolean)))].sort((a, b) => a.localeCompare(b)).map(label => ({ label, value: label.match(/<([^>]+)>/)?.[1] || label }));
    return { from: collect('from'), to: collect('to'), cc: collect('cc') };
  }
  function peopleOptions(field) {
    const people = data.people || (data.people = samplePeople());
    let list = people[field] || [];
    if (field === 'to' && filterValues?.includeCC) { const seen = new Set(list.map(p => p.value)); list = [...list, ...(people.cc || []).filter(p => !seen.has(p.value))].sort((a, b) => a.label.toLowerCase().localeCompare(b.label.toLowerCase())); }
    const picked = new Set(state.people[field]);
    const query = state.peopleQuery[field].toLowerCase();
    return list.filter(p => !picked.has(p.value) && (!query || p.label.toLowerCase().includes(query))).slice(0, 200);
  }
  function peoplePicker(field, label, placeholder, extra = '') {
    const labels = new Map([...(data.people?.[field] || []), ...(field === 'to' ? data.people?.cc || [] : [])].map(p => [p.value, p.label]));
    const chips = state.people[field].map((value, i) => `<span class="chip person-chip" title="${esc(labels.get(value) || value)}"><span>${esc(labels.get(value) || value)}</span><button type="button" data-people-remove="${field}" data-index="${i}" aria-label="Remove ${esc(value)}">×</button></span>`).join('');
    const open = state.peopleOpen === field;
    const options = open ? peopleOptions(field) : [];
    const typed = state.peopleQuery[field].trim();
    const hint = typed ? `<div class="people-hint">Enter adds “${esc(typed)}” as a contains-match</div>` : '';
    const popup = open ? `<div class="picker-popup people-popup" role="listbox">${hint}${options.map((p, i) => `<button type="button" role="option" class="people-option ${i === state.peopleActive ? 'active' : ''}" data-people-add="${field}" data-value="${esc(p.value)}" title="${esc(p.label)}">${esc(p.label)}</button>`).join('') || (typed ? '' : '<div class="empty">No more people</div>')}</div>` : '';
    return `<div class="filter-field wide people-picker" data-people="${field}">${label}<div class="people-box">${chips}<input data-people-input="${field}" autocomplete="off" placeholder="${state.people[field].length ? 'or…' : placeholder}" value="${esc(state.peopleQuery[field])}" aria-label="${label}" aria-expanded="${open}"><span class="people-caret" data-people-toggle="${field}">▾</span></div>${popup}${extra}</div>`;
  }
  function focusPeople(field) {
    const input = app.querySelector(`[data-people-input="${field}"]`);
    if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  }
  function addPerson(field, value) {
    value = value.trim();
    if (value && !state.people[field].includes(value)) state.people[field].push(value);
    state.peopleQuery[field] = ''; state.peopleActive = -1;
    render(); focusPeople(field);
  }
  window.addEventListener('digest-progress', event => notice(event.detail));
  function merge(kind, item) {
    const i = data[kind].findIndex(row => row.id === item.id);
    if (i < 0) data[kind].push(item); else data[kind][i] = { ...data[kind][i], ...item };
    return item;
  }
  async function loadBody(kind, id) {
    if (!id) return;
    const row = data[kind].find(item => item.id === id);
    if (row?.body !== undefined) return row;
    if (!adapter) return row;
    const item = await callBackend(kind === 'chains' ? 'getChain' : 'getMessage', id);
    if (item) merge(kind, item);
    return item;
  }
  async function copyItems(kind, ids, prefix = '') {
    const items = [];
    for (const id of ids) {
      const item = await loadBody(kind, id);
      if (!item) return notice('Could not load all selected items; nothing copied.');
      items.push(kind === 'messages' ? `Subject: ${item.subject}\nFrom: ${item.from}\nTo: ${item.to}\n\n${item.body}` : item.body);
    }
    await copy(prefix + items.join('\n\n' + '='.repeat(72) + '\n\n'));
  }
  let folderRequest = 0;
  async function chooseFolder(folder) {
    const request = ++folderRequest;
    state.folder = folder;
    state.selected.clear();
    if (adapter) {
      const rows = await callBackend('getChains', folder);
      if (request !== folderRequest) return;
      if (!rows) return;
      state.folderChainIds = rows.map(row => row.id);
      rows.forEach(row => merge('chains', row));
    }
    state.chain = chainRows()[0]?.id;
    render();
    await loadBody('chains', state.chain);
    render();
  }
  let data = { account: 'Local mailbox', lastSynced: '', totalRows: 0, folders: [], messages: [], chains: [], prompts: [], disclaimers: [], autoClean: false };
  const state = { tab: 'folders', folder: '', checked: new Set(), selected: new Set(), chain: undefined, message: undefined, chains: [], messages: [], filter: '', results: data.messages, raw: false, detail: true, showSystem: false, collapsed: new Set(), dbFolders: new Set(), dbCollapsed: new Set(), dbFolderOpen: false, dbFolderFilter: '', layoutWidths: { folders: null, database: null }, queueSplits: { folders: 50, database: 50 }, tableColumns: Object.fromEntries(Object.entries(tableColumnDefaults).map(([kind, columns]) => [kind, columns.map(column => ({ ...column }))])), people: { from: [], to: [], cc: [] }, peopleQuery: { from: '', to: '', cc: '' }, peopleOpen: null, peopleActive: -1 };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const date = value => !value ? '' : new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value)).replace(',', ' ·');
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
  const chainRows = () => data.chains.filter(c => (adapter && state.folderChainIds ? state.folderChainIds.includes(c.id) : c.folder === state.folder) && `${c.subject} ${c.from} ${c.to}`.toLowerCase().includes(state.filter.toLowerCase()));
  const queueText = kind => state[kind].map(id => data[kind].find(x => x.id === id)).filter(Boolean).map(x => `Subject: ${x.subject}\n\n${x.body}`).join('\n\n' + '='.repeat(80) + '\n\n');
  function queue(kind) {
    return `<div class="queue-box">Queued ${kind}: ${state[kind].length} ${button('Copy queued', 'copy-queue', `data-kind="${kind}"`)}<div class="queue-items">${state[kind].map(id => { const item = data[kind].find(x => x.id === id); return `<span class="chip" title="${esc(item?.subject)}">${esc(item?.subject)} ${button('×', 'remove-queue', `data-kind="${kind}" data-id="${esc(id)}" aria-label="Remove queued item"`)}</span>`; }).join('')}</div>${button('Clear', 'clear-queue', `data-kind="${kind}"`)}</div>`;
  }
  function installDropQueues() {
    const folderColumn = app.querySelector('.folders-layout .queue-column');
    const column = folderColumn || app.querySelector('.database-layout > aside.panel:first-child');
    if (!column) return;
    const name = folderColumn ? 'folders' : 'database';
    const messageCaption = folderColumn ? 'Queues the latest individual message from each dragged chain.' : 'Queues each dragged message individually.';
    const messageInstruction = folderColumn ? 'Drag one or more chain rows<br>from the table.' : 'Drag one or more selected rows<br>from the table on the right.';
    column.classList.add('dual-queues');
    column.dataset.queueLayout = name;
    column.style.gridTemplateRows = `minmax(120px, ${state.queueSplits[name]}fr) 9px minmax(120px, ${100 - state.queueSplits[name]}fr)`;
    column.innerHTML = `<section class="folder-drop-group"><div class="section-title">Drop for messages</div><div class="drop-caption">${messageCaption}</div><div class="dropzone" data-drop="messages"><strong>Single or Multi MSG Drop</strong><span>${messageInstruction}</span></div>${queue('messages')}</section><div class="queue-section-resize" data-queue-resize="${name}" role="separator" aria-orientation="horizontal" aria-label="Resize message and chain queue sections" tabindex="0" title="Drag to resize message and chain sections"></div><section class="folder-drop-group"><div class="section-title">Drop for chains</div><div class="drop-caption">${folderColumn ? 'Queues each complete dragged chain.' : 'Queues the full chain containing each dragged message.'}</div><div class="dropzone chains" data-drop="chains"><strong>Full Latest Chain Drop</strong><span>${folderColumn ? 'Drag one or more chain rows;<br>Copy queued joins all full chains.' : 'Drag one or more selected rows;<br>chips are labelled «count | subject».'}</span></div>${queue('chains')}</section>`;
  }
  function prompts() {
    return `<section class="prompts"><div class="prompts-heading"><strong>Prompts</strong><span>Click Copy to copy prompt + queued chains.</span></div><div class="prompts-list">${data.prompts.map((p, i) => `<div class="prompt-row">${button('Copy', 'copy-prompt', `data-index="${i}"`)}<span class="triangle">▸</span><div class="prompt-fields"><input aria-label="Prompt title" data-prompt="${i}" data-field="title" value="${esc(p.title)}"><textarea aria-label="Prompt text" data-prompt="${i}" data-field="text">${esc(p.text)}</textarea></div>${button('×', 'remove-prompt', `data-index="${i}" aria-label="Remove prompt"`)}</div>`).join('')}</div></section>`;
  }
  function table(rows, kind) {
    const chains = kind === 'chains';
    const cols = state.tableColumns[kind];
    return `<div class="table-scroll"><table aria-label="${kind}"><colgroup>${cols.map(c => `<col data-col-key="${c.key}" style="width:${c.width}%">`).join('')}</colgroup><thead><tr>${cols.map((c, index) => `<th scope="col" tabindex="0" draggable="true" data-sort="${c.key}" data-kind="${kind}" data-column-key="${c.key}" title="Click to sort; drag to reorder">${esc(c.label)}${state.sortKind === kind && state.sortKey === c.key ? (state.sortDirection === 1 ? ' ▲' : ' ▼') : ''}${index < cols.length - 1 ? `<span class="column-resize" data-column-resize="${c.key}" data-column-kind="${kind}" title="Drag to resize column"></span>` : ''}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr draggable="true" tabindex="0" data-row="${esc(row.id)}" data-kind="${kind}" class="${state.selected.has(row.id) || (state.selected.size === 0 && (chains ? state.chain : state.message) === row.id) ? 'selected' : ''}" aria-selected="${state.selected.has(row.id)}">${cols.map(({ key }) => `<td title="${esc(row[key])}" class="${key === 'count' || key === 'attachments' ? 'number' : ''}">${esc(key === 'received' ? date(row[key]) : row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table>${rows.length ? '' : '<div class="empty">No results</div>'}</div>`;
  }
  function refreshBox(title, mode, content) { return `<div class="refresh-box">${title}${content}${button('Refresh', 'refresh', `data-mode="${mode}"`, true)}</div>`; }
  function folders() {
    const chain = data.chains.find(c => c.id === state.chain);
    const tree = currentTree();
    return `<div class="folders-layout"><aside class="sidebar"><section class="panel folder-panel"><div class="section-title">Folders ${button('Refresh folder structure', 'refresh-folders')}</div><label class="system-toggle"><input type="checkbox" id="show-system" ${state.showSystem ? 'checked' : ''}>Show system folders</label><div class="tree">${treeRows(tree, -1)}</div></section><section class="panel"><div class="section-title">Refresh</div>${refreshBox('Cutoff', 'cutoff', '<div><input id="days" type="number" min="0" value="0" aria-label="Cutoff days">days <input id="hours" type="number" min="0" value="24" aria-label="Cutoff hours">hours</div><div><input id="minutes" type="number" min="0" value="0" aria-label="Cutoff minutes">minutes</div>')}${refreshBox('Refresh from last update time', 'last', `<div class="muted">Earliest refresh among selected: ${esc(data.lastSynced)}<br>London</div>`)}${refreshBox('Refresh from start', 'start', '<div class="muted">Scans every selected (sub)folder in full — no cutoff.</div>')}<div class="refresh-note">${adapter ? 'Backend adapter supplied.' : 'Preview data — Outlook is not connected.'}</div></section></aside><aside class="panel queue-column"><div class="dropzone" data-drop="chains"><strong>Drop chains here</strong></div>${queue('chains')}</aside><section class="panel"><div class="panel-heading"><h2>${esc(state.folder)}</h2><span class="muted">${esc(data.account)} - ${esc(state.folder)}</span>${button('Copy all chains', 'copy-all', '', true)}</div><input class="chain-filter" id="chain-filter" placeholder="Filter chains…" aria-label="Filter chains" value="${esc(state.filter)}">${table(chainRows(), 'chains')}</section><aside class="panel detail-panel"><div class="panel-heading"><h2>${esc(chain?.subject ?? 'Select a chain')}</h2>${button('Copy to clipboard', 'copy-chain', '', true)}</div><pre class="reader">${esc(chain?.body)}</pre>${prompts()}</aside></div>`;
  }
  function filterField(label, name, placeholder = '', value = '', type = 'text', wide = false) { return `<label class="filter-field ${wide ? 'wide' : ''}">${label}<input name="${name}" type="${type}" placeholder="${placeholder}" value="${value}"></label>`; }
  function database() {
    const message = data.messages.find(m => m.id === state.message);
    return `<section class="panel filter-panel"><form class="filter-form" id="query-form">${folderPicker()}${filterField('From date', 'fromDate', '', (adapter ? '' : '2026-09-09'), 'date')}${filterField('To date', 'toDate', '', (adapter ? '' : '2026-09-13'), 'date')}${filterField('Last N days', 'days', '', (adapter ? '' : '4'), 'number')}${peoplePicker('from', 'Sent by', 'Pick or type a sender…')}${peoplePicker('to', 'Sent to', 'Pick or type a To recipient', '<label class="cc-option"><input name="includeCC" type="checkbox">or CC’d</label>')}${peoplePicker('cc', 'CC’d to', 'Pick or type a CC recipient')}${filterField('Subject contains', 'subject', 'e.g. Universe', '', 'text', true)}${filterField('Body contains', 'body', 'e.g. follow up', '', 'text', true)}<label class="filter-field limit">Limit<input name="limit" type="number" min="1" value="500"></label><label class="evenings"><input name="evenings" type="checkbox">Evenings (18:00–22:00 UK)</label><div class="filter-buttons"><button class="primary" type="submit">Query</button><button type="reset">Clear</button></div></form><div class="filter-status" id="query-status">${state.results.length} row(s) returned.${adapter ? '' : ' (Preview data)'}</div></section><div class="database-layout ${state.detail ? '' : 'no-detail'}"><aside class="panel"><div class="section-title">Drop for messages</div><div class="drop-caption">Queues each dragged message individually.</div><div class="dropzone" data-drop="messages"><strong>Single or Multi MSG Drop</strong><span>Drag one or more selected rows from<br>the table on the right.</span></div>${queue('messages')}<div class="section-title">Drop for chains</div><div class="drop-caption">Queues the full chain containing each dragged message.</div><div class="dropzone chains" data-drop="chains"><strong>Full Latest Chain Drop</strong><span>Drag one or more selected rows;<br>chips are labelled «count | subject».</span></div>${queue('chains')}</aside><section class="panel messages-panel"><div class="panel-heading"><h2>Messages</h2><span class="muted">${state.results.length} row(s)</span>${button('Chain & copy results', 'copy-results', '', true)}</div>${table(state.results, 'messages')}</section>${state.detail ? `<aside class="panel detail-panel"><div class="panel-heading">${button('← Close', 'close-detail')}<h2>${esc(message?.subject ?? 'Select a message')}</h2><label style="white-space:nowrap"><input type="checkbox" id="show-raw" ${state.raw ? 'checked' : ''}>Show raw</label>${button('Copy body', 'copy-message', '', true)}</div><dl class="metadata"><dt>Received</dt><dd>${message ? esc(date(message.received)) : ''}</dd><dt>Attachments</dt><dd>${message?.attachments ?? ''}</dd><dt>Last seen</dt><dd>${esc(message?.lastSeen ?? '')}</dd></dl><pre class="reader">${esc(state.raw ? message?.rawBody : message?.body)}</pre>${prompts()}</aside>` : ''}</div>`;
  }
  function disclaimers() {
    return `<section class="panel disclaimers-panel"><div class="disclaimer-toolbar"><h2>Disclaimer list</h2><span class="muted">Add each disclaimer as its own entry (literal match, case-insensitive).</span><label><input id="auto-clean" type="checkbox" ${data.autoClean ? 'checked' : ''}>Auto-clean new emails</label>${button('Start backfilling', 'backfill', '', true)}</div>${data.disclaimers.map((d, i) => `<div class="disclaimer-row ${d.enabled ? '' : 'disabled'}">${button(d.enabled ? '▸' : '▹', 'toggle-disclaimer', `data-index="${i}" aria-label="Toggle disclaimer" aria-pressed="${d.enabled}"`)}<textarea data-disclaimer="${i}" aria-label="Disclaimer ${i + 1}">${esc(d.text)}</textarea>${button('×', 'remove-disclaimer', `data-index="${i}" aria-label="Remove disclaimer"`)}</div>`).join('')}<div class="disclaimer-actions">${button('Add disclaimer', 'add-disclaimer')}${button('Save disclaimers', 'save-disclaimers', '', true)}<span class="muted">${adapter ? 'Save changes before leaving.' : 'Preview edits stay in this page only.'}</span></div></section>`;
  }
  let filterValues = null;
  function render() {
    app.innerHTML = state.tab === 'folders' ? folders() : state.tab === 'database' ? database() : disclaimers();
    installDropQueues();
    installResizeHandles();
    // Indeterminate is a DOM property only; it cannot be set from markup.
    app.querySelectorAll('[data-indeterminate="1"]').forEach(box => { box.indeterminate = true; });
    document.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === state.tab); b.setAttribute('aria-current', b.dataset.tab === state.tab ? 'page' : 'false'); });
    document.querySelector('#sync-status').textContent = `Last synced: ${data.lastSynced}    DB rows: ${data.totalRows}`;
    document.querySelector('#sync-status').title = adapter ? 'Backend data' : 'Synthetic preview data';
    if (state.tab === 'database' && filterValues) for (const [key, value] of Object.entries(filterValues)) { const field = app.querySelector(`[name="${key}"]`); if (field) { if (field.type === 'checkbox') field.checked = value; else field.value = value; } }
  }
  app.addEventListener('pointerdown', event => {
    const handle = event.target.closest('[data-column-resize]');
    if (!handle) return;
    event.preventDefault();
    event.stopPropagation();
    const kind = handle.dataset.columnKind;
    const columns = state.tableColumns[kind];
    const index = columns.findIndex(column => column.key === handle.dataset.columnResize);
    if (index < 0 || index >= columns.length - 1) return;
    const tableElement = handle.closest('table');
    const headers = [...tableElement.querySelectorAll('thead th')];
    const initialLeft = headers[index].getBoundingClientRect().width;
    const pairWidth = initialLeft + headers[index + 1].getBoundingClientRect().width;
    const pairPercent = columns[index].width + columns[index + 1].width;
    const startX = event.clientX;
    document.body.classList.add('resizing-columns');

    const move = moveEvent => {
      const left = Math.max(36, Math.min(pairWidth - 36, initialLeft + moveEvent.clientX - startX));
      columns[index].width = pairPercent * left / pairWidth;
      columns[index + 1].width = pairPercent - columns[index].width;
      const colElements = tableElement.querySelectorAll('col');
      colElements[index].style.width = `${columns[index].width}%`;
      colElements[index + 1].style.width = `${columns[index + 1].width}%`;
    };
    const stop = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', stop);
      document.removeEventListener('pointercancel', stop);
      document.body.classList.remove('resizing-columns');
      saveTableColumns();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop);
    document.addEventListener('pointercancel', stop);
  });
  app.addEventListener('pointerdown', event => {
    const handle = event.target.closest('[data-queue-resize]');
    if (!handle) return;
    event.preventDefault();
    const column = handle.parentElement;
    const sections = [...column.querySelectorAll(':scope > .folder-drop-group')];
    const initial = sections.map(section => section.getBoundingClientRect().height);
    const available = initial[0] + initial[1];
    const startY = event.clientY;
    const name = handle.dataset.queueResize;
    document.body.classList.add('resizing-queues');

    const move = moveEvent => {
      const top = Math.max(120, Math.min(available - 120, initial[0] + moveEvent.clientY - startY));
      const percent = top / available * 100;
      state.queueSplits[name] = percent;
      column.style.gridTemplateRows = `minmax(120px, ${percent}fr) 9px minmax(120px, ${100 - percent}fr)`;
    };
    const stop = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', stop);
      document.removeEventListener('pointercancel', stop);
      document.body.classList.remove('resizing-queues');
      saveQueueSplits();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop);
    document.addEventListener('pointercancel', stop);
  });
  app.addEventListener('pointerdown', event => {
    const handle = event.target.closest('[data-resize-layout]');
    if (!handle) return;
    event.preventDefault();
    const layout = handle.parentElement;
    const name = handle.dataset.resizeLayout;
    const index = Number(handle.dataset.resizeIndex);
    const panels = [...layout.children].filter(child => !child.classList.contains('resize-handle'));
    const initial = panels.map(panel => panel.getBoundingClientRect().width);
    const pairWidth = initial[index] + initial[index + 1];
    const startX = event.clientX;
    const minimums = layoutMinimums[name];
    document.body.classList.add('resizing-sections');

    const move = moveEvent => {
      const left = Math.max(minimums[index], Math.min(pairWidth - minimums[index + 1], initial[index] + moveEvent.clientX - startX));
      const current = [...initial];
      current[index] = left;
      current[index + 1] = pairWidth - left;
      const widths = layoutDefaults[name].map((fallback, position) => current[position] || state.layoutWidths[name]?.[position] || fallback);
      state.layoutWidths[name] = widths;
      layout.style.gridTemplateColumns = layoutTemplate(name, widths, panels.length);
    };
    const stop = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', stop);
      document.removeEventListener('pointercancel', stop);
      document.body.classList.remove('resizing-sections');
      saveLayoutWidths();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', stop);
    document.addEventListener('pointercancel', stop);
  });
  async function selectRow(row, additive) {
    const id = row.dataset.row;
    if (!additive) state.selected.clear();
    if (additive && state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    if (row.dataset.kind === 'chains') state.chain = id; else { state.message = id; state.detail = true; }
    render();
    await loadBody(row.dataset.kind, id);
    render();
  }
  document.addEventListener('click', async event => {
    // Close the picker in place: re-rendering here would detach the clicked
    // element (e.g. the Query button) before its default action runs.
    if (state.dbFolderOpen && !event.target.closest('.folder-picker')) {
      state.dbFolderOpen = false;
      app.querySelector('.picker-popup')?.remove();
      app.querySelector('.picker-toggle')?.setAttribute('aria-expanded', 'false');
    }
    if (state.peopleOpen && !event.target.closest(`[data-people="${state.peopleOpen}"]`)) {
      app.querySelector(`[data-people="${state.peopleOpen}"] .people-popup`)?.remove();
      state.peopleOpen = null; state.peopleActive = -1;
    }
    const personAdd = event.target.closest('[data-people-add]');
    if (personAdd) { addPerson(personAdd.dataset.peopleAdd, personAdd.dataset.value); return; }
    const personRemove = event.target.closest('[data-people-remove]');
    if (personRemove) { const field = personRemove.dataset.peopleRemove; state.people[field].splice(Number(personRemove.dataset.index), 1); render(); focusPeople(field); return; }
    const peopleBox = event.target.closest('.people-box');
    if (peopleBox) { const field = peopleBox.closest('[data-people]').dataset.people; if (event.target.closest('[data-people-toggle]') && state.peopleOpen === field) { state.peopleOpen = null; render(); } else { state.peopleOpen = field; render(); focusPeople(field); } return; }
    const tab = event.target.closest('[data-tab]');
    if (tab) { state.tab = tab.dataset.tab; state.selected.clear(); render(); if (adapter) { await loadBody(state.tab === 'folders' ? 'chains' : 'messages', state.tab === 'folders' ? state.chain : state.message); render(); } return; }
    const toggle = event.target.closest('[data-twisty]');
    if (toggle) { const scope = toggle.dataset.scope; const set = scopeCollapsed(scope); const key = toggle.dataset.twisty; if (set.has(key)) set.delete(key); else set.add(key); saveCollapsed(scope); render(); return; }
    const folder = event.target.closest('[data-folder]');
    if (folder) { await chooseFolder(folder.dataset.folder); return; }
    const row = event.target.closest('[data-row]'); if (row) { await selectRow(row, event.ctrlKey || event.metaKey); return; }
    if (event.target.closest('[data-column-resize]')) return;
    const sort = event.target.closest('[data-sort]'); if (sort) { const rows = sort.dataset.kind === 'chains' ? data.chains : state.results; const key = sort.dataset.sort; state.sortDirection = state.sortKind === sort.dataset.kind && state.sortKey === key ? -state.sortDirection : 1; state.sortKind = sort.dataset.kind; state.sortKey = key; rows.sort((a, b) => (typeof a[key] === 'number' ? a[key] - b[key] : String(a[key]).localeCompare(String(b[key]))) * state.sortDirection); render(); return; }
    const el = event.target.closest('[data-action]'); if (!el) return;
    const kind = el.dataset.kind, index = Number(el.dataset.index);
    switch (el.dataset.action) {
      case 'toggle-db-folders': state.dbFolderOpen = !state.dbFolderOpen; render(); if (state.dbFolderOpen) document.querySelector('#db-folder-filter')?.focus(); break;
      case 'db-folders-clear': state.dbFolders.clear(); render(); break;
      case 'db-folders-done': state.dbFolderOpen = false; state.dbFolderFilter = ''; render(); break;
      case 'copy-chain': await copyItems('chains', state.chain ? [state.chain] : []); break;
      case 'copy-message': { const m = await loadBody('messages', state.message); await copy(state.raw ? m?.rawBody : m?.body); break; }
      case 'copy-all': await copyItems('chains', chainRows().map(c => c.id)); break;
      case 'copy-results': { const ids = new Set(state.results.map(m => m.chainId)); await copyItems('chains', [...ids]); break; }
      case 'copy-queue': await copyItems(kind, state[kind]); break;
      case 'clear-queue': state[kind] = []; render(); break;
      case 'remove-queue': state[kind] = state[kind].filter(id => id !== el.dataset.id); render(); break;
      case 'copy-prompt': await copyItems('chains', state.chains, data.prompts[index].text + '\n\n'); break;
      case 'remove-prompt': data.prompts.splice(index, 1); render(); break;
      case 'close-detail': state.detail = false; render(); break;
      case 'toggle-disclaimer': data.disclaimers[index].enabled = !data.disclaimers[index].enabled; render(); break;
      case 'remove-disclaimer': data.disclaimers.splice(index, 1); render(); break;
      case 'add-disclaimer': data.disclaimers.push({ text: '', enabled: true }); render(); app.querySelectorAll('[data-disclaimer]')[data.disclaimers.length - 1].focus(); break;
      case 'save-disclaimers': { const result = await callBackend('saveDisclaimers', { disclaimers: data.disclaimers, autoClean: data.autoClean }); if (result) { Object.assign(data, result); data.chains.forEach(row => { delete row.body; }); data.messages.forEach(row => { delete row.body; delete row.rawBody; }); render(); notice('Disclaimers saved. Existing emails are cleaned only when you start backfilling.'); } break; }
      case 'backfill': { const result = await callBackend('backfill', { disclaimers: data.disclaimers, autoClean: data.autoClean }); if (result) { data = result; data.folders = orderedFolders(data.folders); state.results = data.messages; state.chains = []; state.messages = []; state.folderChainIds = null; await chooseFolder(state.folder); notice('Backfilling completed.'); } break; }
      case 'refresh-folders': {
        const result = await callBackend('refreshFolders');
        if (result) {
          data.folders = orderedFolders(result);
          data.folderTree = treeFromPaths(data.folders);
          render();
        }
        break;
      }
      case 'refresh': { if (!state.checked.size) return notice('Tick at least one folder to refresh. Only ticked folders are scanned.'); const cutoff = Object.fromEntries(['days', 'hours', 'minutes'].map(k => [k, Number(document.getElementById(k)?.value || 0)])); if (Object.values(cutoff).some(n => !Number.isFinite(n) || n < 0)) return notice('Enter a valid non-negative cutoff.'); const result = await callBackend('refresh', { mode: el.dataset.mode, folders: [...state.checked], cutoff }); if (result) { data = result; data.folders = orderedFolders(data.folders); state.results = data.messages; state.chains = []; state.messages = []; state.folderChainIds = null; await chooseFolder(state.folder); } break; }
    }
  });
  app.addEventListener('input', event => {
    const el = event.target;
    if (el.dataset.prompt !== undefined) data.prompts[Number(el.dataset.prompt)][el.dataset.field] = el.value;
    if (el.dataset.disclaimer !== undefined) data.disclaimers[Number(el.dataset.disclaimer)].text = el.value;
    if (el.id === 'chain-filter') { const pos = el.selectionStart; state.filter = el.value; render(); const next = document.querySelector('#chain-filter'); next.focus(); next.setSelectionRange(pos, pos); }
    if (el.id === 'db-folder-filter') { const pos = el.selectionStart; state.dbFolderFilter = el.value; render(); const next = document.querySelector('#db-folder-filter'); next.focus(); next.setSelectionRange(pos, pos); }
    if (el.dataset.peopleInput) { const field = el.dataset.peopleInput, pos = el.selectionStart; state.peopleQuery[field] = el.value; state.peopleOpen = field; state.peopleActive = -1; render(); const next = app.querySelector(`[data-people-input="${field}"]`); next.focus(); next.setSelectionRange(pos, pos); }
  });
  app.addEventListener('focusin', event => {
    const field = event.target.dataset?.peopleInput;
    if (field && state.peopleOpen !== field) { state.peopleOpen = field; state.peopleActive = -1; render(); focusPeople(field); }
    else if (!field && state.peopleOpen && !event.target.closest(`[data-people="${state.peopleOpen}"]`)) { app.querySelector(`[data-people="${state.peopleOpen}"] .people-popup`)?.remove(); state.peopleOpen = null; state.peopleActive = -1; }
  });
  app.addEventListener('change', event => {
    const el = event.target;
    if (el.dataset.folderCheck) { const set = scopeSet(el.dataset.scope); if (el.checked) set.add(el.dataset.folderCheck); else set.delete(el.dataset.folderCheck); if (el.dataset.scope === 'folders') saveChecked(); render(); return; }
    if (el.dataset.branchCheck) { const set = scopeSet(el.dataset.scope); for (const path of branchPaths(el.dataset.branchCheck, el.dataset.scope)) { if (el.checked) set.add(path); else set.delete(path); } if (el.dataset.scope === 'folders') saveChecked(); render(); return; }
    if (el.id === 'show-system') { state.showSystem = el.checked; render(); }
    if (el.id === 'show-raw') { state.raw = el.checked; render(); }
    if (el.id === 'auto-clean') data.autoClean = el.checked;
    if (el.closest('#query-form')) filterValues = readFilters(el.form);
  });
  const pickedPeople = field => [...new Set([...state.people[field], state.peopleQuery[field].trim()].filter(Boolean))];
  function readFilters(form) { return { ...Object.fromEntries([...form.elements].filter(e => e.name).map(e => [e.name, e.type === 'checkbox' ? e.checked : e.value])), folders: [...state.dbFolders], from: pickedPeople('from'), to: pickedPeople('to'), cc: pickedPeople('cc') }; }
  app.addEventListener('submit', async event => {
    event.preventDefault(); if (event.target.id !== 'query-form') return;
    const f = filterValues = readFilters(event.target);
    if (adapter) { const result = await callBackend('queryMessages', f); if (result) { state.results = result; for (const m of result) { const i = data.messages.findIndex(x => x.id === m.id); if (i >= 0) data.messages[i] = m; else data.messages.push(m); } render(); } return; }
    const contains = (text, query) => String(text || '').toLowerCase().includes(query.toLowerCase());
    const anyOf = (text, terms) => !terms.length || terms.some(term => contains(text, term));
    // Demo is anchored to the sample snapshot, not today's date.
    const cutoff = f.days ? new Date(data.lastSynced + 'Z').getTime() - Number(f.days) * 86400000 : -Infinity;
    state.results = data.messages.filter(m => {
      const londonDate = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/London', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(m.received));
      const hour = Number(new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/London', hour: '2-digit', hourCycle: 'h23' }).format(new Date(m.received)));
      return (!f.folders.length || f.folders.includes(m.folder)) && (!f.fromDate || londonDate >= f.fromDate) && (!f.toDate || londonDate <= f.toDate) && new Date(m.received).getTime() >= cutoff && anyOf(m.from, f.from) && (anyOf(m.to, f.to) || (f.includeCC && anyOf(m.cc, f.to))) && anyOf(m.cc, f.cc) && contains(m.subject, f.subject) && contains(m.body, f.body) && (!f.evenings || (hour >= 18 && hour < 22));
    }).slice(0, Math.max(1, Number(f.limit) || 500)); state.selected.clear(); render();
  });
  app.addEventListener('reset', async () => { state.dbFolders.clear(); for (const field of ['from', 'to', 'cc']) { state.people[field] = []; state.peopleQuery[field] = ''; } filterValues = { folders: [], fromDate: '', toDate: '', days: '', from: [], to: [], cc: [], subject: '', body: '', limit: '500', includeCC: false, evenings: false }; if (adapter) { const result = await callBackend('queryMessages', filterValues); if (result) { result.forEach(row => merge('messages', row)); state.results = result; } } else state.results = [...data.messages]; setTimeout(render, 0); });
  app.addEventListener('keydown', event => {
    const peopleField = event.target.dataset?.peopleInput;
    if (peopleField) {
      const options = peopleOptions(peopleField);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (!options.length) return;
        state.peopleOpen = peopleField;
        state.peopleActive = event.key === 'ArrowDown' ? Math.min(state.peopleActive + 1, options.length - 1) : Math.max(state.peopleActive - 1, 0);
        render(); focusPeople(peopleField);
        app.querySelector(`[data-people="${peopleField}"] .people-option.active`)?.scrollIntoView({ block: 'nearest' });
        return;
      }
      if (event.key === 'Enter') {
        const active = options[state.peopleActive];
        if (active) { event.preventDefault(); addPerson(peopleField, active.value); }
        else if (state.peopleQuery[peopleField].trim()) { event.preventDefault(); addPerson(peopleField, state.peopleQuery[peopleField]); }
        return;
      }
      if (event.key === 'Backspace' && !event.target.value && state.people[peopleField].length) { state.people[peopleField].pop(); render(); focusPeople(peopleField); return; }
      if (event.key === 'Escape') { app.querySelector(`[data-people="${peopleField}"] .people-popup`)?.remove(); state.peopleOpen = null; state.peopleActive = -1; return; }
    }
    if (event.altKey && event.target.matches('[data-folder]') && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
      event.preventDefault();
      const source = event.target.dataset.folder;
      const tree = currentTree();
      const siblings = (parentOf(source) ? findNode(tree, parentOf(source))?.children : tree) || [];
      const index = siblings.findIndex(node => node.path === source);
      const target = siblings[index + (event.key === 'ArrowUp' ? -1 : 1)]?.path;
      if (target) { moveFolder(source, target, event.key === 'ArrowDown'); app.querySelector(`[data-folder="${CSS.escape(source)}"]`)?.focus(); }
      return;
    }
    if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('[data-row], [data-sort]')) { event.preventDefault(); event.target.click(); }
  });
  app.addEventListener('dragstart', event => {
    if (event.target.closest('[data-column-resize]')) { event.preventDefault(); return; }
    const column = event.target.closest('[data-column-key]');
    if (column) {
      event.dataTransfer.setData('application/x-outlook-column', JSON.stringify({ kind: column.dataset.kind, key: column.dataset.columnKey }));
      event.dataTransfer.effectAllowed = 'move';
      column.classList.add('column-dragging');
      return;
    }
    const folder = event.target.closest('[data-folder-row]');
    if (folder) {
      event.dataTransfer.setData('application/x-outlook-folder-order', folder.dataset.folderRow);
      event.dataTransfer.effectAllowed = 'move';
      folder.classList.add('dragging');
      return;
    }
    const row = event.target.closest('[data-row]'); if (!row) return;
    const ids = state.selected.has(row.dataset.row) ? [...state.selected] : [row.dataset.row];
    event.dataTransfer.setData('application/x-outlook-digest', JSON.stringify({ kind: row.dataset.kind, ids })); event.dataTransfer.effectAllowed = 'copy';
  });
  app.addEventListener('dragover', event => { const column = event.target.closest('[data-column-key]'); if (column && event.dataTransfer.types.includes('application/x-outlook-column')) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; app.querySelectorAll('.column-dragover').forEach(header => header.classList.remove('column-dragover')); column.classList.add('column-dragover'); return; } const folder = event.target.closest('[data-folder-row]'); if (folder) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; app.querySelectorAll('.folder-dragover').forEach(row => row.classList.remove('folder-dragover')); folder.classList.add('folder-dragover'); return; } const zone = event.target.closest('[data-drop]'); if (zone) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; zone.classList.add('dragover'); } });
  app.addEventListener('dragleave', event => { event.target.closest('[data-drop]')?.classList.remove('dragover'); event.target.closest('[data-column-key]')?.classList.remove('column-dragover'); });
  app.addEventListener('dragend', () => app.querySelectorAll('.dragging, .folder-dragover, .dragover, .column-dragging, .column-dragover').forEach(element => element.classList.remove('dragging', 'folder-dragover', 'dragover', 'column-dragging', 'column-dragover')));
  app.addEventListener('drop', async event => {
    const columnTarget = event.target.closest('[data-column-key]');
    const columnPayload = event.dataTransfer.getData('application/x-outlook-column');
    if (columnTarget && columnPayload) {
      event.preventDefault();
      try {
        const { kind, key } = JSON.parse(columnPayload);
        if (kind !== columnTarget.dataset.kind || key === columnTarget.dataset.columnKey) return;
        const columns = state.tableColumns[kind];
        const sourceIndex = columns.findIndex(column => column.key === key);
        if (sourceIndex < 0) return;
        const [moving] = columns.splice(sourceIndex, 1);
        let targetIndex = columns.findIndex(column => column.key === columnTarget.dataset.columnKey);
        if (event.clientX > columnTarget.getBoundingClientRect().left + columnTarget.getBoundingClientRect().width / 2) targetIndex++;
        columns.splice(targetIndex, 0, moving);
        saveTableColumns();
        render();
      } catch { /* Ignore unrelated drags over column headings. */ }
      return;
    }
    const folderTarget = event.target.closest('[data-folder-row]');
    if (folderTarget) {
      event.preventDefault();
      const source = event.dataTransfer.getData('application/x-outlook-folder-order');
      const box = folderTarget.getBoundingClientRect();
      moveFolder(source, folderTarget.dataset.folderRow, event.clientY > box.top + box.height / 2);
      return;
    }
    const zone = event.target.closest('[data-drop]'); if (!zone) return; event.preventDefault(); zone.classList.remove('dragover');
    try { const payload = JSON.parse(event.dataTransfer.getData('application/x-outlook-digest')); if (!['messages', 'chains'].includes(payload.kind) || !Array.isArray(payload.ids)) return;
      const kind = zone.dataset.drop; let ids = payload.ids;
      if (kind === 'chains' && payload.kind === 'messages') ids = ids.map(id => data.messages.find(m => m.id === id)?.chainId);
      if (kind === 'messages' && payload.kind === 'chains') ids = ids.map(id => data.chains.find(chain => chain.id === id)?.latestMessageId);
      if (adapter) for (const id of ids.filter(Boolean)) await loadBody(kind, id);
      ids = ids.filter(id => data[kind].some(item => item.id === id)); state[kind] = [...new Set([...state[kind], ...ids])]; render();
    } catch { notice('Drag rows from the table. File imports are not included.'); }
  });
  async function start() {
    if (adapter) { app.textContent = 'Loading…'; try { if (!adapter.getInitialData) throw new Error('getInitialData is required'); data = await adapter.getInitialData(); await loadServerPreferences(); data.folders = orderedFolders(data.folders); data.folderTree = data.folderTree?.length ? data.folderTree : treeFromPaths(data.folders); state.results = data.messages; state.folder = data.folders[0] || ''; state.checked = loadChecked(data.folders) || new Set(); state.chain = data.chains[0]?.id; state.message = data.messages[0]?.id; } catch (error) { app.textContent = `Unable to load backend data: ${error.message}`; return; } }
    else { data.folders = orderedFolders(data.folders); data.folderTree = treeFromPaths(data.folders); state.checked = loadChecked(data.folders) || state.checked; }
    state.collapsed = loadCollapsed(collapsedKey);
    state.dbCollapsed = loadCollapsed(dbCollapsedKey);
    loadLayoutWidths();
    loadQueueSplits();
    loadTableColumns();
    render();
    if (adapter) { await loadBody('chains', state.chain); await loadBody('messages', state.message); render(); }
  }
  start();
})();
