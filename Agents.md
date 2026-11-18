# Netword 開發與維護說明

## 1. 專案目的與目前狀態

提供 Windows／Linux 區網主機探索、集中服務監控與 Agent 主機指標。介面使用繁體中文。

目前管理端已部署在本機 Docker Desktop，專案目錄為 `E:\netword`，網址為 **http://127.0.0.1:8080**。原本直接執行的 Python 管理程序已停止，請勿同時啟動兩個管理程序共用設定檔。

目前四台主機均已部署 Agent，完成連線驗證：

| IP | 系統／主機名稱 | 管理帳號 | Agent 服務檢查 |
| --- | --- | --- | --- |
| 192.168.6.6 | Windows／DESKTOP-KUAN | markkuan | sshd |
| 192.168.6.23 | Linux／la-ubuntu-server | landagent | ssh |
| 192.168.6.44 | Linux／landagent-ubuntu | landagent | ssh |
| 192.168.6.218 | Windows Server 2022／win2022-server | markkuan | sshd |

狀態可能隨實際網路與主機運作改變，表格不是即時健康保證。

## 2. 架構與檔案

- `server.py`：Python 標準函式庫 HTTP 管理端、設定保存、背景監控、區網探索及 API。
- `agent.py`：在被監控主機上執行，透過 psutil 提供 CPU、記憶體、磁碟、運行時間及指定系統服務狀態。
- `requirements-agent.txt`：Agent 相依套件，psutil 版本範圍 `>=6,<8`。
- `static/index.html`：單頁介面與設定、探索、詳情對話框。
- `static/app.js`：API 輪詢、畫面狀態、戰情卡片與表單操作，無前端框架或打包流程。
- `static/style.css`：深色響應式介面、桌面四台戰情總覽與全螢幕樣式。
- `Dockerfile`、`compose.yaml`、`.dockerignore`：管理端映像與本機部署設定。
- `data/hosts.json`：主機設定與 Agent Token；屬機密資料，不可提交版本控制或打包進映像。
- `tests/test_server.py`：管理端驗證、狀態判定、HTTP 安全、保存與逾時測試。
- `tests/test_agent.py`：Agent 指標、認證、來源限制及請求處理測試。
- `tests/test_integration.py`：具備 psutil 時執行的真實 Agent 整合測試。
- `tests/browser_smoke.mjs`：使用 Chrome／Edge DevTools Protocol 的瀏覽器操作測試，無 npm 相依套件。
- `README.md`：使用者啟動、部署與 Agent 安裝說明。

管理端需 Python 3.10+；瀏覽器測試需 Node.js 22+ 與 Chrome 或 Edge。管理端不需要 psutil。

## 3. 本機 Docker 維護

在 `E:\netword` 執行：

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
- `./data` 綁定掛載到 `/app/data`。重建映像不會清除設定。
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

- `GET /api/state`：全部主機、最新檢查、Agent 指標、近期歷史與監測間隔。
- `POST /api/hosts`、`PUT /api/hosts/{id}`、`DELETE /api/hosts/{id}`：主機設定維護。
- `POST /api/refresh`：安排背景立即檢查，不保證回應當下已完成量測。
- `POST /api/scan`：提交 `{range, ports}` 探索工作。
- `GET /api/scan`：取得探索進度、結果與錯誤。

主機設定包含 `id`、`name`、`ip`、`checks`、`agent_enabled`、`agent_port`；Token 只供寫入，公開回應僅提供是否已設定，不回傳內容。編輯時省略 `agent_token` 會保留既有值，空字串代表清除，前端不可混淆。

最新狀態位於 `host.last`，包含 `status`、`timestamp`、`latency_ms`、`checks` 與 `agent`。Agent 成功時指標在 `last.agent.metrics`，包含 `hostname`、`os`、`cpu_percent`、`memory`、`disks`、`uptime_seconds`、`services`。

- `up`：有可達證據，且設定的檢查無失敗。
- `degraded`：主機可達，但部分檢查或 Agent／指定服務異常。
- `down`：沒有取得可達證據，不等同證明主機關機。
- 無歷史或尚未檢查時，前端顯示待檢查，不可視為正常。

