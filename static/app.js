(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const labels = { up: '運作正常', degraded: '部分異常', down: '無法連線', unknown: '待檢查' };
  const model = {
    hosts: [], loaded: false, filter: 'all', search: '', detailId: null, editId: null,
    stateBusy: false, stateAgain: false, stateError: false, hostSignature: null,
    detailSignature: null, extraChecks: [], formPending: false, scanBusy: false, scanStarting: false,
    adding: false, scan: { running: false, progress: 0, total: 0, results: [] },
    selected: new Set(), scanSignature: null, scanTimer: null, addFailures: ''
  };

  // All API data enters the DOM as text or DOM properties, never HTML.
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    node.setAttribute('class', 'icon');
    node.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    node.append(use);
    return node;
  }
  function showError(id, message) {
    $(id).textContent = message || '';
    $(id).hidden = !message;
  }
  function toast(message, isError = false) {
    const node = el('div', `toast${isError ? ' error' : ''}`);
    node.append(el('span', '', message));
    const close = el('button', 'icon-button');
    close.type = 'button';
    close.setAttribute('aria-label', '關閉通知');
    close.append(icon('close'));
    close.addEventListener('click', () => node.remove());
    node.append(close);
    $('toasts').append(node);
    setTimeout(() => node.remove(), isError ? 12000 : 6500);
  }
  async function api(path, method = 'GET', body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      const options = { method, signal: controller.signal, headers: { Accept: 'application/json' }, cache: 'no-store' };
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      const raw = await response.text();
      let data;
      try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
      if (!response.ok) throw new Error(data?.error || `請求失敗（HTTP ${response.status}）`);
      if (method === 'GET' && (!data || typeof data !== 'object')) throw new Error('服務回傳的資料格式不正確');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('請求逾時，請稍後重試');
      if (error instanceof TypeError) throw new Error('無法連接服務，請確認伺服器與網路連線');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  const statusOf = (host) => Object.hasOwn(labels, host.last?.status) ? host.last.status : 'unknown';
  const numeric = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const ms = (value) => numeric(value) ? Number(value).toLocaleString('zh-TW', { maximumFractionDigits: 1 }) : '—';
  function dateOf(value) {
    if (value === null || value === undefined || value === '') return null;
    const decoded = typeof value === 'string' && /^\d+(\.\d+)?$/.test(value) ? Number(value) : value;
    const stamp = typeof decoded === 'number' && decoded < 1e12 ? decoded * 1000 : decoded;
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  function dateText(value) {
    return dateOf(value)?.toLocaleString('zh-TW', { hour12: false }) || '尚無紀錄';
  }
  function relative(value) {
    const date = dateOf(value);
    if (!date) return '尚未檢查';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return `${seconds} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分鐘前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小時前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }
  function badge(status) {
    const known = Object.hasOwn(labels, status) ? status : 'unknown';
    const node = el('span', `status-badge ${known}`);
    node.append(el('span', `status-dot ${known}`), document.createTextNode(labels[known]));
    return node;
  }
  function latency(value) {
    const node = el('span', 'latency', ms(value));
    if (numeric(value)) node.append(el('small', '', 'ms'));
    return node;
  }
  function historyOf(host) {
    return (Array.isArray(host.history) ? host.history : []).slice().sort((a, b) =>
      (dateOf(a.timestamp)?.getTime() || 0) - (dateOf(b.timestamp)?.getTime() || 0));
  }
  function historyBars(history, detailed = false) {
    const node = el('div', detailed ? 'detail-history' : 'history-bars');
    const recent = history.slice(detailed ? -60 : -20);
    if (!recent.length) {
      node.append(el('span', 'history-empty', '尚無紀錄'));
      return node;
    }
    const counts = { up: 0, degraded: 0, down: 0, unknown: 0 };
    recent.forEach((item) => {
      const status = Object.hasOwn(labels, item.status) ? item.status : 'unknown';
      counts[status]++;
      const bar = el('span', `history-bar ${status}`);
      bar.title = `${dateText(item.timestamp)} · ${labels[status]} · ${ms(item.latency_ms)} ms`;
      node.append(bar);
    });
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', `最近 ${recent.length} 筆：正常 ${counts.up}、部分異常 ${counts.degraded}、無法連線 ${counts.down}、待檢查 ${counts.unknown}`);
    return node;
  }
  function focusKey() {
    return document.activeElement?.dataset?.focusKey;
  }
  function restoreFocus(container, key) {
    if (!key) return;
    const target = [...container.querySelectorAll('[data-focus-key]')].find((node) => node.dataset.focusKey === key);
    target?.focus({ preventScroll: true });
  }

  function renderHosts(force = false) {
    const signature = JSON.stringify([model.hosts, model.filter, model.search, model.loaded]);
    if (!force && model.hostSignature === signature) {
      document.querySelectorAll('[data-timestamp]').forEach((node) => { node.textContent = relative(node.dataset.timestamp); });
      return;
    }
    model.hostSignature = signature;
    const total = model.hosts.length;
    const up = model.hosts.filter((host) => statusOf(host) === 'up').length;
    const degraded = model.hosts.filter((host) => statusOf(host) === 'degraded').length;
    const down = model.hosts.filter((host) => statusOf(host) === 'down').length;
    const times = model.hosts.map((host) => host.last?.latency_ms).filter(numeric).map(Number);
    $('stat-total').textContent = model.loaded ? total : '—';
    $('stat-up').textContent = model.loaded ? up : '—';
    $('stat-attention').textContent = model.loaded ? degraded + down : '—';
    $('attention-caption').textContent = `${degraded} 台部分異常 · ${down} 台無法連線`;
    $('stat-latency').textContent = times.length ? ms(times.reduce((a, b) => a + b, 0) / times.length) : '—';
    $('host-count').textContent = total;
    $('nav-count').textContent = total;
    const query = model.search.trim().toLowerCase();
    const filtered = model.hosts.filter((host) => {
      const status = statusOf(host);
      return (model.filter === 'all' || model.filter === status || (model.filter === 'attention' && ['down', 'degraded'].includes(status))) &&
        `${host.name || ''} ${host.ip || ''}`.toLowerCase().includes(query);
    });
    $('host-count').textContent = filtered.length === total ? total : `${filtered.length} / ${total}`;
    renderBoard(filtered);
    const key = focusKey();
    const rows = document.createDocumentFragment();
    filtered.forEach((host) => {
      const row = el('tr');
      const identity = el('td');
      const wrapper = el('div', 'host-identity');
      const avatar = el('span', 'host-avatar');
      avatar.append(icon('server'));
      const info = el('div');
      const link = el('button', 'host-link', host.name || host.ip);
      link.type = 'button';
      link.dataset.focusKey = `host-${host.id}`;
      link.title = `${host.name || host.ip}：查看詳情`;
      link.addEventListener('click', () => openDetail(host.id));
      info.append(link, el('div', 'host-address mono', host.ip));
      wrapper.append(avatar, info);
      identity.append(wrapper);
      const state = el('td'); state.append(badge(statusOf(host)));
      const time = el('td'); time.append(latency(host.last?.latency_ms));
      const history = el('td'); history.append(historyBars(historyOf(host)));
      const last = el('td', 'last-check', relative(host.last?.timestamp));
      last.title = dateText(host.last?.timestamp);
      if (host.last?.timestamp != null) last.dataset.timestamp = host.last.timestamp;
      const action = el('td');
      const detail = el('button', 'icon-button');
      detail.type = 'button';
      detail.dataset.focusKey = `detail-${host.id}`;
      detail.setAttribute('aria-label', `查看 ${host.name || host.ip} 詳情`);
      detail.append(icon('arrow'));
      detail.addEventListener('click', () => openDetail(host.id));
      action.append(detail);
      row.append(identity, state, time, history, last, action);
      rows.append(row);
    });
    $('host-rows').replaceChildren(rows);
    restoreFocus($('host-rows'), key);
    $('host-empty').hidden = filtered.length > 0;
    const isFiltered = total > 0 || !!query || model.filter !== 'all';
    $('empty-title').textContent = !model.loaded ? '正在載入監測資料…' : isFiltered ? '找不到符合條件的主機' : '準備好，看見你的網路';
    $('empty-description').textContent = !model.loaded ? '等待伺服器回應；連線失敗時會自動重試。' : isFiltered ? '試試其他關鍵字，或切換狀態篩選。' : '探索區域網路中的裝置，或手動加入第一台主機。';
    $('empty-add').hidden = !model.loaded || isFiltered;
  }

  function renderBoard(hosts) {
    const key = focusKey();
    const cards = document.createDocumentFragment();
    hosts.forEach((host) => {
      const last = host.last || {};
      const agent = last.agent || {};
      const metrics = agent.ok ? agent.metrics || {} : {};
      const card = el('article', `host-card ${statusOf(host)}`);
      card.dataset.hostId = host.id;
      const heading = el('div', 'card-heading');
      const identity = el('div', 'card-identity');
      const name = el('button', 'host-link', metrics.hostname || host.name || host.ip);
      name.type = 'button';
      name.dataset.focusKey = `board-${host.id}`;
      name.addEventListener('click', () => openDetail(host.id));
      identity.append(name, el('div', 'mono muted', `${host.ip} · ${metrics.os || '作業系統未知'}`));
      heading.append(identity, badge(statusOf(host)));
      card.append(heading);
      if (!agent.ok) {
        card.append(el('div', 'card-agent-note', !host.agent_enabled ? '未啟用 Agent：無主機指標' : !host.last ? '等待首次檢查' : `Agent 無法取得指標：${agent.error || '等待回應'}`));
      }
      const disks = (Array.isArray(metrics.disks) ? metrics.disks : []).filter((disk) => disk && numeric(disk.percent));
      const busiest = disks.reduce((max, disk) => !max || Number(disk.percent) > Number(max.percent) ? disk : max, null);
      const grid = el('div', 'card-metrics');
      const definitions = [
        ['CPU', metrics.cpu_percent, '處理器使用率'],
        ['記憶體', metrics.memory?.percent, metrics.memory ? `${bytes(metrics.memory.used)} / ${bytes(metrics.memory.total)}` : '無量測資料'],
        ['磁碟最高', busiest?.percent, busiest ? `${busiest.mountpoint || '磁碟'} · 共 ${disks.length} 個掛載點` : '無量測資料']
      ];
      definitions.forEach(([label, value, caption]) => {
        const metric = el('div', 'card-metric');
        metric.append(el('span', 'muted', label), el('strong', percent(value) >= 90 ? 'usage-critical' : '', numeric(value) ? `${ms(percent(value))}%` : '—'), meter(value));
        const hint = el('small', 'muted', caption);
        hint.title = caption;
        metric.append(hint);
        grid.append(metric);
      });
      card.append(grid);
      const services = el('div', 'card-services');
      const serviceSummary = el('div', 'card-section-label');
      services.append(serviceSummary);
      let goodCount = 0, badCount = 0, unknownCount = 0;
      const tags = el('div', 'card-service-tags');
      const checks = Array.isArray(last.checks) ? last.checks : [];
      checks.forEach((check) => {
        const tag = el('span', `service-chip ${check.ok === true ? 'good' : 'bad'}`, `${check.label || check.type || '服務'} · ${check.ok === true ? '正常' : '異常'}`);
        if (check.ok === true) goodCount++; else badCount++;
        tag.title = check.detail || '';
        tags.append(tag);
      });
      (Array.isArray(metrics.services) ? metrics.services : []).forEach((service) => {
        const status = String(service.status || '未知');
        const good = ['active', 'running'].includes(status.toLowerCase());
        const unknown = ['unknown', '未知', ''].includes(status.toLowerCase());
        if (good) goodCount++; else if (unknown) unknownCount++; else badCount++;
        tags.append(el('span', `service-chip ${good ? 'good' : unknown ? 'unknown' : 'bad'}`, `${service.name} · ${status}`));
      });
      serviceSummary.textContent = `服務：${goodCount} 正常 / ${badCount} 異常 / ${unknownCount} 未知（已設定項目）`;
      if (!tags.children.length) tags.append(el('span', 'muted', '尚無服務檢查結果'));
      services.append(tags);
      card.append(services);
      const footer = el('div', 'card-footer');
      footer.append(el('span', '', `回應 ${ms(last.latency_ms)} ms`), el('span', '', `運行 ${uptime(metrics.uptime_seconds)}`));
      const checked = el('span', '', relative(last.timestamp));
      if (last.timestamp != null) checked.dataset.timestamp = last.timestamp;
      checked.title = dateText(last.timestamp);
      footer.append(checked);
      card.append(footer);
      cards.append(card);
    });
    $('host-board').replaceChildren(cards);
    restoreFocus($('host-board'), key);
  }

  async function loadState() {
    if (model.stateBusy) { model.stateAgain = true; return; }
    model.stateBusy = true;
    try {
      const data = await api('/api/state');
      if (!Array.isArray(data.hosts)) throw new Error('服務回傳的主機清單格式不正確');
      model.hosts = data.hosts;
      model.loaded = true;
      model.stateError = false;
      showError('state-error', '');
      $('connection').firstElementChild.className = 'status-dot up';
      $('connection-text').textContent = '服務連線正常';
      $('last-sync').textContent = `同步於 ${new Date().toLocaleTimeString('zh-TW', { hour12: false })}`;
      $('interval-label').textContent = numeric(data.interval) ? `每 ${data.interval} 秒監測` : '監測週期由伺服器管理';
      renderHosts();
      renderDetail();
      renderScan();
    } catch (error) {
      model.stateError = true;
      showError('state-error', `${error.message}。每 5 秒自動重試；目前資料可能不是最新狀態。`);
      $('connection').firstElementChild.className = 'status-dot down';
      $('connection-text').textContent = '服務連線中斷';
      if (!model.loaded) {
        $('empty-title').textContent = '暫時無法取得監測資料';
        $('empty-description').textContent = '請確認後端服務已啟動，畫面將自動重試。';
        $('empty-add').hidden = true;
      }
    } finally {
      model.stateBusy = false;
      if (model.stateAgain) { model.stateAgain = false; void loadState(); }
    }
  }

  function ipv4(value) {
    const parts = value.split('.');
    if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part) || Number(part) > 255 || (part.length > 1 && part[0] === '0'))) return null;
    return parts.reduce((result, part) => result * 256 + Number(part), 0);
  }
  function privateIP(value) {
    if (ipv4(value) === null) return false;
    const [a, b] = value.split('.').map(Number);
    return a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168);
  }
  function validateRange(value) {
    if (value.includes('/')) {
      const parts = value.split('/');
      if (parts.length !== 2 || !privateIP(parts[0]) || !/^\d{1,2}$/.test(parts[1]) || Number(parts[1]) > 32) throw new Error('請輸入有效的私有 IPv4 CIDR，例如 192.168.1.0/24');
      const prefix = Number(parts[1]);
      const block = 2 ** (32 - prefix);
      const start = Math.floor(ipv4(parts[0]) / block) * block;
      const toIP = (n) => [24, 16, 8, 0].map((bits) => Math.floor(n / 2 ** bits) % 256).join('.');
      if (!privateIP(toIP(start)) || !privateIP(toIP(start + block - 1))) throw new Error('整個探索網段都必須位於私有 IPv4 範圍內');
      return value;
    }
    const parts = value.split('-').map((part) => part.trim());
    if (parts.length !== 2 || !parts.every(privateIP) || ipv4(parts[0]) > ipv4(parts[1])) throw new Error('請輸入有效的起訖範圍，例如 192.168.1.1-192.168.1.254');
    // Prevent a range from crossing public space between separate private blocks.
    const group = (ip) => ip.startsWith('127.') ? '127' : ip.startsWith('10.') ? '10' : ip.startsWith('172.') ? '172' : '192';
    if (group(parts[0]) !== group(parts[1])) throw new Error('起訖位址必須位於同一個私有網路範圍');
    return parts.join('-');
  }
  function parsePorts(value, required = false) {
    if (!value.trim()) {
      if (required) throw new Error('請至少輸入一個 TCP 連接埠');
      return [];
    }
    const parts = value.split(',').map((part) => part.trim());
    if (parts.some((part) => !/^\d+$/.test(part) || Number(part) < 1 || Number(part) > 65535)) throw new Error('TCP 連接埠必須是 1–65535 的整數，並以逗號分隔');
    return [...new Set(parts.map(Number))];
  }

  function openHost(host = null) {
    model.editId = host?.id ?? null;
    $('host-form').reset();
    $('host-dialog-title').textContent = host ? '編輯主機' : '新增主機';
    $('host-name').value = host?.name || '';
    $('host-ip').value = host?.ip || '';
    $('host-ports').value = (host?.checks || []).filter((check) => check.type === 'tcp').map((check) => check.port).join(', ');
    $('host-url').value = (host?.checks || []).find((check) => check.type === 'http')?.url || '';
    model.extraChecks = (host?.checks || []).filter((check) => check.type === 'http').slice(1).map((check) => ({ ...check }));
    $('extra-checks-note').hidden = !model.extraChecks.length;
    $('extra-checks-note').textContent = `另有 ${model.extraChecks.length} 個既有 HTTP 檢查，儲存時將保留。`;
    $('agent-enabled').checked = !!host?.agent_enabled;
    $('agent-port').value = host?.agent_port ?? 9105;
    $('agent-token').value = '';
    $('agent-token').disabled = false;
    $('agent-fields').disabled = !$('agent-enabled').checked;
    $('clear-token-wrap').hidden = !host;
    $('token-help').textContent = host ? (host.agent_token_set ? '已儲存 Token；留空保留原值。' : '尚未設定 Token；留空維持不變。') : '權杖只會傳送，不會顯示。';
    $('agent-token').placeholder = host ? '留空保留原 Token' : '輸入存取權杖';
    showError('host-form-error', '');
    $('host-dialog').showModal();
    $('host-name').focus();
  }
  async function saveHost(event) {
    event.preventDefault();
    if (model.formPending) return;
    showError('host-form-error', '');
    let payload;
    try {
      const name = $('host-name').value.trim();
      const ip = $('host-ip').value.trim();
      if (!name) throw new Error('請輸入主機名稱');
      if (!privateIP(ip)) throw new Error('請輸入有效的私有 IPv4 位址');
      const checks = parsePorts($('host-ports').value).map((port) => ({ type: 'tcp', port }));
      const url = $('host-url').value.trim();
      if (url) {
        let parsed;
        try { parsed = new URL(url); } catch { throw new Error('請輸入完整的 HTTP 或 HTTPS 網址'); }
        if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('檢查網址僅支援 HTTP 或 HTTPS');
        if (parsed.hostname !== ip || parsed.username || parsed.password || parsed.hash) throw new Error('網址須使用此主機的 IP，且不可包含帳密或片段');
        checks.push({ type: 'http', url });
      }
      checks.push(...model.extraChecks);
      const agentEnabled = $('agent-enabled').checked;
      const agentPort = Number($('agent-port').value);
      if (agentEnabled && (!Number.isInteger(agentPort) || agentPort < 1 || agentPort > 65535)) throw new Error('Agent 連接埠必須是 1–65535 的整數');
      payload = { name, ip, checks, agent_enabled: agentEnabled, agent_port: Number.isInteger(agentPort) && agentPort > 0 && agentPort <= 65535 ? agentPort : 9105 };
      const token = $('agent-token').value;
      // Editing with a blank password must never overwrite a stored secret.
      if ($('clear-token').checked) payload.agent_token = '';
      else if (token) payload.agent_token = token;
      else if (model.editId === null) payload.agent_token = '';
    } catch (error) { showError('host-form-error', error.message); return; }
    model.formPending = true;
    $('save-host').disabled = true;
    $('save-host').textContent = '儲存中…';
    const editing = model.editId !== null;
    try {
      await api(editing ? `/api/hosts/${encodeURIComponent(model.editId)}` : '/api/hosts', editing ? 'PUT' : 'POST', payload);
      $('agent-token').value = '';
      $('host-dialog').close();
      toast(editing ? '主機設定已更新' : '主機已加入監測');
      await loadState();
    } catch (error) { showError('host-form-error', error.message); }
    finally {
      model.formPending = false;
      $('save-host').disabled = false;
      $('save-host').textContent = '儲存主機';
    }
  }

  function openDetail(id) {
    model.detailId = id;
    model.detailSignature = null;
    showError('detail-error', '');
    renderDetail();
    $('detail-dialog').showModal();
  }
  function section(title) {
    const node = el('section', 'detail-section');
    node.append(el('h3', '', title));
    return node;
  }
  function percent(value) {
    return numeric(value) ? Math.min(100, Math.max(0, Number(value))) : null;
  }
  function meter(value) {
    const node = el('div', 'meter');
    const bounded = percent(value);
    const fill = el('div', `meter-fill${bounded >= 90 ? ' critical' : bounded >= 75 ? ' warning' : ''}`);
    fill.style.width = `${bounded ?? 0}%`;
    node.append(fill);
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', bounded === null ? '無量測資料' : `使用率 ${ms(bounded)}%`);
    return node;
  }
  function bytes(value) {
    if (!numeric(value)) return '—';
    const n = Math.max(0, Number(value));
    if (n < 1024) return `${ms(n)} B`;
    const index = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** index).toLocaleString('zh-TW', { maximumFractionDigits: 1 })} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][index]}`;
  }
  function uptime(value) {
    if (!numeric(value)) return '—';
    const seconds = Math.max(0, Number(value));
    return `${Math.floor(seconds / 86400)} 天 ${Math.floor(seconds % 86400 / 3600)} 小時 ${Math.floor(seconds % 3600 / 60)} 分`;
  }
  function renderDetail() {
    if (model.detailId === null) return;
    const host = model.hosts.find((item) => item.id === model.detailId);
    if (!host) {
      if ($('detail-dialog').open) $('detail-dialog').close();
      model.detailId = null;
      return;
    }
    const signature = JSON.stringify(host);
    if (signature === model.detailSignature) return;
    model.detailSignature = signature;
    $('detail-title').textContent = host.name || host.ip;
    $('detail-ip').textContent = host.ip;
    const content = document.createDocumentFragment();
    const summary = el('div', 'detail-summary');
    summary.append(badge(statusOf(host)), latency(host.last?.latency_ms));
    content.append(summary, el('p', 'detail-meta', `最後檢查：${dateText(host.last?.timestamp)} · 網路可達：${host.last?.reachable === true ? '是' : host.last?.reachable === false ? '否' : '未知'}`));
    const history = historyOf(host);
    const trend = section('近期狀態');
    trend.append(historyBars(history, true));
    const legend = el('div', 'history-legend');
    legend.append(el('span', '', '較早'), el('span', '', `最近 ${Math.min(history.length, 60)} 筆檢查`), el('span', '', '最新'));
    trend.append(legend);
    content.append(trend);

    const checksSection = section('服務檢查');
    const checks = Array.isArray(host.last?.checks) ? host.last.checks : [];
    if (checks.length) {
      const list = el('div', 'check-list');
      checks.forEach((check) => {
        const row = el('div', 'check-row');
        const state = el('span', `check-status${check.ok ? '' : ' failed'}`);
        state.append(icon(check.ok ? 'check' : 'alert'));
        state.setAttribute('aria-label', check.ok ? '通過' : '失敗');
        const info = el('div', 'check-info');
        info.append(el('div', 'check-label', check.label || String(check.type || '檢查').toUpperCase()), el('div', 'check-detail', check.detail || (check.ok ? '檢查通過' : '檢查未通過')));
        row.append(state, info, latency(check.latency_ms));
        list.append(row);
      });
      checksSection.append(list);
    } else {
      checksSection.append(el('p', 'detail-empty', host.last ? '尚無服務檢查結果。可編輯主機，加入 TCP 或 HTTP 檢查。' : '等待首次檢查。'));
    }
    if (host.checks?.length) {
      const config = host.checks.map((check) => check.type === 'tcp' ? `TCP ${check.port}` : `HTTP ${check.url}`).join(' · ');
      checksSection.append(el('p', 'section-caption', `設定：${config}`));
    }
    content.append(checksSection);
    const agent = host.last?.agent;
    const agentSection = section('Agent 主機指標');
    if (!host.agent_enabled) agentSection.append(el('p', 'detail-empty', '尚未啟用 Agent。啟用後可檢視 CPU、記憶體、磁碟與服務資訊。'));
    else if (!agent?.ok) agentSection.append(el('p', 'error-banner', agent?.error || '等待 Agent 回應；請確認 Agent 已啟動、連接埠與 Token 設定正確。'));
    else if (!agent.metrics) agentSection.append(el('p', 'detail-empty', 'Agent 已連線，尚無指標資料。'));
    else {
      const metrics = agent.metrics;
      const grid = el('div', 'metrics-grid');
      [['CPU 使用率', metrics.cpu_percent, null], ['記憶體使用率', metrics.memory?.percent, `${bytes(metrics.memory?.used)} / ${bytes(metrics.memory?.total)}`]].forEach(([title, value, sub]) => {
        const card = el('div', 'metric-card');
        card.append(el('div', 'metric-label', title), el('div', 'metric-value', numeric(value) ? `${ms(value)}%` : '—'), meter(value));
        if (sub) card.append(el('div', 'metric-sub', sub));
        grid.append(card);
      });
      agentSection.append(grid);
      const info = el('dl', 'agent-info');
      [['主機名稱', metrics.hostname || '—'], ['作業系統', metrics.os || '—'], ['運行時間', uptime(metrics.uptime_seconds)]].forEach(([key, value]) => {
        info.append(el('dt', '', key), el('dd', '', value));
      });
      agentSection.append(info);
      if (Array.isArray(metrics.disks) && metrics.disks.length) {
        const disks = section('磁碟空間');
        metrics.disks.forEach((disk) => {
          const row = el('div', 'disk-row');
          const heading = el('div', 'disk-heading');
          heading.append(el('span', 'mono', disk.mountpoint || '磁碟'), el('span', '', `${ms(disk.percent)}% · ${bytes(disk.used)} / ${bytes(disk.total)}`));
          row.append(heading, meter(disk.percent));
          disks.append(row);
        });
        agentSection.append(disks);
      }
      if (Array.isArray(metrics.services) && metrics.services.length) {
        const services = section('系統服務');
        const list = el('div', 'service-list');
        metrics.services.forEach((service) => {
          const row = el('div', 'service-row');
          row.append(el('span', '', service.name), el('span', '', service.status || '未知'));
          list.append(row);
        });
        services.append(list);
        agentSection.append(services);
      }
    }
    content.append(agentSection);
    const records = section('檢查歷史');
    if (history.length) {
      const wrapper = el('div', 'history-table-wrap');
      const table = el('table', 'history-table');
      const head = el('thead');
      const header = el('tr');
      ['檢查時間', '狀態', '回應時間'].forEach((label) => { const th = el('th', '', label); th.scope = 'col'; header.append(th); });
      head.append(header);
      const body = el('tbody');
      history.slice().reverse().forEach((item) => {
        const row = el('tr');
        const state = el('td'); state.append(badge(item.status));
        const time = el('td'); time.append(latency(item.latency_ms));
        row.append(el('td', 'mono', dateText(item.timestamp)), state, time);
        body.append(row);
      });
      table.append(head, body);
      wrapper.append(table);
      records.append(wrapper);
    } else records.append(el('p', 'detail-empty', '首次檢查完成後，歷史紀錄將顯示於此。'));
    content.append(records);
    $('detail-content').replaceChildren(content);
  }

  async function removeHost() {
    const host = model.hosts.find((item) => item.id === model.detailId);
    if (!host || $('delete-host').disabled) return;
    if (!window.confirm(`確定移除「${host.name || host.ip}」的監測與歷史紀錄？此操作無法復原。`)) return;
    $('delete-host').disabled = true;
    $('edit-host').disabled = true;
    try {
      showError('detail-error', '');
      await api(`/api/hosts/${encodeURIComponent(host.id)}`, 'DELETE');
      if (model.detailId === host.id) {
        $('detail-dialog').close();
        model.detailId = null;
      }
      toast('主機已移除');
      await loadState();
    } catch (error) {
      if (model.detailId === host.id && $('detail-dialog').open) showError('detail-error', error.message);
      else toast(error.message, true);
    }
    finally { $('delete-host').disabled = false; $('edit-host').disabled = false; }
  }

  const discovered = () => {
    const seen = new Set();
    return (Array.isArray(model.scan.results) ? model.scan.results : []).filter((item) => {
      if (!privateIP(String(item.ip || '')) || seen.has(item.ip)) return false;
      seen.add(item.ip);
      return true;
    });
  };
  function monitored(ip) { return model.hosts.some((host) => host.ip === ip); }
  function updateScanSelection() {
    const available = discovered().filter((item) => !monitored(item.ip));
    const valid = new Set(available.map((item) => item.ip));
    for (const ip of model.selected) if (!valid.has(ip)) model.selected.delete(ip);
    const count = model.selected.size;
    $('scan-selected-count').textContent = `已選取 ${count} 個裝置`;
    $('add-discovered').disabled = !count || model.adding || !model.loaded || model.stateError;
    $('add-discovered').textContent = model.adding ? '加入中…' : `加入監測${count ? `（${count}）` : ''}`;
    $('scan-select-all').disabled = !available.length || model.adding || !model.loaded;
    $('scan-select-all').checked = available.length > 0 && count === available.length;
    $('scan-select-all').indeterminate = count > 0 && count < available.length;
    $('scan-rows').querySelectorAll('input[type=checkbox]').forEach((input) => {
      input.checked = model.selected.has(input.value);
      input.disabled = monitored(input.value) || model.adding || !model.loaded;
    });
  }
  function renderScan() {
    const scan = model.scan;
    const running = !!scan.running;
    const total = numeric(scan.total) ? Math.max(0, Number(scan.total)) : 0;
    const progress = numeric(scan.progress) ? Math.max(0, Number(scan.progress)) : 0;
    $('start-scan').disabled = running || model.scanStarting || model.adding;
    $('scan-range').disabled = running || model.scanStarting || model.adding;
    $('scan-ports').disabled = running || model.scanStarting || model.adding;
    $('start-scan').lastElementChild.textContent = model.scanStarting ? '啟動中…' : running ? '探索中…' : '開始探索';
    $('scan-progress-text').textContent = `${progress.toLocaleString()} / ${total.toLocaleString()}`;
    $('scan-progress').max = total || 1;
    if (running && !total) $('scan-progress').removeAttribute('value');
    else $('scan-progress').value = Math.min(progress, total);
    const results = discovered();
    $('scan-status').textContent = running ? '正在探索網路裝置…' : scan.error ? '探索已停止，發生錯誤' : total || results.length ? '探索已完成' : '尚未開始探索';
    $('scan-result-count').textContent = `發現 ${results.length} 個裝置`;
    const signature = JSON.stringify([results, model.hosts.map((host) => host.ip), model.adding, model.loaded]);
    if (signature !== model.scanSignature) {
      model.scanSignature = signature;
      const key = focusKey();
      const rows = document.createDocumentFragment();
      results.forEach((item) => {
        const exists = monitored(item.ip);
        const row = el('tr');
        const selection = el('td');
        const checkbox = el('input');
        checkbox.type = 'checkbox'; checkbox.value = item.ip;
        checkbox.dataset.focusKey = `scan-${item.ip}`;
        checkbox.setAttribute('aria-label', `選取 ${item.hostname || item.ip}`);
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) model.selected.add(item.ip); else model.selected.delete(item.ip);
          updateScanSelection();
        });
        selection.append(checkbox);
        const identity = el('td');
        identity.append(el('div', 'device-name', item.hostname || '網路裝置'), el('div', 'host-address mono', item.ip));
        if (exists) identity.append(el('span', 'section-caption', '已加入監測'));
        const ports = el('td');
        const open = (Array.isArray(item.open_ports) ? item.open_ports : []).filter((port) => Number.isInteger(Number(port)) && Number(port) > 0 && Number(port) <= 65535);
        if (open.length) open.forEach((port) => ports.append(el('span', 'port-tag', port)));
        else ports.append(el('span', 'muted', '未發現'));
        const time = el('td'); time.append(latency(item.latency_ms));
        row.append(selection, identity, ports, time);
        rows.append(row);
      });
      $('scan-rows').replaceChildren(rows);
      restoreFocus($('scan-rows'), key);
    }
    $('scan-empty').hidden = results.length > 0;
    $('scan-empty').textContent = running ? '正在尋找可回應的裝置，請稍候…' : total ? '未發現可回應的裝置，請確認網段、連接埠與防火牆設定。' : '探索完成後，可在這裡選取要監測的裝置。';
    updateScanSelection();
  }
  function scheduleScanPoll() {
    clearTimeout(model.scanTimer);
    model.scanTimer = setTimeout(() => void pollScan(), model.scan.running ? 1500 : 5000);
  }
  async function pollScan() {
    if (model.scanBusy || model.scanStarting) return;
    model.scanBusy = true;
    try {
      const data = await api('/api/scan');
      if (!Array.isArray(data.results)) throw new Error('服務回傳的探索結果格式不正確');
      model.scan = data;
      showError('scan-error', data.error || model.addFailures);
      renderScan();
    } catch (error) {
      showError('scan-error', `${error.message}。將自動重試取得探索狀態。`);
    } finally {
      model.scanBusy = false;
      scheduleScanPoll();
    }
  }
  function openScan() {
    $('scan-dialog').showModal();
    renderScan();
    if (!model.scanBusy && !model.scanStarting) void pollScan();
  }
  async function startScan(event) {
    event.preventDefault();
    if (model.scan.running || model.scanStarting || model.adding) return;
    let payload;
    try { payload = { range: validateRange($('scan-range').value.trim()), ports: parsePorts($('scan-ports').value) }; }
    catch (error) { showError('scan-error', error.message); return; }
    // Serialize with an in-flight GET without losing the user's first click.
    model.scanStarting = true;
    clearTimeout(model.scanTimer);
    showError('scan-error', '');
    renderScan();
    try {
      while (model.scanBusy) await new Promise((resolve) => setTimeout(resolve, 50));
      clearTimeout(model.scanTimer);
      if (model.scan.running) throw new Error('掃描已在執行中');
      await api('/api/scan', 'POST', payload);
      model.scan = { running: true, progress: 0, total: 0, results: [] };
      model.selected.clear();
      model.addFailures = '';
      $('scan-feedback').textContent = '';
    } catch (error) { showError('scan-error', error.message); }
    finally {
      model.scanStarting = false;
      renderScan();
      scheduleScanPoll();
    }
  }
  async function addDiscovered() {
    if (model.adding || !model.selected.size || !model.loaded || model.stateError) return;
    const chosen = discovered().filter((item) => model.selected.has(item.ip) && !monitored(item.ip));
    model.adding = true;
    renderScan();
    let added = 0;
    const failures = [];
    // Sequential writes keep server load predictable and allow partial success recovery.
    for (const item of chosen) {
      try {
        const checks = [...new Set((item.open_ports || []).map(Number))].filter((port) => Number.isInteger(port) && port > 0 && port <= 65535).map((port) => ({ type: 'tcp', port }));
        const host = await api('/api/hosts', 'POST', { name: item.hostname || item.ip, ip: item.ip, checks, agent_enabled: false, agent_port: 9105, agent_token: '' });
        if (host?.id != null && !model.hosts.some((existing) => existing.id === host.id)) model.hosts.push(host);
        model.selected.delete(item.ip);
        added++;
      } catch (error) { failures.push(`${item.ip}：${error.message}`); }
    }
    model.adding = false;
    model.addFailures = failures.length ? `部分裝置未能加入，可保留勾選後重試。${failures.join('；')}` : '';
    showError('scan-error', model.addFailures);
    renderHosts();
    renderScan();
    $('scan-feedback').textContent = added ? `已將 ${added} 個裝置加入監測。` : '';
    if (added) toast(`已將 ${added} 個裝置加入監測`);
    if (failures.length) toast(`${failures.length} 個裝置加入失敗，請查看探索視窗`, true);
    await loadState();
  }

  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.close === 'host-dialog' && model.formPending) return;
    $(button.dataset.close).close();
  }));
  $('host-dialog').addEventListener('cancel', (event) => { if (model.formPending) event.preventDefault(); });
  $('host-dialog').addEventListener('close', () => { $('agent-token').value = ''; });
  $('detail-dialog').addEventListener('close', () => { model.detailId = null; });
  $('add-host').addEventListener('click', () => openHost());
  $('empty-add').addEventListener('click', () => openHost());
  $('host-form').addEventListener('submit', saveHost);
  $('agent-enabled').addEventListener('change', () => { $('agent-fields').disabled = !$('agent-enabled').checked; });
  $('clear-token').addEventListener('change', () => {
    $('agent-token').disabled = $('clear-token').checked;
    if ($('clear-token').checked) $('agent-token').value = '';
  });
  $('edit-host').addEventListener('click', () => {
    const host = model.hosts.find((item) => item.id === model.detailId);
    if (host) openHost(host);
  });
  $('delete-host').addEventListener('click', removeHost);
  $('view-toggle').addEventListener('click', () => {
    const table = $('host-table-view').hidden;
    $('host-table-view').hidden = !table;
    $('host-board').hidden = table;
    $('view-toggle').textContent = table ? '切換戰情總覽' : '切換清單';
    $('view-toggle').setAttribute('aria-pressed', String(table));
  });
  function wallMode(enabled) {
    document.body.classList.toggle('wall-mode', enabled);
    $('wall-toggle').textContent = enabled ? '離開全螢幕' : '全螢幕總覽';
    $('wall-toggle').setAttribute('aria-pressed', String(enabled));
  }
  $('wall-toggle').addEventListener('click', async () => {
    const enabled = !document.body.classList.contains('wall-mode');
    wallMode(enabled);
    if (enabled) {
      model.filter = 'all';
      model.search = '';
      $('host-search').value = '';
      document.querySelectorAll('[data-filter]').forEach((button) => {
        const active = button.dataset.filter === 'all';
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
      renderHosts();
      if ($('host-board').hidden) $('view-toggle').click();
      try { await document.documentElement.requestFullscreen(); } catch { /* Focus mode also works without browser fullscreen permission. */ }
    } else if (document.fullscreenElement) {
      try { await document.exitFullscreen(); } catch { /* Keep focus mode controls usable. */ }
    }
  });
  document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) wallMode(false); });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !document.fullscreenElement) wallMode(false); });
  $('host-search').addEventListener('input', (event) => { model.search = event.target.value; renderHosts(); });
  document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
    model.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    renderHosts();
  }));
  $('refresh').addEventListener('click', async () => {
    if ($('refresh').disabled) return;
    $('refresh').disabled = true;
    $('refresh').lastElementChild.textContent = '排程中…';
    try {
      await api('/api/refresh', 'POST', {});
      toast('已安排檢查；最新結果將自動同步');
      await loadState();
    } catch (error) { toast(error.message, true); }
    finally { $('refresh').disabled = false; $('refresh').lastElementChild.textContent = '立即檢查'; }
  });
  ['open-scan', 'nav-scan'].forEach((id) => $(id).addEventListener('click', openScan));
  $('scan-form').addEventListener('submit', startScan);
  $('scan-select-all').addEventListener('change', (event) => {
    model.selected.clear();
    if (event.target.checked) discovered().filter((item) => !monitored(item.ip)).forEach((item) => model.selected.add(item.ip));
    updateScanSelection();
  });
  $('add-discovered').addEventListener('click', addDiscovered);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void loadState(); });
  renderHosts();
  renderScan();
  void loadState();
  void pollScan();
  setInterval(() => void loadState(), 5000);
})();
