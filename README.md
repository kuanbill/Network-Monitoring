# Netword 區網主機監控

Windows／Linux 混合環境的輕量網頁工具。管理端只需 Python 3.10+，無需 Node.js、資料庫或 Python 第三方套件。

## 遠端開機（Wake-on-LAN）

在「主機詳情 → 編輯設定」填入受監測主機**有線網卡**的 MAC 位址並儲存。重新開啟主機詳情後，按「遠端開機」。清空 MAC 並儲存即可停用。管理端以 UDP 9 在本地區網廣播 Magic Packet；顯示已送出不代表已開機，請查看後續監測結果。

目標主機須保持供電，並在 BIOS／UEFI 與網卡啟用 Wake-on-LAN。管理筆電須保持開機，並能將廣播送達目標網段。跨 VLAN、Wi-Fi 用戶端隔離及 Docker Desktop 虛擬網路可能阻擋廣播；遇到這種情況，請先以同網段的 Windows 原生管理端驗證。不要同時執行兩個管理端共用資料目錄。

實機驗證：`192.168.6.44`（`00:D8:61:B5:BC:83`）調整 BIOS 後，已由管理筆電 `192.168.6.63` 直接送封包成功從關機喚醒，SSH 及 Agent 均恢復。此次驗證不包含監控介面按鈕或 Docker 網路。

## 啟動管理端

在本專案目錄執行：

```powershell
python server.py
```

開啟 **http://127.0.0.1:8080**（亦可用 localhost）。保持終端機開啟，Ctrl+C 停止。

```powershell
python server.py --port 8088 --interval 30 --data-dir ./data
```

`--interval` 為尚未儲存設定時的背景監測預設等待秒數，允許 5–3600 秒；已有 `settings.json` 時，以已儲存值優先。

### 調整更新頻率

在左側工具列按「設定」，找到「更新頻率」：

- **畫面同步間隔**：1–3600 秒，預設 5 秒；只同步管理端資料，不會重新量測主機。
- **背景監測間隔**：5–3600 秒，預設 30 秒；每輪量測完成後等待此秒數，主機多或逾時時，實際量測間隔更長。
- 儲存後目前畫面立即套用；背景監測間隔有變更時會安排新一輪檢查。其他已開啟的畫面在下次同步時套用。
- 設定會保存，重新啟動仍保留。一般為資料目錄的 `settings.json`；曾成功匯入的安裝則保存於 `configuration.json`。`GET /api/settings`、`PUT /api/settings` 使用 `refresh_interval`、`monitor_interval` 整數秒數。
- 更短的間隔會增加網路／主機負載，也會影響以連續輪次確認的異常通知時間；不會改變 Agent 本身的指標收集頻率。

### 匯出與匯入設定

- 在左側「設定」按「匯出設定」，下載目前的主機清單、更新頻率及郵件設定。JSON **不含 Agent Token 或 SMTP 密碼**，但仍可能包含內部 IP、檢查網址和郵件資訊，請勿公開。
- 選取先前匯出的 JSON，查看取代前預覽。只有按「確認取代全部設定」才會保存；**檔案中沒有的主機會移除**。若要保留目前設定，可先按「匯出設定」。
- 只有相同 IP／Agent 連接埠，或相同 SMTP 主機／連接埠／TLS 模式／登入帳號，才保留本機對應憑證。預覽會列出待補填項目：缺少 Token 的 Agent 和相依的 Docker 監控暫停，請在主機設定重新輸入 Token、啟用 Agent，並按下方 Docker 說明重新啟用 Docker；缺少 SMTP 密碼時，在「郵件通知」補填並重新啟用。匯入不會寄測試信。
- 首次成功匯入後，改用資料目錄的 **`configuration.json`** 保存全部設定。舊 `hosts.json`、`settings.json`、`notifications.json` 不會被更新；若新檔有問題，程式不會自動退回舊檔。包含密碼的完整備份應在管理端停止後，備份整個受保護的 `data` 目錄。不可用舊版開啟曾成功匯入的資料目錄，也不要把有憑證的檔案上傳或放進安裝檔。

## Windows 安裝版（獨立視窗＋系統匣）

