# Netword 開發與維護說明

## 1. 專案目的與目前狀態

提供 Windows／Linux 區網主機探索、集中服務監控與 Agent 主機指標。介面使用繁體中文。

目前管理端已部署在本機 Docker Desktop，專案目錄為 `E:\netword-monitor`，網址為 **http://127.0.0.1:8080**。原本直接執行的 Python 管理程序已停止，請勿同時啟動兩個管理程序共用設定檔。

目前五台主機均已部署 Agent，各台已完成連線驗證：

| IP | 系統／主機名稱 | 管理帳號 | Agent 服務檢查 |
| --- | --- | --- | --- |
| 192.168.6.6 | Windows／DESKTOP-KUAN | markkuan | sshd |
| 192.168.6.23 | Linux／la-ubuntu-server | landagent | ssh |
| 192.168.6.44 | Linux／landagent-ubuntu | landagent | ssh |
| 192.168.6.218 | Windows Server 2022／win2022-server | markkuan | sshd |
| 192.168.6.212 | Linux／ur-webserver | uradmin | ssh |

狀態可能隨實際網路與主機運作改變，表格不是即時健康保證。

## 2. 架構與檔案

- `server.py`：Python 標準函式庫 HTTP 管理端、設定保存、背景監控、區網探索及 API。
- `agent.py`：在被監控主機上執行，透過 psutil 提供 CPU、記憶體、磁碟、運行時間及指定系統服務狀態。
- `notifications.py`：SMTP 設定驗證與安全原子保存、異常／恢復狀態機、單一背景寄信工作與有界佇列，僅 Python 標準函式庫。
- `website_probe.py`：`.23`、`.44` 的固定 HTTPS 探測；只連已驗證的公開單播 IP，仍以原網域驗證憑證，不跳轉；每站一份在途工作、總期限 5 秒。
- `requirements-agent.txt`：Agent 相依套件，psutil 版本範圍 `>=6,<8`。
- `windows_app.py`：Windows 每使用者 WebView2 獨立視窗與系統匣，透過事件控制獨立管理子程序，固定回環 8080，不與 Docker 共用設定。Windows 使用 `SO_EXCLUSIVEADDRUSE` 避免重複綁定。視窗 X 隱藏至系統匣，離開才停止；需 WebView2 Runtime，明確拒絕 MSHTML 回退。桌面 API 使用每個控制器獨立隨機路徑，避免停止後舊頁面誤操作後來接手 8080 的 Docker；不是登入認證。
- `packaging/`、`requirements-windows.txt`：PyInstaller／Inno Setup 安裝包、圖示、繁中訊息與授權收集腳本。輸出 `dist/installer/NetwordMonitor-Setup-<版本>-x64.exe`，不含正式機密；建置工具與輸出忽略 Git。
- Windows 設定位於 `%LOCALAPPDATA%\NetwordMonitor\data`，日誌位於 `logs`；安裝目錄為 `%LOCALAPPDATA%\Programs\NetwordMonitor`，解除安裝保留設定。尚未切換正式 Docker 或自動搬移設定；切換須先停止一方並遵循 README。
- `static/index.html`：單頁介面與設定、探索、詳情對話框。
- `static/app.js`：API 輪詢、畫面狀態、戰情卡片與表單操作，無前端框架或打包流程。
- `static/style.css`：深色響應式介面、桌面四台戰情總覽與全螢幕樣式。
- `Dockerfile`、`compose.yaml`、`.dockerignore`：管理端映像與本機部署設定。
- `data/settings.json`：共用畫面同步與背景監測間隔，原子保存，重啟保留；Windows 版使用其獨立資料目錄，不隨安裝包提供。
- `data/hosts.json`：主機設定與 Agent Token；屬機密資料，不可提交版本控制或打包進映像。
- `data/notifications.json`：通知設定與明文 SMTP 密碼，同屬機密資料；密碼只供寫入，省略保留、空字串清除，API 僅回傳 `password_set`。
- `tests/test_server.py`：管理端驗證、狀態判定、HTTP 安全、保存與逾時測試。
- `tests/test_agent.py`：Agent 指標、認證、來源限制及請求處理測試。
- `tests/test_integration.py`：具備 psutil 時執行的真實 Agent 整合測試。
- `tests/test_notifications.py`：SMTP 驗證／機密保存、異常恢復去重順序、有界佇列／重試、傳送逾時與接受結果不確定測試，不寄真實郵件。
- `tests/test_windows_app.py`：隔離的 Windows 事件／單例鎖、系統匣選單、後端啟停／重啟、持久化、權限及連接埠衝突測試。`tests/windows_package_smoke.py` 驗證真實安裝／解除安裝與 frozen 後端，不使用正式設定或 8080。
- `tests/windows_ui_smoke.py`：真實 WebView2／系統匣載入、關閉隱藏、重開與離開測試；使用臨時設定與隨機回環連接埠，不修改正式 Docker 或桌面設定。
- Windows 維護注意：桌面 HTTP Handler 動態為 HTML 靜態資源與 `app.js` API 請求加上工作階段路徑，避免後端停止後舊頁面連入其他管理端。修改 `static/app.js` 的 `fetch(path, options)` 呼叫形式時，須同步更新轉換與隔離測試；不相符時桌面腳本拒絕載入。pywebview 固定 6.2.1，升級須重驗 WebView2 renderer 與 Windows SessionEnding 行為。
- `packaging/licenses/WebView2-SDK/` 保留已隨程式散布之 SDK 1.0.3856.49 的官方授權、NOTICE 與來源，須提交 Git；其他相依授權由 `collect_licenses.py` 在建置時收集。SDK DLL 與系統 WebView2 Runtime 不同，Runtime 仍需由使用者另外安裝。
- `tests/browser_smoke.mjs`：使用 Chrome／Edge DevTools Protocol 的瀏覽器操作測試，無 npm 相依套件。
- `README.md`：使用者啟動、部署與 Agent 安裝說明。

