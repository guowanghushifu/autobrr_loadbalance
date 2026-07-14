const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

let snapshot = { configured_instances: [], instances: [], whitelist: [], events: [], telegram: {}, metrics_history: [], tracker_stats: [], traffic_totals: {}, dashboard_timezone: { name: 'Asia/Shanghai', configured: false } };
let toastTimer;
let telegramDirty = false;
let draggedColumn = null;
let quickEditingName = null;
let logRecords = [];
let logCursor = 0;
let logRequestActive = false;
let sortState = { key: 'name', direction: 'asc' };
let trackerSortState = { key: 'torrent_count', direction: 'desc' };

const viewTitles = { overview: '概览', instances: '实例', traffic: '流量分析', webhook: 'Webhook', telegram: 'Telegram', events: '事件', logs: '日志' };
const sortKeyNames = { upload_speed: '上传速度', download_speed: '下载速度', upload_download_speed: '上传与下载速度', active_downloads: '活跃下载数', total_downloads: '全部下载数' };

function notify(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3400);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function speed(kib) {
  const megabytesPerSecond = Number(kib || 0) * 1024 / 1_000_000;
  return `${megabytesPerSecond.toFixed(2)} M/s`;
}

function traffic(bytes) {
  return `${(Number(bytes || 0) / 1_000_000_000_000).toFixed(3)} TB`;
}

function dashboardTimezone() { return snapshot.dashboard_timezone?.name || 'Asia/Shanghai'; }
function timezoneDisplayName(name) { return name === 'Asia/Shanghai' ? 'UTC+8 北京时间' : name; }
function formatDateTime(value) {
  try { return new Date(value).toLocaleString('zh-CN', { timeZone: dashboardTimezone() }); }
  catch (_) { return new Date(value).toLocaleString('zh-CN'); }
}
function formatTime(value) {
  try { return new Date(value).toLocaleTimeString('zh-CN', { timeZone: dashboardTimezone(), hour: '2-digit', minute: '2-digit' }); }
  catch (_) { return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }
}

function showView(name, updateHash = true) {
  if (!viewTitles[name]) name = 'overview';
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === name));
  $$('.view').forEach((panel) => panel.classList.toggle('active', panel.dataset.viewPanel === name));
  $('#page-title').textContent = viewTitles[name];
  if (updateHash) history.replaceState(null, '', `#${name}`);
  if (name === 'traffic') requestAnimationFrame(renderTraffic);
  if (name === 'logs') fetchLogs(logCursor === 0);
}

function setIconLabel(element, iconName, text) {
  const icon = document.createElement('img'); icon.className = 'button-icon'; icon.src = `/static/icons/${iconName}.svg`; icon.alt = '';
  element.replaceChildren(icon, document.createTextNode(text));
}

function setIconOnly(element, iconName, label) {
  const icon = document.createElement('img'); icon.className = 'button-icon'; icon.src = `/static/icons/${iconName}.svg`; icon.alt = '';
  element.replaceChildren(icon); element.classList.add('icon-button'); element.title = label; element.setAttribute('aria-label', label);
}

function cell(row, content, className = '') {
  const td = document.createElement('td');
  td.className = className;
  if (content instanceof Node) td.append(content); else td.textContent = content;
  row.append(td);
  return td;
}

function metric(primary, secondary = '') {
  const box = document.createElement('div'); box.className = 'metric';
  const main = document.createElement('span'); main.textContent = primary; box.append(main);
  if (secondary) { const small = document.createElement('small'); small.textContent = secondary; box.append(small); }
  return box;
}