支援 Windows 10／11 x64，目標電腦不需 Python 或 Docker，但需 Microsoft Edge WebView2 Runtime（多數電腦已內建；缺少時請從 https://developer.microsoft.com/microsoft-edge/webview2/ 安裝 Evergreen Runtime）。不會自動下載或要求管理員權限，也不會改用舊版 IE 引擎。安裝檔為
`dist/installer/NetwordMonitor-Setup-1.4.0-x64.exe`；尚未數位簽章，Windows 可能提示發行者不明，請先核對來源與 SHA-256。

- 安裝僅供目前使用者，不需管理員權限。安裝目錄為 `%LOCALAPPDATA%\Programs\NetwordMonitor`。
- 啟動後自動開始監控並開啟獨立 WebView2 桌面視窗，不另開瀏覽器分頁。右下系統匣可開啟介面、啟動／停止監控、開啟資料／日誌資料夾及離開。圖示可能位於隱藏圖示區；按視窗 X 會隱藏至系統匣並繼續監控，從系統匣選「離開」才會結束程式。
- 可於安裝時選擇「登入 Windows 後自動啟動」，預設不勾選。這不是 Windows 服務，登出後不持續執行。
- 桌面視窗使用獨立隨機工作階段路徑，停止後不會將 API 操作送到後來啟動的 Docker／另一個桌面管理端；這不是登入認證。
- 僅綁定 `127.0.0.1:8080`，保留原有 Host／Origin 防護。重複開啟會提示已有程式執行；8080 已被 Docker／其他程式占用時不會啟動管理端，也不會自動開啟另一個程式的頁面。
- 設定在 `%LOCALAPPDATA%\NetwordMonitor\data`，日誌在同目錄下的 `logs`，限制目前使用者、SYSTEM、Administrators 存取。日誌最多約 4 MB。設定不放在安裝目錄，升級／解除安裝會保留資料。
- 安裝包不含任何主機設定、Token、SMTP 密碼或備份，首次使用為空設定。各主機 Agent 仍需獨立部署，並確認管理電腦的實際來源 IP 在白名單內。
- 停止／離開時先要求背景程序正常結束，最多等候 10 秒，仍未結束才終止該子程序；已提交 SMTP 的信無法撤回，記憶體歷史與佇列會清除。

### 從現有 Docker 設定移轉（選用）

**目前部署不會被安裝或建置自動停止，也不會自動搬移機密。** 確定要切換時：

1. 備份專案 `data`，在 `E:\netword-monitor` 執行 `docker compose down`，保留原始設定。
2. 安裝並啟動 Windows 版一次，從系統匣選「離開」，確認背景管理端已停止。
3. 先備份來源與目的地的完整資料目錄。若原部署已使用 `data/configuration.json`，複製此檔至 `%LOCALAPPDATA%\NetwordMonitor\data`，並使用支援匯入的新版本。否則複製來源的 `hosts.json`、`notifications.json`、`settings.json`（以實際存在者為準）至乾淨的目的地資料目錄，避免以前的 `configuration.json` 或其他舊檔掩蓋來源設定。檔案含明文 Token／SMTP 密碼，請勿以郵件或 Git 傳送。
4. 重新開啟 Windows 版，驗證主機、Agent 與通知。不要讓兩個管理端同時使用同一份設定。

升級前先從系統匣離開；重新安裝即可更新程式。要回復 Docker，先離開 Windows 版，再於專案目錄執行 `docker compose up -d`；兩邊設定各自獨立，若有修改，須在都停止時自行同步。

### 建置 Windows 安裝檔

