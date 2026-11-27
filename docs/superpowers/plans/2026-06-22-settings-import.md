# Sidebar Settings and Configuration Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在左側「設定」集中管理更新頻率、匯出及可預覽、可確認的全量取代匯入。

**Architecture:** 匯入先嚴格驗證公開 v1 格式，再由 `Monitor` 使用本機設定推算預覽。確認以單次原子寫入保存全部設定；既有未匯入的安裝仍使用原三個檔案。介面延用原生對話框，桌面版維持工作階段路徑隔離。

**Tech Stack:** Python 3.10+ 標準函式庫、原生 HTML/CSS/JavaScript、`unittest`、Node 22+ 瀏覽器測試。

**Spec:** [左側設定與全量匯入設計](../specs/2026-06-22-settings-import-design.md)

## Global Constraints

- `netword-monitor`、`version: 1`；`settings`、`hosts`、`notifications` 必備。結果是取代，不是合併；可匯入空清單。
- 憑證不可匯出、由匯入檔提供、出現在預覽或錯誤中。只按既已核准的 Agent IP＋埠或 SMTP 主機＋埠＋TLS 模式＋登入帳號保留本機憑證。
- 預覽端點本文最多 40 MiB（UTF-8，接受 UTF-8 BOM），一次最多一個解析／預覽，既有端點保持 32 KiB。一次性確認碼最多存活 10 分鐘；真正的設定變更使其過期。
- 匯入必須整體保存，重啟後一致，不對正式 Agent／SMTP 進行驗證或測試寄信，不降低 Host／Origin 或桌面工作階段防護。
- 保持現有 Python／前端架構，繁體中文；不碰正式資料、部署、安裝包與版本號。

## Review Focus

1. SMTP 帳號看似相同但連線目標變更時不得保留密碼；同 IP 換 Agent 埠也一樣。（Task 1）
2. 只改本機 Token 或密碼、無公開欄位變動，已開啟的預覽也必須失效。（Task 2）
3. 持久化失敗後，仍可用有效預覽重試；重啟不出現三個區段混合新舊的情況。（Task 2）
4. v1 下載檔若含 BOM 應能匯入；含重複 JSON 欄位不得被瀏覽器先解析成合法檔。（Tasks 1、3、4）
5. 桌面工作階段的匯入請求不得在停止或重開後誤送給另一個管理端；取消或回應遲到都不替使用者套用。（Tasks 3、4）

### Task 1: 嚴格匯入格式與憑證配對

**Files:** 新增 `config_import.py`、`tests/test_config_import.py`；更新 `Dockerfile`、`.dockerignore` 的來源白名單。

**Interfaces:** `parse_export(raw: bytes) -> dict` 驗證 UTF-8、JSON 與公開欄位結構。`prepare_import(document, current, *, normalize_ip, validate_host, validate_frequency) -> tuple[dict, dict]` 回傳私密候選資料與安全預覽摘要；`current` 與候選資料均以 `hosts`、`settings`、`notifications` 三鍵表示，前兩項的驗證器由 `server.py` 注入，SMTP 使用 `notifications.validate_settings`。摘要欄位：`hosts`（`added`、`updated`、`removed`、`unchanged` 數字和 `removed_hosts` 的 IP／名稱清單）、`settings`（套用後兩個值）、`notifications`（啟用狀態與變更後的 `smtp_host`、`smtp_port`、`security`、`recipients`，以及 `target_changed`、`account_changed`、`recipients_changed`）、`agent_credentials_needed`（IP／名稱與 `docker_was_enabled`）、`smtp_password_needed: bool`。

