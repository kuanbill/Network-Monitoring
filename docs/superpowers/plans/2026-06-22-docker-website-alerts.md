# Docker and Website Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `.23` 和 `.44` 上分別量測 Docker 服務與網站入口，並獨立寄出異常與恢復信。

**Architecture:** 新的固定目標 HTTPS 探測模組不提供任意網址輸入。管理端保留原本的主機可達判定，並將主機、Docker、網站觀測交給同一個寄件工作，依事件類型分別管理狀態。

**Tech Stack:** Python 3.10+ 標準函式庫、`unittest`、原生 JavaScript、Node.js 瀏覽器測試。

**Spec:** [Docker 與網站獨立告警設計](../specs/2026-06-22-docker-website-alerts-design.md)

## Global Constraints

- `192.168.6.23` → `https://urc.landagent.com.tw/`；`192.168.6.44` → `https://ai.tailan.com.tw/`；兩台皆使用既有 Agent 的 `docker.service`。
- 「不列舉容器、不檢查 Docker API、不存取 Docker socket、不新增 Agent 的權限、不自動重啟或透過管理端執行遠端命令。」
- HTTPS：僅 `2xx`、不跳轉、不讀取頁面內容、不送登入資訊、系統信任庫與指定網域驗證；從 DNS 到 HTTP 標頭共用 5 秒期限。
- 僅連驗證過的全球可路由單播 IP；不放寬既有 HTTP 檢查的主機 IP 規則。
- 旗標預設關閉；不新增設定頁面元件。畫面與郵件使用繁體中文，公開 API、匯出和郵件不含機密。
- 沿用既有 Gmail 設定與門檻（預設各 2 次）。開發與驗收不碰正式設定、站點、Agent 或 SMTP；上線另行確認。

## Review Focus

1. 一站解析卡住不應阻擋另一站；逾時後解析成功也不能發出新的連線。（任務 1）
2. 第一個可用的網路路徑回傳 `302` 時，不嘗試將異常洗成另一個位址的 `200`。（任務 1）
3. Agent 未回傳 `docker.service`、回傳 `unknown` 和 Agent 整體失聯，不會全部顯示成「已停止」。（任務 2）
4. 切換 Docker 監控使既有主機事件作廢，但不使已排隊的網站信作廢；更新保存失敗不使任何事件作廢。（任務 3）
5. 既有主機編輯表單省略新欄位與 Token 時保留設定；既有主機詳情能區分無資料、失敗與通過。（任務 4）

---

### Task 1: 固定網站 HTTPS 探測

**Files:** 新增 `website_probe.py`、`tests/test_website_probe.py`；更新 `Dockerfile`、`.dockerignore` 的來源白名單。Windows 打包使用靜態匯入，不另增資料檔。

**Interfaces:** `WEBSITE_DOMAINS: dict[str, str]`（以主機 IP 查兩個固定網域）；`probe_website(host_ip: str) -> dict` 只接受表中 IP，回傳 `ok: bool | None`、`code: str`、`latency_ms: float | None`，僅 `code == 'http'` 另外帶 `http_status: int`。`code` 為 `http`、`dns`、`certificate`、`connection`、`timeout` 或 `busy`。