在 Windows x64 安裝 Python 3.10+ 與 Inno Setup 6，再於專案執行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1 -Iscc "C:\Path\To\ISCC.exe"
# 僅測試安裝包，使用臨時資料與隨機連接埠，不觸碰正式設定：
python tests/windows_package_smoke.py --installer dist/installer/NetwordMonitor-Setup-1.4.0-x64.exe
# 已有正式桌面安裝時不覆寫其登錄，改驗證可攜式後端：
python tests/windows_package_smoke.py --exe dist/NetwordMonitor/NetwordMonitor.exe
# 真實 WebView2／系統匣介面測試，同樣使用臨時資料與隨機連接埠：
.venv-windows-build\Scripts\python.exe tests/windows_ui_smoke.py
```

腳本建立獨立 `.venv-windows-build`、安裝 `requirements-windows.txt`，透過 PyInstaller 產出整個 `dist/NetwordMonitor` 資料夾，再以 Inno Setup 建立安裝檔。不可只複製其中的 EXE；`_internal` 也是必要部分。相依套件授權在 `_internal/licenses`。已使用 Inno Setup 6.4.3 驗證；使用新版編譯器請先確認其商業使用授權條件。建置工具／輸出均忽略 Git，不應將本機 `.venv` 或設定加入打包白名單。

## Docker 部署（本機 Docker Desktop）

在 `E:\netword-monitor` 執行，先停止原本佔用 8080 的 `python server.py`，避免兩個管理程序共用設定檔：

```powershell
docker compose up -d --build
docker compose ps
docker compose logs --tail 50
```

網址仍是 **http://127.0.0.1:8080**。Compose 只將網頁發佈到本機回環位址，不開放區網連入；容器內的 `--bind 0.0.0.0` 僅用於 Docker 轉送，Host／Origin 防護仍保留。

- `./data` 掛載到 `/app/data`，保留現有主機設定與 Agent Token；不會把設定或 Token 打包進映像。請保護此資料夾與備份檔，勿提交版本控制。
- 重建／重啟容器不會清除設定；近期監測歷史只存在記憶體，重啟後重新累積。
- `docker compose restart` 重啟，`docker compose down` 停止（保留本機 `data`）。程式修改後執行 `docker compose up -d --build` 更新。
- 容器採非 root、唯讀根檔案系統及 `unless-stopped` 重啟策略；要隨 Windows 登入恢復，Docker Desktop 也必須啟動。
- Windows／Linux Agent 仍須在各被監控主機執行，不應移入管理容器，否則量測的是容器本身。
- Docker Desktop 的區網連線來源可能經 NAT；更換主機或 Docker 網路後，需重新確認 Agent 來源白名單與防火牆。容器中的 `127.0.0.1` 是容器，不是 Windows 主機，監測 Windows 應使用其區網 IP。
- 若新建資料夾部署於原生 Linux，綁定掛載的 `data` 需讓 UID/GID `10001:10001` 可寫。

回到原生 Python 執行時，先 `docker compose down`，再執行 `python server.py`。

## 使用方式

1. 輸入你有權管理的網段，例如 `192.168.1.0/24` 或 `192.168.1.10-192.168.1.50`。
2. 掃描透過 ICMP Ping 與指定 TCP 連接埠發現有回應的裝置；選取後加入監控。也可手動新增 IP。
3. 設定 TCP 服務連接埠，例如 SSH `22`、HTTP `80`、HTTPS `443`、RDP `3389`、SQL Server `1433`。
4. 網站可另設定 HTTP／HTTPS URL，例如 `http://192.168.1.10:8080/health`。URL 必須使用該主機的 IP。HTTPS 會驗證憑證，憑證須受管理端信任且包含該 IP。
5. 預設「主機戰情總覽」以卡片並排顯示主機名稱、IP、系統、CPU、記憶體、磁碟最高使用率、已設定服務狀態、回應時間及運行時間。沒有 Agent 資料時顯示缺值，不會當成 0%。
6. 點「全螢幕總覽」進入適合四台主機的桌面 2×2 戰情畫面（已驗證 1366×768）；進入時會清除篩選並顯示全部主機。可按 Esc 或「離開全螢幕」返回；瀏覽器不允許全螢幕時仍可使用專注模式。手機改為單欄，多主機或大量服務時仍可能需要捲動。
7. 點主機名稱查看完整磁碟清單、檢查結果及最近 120 次狀態；「切換清單」可回到原本表格。服務清單只包含設定的檢查及 Agent `--services` 項目，並非列出作業系統的所有服務。

### 狀態判定

- **正常 up**：有主機可達證據，且沒有檢查回報失敗；個別項目仍可能暫無資料。
- **異常 degraded**：主機有回應，但有檢查回報失敗。
- **無回應 down**：沒有取得主機可達證據；不代表一定關機，防火牆也可能阻擋探測。獨立網站回應不代表主機可達。
- 尚未檢查的主機不顯示既有結果。

TCP 成功只表示可建立連線，不保證資料庫等應用程式功能正常；使用主機 IP 的既有 HTTP 檢查接受 200–399，不自動跟隨重新導向。Ping 延遲是探測程序耗時的近似值，不是精確的 ICMP RTT；ICMP 不通時可能顯示成功 TCP／HTTP 檢查耗時。

### 固定主機的 Docker 與網站監控