- [ ] **測試：** `test_accepts_export_and_pre_docker_v1` 用現有匯出與略過較新旗標的 v1 檔，斷言預設值、已知 IP 保留原 ID、未知 IP 不採來源 ID、舊主機與頻率遭取代，空清單合法且在摘要列出移除。
- [ ] **測試：** `test_rejects_ambiguous_or_secret_documents` 檢查 BOM 正常、非 UTF-8、巢狀重複鍵、NaN/Infinity/溢位、孤立代理碼、重複主機 IP、未知版本（含布林值）、`agent_token`／`password` 或執行狀態欄位、無效的 URL／SMTP／Docker＋Agent 組合均拒絕，且錯誤不含輸入的秘密。`test_matches_credentials_to_connections_only` 比對同 IP／埠、同 SMTP 組合成功；更換埠、主機、TLS 模式或帳號不得保留；沒有憑證時暫停相依項目並保留網站，免驗證 TLS SMTP 保持可用。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_config_import.py -v`；預期新模組或其行為尚不存在。
- [ ] **實作：** `parse_export` 使用 UTF-8-sig 嚴格解碼、物件配對鉤子拒絕重複鍵、固定區段和欄位允許清單、拒絕非法 Unicode 和非有限數值；封裝為不含原始內容的區段錯誤。`prepare_import` 先驗證檔案本身旗標與非機密欄位，再按驗證後的連線目標配對；不足的憑證只能從記憶體中的本機設定取得，不使用偽造值通過保存驗證。新增空白憑證的主機以驗證後的停用狀態保存，並在摘要保留補填提示。Docker 白名單加入新模組。
- [ ] **綠燈並提交：** 重跑上述測試，預期全數通過；提交 `Validate public settings imports and match local credentials`。

### Task 2: 整批保存與一致的生命週期

**Files:** 修改 `server.py`（`Monitor`、`Monitor.save`、`update_settings`）、`notifications.py`（持久化與提交分離）；新增 `tests/test_config_transactions.py`。

**Interfaces:** `Monitor.preview_import(document: dict) -> dict` 回傳 Task 1 摘要加 `confirmation_token: str`；`Monitor.apply_import(confirmation_token: str) -> dict` 回傳 `{'ok': True, 'settings': {...}}`；無效或逾期確認碼拋 `ImportConflict(ValueError)`。`Monitor.update_notifications(data: dict) -> dict` 取代 Handler 直接呼叫通知物件。私密 `configuration.json` 使用內部 `version: 1` 及 `hosts`、`settings`、`notifications`；不把公開匯出規則套到私密憑證欄位。

- [ ] **測試：** `test_apply_is_atomic_and_persists_after_restart`：舊檔三區有資料、預覽不寫檔；模擬原子替換失敗，既有磁碟、三個記憶體區段、通知版本皆不變；重試成功，三區重啟一致，舊檔不變。損壞的新檔不回退；啟用單檔後的主機、頻率、通知一般寫入同樣跨重啟保存，失敗不覆寫其他區段。
- [ ] **測試：** `test_preview_revision_expiry_and_single_use`：下一次成功預覽、只改 Token、只改 SMTP 密碼、修改後改回、或滿 10 分鐘皆使舊碼無效；狀態量測與同值保存不使其失效。確認碼使用後不得再套用，雙請求只有一筆成功。
- [ ] **測試：** `test_import_discards_stale_measurement_and_email_job` 阻塞背景量測、建立假 SMTP 待送事件、成功匯入後再釋放量測；舊主機沒有覆寫狀態、舊工作無效、喚醒新一輪，不寄實信。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_config_transactions.py -v`，預期 Monitor 還沒有匯入介面。
- [ ] **實作：** Monitor 持有共有鎖；涉及通知時鎖定順序固定為 Monitor → `NotificationManager.lock`。將通知的驗證、保存與發布分開，保留舊安裝的 `NotificationManager.update` 行為；正常公開 API 一律經 Monitor。成功保存且內容真正變更才遞增修訂號，預覽儲存當時修訂、隨機碼與單調時間到期值。首次套用及後續保存用同目錄 mode 0600 暫存、`fsync` 和 `os.replace`；只有替換成功才更新欄位與通知世代。匯入後採用新主機物件、清空量測、重設通知、使預覽失效並喚醒；保存出錯保留預覽到期前可重試。
- [ ] **綠燈並提交：** 跑交易測試與 `python -m unittest discover -s tests -q`；無新失敗後提交 `Apply configuration imports atomically`。

### Task 3: 本機 API、請求限制與桌面隔離

**Files:** 修改 `server.py` 的 `Handler.mutate`；擴充 `tests/test_server.py`、`tests/test_windows_app.py`。