- [ ] **寫失敗測試：** `test_public_addresses_and_tls_hostname` 使用假解析與傳輸，斷言跳過 `127.0.0.1`、RFC1918、`100.64.0.1`、`fe80::1`、多播、保留及 IPv4-mapped IPv6，依序使用合格的數字位址；TLS 與 HTTP Host 仍是 `urc.landagent.com.tw`，`GET /` 無認證、無代理且只讀標頭。只回受限位址得到 `('dns', False)`，不呼叫傳輸。
- [ ] **寫失敗測試：** `test_http_classification_and_fallback` 驗證 `204 → True`、`302/404/503 → False`，第一份 HTTP 回應即結束，不跟隨 Location 或在收到 `302` 後換位址；連線失敗且仍有時間可改試下一個公開位址。驗證 DNS、憑證、連線及逾時回傳不同代碼，不回傳例外文字。
- [ ] **寫失敗測試：** `test_deadline_and_per_site_busy` 用事件卡住 `.23` 的解析，斷言首次逾時、本輪重入得到 `('busy', None)`、`.44` 仍可立即探測；解除卡住後舊工作不得建立連線。另以受控傳輸驗證緩慢標頭確實中斷，而非僅由測試替身回報逾時。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_website_probe.py -v`，預期先因缺少探測模組或行為失敗。
- [ ] **實作：** 在 `website_probe.py` 設每站一個可佔用工作名額，不排隊；工作持有單調時間絕對期限，DNS 後及各連線階段都重新檢查，呼叫者也等待同一期限。`socket.getaddrinfo` 只解析一次，篩選公開 IPv4/IPv6 的 TCP sockaddr，使用數字端點建立連線；`ssl.create_default_context()` 以固定網域驗證及送 SNI。以剩餘時間及到期關閉傳輸限制 TCP/TLS/標頭，不依賴閒置逾時；安全跨越原始 socket 交給 TLS 後的關閉責任。晚到結果只留在原呼叫的私有結果容器，不更新任何共享量測。Docker 建置上下文與 `COPY` 僅額外納入此模組。
- [ ] **驗證綠燈並提交：** 重跑網站測試，執行 `docker compose config --quiet`；提交 `Add bounded HTTPS checks for fixed sites`。

### Task 2: 管理端設定與狀態

**Files:** 修改 `server.py` 的 `validate_host`、`inspect_host`；擴充 `tests/test_server.py` 的設定、量測及 API 測試。

**Interfaces:** 新增 `docker_monitor_enabled: bool`、`website_monitor_enabled: bool`。`inspect_host(host) -> dict` 保留舊欄位與 `checks`，新增 `host_status`（僅既有主機事件的狀態），各啟用項目於 `last.docker`、`last.website` 提供 `ok`、`code`、`label`、`detail`、`latency_ms`，網站可另帶 `http_status`。Docker `code` 為 systemd 有限狀態集合或 `unknown`、`agent_unavailable`。

- [ ] **寫失敗測試：** `test_fixed_flags_validation_persistence_and_export` 斷言從舊設定補預設 `False`、只接受布林、非對應 IP 拒絕啟用、Agent 關閉時拒絕啟用 Docker、同一請求可一起停用。重新載入、一般編輯省略旗標與 Token、公開 `/api/state` 和設定匯出都驗證，且不回傳 Token；原主機 IP HTTP 仍拒絕網域。
- [ ] **寫失敗測試：** `test_docker_status_and_host_status_are_independent` 以 Agent 替身驗證 `active → True`、`inactive/failed → False`、缺少單元或 `unknown → False`（只說無法確認）、Agent 失聯 `→ None`；若只有 Docker 故障，總覽 `degraded`、`host_status == 'up'`；若 Docker 獨立監控關閉，Agent 回報的 Docker 故障仍屬主機事件。並測 SSH 異常仍屬主機事件。
- [ ] **寫失敗測試：** `test_external_website_does_not_prove_host_reachability` 斷言本機檢查無可達證據且網站 `200` 時，網站 `ok is True`、主機仍 `down`、`reachable is False`；本機正常網站 `503` 時總覽 `degraded`、`host_status == 'up'`；`busy/None` 不使正常主機變成異常。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_server.py -v`，預期新設定及狀態斷言失敗，舊檢查照常。
- [ ] **實作：** `validate_host` 對兩旗標用上一份設定補值，沿用原子保存、公開投影與匯出。`inspect_host` 先依原有證據計算本機狀態，只在獨立旗標啟用時排除精確名稱 `docker.service`；外部探測結果絕不改寫本機可達性或延遲。保留完整 Agent 指標供診斷；獨立結果的繁中描述只取自固定文案與 HTTP 數字狀態。
- [ ] **驗證綠燈並提交：** 重跑管理端測試；提交 `Model Docker and website observations separately`。

### Task 3: 獨立告警、恢復與設定失效

**Files:** 修改 `notifications.py` 的事件、佇列有效性、郵件內容；修改 `server.py` 的 `Monitor.change`；擴充 `tests/test_notifications.py`。

**Interfaces:** 保留 `NotificationManager.observe(host, last)`、`forget(identity, kinds: set[str] | None = None)`（預設全部）。`records` 與版本鍵為 `(host_id, 'host' | 'docker' | 'website')`；未帶 `host_status` 的既有量測繼續用 `status`。只有明確 `ok is False/True` 算異常／成功；`None` 中斷連續計數，不改變已確認事件。