管理端需 Python 3.10+；瀏覽器測試需 Node.js 22+ 與 Chrome 或 Edge。管理端不需要 psutil。

## 3. 本機 Docker 維護

在 `E:\netword-monitor` 執行：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 50
docker compose restart
# 停止但保留本機設定：
docker compose down
```

- Compose 專案為 `netword`，服務為 `monitor`，映像為 `netword-monitor:local`。
- 僅發佈 `127.0.0.1:8080:8080`，不可隨意改為對所有網卡開放。
- 容器內使用 `--bind 0.0.0.0` 接收 Docker 轉送；原生 Python 預設仍只綁定 `127.0.0.1`。
- `./data` 綁定掛載到 `/app/data`。重建映像不會清除設定。實際來源應為 `E:\netword-monitor\data`；不要從舊的 `E:\netword` 目錄重建，以免掛載空目錄或產生寫入權限問題。
- 2026-11-24 已修正舊目錄掛載問題，恢復既有五台主機與 Token；已驗證 API 保存、容器重啟後設定保留及五台 Agent 正常。修復前備份為 `data/hosts.pre-repair-*.json`，含機密 Token，不可提交。
- 非 root UID/GID 為 `10001:10001`，根檔案系統唯讀，移除所有 capabilities，啟用 no-new-privileges。
- 透過 `net.ipv4.ping_group_range` 允許非 root ICMP Ping，不應為此啟用 privileged 容器。
- 根路徑之外的可寫位置為資料掛載與暫存 `/tmp`。
- 包含 HTTP 健康檢查、有限容量日誌與 `unless-stopped` 重啟策略。Docker Desktop 仍須啟動。
- 部署前備份位於 `data/hosts.pre-docker-*.json`，同樣含機密 Token，請妥善保護。
- 容器只包含管理端與前端；Agent 必須留在各實體／虛擬主機，不能改在管理容器中量測。
- 在原生 Linux 使用綁定掛載時，需確認 `data` 可由 UID/GID `10001:10001` 寫入。

管理端或前端檔案修改後，必須重新 `docker compose up -d --build`，不是只重啟容器。不要停止或重建本機其他專案的容器。

回復原生執行：先 `docker compose down`，再於專案目錄執行 `python server.py`。

## 4. 監控與資料契約

主要 API：

- `GET /api/state`：全部主機、最新檢查、Agent 指標、近期歷史、相容欄位 `interval` 與 `settings` 更新頻率設定。
- `GET /api/settings`、`PUT /api/settings`：共用更新頻率；`refresh_interval` 為 1–3600 整數秒、`monitor_interval` 為 5–3600 整數秒，拒絕布林、字串及未知欄位。省略欄位保留現值，保存失敗不得修改記憶體。
- `POST /api/hosts`、`PUT /api/hosts/{id}`、`DELETE /api/hosts/{id}`：主機設定維護。
- `POST /api/refresh`：安排背景立即檢查，不保證回應當下已完成量測。
- `POST /api/scan`：提交 `{range, ports}` 探索工作。
- `GET /api/scan`：取得探索進度、結果與錯誤。
- `GET /api/notifications`、`PUT /api/notifications`：讀取公開通知設定／保存，預設停用，密碼不可回傳。
- `POST /api/notifications/test`：使用已儲存設定排程測試信，回傳 202 不代表送達；GET 設定中的 `delivery` 顯示結果。每 30 秒最多一次。

主機設定包含 `id`、`name`、`ip`、`checks`、`agent_enabled`、`agent_port`；Token 只供寫入，公開回應僅提供是否已設定，不回傳內容。編輯時省略 `agent_token` 會保留既有值，空字串代表清除，前端不可混淆。

`docker_monitor_enabled`、`website_monitor_enabled` 為主機設定的布林旗標，預設 `false`，更新省略時保留。只可對 `.23`（`urc.landagent.com.tw`）及 `.44`（`ai.tailan.com.tw`）啟用，Docker 另須啟用 Agent 並回報 `docker.service`。正式啟用與回復見 README；不透過 Docker socket、不提權、不自動重啟。

最新狀態位於 `host.last`，包含 `status`、`timestamp`、`latency_ms`、`checks` 與 `agent`。Agent 成功時指標在 `last.agent.metrics`，包含 `hostname`、`os`、`cpu_percent`、`memory`、`disks`、`uptime_seconds`、`services`。

`last.host_status` 用於泛用主機通知；獨立量測在 `last.docker`／`last.website`。`ok` 為 `true`、`false` 或 `null`（無資料）。獨立檢查可讓可達主機顯示 `degraded`，網站回應不能當成主機可達證據。

- `up`：有可達證據，且設定的檢查無失敗。
- `degraded`：主機可達，但部分檢查或 Agent／指定服務異常。
- `down`：沒有取得可達證據，不等同證明主機關機。
- 無歷史或尚未檢查時，前端顯示待檢查，不可視為正常。

監測預設每輪完成後等待 30 秒；前端預設每 5 秒同步 API，兩者不是相同量測頻率。面板下方「更新頻率設定」可分別調整並保存至 `settings.json`；已保存監測值優先於 CLI `--interval` 的啟動預設值。監測間隔改變會喚醒等待並安排新一輪檢查，畫面同步變更不觸發量測；其他畫面在下次同步時套用。不變更 Agent 自身的 5 秒收集頻率。每台保留最近 120 筆狀態在記憶體，重啟後重新累積。主機設定以原子替換保存，保存失敗須回復記憶體，避免畫面與檔案不一致。

探索只接受指定私有 IPv4 與本機回環範圍，單次最多 1024 位址；空連接埠清單表示只使用 Ping。Docker 內的 `127.0.0.1` 指容器自己，監控 Windows 管理主機應使用 `192.168.6.6`。

## 5. 戰情總覽介面

- 預設卡片顯示所有符合篩選的主機：CPU、記憶體、磁碟最高使用率、服務摘要、回應時間、運行時間及最後檢查。
- 服務包含設定的 TCP／HTTP、Agent `--services` 和啟用的固定 Docker／網站監控，不是列舉作業系統所有服務。
- 磁碟卡片顯示最高使用率與對應掛載點；完整磁碟清單在主機詳情。
- 無 Agent 資料時顯示缺值，不可當成 0%；服務 `unknown` 與已確認失敗使用不同呈現。
- 全螢幕總覽進入時清除搜尋與狀態篩選，以免漏掉正常主機。四台桌面 2×2 已驗證可在 1366×768 顯示。
- 手機使用單欄；更多主機或大量服務可能需要捲動，不能宣稱任意數量都可單屏呈現。目前已增加至五台，現有桌面兩欄配置會增加第三列，原先四台 2×2 單屏驗證不代表五台仍可單屏顯示。
- 保留原本清單、主機詳情、搜尋與篩選。連線失敗時需顯示資料可能過期的警告，包含全螢幕模式。
- 所有 API 文字使用 DOM `textContent` 或安全屬性，不使用未過濾的 HTML 插入。

## 6. Agent 部署與維護

共通設定：TCP `9105`，使用各主機獨立隨機 Token；`--allow-ip 192.168.6.6` 限制管理端來源。Token 不可寫入開發文件、提交、測試輸出或對話。

### Linux

安裝位置：兩台 `landagent` 主機為 `/home/landagent/netword-agent`；`uradmin@192.168.6.212` 為 `/home/uradmin/netword-agent`。Token 位於各自的 `agent.env`，權限 600，目錄 700。

使用者 systemd 單元：`~/.config/systemd/user/netword-agent.service`。三台均已開啟 Linger，以支援未登入時運行。維護指令須以該服務所屬帳號執行。

```bash
systemctl --user status netword-agent.service
systemctl --user restart netword-agent.service
journalctl --user -u netword-agent.service -n 50 --no-pager
loginctl show-user "$(id -un)" -p Linger
```

`192.168.6.44`、`192.168.6.212` 的 UFW 已由使用者允許管理端連入 9105；`192.168.6.23` 部署時 UFW 未啟用，未擅自改為啟用。最小範圍規則為：

```bash
sudo ufw allow from 192.168.6.6 to any port 9105 proto tcp
```

`192.168.6.23` 缺少可用 ensurepip／pip；`192.168.6.212` 沒有系統 pip。兩台均使用 `venv --without-pip` 搭配官方 pip wheel 完成隔離環境安裝，未修改系統 Python。日後重建環境應先檢查 venv 與 pip 可用性。

### 新增 uradmin@192.168.6.212 的部署紀錄

- 主機名稱 `ur-webserver`，Linux，系統 Python `3.12.3`；隔離環境安裝 psutil `7.2.2`。
- Agent 啟動參數：`--bind 192.168.6.212 --port 9105 --allow-ip 192.168.6.6 --services ssh`。
- 使用者服務已 enable 並運行，`Linger=yes`；失敗自動重啟，`NoNewPrivileges=true`、`UMask=0077`。
- 已更新 Docker 管理端中該 IP 的主機設定，保留原有名稱與服務檢查，啟用 Agent 並設定獨立 Token；未將 Token 記入文件或 Git。
- 初次連線因 UFW 阻擋而逾時；使用者放行上述規則後，已透過管理端 `/api/state` 確認 `status=up`、`agent.ok=true`。
- 驗證當下 CPU `0.0%`、記憶體 `12.4%`、SSH `active`。數值僅為當次紀錄，最新指標以網頁為準。

### Windows

兩台安裝位置均為 `C:\Users\markkuan\AppData\Local\NetwordAgent`，包含 `.venv`、`agent.py`、`agent.env`、`run_agent.py` 與 `agent.log`。目錄 ACL 限制目前使用者、SYSTEM 與 Administrators。

排程工作 `NetwordAgent`：開機觸發、S4U 登入、Limited 權限、不儲存使用者密碼，失敗每分鐘重試。

```powershell
Get-ScheduledTask -TaskName NetwordAgent
Get-ScheduledTaskInfo -TaskName NetwordAgent
Stop-ScheduledTask -TaskName NetwordAgent
Start-ScheduledTask -TaskName NetwordAgent
Get-Content "$env:LOCALAPPDATA\NetwordAgent\agent.log" -Tail 30
```

注意：Windows venv 的 Python 可能實際以 `C:\Python314\python.exe` 執行。防火牆程式路徑必須以監聽程序的實際映像路徑為準，不能只假設是 `.venv\Scripts\python.exe`；`192.168.6.218` 已依實際路徑修正。仍須保留來源 IP 與目的連接埠限制。

Agent 程式修改不會自動更新遠端主機；需另行複製程式、更新相依套件並重新啟動對應服務／排程。

## 7. 安全與操作限制

- 管理介面尚無登入認證，不可直接公開到區網或網際網路。
- 保留 Host／Origin／跨站請求檢查，避免 DNS rebinding 與跨站操作。
- Agent 使用明文 HTTP Bearer Token，只適合可信管理網段或 VPN；不可直接暴露公網。來源白名單不是加密的替代品。
- Docker NAT 或網路拓樸改變後，需確認 Agent 看到的實際來源，勿為了排錯而放寬為所有來源。
- SSH 首次連線必須讓使用者確認主機指紋，不可自動略過或接受變更；私鑰與密碼不可輸出。
- 不透過 Docker 群組繞過 sudo 權限限制；需要管理權限時請使用者執行必要的最小範圍指令。
- HTTP 檢查 URL 必須使用被監控主機的 literal IP，不允許帳密或片段；不跟隨重新導向，HTTPS 驗證憑證。
- 外部 Agent JSON 須拒絕非有限數值、非法 Unicode 及不合結構資料，避免破壞整體狀態 API。
- 網路請求必須有總逾時，不可只依賴 socket 閒置逾時，避免慢速回應卡住監測週期。管理端入站請求總期限 10 秒，同時連線上限 32。
- Agent 使用單一背景收集工作，首次認證請求後啟動，每輪完成後等待 5 秒；HTTP 等待快照最多 0.75 秒，快照從收集開始計算最多有效 15 秒，未就緒／失敗／過期回傳 503。卡住的 OS 查詢不會佔滿 HTTP 工作或持續新增收集執行緒；若一直無法恢復，排除底層問題後需重啟 Agent。
- 2026-11-24 已更新五台 Agent 並重啟服務／排程，程式備份為各安裝目錄的 `agent.py.pre-fix-*`。已修正非 UTF-8 顯示文字、快照收集隔離、管理端指標必要欄位／範圍驗證及安全暫存保存；五台更新後均重新驗證正常，Token 與來源限制未變更。
- 郵件通知支援 Gmail `smtp.gmail.com:587` STARTTLS 或 465 TLS，需兩步驟驗證與應用程式密碼，不支援 OAuth；不允許明文 SMTP，驗證 TLS 憑證。
- 通知依每輪完成量測計數，預設連續異常 2 次／恢復 2 次；同一事件不重複通知。單一 FIFO 寄送工作、256 封佇列、寄送期限 20 秒，異常／恢復失敗最多 3 次、相隔 60 秒。部分拒收或 SMTP 接受結果不確定不自動重寄。壅塞保留待排入恢復，可能合併反覆異常，非保證投遞系統。
- 去重與待寄佇列僅在記憶體；主機、Docker、網站依類型獨立去重。重啟或變更通知設定後重新累積；刪除主機取消全部舊事件，變更檢查或連線只取消受影響的事件。停用無法撤回已進入 SMTP 的郵件。勿在日誌／錯誤回應輸出 SMTP 原始例外，可能含密碼或郵件內容。
- 目前沒有長期歷史資料庫、內建管理介面 TLS 或集中自動更新功能。

## 更新頻率功能開發紀錄（2026-11-24）

- `server.py`：由 `Monitor` 載入／管理設定，沿用既有 Host／Origin 防護提供設定 API；以唯一暫存檔、fsync、原子替換保存，成功後才更新記憶體。既有設定檔格式錯誤時明確中止啟動，不靜默覆寫。
- `static/index.html`、`static/app.js`：新增設定對話框與獨立的畫面／監測間隔，替換固定輪詢計時器，保留請求不重疊機制；頁尾、連線失敗重試文字隨設定變更。Windows 工作階段 API 路徑隔離保持不變。
- `tests/test_server.py`、`tests/browser_smoke.mjs`：涵蓋預設值、上下界／型別、部分更新、保存失敗、喚醒監測、HTTP 防護、重載及動態計時器。`tests/test_windows_app.py`、`tests/windows_package_smoke.py` 驗證獨立桌面後端重啟後保留設定。
- 使用及搬移步驟見 `README.md`。安裝包與 Docker 映像已重建；僅提交原始碼、測試及文件，不提交 `data`、Token、SMTP 密碼、日誌或 `dist` 產物。未變更正式設定、未自動安裝升級，亦未啟動已停止的 Docker 服務。

## 8. 測試與驗收

```powershell
.venv\Scripts\python -m unittest discover -s tests -q
node --check static/app.js
node tests/browser_smoke.mjs
docker compose config --quiet
```

開發環境尚無 psutil 時，先安裝 `requirements-agent.txt`，才能執行完整真實 Agent 整合測試。瀏覽器測試可用 `CHROME_PATH` 指定執行檔，`PYTHON` 指定 Python；使用臨時資料目錄，不可把測試指向正式設定。

目前已驗證：

- 更新頻率功能新增後：89 項 Python 測試通過（開發 venv 略過 3 項 Windows 相依測試），獨立建置環境 12 項 Windows 測試通過；瀏覽器驗證間隔保存／重開／重載、1 秒輪詢替換計時器、跨畫面同步及手機設定框。新版安裝包已重新建置，frozen 後端重啟保留頻率、原生 WebView2／系統匣測試通過。Docker 僅重建映像：當時 Compose 服務未運行，未啟動以免與 Windows 版爭用 8080；未替使用者安裝升級。

- 2026-11-24 WebView2 改版：12 項隔離桌面測試及真實 WebView2 載入／隱藏／重開／離開測試通過；frozen 後端與保存測試通過。現有桌面安裝已存在，本輪未覆寫其登錄或執行安裝／解除安裝測試，未切換正式 Docker。完整開發環境 85 項測試完成（缺少桌面套件略過 3 項）。
- Windows 本機 80 項 Python 測試完成（含真實 Agent 整合測試及通知測試，原開發 venv 缺 pystray 時略過 1 項選單測試）。獨立 Windows 建置環境另驗證全部 7 項桌面測試通過。
- Windows x64 安裝包已完成真實隔離安裝、靜態資源／API、啟動／停止／重啟及保存、解除安裝保留設定測試；未切換或停止正式 Docker。未宣稱已在無 Python 的全新 Windows 主機驗收，尚未數位簽章。
- Docker Linux 映像內管理端 21 項測試已通過；通知測試更新後 24 項通過。
- Gmail 設定介面、密碼留空保留／明確清除、測試排程與手機對話框已通過瀏覽器測試。使用者已設定並確認收到 Gmail 測試信，帳密不可輸出。
- 異常信以繁體中文說明失敗項目、可能原因及建議，包括 TCP／HTTP、Agent 認證／指標與系統服務狀態；不輸出原始錯誤或 HTTP URL，未知服務狀態不視為已確認停止。
- 真實瀏覽器：新增／刪除、探索與勾選加入、安全文字呈現、戰情指標、未知／失敗狀態、篩選、詳情、手機與桌面全螢幕。
- Docker 部署：四台 Agent 均正常、ICMP／TCP 區網可達、單一指定主機探索成功、Host 防護有效。
- Docker 重啟後四台設定與 Token 保持不變，資料目錄可寫，容器健康檢查通過。

未來修改後應重新執行相關測試，並確認沒有把 Token、備份或本機 `.venv` 帶入建置內容。
