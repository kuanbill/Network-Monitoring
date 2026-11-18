# Netword 區網主機監控

Windows／Linux 混合環境的輕量網頁工具。管理端只需 Python 3.10+，無需 Node.js、資料庫或 Python 第三方套件。

## 啟動管理端

在本專案目錄執行：

```powershell
python server.py
```

開啟 **http://127.0.0.1:8080**（亦可用 localhost）。保持終端機開啟，Ctrl+C 停止。

```powershell
python server.py --port 8088 --interval 30 --data-dir ./data
```

`--interval` 為每輪檢查完成後的等待秒數，允許 5–3600 秒；主機多或逾時時，實際更新間隔會更長。

## Docker 部署（本機 Docker Desktop）

在 `E:\netword` 執行，先停止原本佔用 8080 的 `python server.py`，避免兩個管理程序共用設定檔：

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
7. 點主機名稱查看完整磁碟清單、檢查結果及最近 120 次狀態；「切換清單」可回到原本表格。服務僅包含已設定的 TCP／HTTP 檢查與 Agent `--services` 項目，並非列出作業系統的所有服務。

### 狀態判定

- **正常 up**：ICMP、指定服務或代理程式至少一項有回應，且所有已設定檢查成功。
- **異常 degraded**：主機有回應，但某個 TCP／HTTP／代理程式檢查失敗，或已指定的作業系統服務不是 running／active。
- **無回應 down**：所有探測都未成功；不代表一定關機，防火牆也可能阻擋探測。
- 尚未檢查的主機不顯示既有結果。

TCP 成功只表示可建立連線，不保證資料庫等應用程式功能正常；HTTP 200–399 視為成功，不自動跟隨重新導向。Ping 延遲是探測程序耗時的近似值，不是精確的 ICMP RTT；ICMP 不通時可能顯示成功 TCP／HTTP 檢查耗時。

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

### 防火牆與安全

- **只在有授權的私有區網掃描**；限制 RFC1918 或本機 IPv4、每次最多 1024 個位址、16 個 TCP 連接埠；最多監控 256 台主機。
- 管理介面無使用者登入，刻意只綁定 `127.0.0.1` 並驗證 Host／Origin；不直接提供區網遠端管理。遠端管理請使用 SSH tunnel 等安全方式，不要直接對外轉發管理連接埠。
- 代理程式使用 Bearer token，至少 24 個字元。**目前代理程式使用 HTTP，token 與指標在網路上不加密**。僅限可信隔離管理網路／VPN；不適用不可信網段，不要暴露至網際網路。
- 主機防火牆只開放 **管理端 IP → 代理程式 TCP 9105**，不要對所有來源開放；只為需要監控的服務開放必要規則。
- `data/hosts.json` 包含明文代理程式 token，請保護檔案與備份權限（Windows 請設定 ACL）。API 不會回傳 token；編輯時未填 token 會保留原值。
- 本版不含通知、登入權限、TLS 終止、長期歷史儲存、排程安裝／開機自啟。正式環境應再補足維運需求；不要將它視為 Zabbix 的完整替代。

## 資料與限制

- 主機設定儲存在 `data/hosts.json`，重啟後保留。
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