| 主機 | Docker | 網站 |
| --- | --- | --- |
| `192.168.6.23` | `docker.service` | `https://urc.landagent.com.tw/` |
| `192.168.6.44` | `docker.service` | `https://ai.tailan.com.tw/` |

兩項預設關閉，可分別啟用。Docker 只讀取 systemd 狀態，不枚舉容器、不接觸 Docker socket，也不會自動重啟。Agent 失聯時 Docker 標為「無資料」，不誤報 Docker 停止。網站由管理端以固定網域檢查，只接受公開位址與有效的 HTTPS 憑證、5 秒內的 `2xx` 回應；不跟隨重新導向。這項檢查不驗證登入後的功能。

**上線步驟（維護者）：**

1. 在兩台 Linux 以 Agent 帳號確認 `systemctl show docker.service --property=LoadState,ActiveState` 回報系統層級單元已載入並運行。從實際管理端的網路環境確認上表網址透過公開位址正常回應 `2xx`；若只解析到非公開位址、網站固定回應重新導向，或 Docker 為 rootless／不同單元，先核對需求，不要放寬檢查。
2. 若要啟用 Docker 檢查，備份兩台原本的 `~/.config/systemd/user/netword-agent.service`（依原權限保護），將其原有 `--services ssh` 擴充成 `--services ssh docker.service`。保留其餘啟動參數與環境設定，執行 `systemctl --user daemon-reload` 與 `systemctl --user restart netword-agent.service`。確認服務正常，且管理端的 Agent 指標包含 `ssh` 與 `docker.service`；不需更新 Agent 程式、加入 `docker` 群組或重啟 Docker。
3. 在既有部署位置更新並重建管理端（Docker 使用 `docker compose up -d --build`）。不要啟動第二套共用資料的管理端。
4. **Docker／原生管理端：**從同一管理端的本機 API 讀取設定，對正確的主機 ID 更新旗標。此版不提供網頁開關。以下範例啟用 `.23` 的兩項；對 `.44` 改 `$ip`，或按需要單獨設定一項。執行前請檢查主機、連接埠和權限。

   ```powershell
   $base = 'http://127.0.0.1:8080'
   $ip = '192.168.6.23'
   $target = (Invoke-RestMethod "$base/api/state").hosts | Where-Object { $_.ip -eq $ip }
   if (-not $target) { throw "找不到 $ip" }
   $payload = @{
       ip = $target.ip; name = $target.name; checks = @($target.checks)
       agent_enabled = $target.agent_enabled; agent_port = $target.agent_port
       docker_monitor_enabled = $true; website_monitor_enabled = $true
   } | ConvertTo-Json -Depth 8
   Invoke-RestMethod "$base/api/hosts/$($target.id)" -Method Put -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload))
   ```

   不要讀取或傳回 Token；省略 `agent_token` 就會保留。

   **Windows 安裝版：**不要執行上面的 API 範例，桌面版有獨立工作階段路徑。從系統匣選「離開」停止桌面管理端（按視窗 X 不會停止），備份受保護的 `%LOCALAPPDATA%\NetwordMonitor\data`。若有 `configuration.json`，編輯該檔的 `hosts` 清單；否則編輯 `hosts.json`。使用 UTF-8（無 BOM），只在對應 IP 的主機物件內新增或更新 `docker_monitor_enabled`、`website_monitor_enabled` 為 JSON 布林值；Docker 旗標為 `true` 時須保留 `"agent_enabled": true`。不要改其他欄位或列印 Token。儲存後重新啟動；若無法啟動，退出桌面版再還原備份。

   **回復：**先用各版本上述方式關閉相應旗標，再還原原 Agent 參數、重新載入並只重啟 Agent。停用 Agent 前須先或同時關閉 Docker 旗標。
5. 在戰情卡片與主機詳情確認兩項結果。已啟用 Gmail 通知時，Docker 與網站會各自計算異常和恢復；不必停掉正式服務來驗證。

## Email 異常通知（支援 Google Gmail）

在左側「郵件通知」設定，預設停用；只在管理端寄信，不需修改各台 Agent。

### Gmail 設定