const instanceColumns = {
  name: {
    label: '实例', value: (row) => row.config.name.toLowerCase(),
    render: (row) => { const box = document.createElement('div'); box.className = 'instance-name'; const dot = document.createElement('i'); dot.className = `dot ${row.state.connected ? 'ok' : ''}`; const text = document.createElement('span'); text.textContent = row.config.name; box.append(dot, text); return box; },
  },
  ip: {
    label: 'IP', value: (row) => instanceHost(row.config.url),
    render: (row) => {
      const box = document.createElement('div'); box.className = 'ip-cell';
      const value = document.createElement('span'); value.textContent = instanceHost(row.config.url);
      const edit = document.createElement('button'); edit.className = 'text-command'; edit.type = 'button'; setIconOnly(edit, 'pen', '编辑 IP'); edit.onclick = () => startQuickIpEdit(row.config, box);
      box.append(value, edit); return box;
    },
  },
  upload: { label: '上传速度', value: (row) => Number(row.state.upload_speed_kib || 0), render: (row) => metric(speed(row.state.upload_speed_kib)) },
  download: { label: '下载速度', value: (row) => Number(row.state.download_speed_kib || 0), render: (row) => metric(speed(row.state.download_speed_kib)) },
  uploaded: { label: '上传量', value: (row) => Number(row.state.total_uploaded_bytes || 0), render: (row) => metric(`今日 ${traffic(row.state.today_uploaded_bytes)}`, `累计 ${traffic(row.state.total_uploaded_bytes)}`) },
  downloaded: { label: '下载量', value: (row) => Number(row.state.total_downloaded_bytes || 0), render: (row) => metric(`今日 ${traffic(row.state.today_downloaded_bytes)}`, `累计 ${traffic(row.state.total_downloaded_bytes)}`) },
  total_traffic: { label: '总流量', value: (row) => Number(row.state.total_traffic_bytes || 0), render: (row) => metric(`今日 ${traffic(row.state.today_traffic_bytes)}`, `累计 ${traffic(row.state.total_traffic_bytes)}`) },
  tasks: { label: '下载任务', value: (row) => Number(row.state.active_downloads || 0) + Number(row.state.waiting_downloads || 0), render: (row) => metric(`${row.state.active_downloads || 0} 活跃`, `${row.state.waiting_downloads || 0} 等待`) },
  space: { label: '剩余空间', value: (row) => Number(row.state.free_space_gib || 0), render: (row) => metric(`${row.state.free_space_gib || 0} GiB`, `保留 ${row.state.reserved_space_gib || 0} GiB`) },
  traffic: { label: 'VPS 流量限制', value: (row) => Number(row.state.traffic_out_gib || 0), render: (row) => row.state.traffic_limit_gib ? `${row.state.traffic_out_gib} / ${row.state.traffic_limit_gib} GiB` : '未限制' },
  added: { label: '累计添加', value: (row) => Number(row.state.total_added_tasks || 0), render: (row) => String(row.state.total_added_tasks || 0) },
  actions: {
    label: '操作', sortable: false,
    render: (row) => {
      const actions = document.createElement('div'); actions.className = 'row-actions';
      const open = document.createElement('a'); open.className = 'button open'; setIconOnly(open, 'link-round', '打开 qBittorrent'); open.href = row.config.url; open.target = '_blank'; open.rel = 'noopener noreferrer';
      const edit = document.createElement('button'); edit.className = 'button secondary'; setIconOnly(edit, 'pen', '编辑实例'); edit.type = 'button'; edit.onclick = () => openInstance(row.config);
      const clone = document.createElement('button'); clone.className = 'button secondary'; setIconOnly(clone, 'copy', '克隆实例'); clone.type = 'button'; clone.onclick = () => cloneInstance(row.config.name);
      const remove = document.createElement('button'); remove.className = 'button danger'; setIconOnly(remove, 'trash-bin-trash', '删除实例'); remove.type = 'button'; remove.onclick = () => deleteInstance(row.config.name);
      actions.append(open, edit, clone, remove); return actions;
    },
  },
};

const defaultColumnOrder = Object.keys(instanceColumns);
function loadColumnOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem('instanceColumnOrder') || '[]');
    const valid = saved.filter((key) => instanceColumns[key]);
    return [...new Set([...valid, ...defaultColumnOrder])];
  } catch (_) { return [...defaultColumnOrder]; }
}
let columnOrder = loadColumnOrder();