監測預設每輪完成後等待 30 秒；前端每 5 秒同步 API，兩者不是相同量測頻率。每台保留最近 120 筆狀態在記憶體，重啟後重新累積。主機設定以原子替換保存，保存失敗須回復記憶體，避免畫面與檔案不一致。

探索只接受指定私有 IPv4 與本機回環範圍，單次最多 1024 位址；空連接埠清單表示只使用 Ping。Docker 內的 `127.0.0.1` 指容器自己，監控 Windows 管理主機應使用 `192.168.6.6`。

## 5. 戰情總覽介面

- 預設卡片顯示所有符合篩選的主機：CPU、記憶體、磁碟最高使用率、服務摘要、回應時間、運行時間及最後檢查。
- 服務僅包含使用者設定的 TCP／HTTP 檢查及 Agent `--services`，不是列舉作業系統所有服務。
- 磁碟卡片顯示最高使用率與對應掛載點；完整磁碟清單在主機詳情。
- 無 Agent 資料時顯示缺值，不可當成 0%；服務 `unknown` 與已確認失敗使用不同呈現。
- 全螢幕總覽進入時清除搜尋與狀態篩選，以免漏掉正常主機。四台桌面 2×2 已驗證可在 1366×768 顯示。
- 手機使用單欄；更多主機或大量服務可能需要捲動，不能宣稱任意數量都可單屏呈現。
- 保留原本清單、主機詳情、搜尋與篩選。連線失敗時需顯示資料可能過期的警告，包含全螢幕模式。
- 所有 API 文字使用 DOM `textContent` 或安全屬性，不使用未過濾的 HTML 插入。

## 6. Agent 部署與維護

共通設定：TCP `9105`，使用各主機獨立隨機 Token；`--allow-ip 192.168.6.6` 限制管理端來源。Token 不可寫入開發文件、提交、測試輸出或對話。

### Linux

安裝位置：`/home/landagent/netword-agent`。Token 位於 `agent.env`，權限 600，目錄 700。

使用者 systemd 單元：`~/.config/systemd/user/netword-agent.service`。兩台均已開啟 Linger，以支援未登入時運行。

```bash
systemctl --user status netword-agent.service
systemctl --user restart netword-agent.service
journalctl --user -u netword-agent.service -n 50 --no-pager
loginctl show-user landagent -p Linger
```

`192.168.6.44` 的 UFW 已由使用者允許管理端連入 9105；`192.168.6.23` 部署時 UFW 未啟用，未擅自改為啟用。

`192.168.6.23` 的系統 Python 缺少 ensurepip／pip，使用 `venv --without-pip` 搭配官方 pip wheel 完成隔離環境安裝，未修改系統 Python。日後重建環境應先檢查 venv 與 pip 可用性。

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
- 網路請求必須有總逾時，不可只依賴 socket 閒置逾時，避免慢速回應卡住監測週期。
- 目前沒有異常通知、長期歷史資料庫、內建 TLS 或集中自動更新功能。

## 8. 測試與驗收

```powershell
.venv\Scripts\python -m unittest discover -s tests -q
node --check static/app.js
node tests/browser_smoke.mjs
docker compose config --quiet
```

開發環境尚無 psutil 時，先安裝 `requirements-agent.txt`，才能執行完整真實 Agent 整合測試。瀏覽器測試可用 `CHROME_PATH` 指定執行檔，`PYTHON` 指定 Python；使用臨時資料目錄，不可把測試指向正式設定。

目前已驗證：

- Windows 本機 38 項 Python 測試通過。
- Docker Linux 映像內 16 項管理端測試通過。
- 真實瀏覽器：新增／刪除、探索與勾選加入、安全文字呈現、戰情指標、未知／失敗狀態、篩選、詳情、手機與桌面全螢幕。
- Docker 部署：四台 Agent 均正常、ICMP／TCP 區網可達、單一指定主機探索成功、Host 防護有效。
- Docker 重啟後四台設定與 Token 保持不變，資料目錄可寫，容器健康檢查通過。

未來修改後應重新執行相關測試，並確認沒有把 Token、備份或本機 `.venv` 帶入建置內容。