1. 在 Google 帳號開啟「兩步驟驗證」，再建立「應用程式密碼」；Workspace 帳號可能需管理員允許。若帳號不提供此選項，請使用其他支援 TLS 的 SMTP 服務；本版不支援 Gmail OAuth。
2. 點「使用 Gmail 預設」：主機 `smtp.gmail.com`、連接埠 `587`、`STARTTLS`；亦可使用 `465` 搭配 `TLS`。
3. 登入帳號填完整 Gmail／Workspace Email，密碼填 16 字元應用程式密碼（移除分組空白），**不是 Google 一般登入密碼**。請在網頁輸入，不要放進程式、文件、Git 或對話。
4. 寄件人填同一帳號或 Gmail 已驗證的寄件別名，填入收件人（最多 20 位）。
5. 先按「儲存並寄測試信」；查看畫面上的寄送狀態，並檢查收件匣／垃圾郵件。測試使用已儲存設定，且不受通知啟用開關限制；每 30 秒最多一次。
6. 確認測試信後勾選啟用，再儲存。未設定 SMTP 憑證前不會自動寄信。

### 通知規則與限制

- 預設連續 **2 輪** `degraded`／`down` 才通知；連續 **2 輪** `up` 確認恢復，可選擇寄恢復信。次數可設為 1–10，依完成的主機量測計算，不是畫面同步次數。
- 同一異常事件只通知一次，`degraded` 與 `down` 互換不重複通知；確認恢復後再次異常才建立新事件。郵件以繁體中文列出主機名稱、IP、檢查時間（管理端時區）、失敗項目、可能原因與建議處理方式，包含 TCP 連接埠、HTTP 狀態、Agent 與指定系統服務狀態。無法確認根本原因時會明確說明，不將連線失敗視為已確認關機；不包含 Agent Token、原始錯誤或 HTTP URL 私密查詢參數。
- 啟用固定監控後，Docker、網站各自寄異常和恢復通知，不再因它們的單一故障額外寄泛用主機信。缺少資料會中斷該項的連續計數，不視為異常或恢復。
- 郵件使用獨立背景工作及有上限的佇列，不會阻塞主機監測。每封異常／恢復通知最多嘗試 3 次，相隔至少 60 秒；測試信僅嘗試一次。使用 FIFO 保持事件順序，因此 SMTP 故障重試會延後後續郵件。
- 單次寄送期限 20 秒，socket 閒置逾時 5 秒。若 DNS／底層呼叫未能及時退出，顯示結果未確定，等待原工作結束而不持續新增執行緒。
- SMTP 接受郵件不保證收件匣送達。部分收件人拒收或傳送途中斷線、無法確認 SMTP 是否接受時，不自動重寄以避免重複；畫面會顯示警告。恢復信會註明先前接受結果不確定的情況。
- 佇列最多 256 封；壅塞時顯示警告並延後排入，保留尚未排入的恢復狀態，可能合併壅塞期間的反覆異常。此功能不是保證投遞的長期事件紀錄。
- 通知設定保存在 `data/notifications.json`，曾成功匯入則使用 `data/configuration.json`。密碼只供寫入，API 不回傳。一般編輯留空保留原密碼，明確勾選清除才刪除。檔案含明文 SMTP 密碼，請保護檔案、目錄與備份 ACL，禁止提交 Git 或打包進映像。
- 事件去重與待寄佇列在記憶體中，重啟後重新累積；修改通知設定或主機連線設定會取消舊的待寄事件。停用無法撤回已交給 SMTP 的郵件。這是主機／服務狀態通知，未新增 CPU／磁碟使用率門檻告警。

## 選用：在被監控主機安裝代理程式

代理程式提供 CPU、記憶體、磁碟使用量、作業系統、開機時間及指定作業系統服務狀態。只有需要這些資訊的主機才需安裝；基本 TCP／HTTP 監控不需要。

將 `agent.py` 和 `requirements-agent.txt` 複製到被監控主機，建議使用虛擬環境：

```powershell
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -r requirements-agent.txt
# Linux
# .venv/bin/python -m pip install -r requirements-agent.txt
```

產生隨機 token（每台主機建議使用不同 token）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Windows PowerShell：

```powershell
$env:NETMON_AGENT_TOKEN = '貼上剛才產生的token'
.venv\Scripts\python agent.py --bind 0.0.0.0 --port 9105
```

Linux：

```bash
export NETMON_AGENT_TOKEN='貼上剛才產生的token'
.venv/bin/python agent.py --bind 0.0.0.0 --port 9105
```

代理程式預設綁定 `127.0.0.1`；被遠端管理端輪詢時需使用主機區網 IP 或 `0.0.0.0`。管理網頁中編輯主機、啟用代理程式、填入連接埠與同一 token。