function instanceHost(url) {
  try { return new URL(url).hostname.replace(/^\[|\]$/g, ''); } catch (_) { return String(url || '').replace(/^https?:\/\//, '').split(/[/:]/)[0] || '-'; }
}

function replaceInstanceHost(address, host) {
  const value = host.trim();
  const ipv4 = value.split('.');
  const validIpv4 = ipv4.length === 4 && ipv4.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
  let normalized = value;
  if (value.includes(':')) {
    try { normalized = new URL(`http://[${value.replace(/^\[|\]$/g, '')}]`).hostname; } catch (_) { throw new Error('请输入有效的 IPv4 或 IPv6 地址'); }
  } else if (!validIpv4) {
    throw new Error('请输入有效的 IPv4 或 IPv6 地址');
  }
  const parsed = new URL(address);
  parsed.hostname = normalized;
  const result = parsed.toString();
  return address.endsWith('/') ? result : result.replace(/\/$/, '');
}

function instancePayload(config, url) {
  return { original_name: config.name, name: config.name, url, username: config.username, password: '', traffic_check_url: config.traffic_check_url || '', traffic_limit: config.traffic_limit ?? 0, reserved_space: config.reserved_space ?? 0 };
}

function startQuickIpEdit(config, container) {
  quickEditingName = config.name; container.replaceChildren(); container.className = 'quick-ip-form';
  const input = document.createElement('input'); input.value = instanceHost(config.url); input.setAttribute('aria-label', `${config.name} IP`);
  const save = document.createElement('button'); save.className = 'button'; save.type = 'button'; setIconLabel(save, 'diskette', '保存');
  const cancel = document.createElement('button'); cancel.className = 'button secondary'; cancel.type = 'button'; cancel.textContent = '取消';
  cancel.onclick = () => { quickEditingName = null; renderInstances(); };
  save.onclick = async () => {
    save.disabled = true;
    try { const url = replaceInstanceHost(config.url, input.value); const result = await api('/api/dashboard/instances', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(instancePayload(config, url)) }); quickEditingName = null; notify(`${result.name} 的 IP 已更新`); await refresh(); }
    catch (error) { notify(error.message); save.disabled = false; }
  };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') save.click(); if (event.key === 'Escape') cancel.click(); });
  container.append(input, save, cancel); input.focus(); input.select();
}

function renderInstanceHeader() {
  const head = $('#instances-head'); head.replaceChildren();
  columnOrder.forEach((key) => {
    const column = instanceColumns[key];
    const th = document.createElement('th'); th.draggable = true; th.dataset.column = key; th.title = '拖动调整列顺序';
    const label = document.createElement(column.sortable === false ? 'span' : 'button');
    label.className = column.sortable === false ? '' : 'sort-button'; label.textContent = column.label;
    if (column.sortable !== false) {
      label.type = 'button'; label.title = `按${column.label}排序`;
      if (sortState.key === key) { const indicator = document.createElement('img'); indicator.className = 'sort-indicator'; indicator.src = `/static/icons/arrow-${sortState.direction === 'asc' ? 'up' : 'down'}.svg`; indicator.alt = sortState.direction === 'asc' ? '升序' : '降序'; label.append(indicator); }
      label.addEventListener('click', () => { sortState = { key, direction: sortState.key === key && sortState.direction === 'asc' ? 'desc' : 'asc' }; renderInstances(); });
    }
    th.append(label);
    th.addEventListener('dragstart', (event) => { draggedColumn = key; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', key); requestAnimationFrame(() => th.classList.add('dragging')); });
    th.addEventListener('dragend', () => { draggedColumn = null; $$('#instances-head th').forEach((item) => item.classList.remove('dragging', 'drop-target')); });
    th.addEventListener('dragover', (event) => { event.preventDefault(); if (draggedColumn && draggedColumn !== key) th.classList.add('drop-target'); });
    th.addEventListener('dragleave', () => th.classList.remove('drop-target'));
    th.addEventListener('drop', (event) => { event.preventDefault(); th.classList.remove('drop-target'); if (!draggedColumn || draggedColumn === key) return; const from = columnOrder.indexOf(draggedColumn); const to = columnOrder.indexOf(key); columnOrder.splice(from, 1); columnOrder.splice(to, 0, draggedColumn); localStorage.setItem('instanceColumnOrder', JSON.stringify(columnOrder)); renderInstances(); });
    head.append(th);
  });
}

function renderInstances() {
  renderInstanceHeader();
  const body = $('#instances-body'); body.replaceChildren();
  if (!snapshot.configured_instances.length) { const row = document.createElement('tr'); cell(row, '尚未配置 qBittorrent 实例', 'empty').colSpan = columnOrder.length; body.append(row); return; }
  const runtime = new Map(snapshot.instances.map((item) => [item.name, item]));
  const rows = snapshot.configured_instances.map((config) => ({ config, state: runtime.get(config.name) || {} }));
  const column = instanceColumns[sortState.key];
  rows.sort((left, right) => { const a = column.value(left); const b = column.value(right); const result = typeof a === 'string' ? a.localeCompare(b, 'zh-CN') : a - b; return sortState.direction === 'asc' ? result : -result; });
  rows.forEach((item) => { const row = document.createElement('tr'); columnOrder.forEach((key) => cell(row, instanceColumns[key].render(item))); body.append(row); });
}

function renderOverviewInstances() {
  const target = $('#overview-instances'); target.replaceChildren();
  if (!snapshot.instances.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '尚未配置实例'; target.append(empty); return; }
  snapshot.instances.forEach((instance) => {
    const row = document.createElement('div'); row.className = 'overview-instance';
    const name = document.createElement('strong'); const dot = document.createElement('i'); dot.className = `dot ${instance.connected ? 'ok' : ''}`; name.append(dot, document.createTextNode(instance.name));
    const speeds = document.createElement('div'); speeds.className = 'speeds'; speeds.append(document.createTextNode(`↑ ${speed(instance.upload_speed_kib)}`)); const down = document.createElement('small'); down.textContent = `↓ ${speed(instance.download_speed_kib)}`; speeds.append(down);
    const space = document.createElement('div'); space.className = 'space'; space.append(document.createTextNode(`${instance.free_space_gib} GiB`)); const reserved = document.createElement('small'); reserved.textContent = `保留 ${instance.reserved_space_gib} GiB`; space.append(reserved);
    row.append(name, speeds, space); target.append(row);
  });
}

function renderWhitelist() {
  const list = $('#whitelist-list'); list.replaceChildren(); const mode = $('#whitelist-mode');
  if (!snapshot.whitelist.length) { mode.textContent = '当前为空：Webhook 允许任意来源 IP（兼容旧版行为）'; mode.className = 'notice warning'; return; }
  mode.textContent = `已启用限制，仅允许以下 ${snapshot.whitelist.length} 个地址或网段`; mode.className = 'notice';
  snapshot.whitelist.forEach((entry) => { const tag = document.createElement('div'); tag.className = 'tag'; const text = document.createElement('span'); text.textContent = entry; const remove = document.createElement('button'); remove.textContent = '×'; remove.title = `移除 ${entry}`; remove.onclick = () => deleteWhitelist(entry); tag.append(text, remove); list.append(tag); });
}

const statusNames = { queued: '已入队', success: '已添加', error: '失败', blocked: '已拦截', config: '配置' };
function eventNode(event) {
  const item = document.createElement('li');
  const status = document.createElement('span'); status.className = `event-status ${event.status}`; status.textContent = statusNames[event.status] || event.status;
  const detail = document.createElement('div'); detail.className = 'event-detail'; detail.textContent = event.release_name;
  const small = document.createElement('small'); small.textContent = [event.detail, event.source_ip].filter(Boolean).join(' · '); detail.append(small);
  const time = document.createElement('time'); time.textContent = formatDateTime(event.timestamp); item.append(status, detail, time); return item;
}

function renderEvents() {
  const full = $('#events-list'); const compact = $('#overview-events'); full.replaceChildren(); compact.replaceChildren();
  if (!snapshot.events.length) { [full, compact].forEach((list) => { const empty = document.createElement('li'); empty.className = 'empty'; empty.textContent = '暂无事件'; list.append(empty); }); return; }
  snapshot.events.forEach((event, index) => { full.append(eventNode(event)); if (index < 5) compact.append(eventNode(event)); });
}

function renderSummary() {
  const connected = snapshot.instances.filter((item) => item.connected).length;
  const upload = snapshot.instances.reduce((sum, item) => sum + Number(item.upload_speed_kib || 0), 0);
  const download = snapshot.instances.reduce((sum, item) => sum + Number(item.download_speed_kib || 0), 0);
  $('#connected-count').textContent = `${connected} / ${snapshot.instances.length}`;
  $('#total-upload').textContent = speed(upload); $('#total-download').textContent = speed(download); $('#pending-count').textContent = snapshot.pending_count;
  $('#chart-upload-now').textContent = speed(upload); $('#chart-download-now').textContent = speed(download);
  $('#traffic-upload-total').textContent = traffic(snapshot.traffic_totals?.uploaded_bytes);
  $('#traffic-download-total').textContent = traffic(snapshot.traffic_totals?.downloaded_bytes);
  $('#traffic-upload-today').textContent = traffic(snapshot.traffic_totals?.today_uploaded_bytes);
  $('#traffic-download-today').textContent = traffic(snapshot.traffic_totals?.today_downloaded_bytes);
  $('#updated-at').textContent = formatDateTime(snapshot.updated_at); $('#sort-key').textContent = `分配策略 · ${sortKeyNames[snapshot.sort_key] || snapshot.sort_key}`;
  $('#timezone-button').textContent = `时区 · ${timezoneDisplayName(dashboardTimezone())}`;
}

function renderTelegram() {
  const telegram = snapshot.telegram || {};
  if (!telegramDirty) {
    $('#telegram-enabled').checked = Boolean(telegram.enabled);
    $('#telegram-chat-id').value = telegram.chat_id || '';
    $('#telegram-timeout').value = telegram.timeout || 10;
  }
  $('#telegram-state').textContent = telegramDirty ? `待保存：${$('#telegram-enabled').checked ? '启用' : '停用'}` : (telegram.enabled ? `已启用 · ${telegram.chat_id}` : (telegram.bot_token_configured ? '已配置，当前停用' : '尚未配置 Token'));
  $('#telegram-token').placeholder = telegram.bot_token_configured ? '已保存；留空则保持当前 Token' : '请输入 BotFather 提供的 Token';
}

function populateTimezoneOptions(selected) {
  const select = $('#timezone-select');
  if (!select.options.length) {
    const fallback = ['Asia/Shanghai', 'UTC', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Los_Angeles', 'Australia/Sydney'];
    const supported = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : fallback;
    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const zones = [...new Set(['Asia/Shanghai', browserTimezone, ...supported].filter(Boolean))];
    zones.forEach((zone) => { const option = document.createElement('option'); option.value = zone; option.textContent = timezoneDisplayName(zone); select.append(option); });
  }
  if (![...select.options].some((option) => option.value === selected)) { const option = document.createElement('option'); option.value = selected; option.textContent = timezoneDisplayName(selected); select.append(option); }
  select.value = selected;
}

function openTimezoneDialog(required = false) {
  const dialog = $('#timezone-dialog'); populateTimezoneOptions(dashboardTimezone()); dialog.dataset.required = required ? 'true' : 'false'; $('#timezone-cancel').hidden = required; $('#timezone-error').textContent = '';
  if (!dialog.open) dialog.showModal();
}

function renderTimezone() {
  if (!snapshot.dashboard_timezone?.configured) openTimezoneDialog(true);
}

function drawLineChart(canvas, key, color) {
  const rect = canvas.getBoundingClientRect(); if (!rect.width) return;
  const width = rect.width; const height = 280; const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
  const context = canvas.getContext('2d'); context.setTransform(dpr, 0, 0, dpr, 0, 0); context.clearRect(0, 0, width, height);
  const padding = { left: 60, right: 18, top: 20, bottom: 34 }; const plotWidth = width - padding.left - padding.right; const plotHeight = height - padding.top - padding.bottom;
  const points = snapshot.metrics_history || []; const values = points.map((point) => Number(point[key] || 0)); const maximum = Math.max(1, ...values) * 1.12;
  context.font = '11px "LXGW Bright", sans-serif'; context.textAlign = 'right'; context.textBaseline = 'middle';
  for (let index = 0; index <= 4; index += 1) { const y = padding.top + (plotHeight * index / 4); context.strokeStyle = '#e4e9eb'; context.lineWidth = 1; context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke(); context.fillStyle = '#637178'; context.fillText(speed(maximum * (1 - index / 4)).replace('/s', ''), padding.left - 8, y); }
  if (points.length) {
    context.strokeStyle = color; context.lineWidth = 2; context.lineJoin = 'round'; context.lineCap = 'round'; context.beginPath();
    values.forEach((value, index) => { const x = padding.left + (points.length === 1 ? plotWidth : plotWidth * index / (points.length - 1)); const y = padding.top + plotHeight - (value / maximum * plotHeight); if (index === 0) context.moveTo(x, y); else context.lineTo(x, y); }); context.stroke();
    context.fillStyle = '#637178'; context.textBaseline = 'top'; context.textAlign = 'left'; context.fillText(formatTime(points[0].timestamp), padding.left, height - 24); context.textAlign = 'right'; context.fillText(formatTime(points[points.length - 1].timestamp), width - padding.right, height - 24);
  } else { context.fillStyle = '#637178'; context.textAlign = 'center'; context.fillText('等待采集数据', padding.left + plotWidth / 2, padding.top + plotHeight / 2); }
}

const trackerColumns = {
  tracker: { label: 'Tracker', value: (row) => row.tracker, render: (row) => row.tracker },
  instances: { label: '实例', value: (row) => (row.instances || []).join(', '), render: (row) => (row.instances || []).join(', ') },
  instance_counts: { label: '实例种子数', value: (row) => (row.instance_torrent_counts || []).map((item) => `${item.name}:${item.torrent_count}`).join('|'), render: (row) => (row.instance_torrent_counts || []).map((item) => `${item.name}：${item.torrent_count}`).join(' · ') },
  torrent_count: { label: '种子数', value: (row) => Number(row.torrent_count || 0), render: (row) => String(row.torrent_count || 0) },
  active_downloads: { label: '活跃下载', value: (row) => Number(row.active_downloads || 0), render: (row) => String(row.active_downloads || 0) },
  upload_speed: { label: '上传速度', value: (row) => Number(row.upload_speed_kib || 0), render: (row) => speed(row.upload_speed_kib) },
  download_speed: { label: '下载速度', value: (row) => Number(row.download_speed_kib || 0), render: (row) => speed(row.download_speed_kib) },
  today_uploaded: { label: '今日上传', value: (row) => Number(row.today_uploaded_bytes || 0), render: (row) => traffic(row.today_uploaded_bytes) },
  today_downloaded: { label: '今日下载', value: (row) => Number(row.today_downloaded_bytes || 0), render: (row) => traffic(row.today_downloaded_bytes) },
  uploaded: { label: '累计上传', value: (row) => Number(row.uploaded_bytes || 0), render: (row) => traffic(row.uploaded_bytes) },
  downloaded: { label: '累计下载', value: (row) => Number(row.downloaded_bytes || 0), render: (row) => traffic(row.downloaded_bytes) },
};
const trackerColumnKeys = Object.keys(trackerColumns);

function renderTrackerHeader() {
  const head = $('#tracker-head'); head.replaceChildren();
  trackerColumnKeys.forEach((key) => {
    const column = trackerColumns[key]; const th = document.createElement('th'); const button = document.createElement('button');
    button.className = 'sort-button'; button.type = 'button'; button.title = `按${column.label}排序`; button.textContent = column.label;
    if (trackerSortState.key === key) { const indicator = document.createElement('img'); indicator.className = 'sort-indicator'; indicator.src = `/static/icons/arrow-${trackerSortState.direction === 'asc' ? 'up' : 'down'}.svg`; indicator.alt = trackerSortState.direction === 'asc' ? '升序' : '降序'; button.append(indicator); }
    button.addEventListener('click', () => { trackerSortState = { key, direction: trackerSortState.key === key && trackerSortState.direction === 'asc' ? 'desc' : 'asc' }; renderTrackerStats(); });
    th.append(button); head.append(th);
  });
}

function renderTrackerStats() {
  renderTrackerHeader();
  const body = $('#tracker-body'); body.replaceChildren(); const trackers = [...(snapshot.tracker_stats || [])]; $('#tracker-count').textContent = `${trackers.length} TRACKERS`;
  if (!trackers.length) { const row = document.createElement('tr'); cell(row, '暂无 tracker 数据', 'empty').colSpan = trackerColumnKeys.length; body.append(row); return; }
  const column = trackerColumns[trackerSortState.key];
  trackers.sort((left, right) => { const a = column.value(left); const b = column.value(right); const result = typeof a === 'string' ? a.localeCompare(b, 'zh-CN') : a - b; return trackerSortState.direction === 'asc' ? result : -result; });
  trackers.forEach((tracker) => { const row = document.createElement('tr'); trackerColumnKeys.forEach((key) => cell(row, trackerColumns[key].render(tracker))); body.append(row); });
}

function renderTraffic() { drawLineChart($('#upload-chart'), 'upload_speed_kib', '#087f5b'); drawLineChart($('#download-chart'), 'download_speed_kib', '#087e8b'); renderTrackerStats(); }

async function refresh() {
  try {
    snapshot = await api('/api/dashboard/status');
    renderSummary(); renderOverviewInstances(); if (!quickEditingName) renderInstances(); renderWhitelist(); renderTelegram(); renderTimezone(); renderEvents(); renderTrackerStats();
    if ($('[data-view-panel="traffic"]').classList.contains('active')) requestAnimationFrame(renderTraffic);
    $('#connection-state').classList.remove('offline');
  } catch (error) { $('#connection-state').classList.add('offline'); notify(error.message); }
}

function openInstance(config = null) {
  showView('instances'); $('#instance-form').reset(); $('#instance-error').textContent = ''; $('#original-name').value = config?.name || ''; $('#editor-title').textContent = config ? `编辑 ${config.name}` : '添加实例';
  $('#instance-name').value = config?.name || ''; $('#instance-url').value = config?.url || ''; $('#instance-username').value = config?.username || ''; $('#traffic-url').value = config?.traffic_check_url || ''; $('#traffic-limit').value = config?.traffic_limit ?? 0; $('#reserved-space').value = config?.reserved_space ?? 0;
  $('#password-hint').textContent = config?.has_password ? '留空则保持当前密码' : '新实例必须填写密码'; $('#instance-password').required = !config?.has_password; $('#instance-editor').hidden = false; $('#instance-editor').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$('#instance-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('#save-instance'); button.disabled = true; $('#instance-error').textContent = '';
  const payload = { original_name: $('#original-name').value, name: $('#instance-name').value, url: $('#instance-url').value, username: $('#instance-username').value, password: $('#instance-password').value, traffic_check_url: $('#traffic-url').value, traffic_limit: $('#traffic-limit').value, reserved_space: $('#reserved-space').value };
  try { const result = await api('/api/dashboard/instances', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); $('#instance-editor').hidden = true; notify(`${result.name} 已保存${result.connected ? '并连接' : '，但连接失败'}`); await refresh(); } catch (error) { $('#instance-error').textContent = error.message; } finally { button.disabled = false; }
});

async function deleteInstance(name) { if (!confirm(`确认删除实例“${name}”？`)) return; try { await api('/api/dashboard/instances', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); notify(`${name} 已删除`); await refresh(); } catch (error) { notify(error.message); } }

async function cloneInstance(name) { try { const result = await api('/api/dashboard/instances/clone', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); notify(`${result.name} 已克隆${result.connected ? '并连接' : '，但连接失败'}`); await refresh(); } catch (error) { notify(error.message); } }

$('#whitelist-form').addEventListener('submit', async (event) => { event.preventDefault(); const input = $('#whitelist-entry'); try { await api('/api/dashboard/whitelist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ entry: input.value }) }); input.value = ''; await refresh(); } catch (error) { notify(error.message); } });
async function deleteWhitelist(entry) { if (!confirm(`移除白名单“${entry}”？`)) return; try { await api('/api/dashboard/whitelist', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ entry }) }); await refresh(); } catch (error) { notify(error.message); } }

$('#config-file').addEventListener('change', async (event) => { const file = event.target.files[0]; if (!file) return; if (!confirm('导入将替换当前负载均衡配置并重建实例连接，确认继续？')) { event.target.value = ''; return; } const form = new FormData(); form.append('config', file); try { const result = await api('/api/dashboard/config/import', { method: 'POST', body: form }); notify(`已导入 ${result.instances} 个实例；端口等服务设置需重启生效`); await refresh(); } catch (error) { notify(error.message); } finally { event.target.value = ''; } });

$('#telegram-form').addEventListener('input', () => { telegramDirty = true; renderTelegram(); });
$('#telegram-form').addEventListener('change', () => { telegramDirty = true; renderTelegram(); });
$('#telegram-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('#save-telegram'); button.disabled = true; $('#telegram-error').textContent = '';
  const payload = { enabled: $('#telegram-enabled').checked, bot_token: $('#telegram-token').value, chat_id: $('#telegram-chat-id').value, timeout: $('#telegram-timeout').value };
  try { await api('/api/dashboard/telegram', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); $('#telegram-token').value = ''; telegramDirty = false; notify(`Telegram 已${payload.enabled ? '启用' : '停用'}`); await refresh(); } catch (error) { $('#telegram-error').textContent = error.message; } finally { button.disabled = false; }
});

$('#test-telegram').addEventListener('click', async () => { const button = $('#test-telegram'); $('#telegram-error').textContent = ''; if (telegramDirty) { $('#telegram-error').textContent = '请先保存当前配置'; return; } button.disabled = true; try { await api('/api/dashboard/telegram/test', { method: 'POST' }); notify('测试通知已发送'); } catch (error) { $('#telegram-error').textContent = error.message; } finally { button.disabled = false; } });

const logLevelWeight = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
function renderLogs() {
  const stream = $('#log-stream'); const threshold = logLevelWeight[$('#log-level').value] || 20; const descending = $('#log-order').value === 'desc';
  stream.classList.toggle('indent-lines', $('#log-indent').checked); stream.classList.toggle('flat-lines', !$('#log-indent').checked); stream.classList.toggle('no-wrap', $('#log-nowrap').checked);
  let records = logRecords.filter((record) => (logLevelWeight[record.level] || 20) >= threshold); if (descending) records = [...records].reverse(); stream.replaceChildren();
  if (!records.length) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '当前级别暂无日志'; stream.append(empty); }
  records.forEach((record) => { const row = document.createElement('div'); row.className = `log-row ${$('#log-indent').checked ? 'indented' : ''}`; const time = document.createElement('span'); time.className = 'log-time'; time.textContent = formatDateTime(record.timestamp); const level = document.createElement('span'); level.className = `log-level ${record.level}`; level.textContent = record.level; const name = document.createElement('span'); name.className = 'log-name'; name.textContent = record.logger; const message = document.createElement('span'); message.className = 'log-message'; message.textContent = record.message; row.append(time, level, name, message); stream.append(row); });
  $('#log-status').textContent = `${records.length} 条 · 游标 ${logCursor}`;
  if ($('#log-follow').checked) requestAnimationFrame(() => { stream.scrollTop = descending ? 0 : stream.scrollHeight; });
}

async function fetchLogs(reset = false) {
  if (logRequestActive) return; logRequestActive = true;
  if (reset) { logCursor = 0; logRecords = []; }
  try { const result = await api(`/api/dashboard/logs?after=${logCursor}&limit=500`); const ids = new Set(logRecords.map((record) => record.id)); result.logs.forEach((record) => { if (!ids.has(record.id)) logRecords.push(record); }); logRecords = logRecords.slice(-500); logCursor = Math.max(logCursor, Number(result.cursor || 0)); renderLogs(); }
  catch (error) { $('#log-status').textContent = error.message; }
  finally { logRequestActive = false; }
}

$$('.nav-item').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
$$('[data-go-view]').forEach((button) => button.addEventListener('click', () => showView(button.dataset.goView)));
$('#add-instance').addEventListener('click', () => openInstance());
$('#cancel-instance').addEventListener('click', () => { $('#instance-editor').hidden = true; $('#instance-form').reset(); });
$('#refresh-logs').addEventListener('click', () => fetchLogs(true));
$('#timezone-button').addEventListener('click', () => openTimezoneDialog(false));
$('#timezone-cancel').addEventListener('click', () => $('#timezone-dialog').close());
$('#timezone-dialog').addEventListener('cancel', (event) => { if ($('#timezone-dialog').dataset.required === 'true') event.preventDefault(); });
$('#timezone-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = $('#timezone-save'); button.disabled = true; $('#timezone-error').textContent = '';
  try { const result = await api('/api/dashboard/timezone', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ timezone: $('#timezone-select').value }) }); snapshot.dashboard_timezone = { ...result, configured: true }; $('#timezone-dialog').close(); notify(`时区已设置为 ${timezoneDisplayName(result.name)}`); await refresh(); }
  catch (error) { $('#timezone-error').textContent = error.message; }
  finally { button.disabled = false; }
});
['log-level', 'log-order', 'log-follow', 'log-indent', 'log-nowrap'].forEach((id) => $(`#${id}`).addEventListener('change', renderLogs));
window.addEventListener('resize', () => { if ($('[data-view-panel="traffic"]').classList.contains('active')) requestAnimationFrame(renderTraffic); });
window.addEventListener('hashchange', () => showView(location.hash.slice(1), false));

showView(location.hash.slice(1) || 'overview', false);
refresh();
setInterval(refresh, 5000);
setInterval(() => { if ($('[data-view-panel="logs"]').classList.contains('active')) fetchLogs(); }, 2000);
if (document.fonts?.ready) document.fonts.ready.then(() => { if ($('[data-view-panel="traffic"]').classList.contains('active')) requestAnimationFrame(renderTraffic); });