- [ ] **寫失敗測試：** `test_docker_and_website_recover_independently` 依序餵兩輪 Docker 失敗、兩輪兩者皆失敗、兩輪網站恢復、兩輪 Docker 恢復，斷言假 SMTP 主旨依序為 `Docker 服務異常`、`網站異常`、`網站恢復`、`Docker 服務恢復`，沒有泛用主機信。另測第二次故障可重新告警、停用恢復信與停用通知時的行為。
- [ ] **寫失敗測試：** `test_no_data_breaks_threshold_without_false_docker_recovery` 在兩次 Docker 異常或成功間插入 `None`，斷言不誤達門檻；已確認 Docker 事件在 Agent 失聯期間保留。網站故障同時 Agent 失聯時僅主機與網站信；未知 Docker 狀態的郵件不宣稱停止。測郵件只帶固定信任文案及網域，不回顯替身放入的原始錯誤、URL 查詢或 Token。
- [ ] **寫失敗測試：** `test_reconfigure_only_invalidates_affected_events` 驗證 Docker 旗標切換作廢主機與 Docker 待送事件，但保留網站；網站切換只取消網站；改 Agent 連線取消主機與 Docker；改主機 IP 或刪除取消全部；改設定後保存失敗不取消，僅改名稱也不取消。測寄送中作廢後不重試、舊事件不轉成恢復。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_notifications.py -v`，預期獨立事件、無資料及選擇性失效測試失敗。
- [ ] **實作：** 每種類型沿用同一個計數、佇列、限流與寄送流程；更新既有白箱測試改用主機類別鍵，不改其原本斷言。泛用主機信使用 `host_status` 並在 Docker 獨立啟用時排除 `docker.service`，網站結果始終獨立。獨立郵件按結果代碼選固定文案、以對應 IP 取固定網域，寫入量測時間、確認輪數及建議；不輸出例外。`Monitor.change` 僅在保存成功後依差異作廢：IP/刪除為全部、`checks` 為主機、Agent 端點或 Docker 旗標為主機＋Docker、網站旗標只為網站。寄送前及每次重試沿用事件有效性檢查；已開始的 SMTP 嘗試不強制中止。
- [ ] **驗證綠燈並提交：** `python -m unittest discover -s tests -v`；提交 `Notify and recover Docker and website incidents independently`。

### Task 4: 現有畫面呈現、維護說明及驗收

**Files:** 修改 `static/app.js`，視樣式需要更新 `static/style.css`；擴充 `tests/browser_smoke.mjs`；更新 `README.md` 與根目錄 `AGENTS.md` 的資料契約與操作說明。

**Interfaces:** `displayChecks(host)` 結合原本 `host.last.checks` 加 `host.last.docker`／`host.last.website` 結果；以 `ok === true`／`ok === false`／其他分別顯示正常、異常、無資料。表單無新輸入元件。

- [ ] **寫失敗測試：** 瀏覽器現有 API 替身追加兩筆：一筆網站正常／Docker 無資料，一筆網站異常／Docker 正常。斷言卡片及主機詳情顯示三種狀態、Docker 系統單元不重複顯示、SSH 仍存在。在同一替身攔截 `.23` 編輯的 PUT，不轉發到管理端，驗證修改主機名稱時不送出兩個新旗標與 Token；任務 2 的隔離 API 測試驗證收到相同形態的 PUT 後兩旗標與 `agent_token_set` 保留、API 不回傳 Token。
- [ ] **驗證紅燈：** `node tests/browser_smoke.mjs`，預期獨立結果尚未顯示。
- [ ] **實作：** 新結果放入既有卡片與詳情的服務檢查，保留安全 `textContent`、既有 Agent 指標、回應時間與戰情版面。不改 `fetch(path, options)` 呼叫形態及表單的 IP 來源限制。Docker 獨立結果已顯示時，在畫面過濾重複的 `docker.service`；不改 API 的 Agent 原始指標。
- [ ] **撰寫上線與回復：** 說明兩台固定對應、旗標及安全的 API 更新方式（保留 `checks`／Agent 既有欄位，省略 Token），並列出唯讀確認 Docker 單元／HTTPS、保存 Agent 服務原設定、只改 Agent `--services ssh docker.service`、重啟 Agent 後驗證及管理端重建。網站非 `2xx`、非公開 DNS 或非系統級 Docker 則先核對，回復時先關閉旗標再還原 Agent；不示範停掉正式 Docker。說明 Agent 不需程式更新與提權。
- [ ] **驗證綠燈並提交：** 執行 `python -m unittest discover -s tests -q`、`node --check static/app.js`、`node tests/browser_smoke.mjs`、`docker compose config --quiet` 與 `git diff --check`。如 Docker Desktop 可用，可只建置映像及在未發佈連接埠、未掛載正式資料的暫時環境確認模組匯入，不啟動正式 Compose；Windows 環境可用時驗證獨立桌面後端。回報每項實際結果及略過原因。提交 `Show and document Docker and website monitoring`。
