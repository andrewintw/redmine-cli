# redmine-cli

![AI-Assisted](https://img.shields.io/badge/Code-AI--Assisted-blueviolet)

以指令列快速查詢 Redmine：列出指派給自己的 issue、檢視一或多個 issue 的內容／進度／留言、彙總專案未關閉數量，並統計個人已解決的 issue。支援 JSON/CSV 輸出與本地 assignee 快取。

A command-line client for Redmine that lists your assigned issues, shows issue details, progress and comments, summarizes project backlogs, and tracks your resolved work. Includes JSON/CSV output and local assignee caching.

提供七個功能：

- `projects`：列出所有專案，預設以階層表格標示未關閉 issue 數量
- `issues`：列出指派給 `me` 或指定人員的 issue
- `issue`：顯示一或多個 issue 的內容、進度、指派者與留言
- `stats`：統計目前指派給指定人員的 issue，在各專案的 Open / Resolved / Closed 數量
- `assignees`：列出曾出現在 issue 上的 assignee
- `meta`：列出 Tracker、Status 與目前使用者
- `export`：匯出全部 issue 為 JSON/CSV

文件中的專案、主旨、人員與網址範例均為虛構，僅供說明。

## 環境需求

- Python 3.10+
- `requests`

## 設定

1. 複製範例檔並填入你的 Redmine 網址與 API 金鑰：

   ```bash
   cp .env.example .env
   ```

   ```bash
   REDMINE_URL=https://redmine.example.com
   REDMINE_API_KEY=你的金鑰
   REDMINE_INSECURE=false
   ```

2. 限制 `.env` 權限：

   ```bash
   chmod 600 .env
   ```

金鑰也可改用環境變數 `REDMINE_API_KEY` 或 `--api-key-file` 提供。優先序：
命令列參數 > 既有環境變數 > `.env` > 互動式輸入。

若不想建立 `.env`，也可在單次執行時指定：

```bash
python3 redmine_cli.py \
  --url https://redmine.example.com \
  --api-key-file /path/to/api_key
```

## API 金鑰與使用者

- Redmine 的 API key 是**每個使用者各自一組**，權限綁定該帳號的角色。
- 使用某人的 key 執行程式，等同以該使用者的身分與權限操作。
- `--assignee me` 代表金鑰所屬帳號，不是程式作者的帳號。
- 不要共用金鑰；若外洩，應立即到 Redmine 的「我的帳號」頁面重新產生。
- 非 admin 帳號無法列出其他使用者的 key；本工具的 `assignees` 名單是從可見的
  issue 資料整理而來，不需要讀取使用者清單。
- `.env` 已在 `.gitignore` 中，請勿將金鑰提交到版本控制。
- 在沒有終端機的環境（CI、排程、部分 IDE/容器）無法互動輸入金鑰。此時請使用
  `REDMINE_API_KEY` 或 `--api-key-file`；程式會顯示明確錯誤，不會卡住等待輸入。
  需要從標準輸入讀取時可用 `--api-key-file /dev/stdin`。

## 直接執行

```bash
python3 redmine_cli.py
```

不帶參數時，預設列出**指派給自己且未關閉**的 issue（等同
`issues --assignee me --status open`），不會下載全部 issue。結束後會提示可用
`--help` 查看其他指令。

只帶連線選項時，也會套用到預設的 `issues`：

```bash
python3 redmine_cli.py --insecure
python3 redmine_cli.py --insecure --format csv
```

需要完整匯出時請明確執行：

```bash
python3 redmine_cli.py export
```

部分舊式匯出旗標仍可直接使用，會自動視為 `export`：

```bash
python3 redmine_cli.py --dry-run
python3 redmine_cli.py --format both
```

`--project`、`--format json/csv` 等同時支援 `issues` 的選項，未指定子指令時會套用到
`issues`；要匯出整個專案請用 `export --project ...`。

## Help

`--help` 的主要內容就是指令樹，不需要看範例也能知道有哪些指令與常用參數：

```
usage: redmine_cli.py [<指令>] [選項]

指令:
  (不加指令)                          我的未關閉 issue
                                       = issues --assignee me --status open

  issues
  |-- --assignee <me|ID|帳號|姓名>    看別人的 issue
  |-- --status <open|closed|all|名稱>  狀態
  |-- --project <ID|名稱>              只看某專案
  `-- 其他選項見 issues --help

  projects
  |-- --summary                        上層專案摘要
  |-- --project <ID|名稱>              只看某專案
  |-- --hide-empty                     隱藏數量為 0
  `-- 其他選項見 projects --help

  issue <ID> [<ID> ...]                一或多個 issue 內容、進度、留言
  |-- --include <LIST>                 預設 journals；可加 attachments,relations
  |-- --no-comments
  |-- --format <text|json>
  `-- 其他選項見 issue --help

  stats
  |-- --assignee <me|ID|帳號|姓名>    統計對象
  |-- --by-resolution                  依 journal 計算曾解決
  |-- --resolved-status <名稱|ID>
  `-- 其他選項見 stats --help

  assignees                            列出 assignee
  `-- 其他選項見 assignees --help

  meta                                 列出 Tracker / Status
  `-- 其他選項見 meta --help

  export                               匯出全部 issue
  `-- 其他選項見 export --help
```

常用範例（每個指令一個）：

```bash
python3 redmine_cli.py                                   我的未關閉 issue
python3 redmine_cli.py issues --assignee alice_smith      看別人的 issue
python3 redmine_cli.py issue 12345                       單一 issue 內容、進度、留言
python3 redmine_cli.py issue 12345 67890 --format json   多筆 issue JSON
python3 redmine_cli.py projects --summary                上層專案摘要
python3 redmine_cli.py stats --by-resolution             我曾經解決的數量
python3 redmine_cli.py assignees --live                  重建 assignee 名單
python3 redmine_cli.py meta                              列出 Tracker / Status
python3 redmine_cli.py export --format both              匯出全部 issue
```

```bash
python3 redmine_cli.py --help           # 指令樹與常用範例
python3 redmine_cli.py issues --help    # 該指令的所有選項與預設值
```

符號意義：

- `<...>`：必要參數
- `[...]`：可選參數，不加就是預設行為
- `A | B`：擇一

每個子指令的 help 會把選項分成查詢條件、輸出、連線與驗證，並標示預設值與
使用時機。例如 `--status` 預設 `open`，不加只列未關閉 issue；要包含已關閉才加
`--status all`。

## 列出專案

```bash
python3 redmine_cli.py projects
```

預設為完整階層表格，數字代表**未關閉**（Redmine `status_id=open`）的 issue 數：

```
Project          Open  Subtree
---------------  ----  -------
Project A           3       12
  Project A-1       4        4
  Project A-2       5        5
Project B           8        8
```

`Open` 是直接掛在該專案的數量；`Subtree` 是包含所有子專案的總數。
以縮排表示階層，不再使用 `├─` / `└─` 分支符號。

上層摘要（只列根專案，依 `Subtree` 由多到少排序）：

```bash
python3 redmine_cli.py projects --summary
```

```
Group            Open  Subtree
---------------  ----  -------
Project A          12       12
Project B           8        8
```

其他用法：

```bash
python3 redmine_cli.py projects --project "Project A" --summary
python3 redmine_cli.py projects --hide-empty       # 隱藏未關閉 issue 為 0 的專案
python3 redmine_cli.py projects --summary --hide-empty
python3 redmine_cli.py projects --format detail    # ID / 名稱 / identifier / parent
python3 redmine_cli.py projects --format tree      # 舊版樹狀輸出
python3 redmine_cli.py projects --flat             # 緊湊 [名稱] 排列
python3 redmine_cli.py projects --format json      # JSON
python3 redmine_cli.py projects --format csv       # CSV
python3 redmine_cli.py projects --no-count         # 不統計數量，速度快
python3 redmine_cli.py projects --format json --output projects.json
```

`--project` 只顯示指定專案及其子專案；搭配 `--summary` 可直接看該專案的未關閉數量。

`--summary` 需要計數，不能與 `--no-count` 併用。`--summary`、`--format`、`--flat`
三者互斥。

## 列出指派 issue

```bash
python3 redmine_cli.py issues
```

預設是 `--assignee me --status open`，依 `updated_on` 由新到舊排序，輸出：

```
ID   Project    Tracker  Status  Subject                URL
---  ---------  -------  ------  ---------------------  ---------------------------------
123  Project A  Bug      New     Example subject text   https://redmine.example.com/issues/123
456  Project B  Feature  New     Another example        https://redmine.example.com/issues/456
```

指定人員：

```bash
python3 redmine_cli.py issues --assignee 123
python3 redmine_cli.py issues --assignee jane_doe
python3 redmine_cli.py issues --assignee "Jane Doe"
```

`assignee` 是 Redmine 的 Assignee，也就是 `assigned_to` / 指派對象。

`--assignee` 接受 `me`、數字 ID、登入帳號或顯示名稱。帳號比對支援
`jane_doe`、`alice_smith` 這種小寫底線形式；若 API 未提供其他使用者的
login（非 admin 常見），會以顯示名稱推導（小寫、空白轉底線）。
解析優先使用 `output/assignees.json` 小快取，其次才是 `output/issues.json`；
若同時指定 `--project`，會用該專案的直接成員名單解析，不需要先建立快取。
兩者都不存在時不會自動下載全部 issue，而是提示先執行 `assignees --live`
或 `export`，或直接改用數字 ID。

篩選與輸出：

```bash
python3 redmine_cli.py issues --status all
python3 redmine_cli.py issues --status closed
python3 redmine_cli.py issues --status "New,In Progress"
python3 redmine_cli.py issues --tracker Bug --status "New,In Progress"
python3 redmine_cli.py issues --tracker "Feature"
python3 redmine_cli.py issues --project "Project A"
python3 redmine_cli.py issues --limit 20
python3 redmine_cli.py issues --sort id:asc
python3 redmine_cli.py issues --format json
python3 redmine_cli.py issues --format csv --output my_issues.csv
```

`--status` 可用 `open`、`closed`、`all`，或 Status 名稱/ID。
`--tracker` 可用 Tracker 名稱/ID，多個值以逗號分隔。
名稱會自動轉成 Redmine API 需要的 ID，打到不存在的名稱會顯示可用選項。

`--format json` 的每筆記錄包含 `id`、`project`、`project_id`、`tracker`、
`tracker_id`、`status`、`status_id`、`priority`、`subject`、`author`、
`assigned_to`、`done_ratio`、`created_on`、`updated_on` 與 `url`。

## 查看 issue（單筆或多筆）

```bash
python3 redmine_cli.py issue 12345
python3 redmine_cli.py issue 12345 67890
```

輸出內容包含欄位、進度、指派者與留言：

```
ID: 12345
URL: https://redmine.example.com/issues/12345
Project: Project A
Tracker: Bug
Status: In Progress
Priority: Normal
Assignee: USER_NAME
Author: USER_NAME
Progress: 40%
Created: 2026-01-01T00:00:00Z
Updated: 2026-01-02T00:00:00Z

Subject: Example issue subject

Description:
Example description text.

Comments (1):
  [2026-01-02T00:00:00Z] USER_NAME
    Example comment text
```

```bash
python3 redmine_cli.py issue 12345 --no-comments
python3 redmine_cli.py issue 12345 --format json
python3 redmine_cli.py issue 12345 --format json --output issue.json
```

一次查詢多筆時，JSON 輸出為陣列；單筆仍維持物件。重複的 ID 會自動去重；
若部分 ID 不存在，錯誤會印到 stderr，其他成功項目仍會輸出，程式最後以非 0
結束碼結束。

```bash
python3 redmine_cli.py issue 12345 67890 --format json
```

`--include` 可指定要一併取得的關聯資料，逗號分隔，預設 `journals`：

```bash
python3 redmine_cli.py issue 12345 --include journals,attachments,relations
```

可用值：`journals`、`attachments`、`relations`、`children`、`changesets`、
`watchers`、`allowed_statuses`。`--no-comments` 會從 include 移除 `journals`，
即使同時指定 `--include journals` 也一樣。

當 include 指定 `attachments` 或 `relations` 時，text 模式會多出對應區塊，
即使為空也會顯示 `(none)`。其餘 include 值主要影響 JSON 輸出。`Relations`
區塊會顯示關聯的另一個 issue；方向性關聯會以目前 issue 的視角呈現，例如
`blocks` / `blocked`。

`attachments` 只會取得附件的中繼資料（檔名、大小、`content_url`），不會下載
附件檔案；描述中的 Markdown 圖片（例如 `![](image.jpg)`）也不會下載或解析。
若需要實際檔案，請自行使用 `content_url`。

## 各專案統計

### 目前指派狀態

```bash
python3 redmine_cli.py stats --assignee me
```

統計**目前指派給指定人員**的 issue 在各專案的 `Open`、`Resolved`、`Closed`、`Total` 數量：

```
Project    Open  Resolved  Closed  Total
---------  ----  --------  ------  -----
Project A     2         5       1      8
Project B     0         3       0      3
```

這是快速統計，依 issue **目前的 assignee** 計算。若 issue 解決後被轉給其他人，
或目前指派給你但其實是別人解決的，數字就會有偏差。

### 曾解決的 issue（依 journal）

```bash
python3 redmine_cli.py stats --assignee me --by-resolution
```

`--by-resolution` 會檢查該人員更新過的 issue 的 journals，找出「該人員曾將
status 改成 Resolved」的 issue，不受目前 assignee 影響：

```
Project    Resolved
---------  --------
Project A         5
Project B         3
```

- 預設把 Status 名稱 `Resolved` 視為已解決，可用
  `--resolved-status "Status Name"` 改成其他 Status。
- 要同時計入 Closed，可加 `--resolved-status "Resolved,Closed"`。
- 這個模式需要逐筆讀取 journals，issue 越多越慢。
- 可加 `--project "Project A"` 只統計單一專案。
- 支援 `--format json`、`--format csv`、`--output`。

## 列出 assignee

```bash
python3 redmine_cli.py assignees
python3 redmine_cli.py assignees --sort open
python3 redmine_cli.py assignees --format json
python3 redmine_cli.py assignees --live
```

預設讀取 `output/issues.json`；若匯出檔不存在，則讀取 `output/assignees.json`
小快取，不會自動下載全部 issue。`--live` 會忽略快取，直接從 API 重新建立名單
（會下載全部 issue）並更新快取。

`output/assignees.json` 只存姓名、ID、帳號別名與 `Total / Open` 統計，供
`--assignee "帳號/姓名"` 解析及 `assignees` 顯示使用，不會儲存 issue 內容。
輸出包含 `ID`、`Name`、`Account`、`Total`、`Open`；`Account` 可直接用在
`--assignee`。名單有變動時再執行一次 `assignees --live` 即可。

## 列出 Tracker / Status

```bash
python3 redmine_cli.py meta
python3 redmine_cli.py meta --format json
```

Tracker 與 Status 的數量、名稱依 Redmine 站台設定而異。本工具不硬編碼，
每次從 API 取得，因此新增或自訂的 Tracker/Status 也能直接使用。

## 匯出全部 issue

```bash
python3 redmine_cli.py export --dry-run
python3 redmine_cli.py export --format both
python3 redmine_cli.py export --format json
python3 redmine_cli.py export --project my-project
python3 redmine_cli.py export --output-dir /tmp/redmine-export
```

`--project` 可用專案名稱、identifier 或數字 ID，例如 `"Project A"`、
`project-a` 或 `101`。

預設輸出到 `output/`：

- `output/issues.json`：Redmine API 回傳的完整原始資料
- `output/issues.csv`：整理後欄位，UTF-8 BOM 編碼，可直接用 Excel 開啟
- `output/assignees.json`：姓名、ID、帳號別名與統計數量的小快取，供帳號/姓名解析使用

## 匯出參數

| 參數 | 說明 |
| --- | --- |
| `--url` | Redmine 網址，預設讀取 `REDMINE_URL` |
| `--project` | 只匯出指定專案名稱、identifier 或 id |
| `--output-dir` | 輸出目錄，預設為程式所在目錄的 `output` |
| `--format` | `json`、`csv`、`both`（預設） |
| `--api-key-file` | 從檔案讀取 API 金鑰 |
| `--env-file` | 指定 `.env` 路徑 |
| `--insecure` | 停用 TLS 憑證驗證 |
| `--ca-cert` | CA bundle 路徑，用於驗證 TLS 憑證 |
| `--dry-run` | 只查詢總筆數，不匯出 |
| `--page-size` | 每頁筆數，預設 100（上限 100） |
| `--csv-description-limit` | CSV 的 description 最大字元數，預設 32000；`0` 表示不截斷 |
| `--timeout` | 單次請求逾時秒數，預設 30 |
| `--max-retries` | 失敗重試次數，預設 4 |

## CSV 欄位

`id`、`project_id`、`project_name`、`tracker`、`status`、`priority`、`subject`、
`description`、`description_truncated`、`author`、`assigned_to`、`category`、
`fixed_version`、`parent_id`、`start_date`、`due_date`、`created_on`、`updated_on`、
`closed_on`、`estimated_hours`、`spent_hours`、`done_ratio`、`is_private`、`custom_fields`。

CSV 的 `description` 預設截斷至 32000 字元（`description_truncated` 會標示），以符合
Excel 儲存格上限；完整內容保留在 `issues.json`。需要完整描述可加
`--csv-description-limit 0`。

## 測試

```bash
python3 -B -m unittest discover -s tests -v
```

`-B` 可避免產生 `__pycache__/`。測試使用標準庫 `unittest`，不需要額外套件，
也不會連線到 Redmine，且不會建立 `output/`。

若想先清掉先前的 Python 快取：

```bash
rm -rf __pycache__ tests/__pycache__
```

`output/` 只會由 `issues`、`stats`、`assignees`、`export` 等需要快取或匯出的指令
建立；`issue` 指令不會建立它。`output/` 可能包含實際 issue 匯出資料，需要清除時
請先確認內容：

```bash
rm -rf output
```

## TLS 注意事項

若 Redmine 站台未送出完整的中繼憑證，或系統缺少對應的 CA，憑證驗證會失敗。
程式遇到憑證錯誤時會立即停止，不會反覆重試。

建議優先提供 CA bundle：

```bash
python3 redmine_cli.py --ca-cert /path/to/ca.pem
```

或設定環境變數：

```bash
export REQUESTS_CA_BUNDLE=/path/to/ca.pem
```

若沒有 CA bundle，可設定 `REDMINE_INSECURE=true` 或加上 `--insecure`。這會停用
憑證驗證，代表無法防禦中間人攻擊，僅建議在受信任的內網使用。

## 敏感資料與發布

- `.env` 內含 API 金鑰，已被 `.gitignore` 忽略。
- `output/` 內含實際 issue 資料，已被 `.gitignore` 忽略；發布前請確認不要一併打包。
- 編輯器暫存檔（例如 `*.swp`）也可能包含敏感內容，已被 `.gitignore` 忽略。
- 若要以目錄壓縮檔發布，請手動排除 `.env`、`output/`、`__pycache__/` 與暫存檔。

## 疑難排解

- **401 未授權**：金鑰錯誤、過期，或帳號被停用。
- **403 權限不足**：該帳號角色無權讀取部分專案；使用者清單 API 通常需要 admin。
- **404 找不到 API**：網址錯誤，或 Redmine REST API 未啟用。
- **只抓到部分 issue**：確認帳號權限；工具會依 `total_count` 自動翻頁。
- **找不到 assignee 帳號/姓名**：先執行 `assignees --live` 建立
  `output/assignees.json`，或改用數字 ID。帳號可用 `jane_doe` 這種小寫底線形式；
  程式不會再自動下載全部 issue 來解析帳號或姓名。
- **未設定 Redmine 網址**：建立 `.env`、設定 `REDMINE_URL`，或加上 `--url`。
- **無法互動輸入 API 金鑰**：目前環境沒有終端機；請用 `.env`、`REDMINE_API_KEY`
  或 `--api-key-file`。
- **憑證錯誤**：設定 `REQUESTS_CA_BUNDLE`、`REDMINE_INSECURE=true`，或加上 `--insecure`。