**Interfaces:** `POST /api/config/import/preview` 接收原始檔案位元組（`Content-Type: application/json`）；`POST /api/config/import/apply` 只接受 `{"confirmation_token":"…"}`，兩者成功 `200`；確認過期或競態 `409`、解析工作忙碌 `429`、預覽超限 `413`、內容驗證 `400`、寫入失敗 `500`。誤用方法不呼叫匯入。

- [ ] **測試：** `test_import_api_validates_raw_file_and_scope`：GET v1 匯出接 POST 預覽與確認、UTF-8 BOM、重複原始鍵拒絕、Body 在上限以上不讀取、普通 API 仍限 32768；以受控事件卡住第一個預覽時第二個回 `429`。錯誤 Origin／Host 拒絕；驗證期間不改狀態。
- [ ] **測試：** `test_desktop_import_is_scoped_to_live_session`：用臨時桌面工作階段預覽、確認；無前綴與舊路徑皆拒絕，且不往其他管理端寫入。只用空主機清單與替身、不碰正式設定。
- [ ] **驗證紅燈：** `python -m unittest discover -s tests -p test_server.py -v` 及 `python -m unittest discover -s tests -p test_windows_app.py -v`；新路徑目前不存在。
- [ ] **實作：** 在 Host／Origin 防護之後辨識匯入路徑，正確限定方法及 Body 大小；以非阻塞的獨立鎖保護大本文的讀取與驗證，無論連線截止或解析例外都釋放。匯入路徑不要沿用會回顯原始例外的分支；通知正常 PUT 改走 Task 2 介面。桌面仍使用既有工作階段掛接，不新增未隔離入口。
- [ ] **綠燈並提交：** 重跑兩套測試，提交 `Expose bounded local preview and confirmation endpoints`。

### Task 4: 左側入口、匯入預覽與維護說明

**Files:** 修改 `static/index.html`、`static/app.js`、`static/style.css`，擴充 `tests/browser_smoke.mjs`；更新 `README.md`、`Agents.md`、`packaging/build_windows.ps1` 的資料檔禁止清單。

**Interfaces:** 新按鈕 `#nav-settings`，保留 `#settings-dialog` 與頻率表單；設定檔區獨立於頻率表單，提供匯出按鈕、檔案選取、預覽提示、確認與取消。既有 API helper 以 `File` 原樣當 Body，並保留唯一 `fetch(path, options)` 字面呼叫供桌面替換。

- [ ] **測試：** Browser smoke 使用臨時管理端從左導覽於桌面及手機開啟設定，原頁尾按鈕已移除、顯示設定正確。選檔先預覽，檢查新增／移除、頻率和憑證提示；用不可信主機名稱確認純文字。取消後 API 狀態不變；再選同檔可預覽；確認後所有區段取代、頻率表單及總覽更新，未儲存的草稿沒有回寫。另測重複欄位檔案由後端拒絕、空清單警告、過期預覽不能按舊碼重試；正常匯出仍不含秘密。
- [ ] **驗證紅燈：** `node tests/browser_smoke.mjs`；預期找不到左側設定或匯入流程。
- [ ] **實作：** 重用現有 modal、頻率儲存與匯出；預覽區以 `textContent` 填安全摘要、提供警告及可讀的移除清單。取消／切換檔案以請求序號忽略遲到結果；處理等待、重複提交、409 和可重試的保存錯誤，更新頻率可使預覽失效。設定入口與確認按鈕支援鍵盤與手機，焦點與狀態提示清楚；不改郵件導覽，保留桌面 `fetch` 工作階段轉換。
- [ ] **文件與打包保護：** 更新操作教學、敏感的 `configuration.json`／完整離線備份、桌面與 Docker 搬移及舊版相容與手改旗標檔案識別，在建置過濾加入 `configuration.json`。確認既有 `data/` Git 忽略與 Docker 來源白名單涵蓋此檔，且不新增正式資料。不修改版本號、不建置或對現有安裝做更新。
- [ ] **最終驗證並提交：** `python -m unittest discover -s tests -q`、`node --check static/app.js`、`node tests/browser_smoke.mjs`、`git diff --check`；若環境支援，另跑 WebView2 smoke 及 `docker compose config --quiet`，記錄通過／略過原因。提交 `Move settings to sidebar and add confirmed import UI`。