建議加上 `--allow-ip 管理端IP`，在 Agent 層限制連線來源，例如 `--allow-ip 192.168.6.6`；可重複指定多個來源，未指定時不限制來源。這是額外防護，不能取代防火牆或傳輸加密。

可用 `--services` 指定要檢查的服務，例如：

```powershell
# Windows：實際服務名稱，不是顯示名稱
.venv\Scripts\python agent.py --bind 0.0.0.0 --services W3SVC MSSQLSERVER
```

```bash
# Linux：systemd unit 名稱
.venv/bin/python agent.py --bind 0.0.0.0 --services nginx ssh
```

查看 `python agent.py --help` 取得完整參數。未指定服務時不會自動列出所有服務。服務名稱不存在、無權讀取或系統不支援時會顯示 unknown；這也會使主機狀態變為異常。

Agent 在首次通過認證的指標請求後啟動單一背景收集工作，每輪完成後等待 5 秒；HTTP 最多等待 0.75 秒取得快照。從收集開始計算超過 15 秒的資料不會回報成功；首次收集未完成或收集失敗時回傳 503。磁碟／系統查詢即使阻塞，也不會持續新增收集執行緒或佔滿 HTTP 請求容量。Python 無法安全中斷卡住的 OS 查詢；若查詢一直無法恢復，需先排除檔案系統／服務問題，再重啟 Agent。

### 防火牆與安全

- **只在有授權的私有區網掃描**；限制 RFC1918 或本機 IPv4、每次最多 1024 個位址、16 個 TCP 連接埠；最多監控 256 台主機。
- 管理介面無使用者登入，刻意只綁定 `127.0.0.1` 並驗證 Host／Origin；不直接提供區網遠端管理。遠端管理請使用 SSH tunnel 等安全方式，不要直接對外轉發管理連接埠。
- 代理程式使用 Bearer token，至少 24 個字元。**目前代理程式使用 HTTP，token 與指標在網路上不加密**。僅限可信隔離管理網路／VPN；不適用不可信網段，不要暴露至網際網路。
- 主機防火牆只開放 **管理端 IP → 代理程式 TCP 9105**，不要對所有來源開放；只為需要監控的服務開放必要規則。
- `data/hosts.json` 或匯入後的 `data/configuration.json` 包含明文 Agent Token，請保護檔案與備份權限（Windows 請設定 ACL）。儲存使用唯一私密暫存檔，再原子替換。API 不會回傳 Token；一般編輯未填 Token 會保留原值。
- 管理端最多同時處理 32 個連線，每個請求設有 10 秒總期限，避免慢速標頭或本文持續佔用執行緒。
- 本版支援 SMTP 郵件通知，但不含登入權限、TLS 終止、長期歷史儲存、排程安裝／開機自啟。正式環境應再補足維運需求；不要將它視為 Zabbix 的完整替代。

## 資料與限制

- 主機設定重啟後保留；匯入後請備份現行 `data/configuration.json`，不要只備份舊檔。
- 最近 120 次監控結果與掃描結果僅在記憶體中，重啟後清空。
- 掃描會發現各類有回應裝置，並非僅限電腦。第一版不做反向 DNS／MAC／廠商查詢，主機名稱可自行設定。
- 被防火牆阻擋 Ping 且未開放指定連接埠的主機可能被漏掉，可手動加入並設定正確服務或代理程式。
- Linux 的 ICMP 探測需要系統提供 `ping`（例如 Debian／Ubuntu 的 `iputils-ping`）；缺少時仍可透過 TCP／HTTP／代理程式監控。
- CPU／記憶體／磁碟目前提供數值顯示，尚無資源使用率門檻告警；服務檢查失敗會反映在主機狀態。

## 測試

```powershell
python -m unittest discover -s tests -v
```

單元測試只使用標準函式庫並模擬代理程式資料；安裝 psutil 後會額外執行真實代理程式的本機整合測試：

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

另提供不依賴 npm 套件的 Chrome／Edge 瀏覽器冒煙測試（需 Node.js 22+ 與已安裝的瀏覽器；必要時以 `CHROME_PATH` 指定執行檔）：

```powershell
node tests/browser_smoke.mjs
```

測試使用暫存設定與本機連線，不掃描你的實際區網。跨 Windows／Linux 真實服務、防火牆及多主機環境仍需實機驗證。
