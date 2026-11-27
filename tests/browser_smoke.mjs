// Optional real-browser smoke test. Node 22+, Chrome or Edge; no npm dependencies.
// Run from project root: node tests/browser_smoke.mjs
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, writeFile, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createServer } from 'node:net';
import assert from 'node:assert/strict';

const delay = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, label, timeout = 20000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try { const result = await fn(); if (result) return result; } catch {}
    await delay(150);
  }
  throw new Error(`Timed out: ${label}`);
}
const candidates = [process.env.CHROME_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium'].filter(Boolean);
let executable;
for (const candidate of candidates) { try { await access(candidate); executable = candidate; break; } catch {} }
if (!executable) throw new Error('Set CHROME_PATH to Chrome / Edge executable');
const temporary = await mkdtemp(join(tmpdir(), 'netword-browser-'));
const listener = createServer();
await new Promise(r => listener.listen(0, '127.0.0.1', r));
const port = listener.address().port;
await new Promise(r => listener.close(r));
const base = `http://127.0.0.1:${port}`;
let backend, browser, socket;
let nextId = 0;
const pending = new Map();
const errors = [];
const expectedImportErrors = new Map([
  ['/api/config/import/preview', 'status of 400'],
  ['/api/config/import/apply', 'status of 409'],
]);
async function command(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout ${method}`)); }, 15000);
    pending.set(id, { resolve: result => { clearTimeout(timeout); resolve(result); }, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
try {
  backend = spawn(process.env.PYTHON || 'python', ['server.py', '--port', String(port), '--data-dir', join(temporary, 'data')], { cwd: resolve('.'), stdio: 'ignore' });
  backend.on('error', error => errors.push(String(error)));
  await until(async () => (await fetch(base + '/api/state')).ok, 'backend');
  browser = spawn(executable, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${join(temporary, 'profile')}`, base], { stdio: 'ignore' });
  browser.on('error', error => errors.push(String(error)));
  const debugPort = await until(async () => (await readFile(join(temporary, 'profile', 'DevToolsActivePort'), 'utf8')).split('\n')[0], 'browser debugger');
  const page = await until(async () => (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(p => p.type === 'page' && p.url.startsWith(base)), 'page target');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.id && pending.has(data.id)) {
      const item = pending.get(data.id); pending.delete(data.id);
      if (data.error) item.reject(new Error(JSON.stringify(data.error))); else item.resolve(data.result);
    }
    if (data.method === 'Runtime.exceptionThrown') errors.push(JSON.stringify(data.params.exceptionDetails));
    if (data.method === 'Log.entryAdded' && data.params.entry.level === 'error' && !data.params.entry.url?.endsWith('/favicon.ico')) {
      const entry = data.params.entry;
      if (![...expectedImportErrors].some(([path, text]) => entry.url === base + path && entry.text.includes(text))) {
        errors.push(entry.text);
      }
    }
  };
  await command('Runtime.enable');
  await command('Log.enable');
  await until(() => evaluate(`document.querySelector('#connection-text').textContent.includes('已連') || document.querySelector('#last-sync').textContent !== '尚未同步'`), 'initial state');
  await evaluate(`document.querySelector('#add-host').click(); document.querySelector('#host-ip').value='127.0.0.1'; document.querySelector('#host-name').value='Browser smoke <test>'; document.querySelector('#host-ports').value='${port}'; document.querySelector('#host-form').requestSubmit()`);
  await until(async () => (await (await fetch(base + '/api/state')).json()).hosts.length === 1, 'add host');
  await until(() => evaluate(`document.querySelector('#host-rows').textContent.includes('Browser smoke <test>')`), 'host table');
  assert.equal(await evaluate(`document.querySelectorAll('#host-rows test').length`), 0, 'host text must not become HTML');
  await evaluate(`document.querySelector('#open-scan').click(); document.querySelector('#scan-range').value='127.0.0.1/32'; document.querySelector('#scan-ports').value='${port}'; document.querySelector('#scan-form').requestSubmit()`);
  await until(async () => { const scan = await (await fetch(base + '/api/scan')).json(); return !scan.running && scan.progress === 1; }, 'scan');
  await until(() => evaluate(`document.querySelector('#scan-rows').textContent.includes('127.0.0.1')`), 'scan table');
  await evaluate(`document.querySelector('#scan-dialog').close()`);
  // Click the row's detail button (or row itself for keyboard-enabled row layouts).
  await evaluate(`(document.querySelector('#host-rows button') || document.querySelector('#host-rows tr')).click()`);
  await until(() => evaluate(`document.querySelector('#detail-dialog').open`), 'host details');
  assert.ok(await evaluate(`document.querySelector('#detail-content').textContent.length > 20`));
  await evaluate(`window.confirm=()=>true; document.querySelector('#delete-host').click()`);
  await until(async () => (await (await fetch(base + '/api/state')).json()).hosts.length === 0, 'delete host');
  await until(() => evaluate(`!document.querySelector('#detail-dialog').open && document.querySelector('#host-rows').children.length===0`), 'delete rendered');
  await evaluate(`document.querySelector('#open-scan').click()`);
  await until(() => evaluate(`!document.querySelector('#scan-select-all').disabled`), 'discovered host available');
  await evaluate(`document.querySelector('#scan-select-all').click(); document.querySelector('#add-discovered').click()`);
  await until(async () => (await (await fetch(base + '/api/state')).json()).hosts.length === 1, 'add discovered host');
  await evaluate(`document.querySelector('#scan-dialog').close(); document.querySelector('#add-host').click()`);
  assert.equal(await evaluate(`document.querySelector('#agent-port').value`), '9105');
  await evaluate(`document.querySelector('#host-dialog').close()`);
  await command('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`), true, 'mobile layout must fit');
  // Email settings use only the temporary server; never send a real test email.
  await evaluate(`document.querySelector('#nav-notifications').click()`);
  await until(() => evaluate(`!document.querySelector('#save-notification').disabled`), 'email settings loaded');
  assert.equal(await evaluate(`document.querySelector('#mail-host').value`), 'smtp.gmail.com');
  assert.equal(await evaluate(`document.querySelector('#mail-port').value`), '587');
  assert.equal(await evaluate(`document.querySelector('#notification-enabled').checked`), false);
  assert.equal(await evaluate(`document.querySelector('#notification-dialog').scrollWidth<=document.querySelector('#notification-dialog').clientWidth`), true, 'email dialog fits mobile');
  await evaluate(`document.querySelector('#mail-username').value='smoke@gmail.com'; document.querySelector('#mail-sender').value='smoke@gmail.com'; document.querySelector('#mail-recipients').value='admin@example.com'; document.querySelector('#mail-password').value='fake-smoke-password'; document.querySelector('#save-notification').click()`);
  await until(async () => (await (await fetch(base + '/api/notifications')).json()).password_set, 'email secret saved');
  await until(() => evaluate(`!document.querySelector('#save-notification').disabled && document.querySelector('#mail-password').value===''`), 'email secret cleared from form');
  const mailPublic = await (await fetch(base + '/api/notifications')).json();
  assert.ok(!JSON.stringify(mailPublic).includes('fake-smoke-password'), 'email secret must be write-only');
  await evaluate(`document.querySelector('#notification-dialog').close(); document.querySelector('#nav-notifications').click()`);
  await until(() => evaluate(`!document.querySelector('#save-notification').disabled`), 'email settings reopened');
  assert.equal(await evaluate(`document.querySelector('#mail-password').value`), '');
  await evaluate(`document.querySelector('#save-notification').click()`);
  await until(() => evaluate(`!document.querySelector('#save-notification').disabled`), 'email blank password save');
  assert.equal((await (await fetch(base + '/api/notifications')).json()).password_set, true, 'blank password preserves secret');
  await evaluate(`window.__mailFetch=window.fetch; window.__mailTests=0; window.fetch=(path,options)=>String(path)==='/api/notifications/test'?(window.__mailTests++,Promise.resolve(new Response(JSON.stringify({ok:true,message:'模擬測試已排程'}),{status:202}))):window.__mailFetch(path,options); document.querySelector('#test-notification').click()`);
  await until(() => evaluate(`window.__mailTests===1 && !document.querySelector('#test-notification').disabled`), 'email test queued');
  await evaluate(`window.fetch=window.__mailFetch; document.querySelector('#mail-clear-password').click(); document.querySelector('#save-notification').click()`);
  await until(async () => !(await (await fetch(base + '/api/notifications')).json()).password_set, 'email secret explicitly cleared');
  await until(() => evaluate(`!document.querySelector('#save-notification').disabled`), 'email clear completed');
  await evaluate(`document.querySelector('#notification-dialog').close()`);
  // Frequency settings persist in this isolated server and reschedule the active page timer.
  assert.equal(await evaluate(`!!document.querySelector('.sidebar #nav-settings') && !document.querySelector('.panel-footer #open-settings')`), true, 'settings lives in the sidebar');
  await evaluate(`document.querySelector('#nav-settings').click()`);
  await until(() => evaluate(`!document.querySelector('#save-settings').disabled`), 'frequency settings loaded');
  assert.equal(await evaluate(`document.querySelector('#refresh-interval').value`), '5');
  assert.equal(await evaluate(`document.querySelector('#settings-dialog').scrollWidth<=document.querySelector('#settings-dialog').clientWidth`), true, 'frequency dialog fits mobile');
  await evaluate(`document.querySelector('#monitor-interval').value='4'`);
  assert.equal(await evaluate(`document.querySelector('#settings-form').checkValidity()`), false, 'invalid monitor interval rejected');
  await evaluate(`document.querySelector('#refresh-interval').value='1'; document.querySelector('#monitor-interval').value='60'; document.querySelector('#settings-form').requestSubmit()`);
  await until(() => evaluate(`!document.querySelector('#settings-dialog').open && document.querySelector('#refresh-interval-label').textContent.includes('1 秒') && document.querySelector('#interval-label').textContent.includes('60 秒')`), 'frequency saved and displayed');
  assert.deepEqual(await (await fetch(base + '/api/settings')).json(), {refresh_interval:1, monitor_interval:60});
  await evaluate(`window.__frequencyFetch=window.fetch; window.__statePolls=0; window.fetch=(path,options)=>{if(String(path)==='/api/state')window.__statePolls++; return window.__frequencyFetch(path,options)}`);
  await delay(2700);
  assert.ok(await evaluate(`window.__statePolls>=2 && window.__statePolls<=3`), 'new 1-second timer replaces old timer without duplication');
  await evaluate(`window.fetch=window.__frequencyFetch; document.querySelector('#nav-settings').click()`);
  await until(() => evaluate(`!document.querySelector('#save-settings').disabled`), 'frequency reopened');
  assert.equal(await evaluate(`document.querySelector('#monitor-interval').value`), '60');
  await evaluate(`document.querySelector('#settings-dialog').close()`);
  await command('Page.reload');
  await until(() => evaluate(`document.querySelector('#refresh-interval-label').textContent.includes('1 秒') && document.querySelector('#interval-label').textContent.includes('60 秒')`), 'saved frequency loaded after page reload');
  // A second client changes the shared setting; this page picks it up via /api/state.
  await fetch(base + '/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({monitor_interval:30})});
  await until(() => evaluate(`document.querySelector('#interval-label').textContent.includes('30 秒')`), 'shared frequency change synchronized');
  // Real import requests use only this temporary server and loopback hosts.
  const priorConfig = await (await fetch(base + '/api/config/export')).json();
  const upcoming = structuredClone(priorConfig);
  upcoming.settings = {refresh_interval:1, monitor_interval:45};
  upcoming.hosts = [{ip:'127.0.0.2', name:'匯入 <img src=x>', agent_enabled:true, agent_port:9105}];
  upcoming.notifications.smtp_host = 'mail.example.com';
  const upcomingJSON = JSON.stringify(upcoming);
  await command('Emulation.setDeviceMetricsOverride', {width:320,height:740,deviceScaleFactor:1,mobile:true});
  await evaluate(`document.querySelector('#nav-settings').focus()`);
  assert.equal(await evaluate('document.activeElement.id'), 'nav-settings', 'mobile settings can receive keyboard focus');
  await command('Input.dispatchKeyEvent', {type:'rawKeyDown',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:32});
  await command('Input.dispatchKeyEvent', {type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:32});
  await until(() => evaluate(`document.querySelector('#settings-dialog').open && !document.querySelector('#save-settings').disabled`), 'settings opens by keyboard on mobile');
  await evaluate(`window.__download = null; window.__createConfigURL = URL.createObjectURL;
    URL.createObjectURL = (blob) => { window.__download = blob.text(); return window.__createConfigURL(blob); };
    document.querySelector('#export-config').click()`);
  await until(() => evaluate(`!!window.__download`), 'export creates a downloadable JSON file');
  assert.deepEqual(JSON.parse(await evaluate('window.__download')), priorConfig);
  await evaluate('URL.createObjectURL = window.__createConfigURL');
  await evaluate(`window.__selectConfig = (text) => {
    const input = document.querySelector('#import-config-file'); const files = new DataTransfer();
    files.items.add(new File([text], 'netword-settings.json', {type:'application/json'}));
    input.files = files.files; input.dispatchEvent(new Event('change', {bubbles:true}));
  }; document.querySelector('#monitor-interval').value='123'; window.__selectConfig(${JSON.stringify('\ufeff' + upcomingJSON)})`);
  await until(() => evaluate(`!document.querySelector('#import-preview').hidden`), 'replacement preview ready');
  assert.ok(await evaluate(`{const p=document.querySelector('#import-preview'); p.textContent.includes('移除') && p.textContent.includes('127.0.0.1') && p.textContent.includes('45 秒') && p.textContent.includes('127.0.0.2') && p.textContent.includes('Token') && p.textContent.includes('<img src=x>') && !p.querySelector('img')}`));
  assert.ok(await evaluate(`{const d=document.querySelector('#settings-dialog'); d.scrollWidth <= d.clientWidth && document.documentElement.scrollWidth <= innerWidth}`), 'import preview fits 320px');
  await evaluate(`document.querySelector('#cancel-import').click()`);
  assert.equal(await evaluate(`document.querySelector('#import-preview').hidden`), true);
  assert.deepEqual(await (await fetch(base + '/api/config/export')).json(), priorConfig, 'cancel never writes settings');
  await evaluate(`window.__selectConfig(${JSON.stringify(upcomingJSON.replace('"version":1', '"version":1,"version":1'))})`);
  await until(() => evaluate(`!document.querySelector('#import-error').hidden && document.querySelector('#import-error').textContent.includes('重複')`), 'duplicate keys refused by server');
  const emptyImport = {...upcoming, hosts:[]};
  await evaluate(`window.__selectConfig(${JSON.stringify(JSON.stringify(emptyImport))})`);
  await until(() => evaluate(`document.querySelector('#import-preview').textContent.includes('清空') && !document.querySelector('#import-preview').hidden`), 'empty import warning');
  await evaluate(`window.__selectConfig(${JSON.stringify(upcomingJSON)})`);
  await until(() => evaluate(`!document.querySelector('#import-preview').hidden && document.querySelector('#import-preview').textContent.includes('127.0.0.2')`), 'select same file again');
  await fetch(base + '/api/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:'{"monitor_interval":35}'});
  await evaluate(`document.querySelector('#apply-import').click()`);
  await until(() => evaluate(`document.querySelector('#import-error').textContent.includes('失效') && document.querySelector('#import-preview').hidden`), 'stale preview requires choosing file again');
  await evaluate(`window.__selectConfig(${JSON.stringify(upcomingJSON)})`);
  await until(() => evaluate(`!document.querySelector('#import-preview').hidden`), 'fresh preview after expiry');
  await evaluate(`document.querySelector('#apply-import').click()`);
  await until(() => evaluate(`document.querySelector('#monitor-interval').value==='45' && document.querySelector('#import-status').textContent.includes('已匯入')`), 'apply replaces the unfinished frequency draft');
  const imported = await (await fetch(base + '/api/config/export')).json();
  assert.deepEqual(imported.settings, upcoming.settings);
  assert.equal(imported.hosts.length, 1);
  assert.equal(imported.hosts[0].ip, '127.0.0.2');
  assert.equal(imported.hosts[0].agent_enabled, false);
  assert.equal(imported.notifications.smtp_host, 'mail.example.com');
  assert.ok(!JSON.stringify(imported).includes('agent_token') && !JSON.stringify(imported).includes('"password"'));
  await evaluate(`document.querySelector('#settings-dialog').close()`);
  await until(() => evaluate(`document.querySelector('#host-rows').textContent.includes('127.0.0.2')`), 'overview refreshes after import');
  await command('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  // Keep fixture data inside this page; never modify deployed hosts or saved tokens.
  const fixture = {interval:30, settings:{refresh_interval:1,monitor_interval:30}, hosts:Array.from({length:4}, (_, index) => ({
    id:`fixture-${index}`, name:`Fixture ${index}`, ip:`192.168.8.${index+1}`, agent_enabled:index!==3, history:[],
    last:{timestamp:Date.now()/1000, status:index===2?'degraded':'up', latency_ms:1.5,
      checks:[{type:'tcp',label:'TCP 22',ok:true}],
      agent:{ok:index!==3, metrics:{hostname:index===0?'Host <img src=x onerror=alert(1)>':`Host ${index}`,os:index%2?'Linux':'Windows',cpu_percent:12.3,memory:{percent:45.6,used:4000000000,total:8000000000},uptime_seconds:100000,disks:[{mountpoint:'C:/',percent:67.8}],services:[{name:'ssh',status:index===2?'stopped':index===1?'unknown':'running'}]}}
    }
  }))};
  await evaluate(`window.__originalFetch=window.fetch; window.__fixture=${JSON.stringify(fixture)}; window.fetch=(path,options)=>String(path)==='/api/state'?Promise.resolve(new Response(JSON.stringify(window.__fixture),{status:200})):window.__originalFetch(path,options)`);
  await until(() => evaluate(`document.querySelectorAll('.host-card').length===4`), 'four-host metrics board');
  assert.equal(await evaluate(`document.querySelectorAll('.host-card img').length`),0,'metrics names must remain plain text');
  assert.ok(await evaluate(`document.querySelector('.host-card').textContent.includes('12.3%') && document.querySelector('.host-card').textContent.includes('45.6%') && document.querySelector('.host-card').textContent.includes('67.8%')`));
  assert.ok(await evaluate(`document.querySelectorAll('.host-card')[2].querySelector('.service-chip.bad').textContent.includes('stopped')`));
  assert.ok(await evaluate(`document.querySelectorAll('.host-card')[1].querySelector('.service-chip.unknown').textContent.includes('unknown')`));
  assert.ok(await evaluate(`document.querySelectorAll('.host-card')[3].textContent.includes('未啟用 Agent') && !document.querySelectorAll('.host-card')[3].textContent.includes('12.3%')`));
  assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true,'mobile metrics board fits');
  await command('Emulation.setDeviceMetricsOverride', {width:1366,height:768,deviceScaleFactor:1,mobile:false});
  await evaluate(`document.querySelector('#view-toggle').click()`);
  assert.equal(await evaluate(`document.querySelector('#host-board').hidden`),true,'list mode is available');
  await evaluate(`document.querySelector('#wall-toggle').click()`);
  await until(() => evaluate(`document.body.classList.contains('wall-mode') && !document.querySelector('#host-board').hidden`),'wall mode');
  assert.equal(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),true,'wall has no horizontal overflow');
  const fit = await evaluate(`({bottom:document.querySelector('.hosts-panel').getBoundingClientRect().bottom,height:innerHeight})`);
  assert.ok(fit.bottom <= fit.height, `All four cards must fit desktop wall: ${JSON.stringify(fit)}`);
  if (process.env.SCREENSHOT_PATH) {
    const shot=await command('Page.captureScreenshot',{format:'png'});
    await writeFile(process.env.SCREENSHOT_PATH,Buffer.from(shot.data,'base64'));
  }
  await evaluate(`document.querySelector('[data-filter="attention"]').click()`);
  assert.equal(await evaluate(`document.querySelectorAll('.host-card').length`),1,'status filters apply to cards');
  await evaluate(`document.querySelector('.host-card .host-link').click()`);
  assert.equal(await evaluate(`document.querySelector('#detail-dialog').open`),true,'board opens host detail');
  await evaluate(`document.querySelector('#detail-dialog').close(); document.querySelector('#wall-toggle').click()`);
  assert.equal(await evaluate(`document.body.classList.contains('wall-mode')`),false,'wall mode can be exited');
  await evaluate(`document.querySelector('#wall-toggle').click()`);
  await until(() => evaluate(`document.querySelectorAll('.host-card').length===4`),'fullscreen clears prior filters');
  Object.assign(fixture.hosts[0], {ip:'192.168.6.23', checks:[], agent_port:9105, agent_token_set:true,
    docker_monitor_enabled:true, website_monitor_enabled:true});
  Object.assign(fixture.hosts[0].last, {
    docker:{ok:null,label:'Docker 服務',detail:'Agent 無法提供資料'},
    website:{ok:true,label:'網站 urc.landagent.com.tw',detail:'HTTP 204',latency_ms:10},
  });
  Object.assign(fixture.hosts[1], {ip:'192.168.6.44', docker_monitor_enabled:true, website_monitor_enabled:true});
  Object.assign(fixture.hosts[1].last, {
    status:'degraded', docker:{ok:true,label:'Docker 服務',detail:'systemd 回報 Docker 服務運行中'},
    website:{ok:false,label:'網站 ai.tailan.com.tw',detail:'HTTP 503',latency_ms:12},
  });
  fixture.hosts[1].last.agent.metrics.services.push({name:'docker.service',status:'active'});
  await evaluate(`window.__fixture=${JSON.stringify(fixture)}; window.fetch=(path,options)=>{
    if(String(path)==='/api/state') return Promise.resolve(new Response(JSON.stringify(window.__fixture),{status:200}));
    if(String(path)==='/api/hosts/fixture-0' && options?.method==='PUT'){
      window.__editedHost=JSON.parse(options.body);
      return Promise.resolve(new Response('{}',{status:200}));
    }
    return window.__originalFetch(path,options);
  }`);
  await until(() => evaluate(`document.querySelectorAll('.host-card')[0].textContent.includes('192.168.6.23')`), 'monitor fixture loaded');
  assert.ok(await evaluate(`Array.from(document.querySelectorAll('.host-card')[0].querySelectorAll('.service-chip')).some(chip=>chip.textContent==='Docker 服務 · 無資料' && chip.classList.contains('unknown'))`), 'missing Docker data is distinct from failure');
  assert.ok(await evaluate(`document.querySelectorAll('.host-card')[0].textContent.includes('網站 urc.landagent.com.tw · 正常')`));
  assert.ok(await evaluate(`{const card=document.querySelectorAll('.host-card')[1]; card.textContent.includes('網站 ai.tailan.com.tw · 異常') && card.textContent.includes('Docker 服務 · 正常') && !card.textContent.includes('docker.service') && card.textContent.includes('ssh');}`));
  await evaluate(`document.querySelectorAll('.host-card')[0].querySelector('.host-link').click()`);
  assert.ok(await evaluate(`Array.from(document.querySelectorAll('#detail-content .check-row')).some(row=>row.textContent.includes('Docker 服務') && row.textContent.includes('無資料') && row.textContent.includes('Agent 無法提供資料'))`));
  await evaluate(`document.querySelector('#detail-dialog').close(); document.querySelectorAll('.host-card')[1].querySelector('.host-link').click()`);
  assert.ok(await evaluate(`{const text=document.querySelector('#detail-content').textContent; text.includes('HTTP 503') && text.includes('systemd 回報 Docker 服務運行中') && !text.includes('docker.service') && text.includes('ssh');}`));
  await evaluate(`document.querySelector('#detail-dialog').close(); document.querySelectorAll('.host-card')[0].querySelector('.host-link').click(); document.querySelector('#edit-host').click(); document.querySelector('#host-name').value='Fixture renamed'; document.querySelector('#host-form').requestSubmit()`);
  await until(() => evaluate(`window.__editedHost?.name==='Fixture renamed'`), 'ordinary edit of monitored host');
  assert.ok(await evaluate(`!('docker_monitor_enabled' in window.__editedHost) && !('website_monitor_enabled' in window.__editedHost) && !('agent_token' in window.__editedHost)`));
  await until(() => evaluate(`!document.querySelector('#host-dialog').open`), 'ordinary edit saved');
  await evaluate(`window.fetch=()=>Promise.reject(new TypeError('simulated offline'))`);
  await until(() => evaluate(`!document.querySelector('#state-error').hidden`),'connection failure warning');
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('#state-error')).display!=='none'`),true,'stale-data warning remains visible in wall mode');
  assert.deepEqual(errors, []);
  console.log('PASS: host/scan and settings workflows, Gmail, metrics board, separate Docker/website status and host editing, safe rendering, filters, details, mobile and desktop wall');
} catch (error) {
  if (socket?.readyState === WebSocket.OPEN) {
    try { console.error('Browser diagnostic:', await evaluate(`JSON.stringify({hostError:document.querySelector('#host-form-error').textContent,scanError:document.querySelector('#scan-error').textContent,scanStatus:document.querySelector('#scan-status').textContent,scanRange:document.querySelector('#scan-range').value,scanPorts:document.querySelector('#scan-ports').value})`)); } catch {}
  }
  console.error('Browser errors:', errors);
  throw error;
} finally {
  if (socket?.readyState === WebSocket.OPEN) { try { await command('Browser.close'); } catch {} socket.close(); }
  browser?.kill();
  backend?.kill();
  await delay(600);
  await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 250 });
}
