# 開發與維運筆記

這份文件放的是 README 不需要、但維護時很有用的細節：
為什麼選 Qt、效能的實測數據、打包流程、疑難排解，以及開發過程中修掉的問題。

一般使用請看 [README](../README.md)。

---


把 Blackboard **Ultra** 課程的 class content（課程大綱整棵樹）和 announcements 完整抓到本機，
轉成可讀的 Markdown + 原始 JSON，附件、內嵌圖片一起下載。

提供**獨立桌面應用程式**（像檔案總管一樣瀏覽與勾選下載）與完整 CLI。

針對你的站台預設好了：`https://twc.blackboard.com`，課程 `_12529_1`
（`.../ultra/courses/_12529_1/outline`）。

---

## 0. 直接下載執行檔（不需要 Python）

到 [Releases](../../releases) 下載 `bbpull-*-windows-x64.zip`，解壓後執行：

| 檔案 | 用途 |
| --- | --- |
| `bbpull-gui.exe` | 雙擊直接開啟視窗（不會跳出黑色命令視窗） |
| `bbpull.exe` | 命令列版本，所有子命令都能用，也可以開視窗 |

第一次開啟會問站台／帳號／密碼。密碼預設用 **Windows DPAPI 加密**儲存，
綁定你的 Windows 帳號。

> 打包版本把帳號資料存在 `%LOCALAPPDATA%\bbpull\`（**不是**解壓縮的資料夾），
> 所以換版本或搬動資料夾都不會遺失登入狀態。
>
> 想確認執行檔完整可用：`bbpull.exe selftest`（會檢查相依套件、GUI 引擎、
> Qt 平台外掛、目錄可寫性）。

---

## 1. 桌面應用程式（推薦）

```powershell
python -m bbpull gui
```

這是一個**真正的 Windows 桌面視窗**，不是瀏覽器、不需要開網頁、不會啟動任何伺服器。
關掉視窗程式就結束。

**介面有兩套，預設用 Qt：**

| | 引擎 | 何時使用 |
| --- | --- | --- |
| **Qt（預設）** | PySide6 | 需要 `pip install PySide6-Essentials`（~73 MB） |
| Tk（降級） | 內建 tkinter | 沒裝 PySide6 時使用，或 `bbpull gui --tk` 強制 |

### 用專案虛擬環境（建議，一行搞定）

這台機器上有多個 Python（anaconda base、3.13、3.12、3.11、3.10）。
PySide6 是裝在**某一個 Python** 裡，不是整台機器——所以「用哪個 python」會直接
決定你開到 Qt 還是 Tk。**建立專案專屬的 `.venv` 就能把這件事固定下來**：

```powershell
python -m bbpull venv --create
```

它會建立 `.venv/` 並安裝 `requests`、`PySide6-Essentials`、`customtkinter`
（首次約下載 80 MB）。之後：

```powershell
bbpull.cmd                 # 會自動選用 .venv
.\.venv\Scripts\python -m bbpull gui
```

**而且不管你用哪個 Python 呼叫，都會自動切換到 `.venv`。**
`python -m bbpull ...` 會偵測到專案環境並重新以它執行，所以 anaconda base、
3.13、3.12 啟動的結果完全一致。想刻意用目前的直譯器：

```powershell
$env:BBPULL_NO_REEXEC = "1"; python -m bbpull gui
```

查看環境狀態：

```powershell
python -m bbpull venv
```

```
虛擬環境檢查
  專案位置     : Z:\PROGRAMMING\雜項\bb-ultra-pull
  環境目錄     : Z:\PROGRAMMING\雜項\bb-ultra-pull\.venv
  狀態         : 已建立（Python 3.13.1）
  目前執行中   : 就是這個環境

  套件：
    requests       2.34.2
    PySide6        6.11.2
    customtkinter  6.0.0
    tkinter        ok
```

### 不想用虛擬環境時：確認引擎

```powershell
python -m bbpull gui --check          # 看目前用哪個引擎、為什麼
python -m bbpull gui --qt             # 強制 Qt；裝不起來就報錯，不靜默降級
python -m bbpull gui --tk             # 強制 Tk
```

`--check` 會列出這台機器上每個 Python 的狀況：

```
GUI 引擎檢查
  執行中的 Python : C:\Python313\python.exe
  PySide6        : 6.11.2
  預設會使用 : Qt（PySide6）
  原因       : PySide6 6.11.2 is available in this interpreter

  這台機器上的 Python：
    C:\Python313\python.exe  [PySide6 6.11.2] ←使用中
    C:\Python312\python.exe  [沒有 PySide6]

  這些 Python 已經有 PySide6，可以直接用它們啟動：
    C:\Python313\python.exe -m bbpull gui
```

**視窗標題一定會標明版本**：`bbpull — Blackboard 課程下載器（Qt）` 或 `（Tk）`。
這是刻意的——先前有整整一輪除錯在描述錯的介面，因為兩者外觀不易分辨。

**降級絕不靜默**：走到 Tk 時，主控台會印出警告與安裝指令，
視窗狀態列也會顯示「此為降級的 Tk 介面」。

### 為什麼預設改成 Qt

原本用 Tk，清單是「每一列一個 widget」——而 **Tk 的每個 widget 就是一個作業系統視窗**。
實測（同一份清單、兩種渲染策略）：

| 列數 | Tk（每列一 widget） | Qt（model/view） | 差距 |
| --- | --- | --- | --- |
| 120 | 429 ms | **0.4 ms** | 1046× |
| 1,000 | 2,235 ms | **1.7 ms** | 1284× |
| 5,000 | ~14,105 ms | **9.1 ms** | 1546× |

原因不是微優化不足，而是**架構錯誤**：Tk 的成本隨列數**線性成長**，Qt 的
`QTreeView` + model + delegate 只繪製**看得見的列**，所以成本是**平的**。
91 張課程卡在 Tk 版產生 594 個 widget（整個視窗 829 個），側欄捲動要 2.9 ms/步；
Qt 版是零 widget、純繪製。

**引擎完全沒有改動**：下載、登入、快取、選擇性下載、學年學期篩選邏輯全部共用，
只有畫面層換掉（呈現層 3,002 行改寫，引擎 4,653 行與 4,000+ 個既有測試原封不動）。

```
┌────────────────────────────────────────────────────────────────────────────┐
│ bb  bbpull   23002220 · NUR2051 Nursing Practicum I · 134 項目 · 3 公告     │
├──────────────┬─────────────────────────────────────────────────────────────┤
│ 我的課程      │  ← → ↑   課程根目錄 › Week 1 › Day 1        [篩選…] [本資料夾▾] │
│ [搜尋課程…]   ├─────────────────────────────────────────────────────────────┤
│              │  23 個項目   ·   已選 5 項（約 7 個檔案）                    │
│ ▸ Clinical…  │  ☑ 📁 Week 1            資料夾 · 12 個項目   [下載全部][開啟] │
│ ▸ Library    │  ☑ 📄 Overview          文件                 [下載]          │
│ ▸ CAPLE      │  ☐ 📎 Slides.pdf        檔案                 [下載]          │
│ ▸ NUR2051 ◀  │  ☐ ❓ Quiz              測驗／作業                            │
│              │  ☐ 🔗 Zoom Classroom    互動工具                              │
├──────────────┴─────────────────────────────────────────────────────────────┤
│ 已選 5 個項目   約 7 個檔案 · 含 4 個資料夾 · 來自 2 個位置                  │
│                              [檢視選取] [清除] [下載選取項目]               │
└────────────────────────────────────────────────────────────────────────────┘
```

### 檔案總管式導覽

| 操作 | 方式 |
| --- | --- |
| 進入資料夾 | 雙擊該列，或按「開啟」 |
| 上一頁／下一頁 | 工具列 ← → ，或 `Alt+←` / `Alt+→` |
| 上一層 | 工具列 ↑ ，或 `Alt+↑` |
| 跳到上層任一層 | 點擊路徑列（breadcrumb）上的任一段 |
| 篩選目前資料夾 | 右上「篩選…」輸入關鍵字 |
| 搜尋整門課 | 把範圍切成「整門課」，再輸入關鍵字（會顯示每項的所在路徑） |
| 重新讀取課程結構 | `F5` 或工具列重新整理 |
| 全選目前可見項目 | 工具列 ✓ ，或 `Ctrl+A` |
| 下載選取項目 | 托盤「下載選取項目」，或 `Ctrl+D` |
| 切換亮／暗色 | 右上月亮／太陽圖示 |

### 課程分類與篩選

左側課程會**依學年分組**（最新在最上），每張課程卡片顯示學年／學期標籤、課名與編號，
卡片之間有底色與邊框分隔，不再是擠在一起的文字清單。

上方兩個下拉選單可依**學年**與**學期**篩選：

```
┌──────────────────┬──────────────────┐
│ 全部學年      ▾  │ 全部學期      ▾  │
└──────────────────┴──────────────────┘
```

- 學年選項由實際課程資料產生（例如 `2026/27`、`2025/26`、`2024/25`），最新在前。
- 名稱沒有標示學年的課程（例如 `Library`）或無法取得名稱的課程，會歸到
  **未標示學年**，不會被漏掉或誤分類。
- 學期選項會**跟著選定的學年變動**；若原本選的學期在新學年不存在，會自動回到「全部學期」，
  不會變成一片空白。
- 搜尋框同時比對課名與課程編號，輸入時有 **debounce**，不會每打一個字就重建整個清單。

> 學期標示為「第 1 / 2 / 3 學期」而非「上／下／暑期」，因為各校對應方式不同；
> 標一個可能錯的名稱比中性的編號更糟。

### 啟動時的空白提示頁

開啟程式、還沒選課時，右側會顯示一個置中的提示頁（不是空白畫面）：

```
                        ┌────────┐
                        │  資料夾 │
                        └────────┘
                        尚未選擇課程
              從左邊挑一門課開始瀏覽教材內容。

              ● 勾選資料夾會連同底下所有內容一起下載
              ● 可以在不同資料夾之間累積選取，再一次下載
              ● Alt+← 返回上一頁，Alt+↑ 回上一層
              ● 用左上的學年／學期下拉選單快速縮小課程範圍
```

未登入時顯示登入提示；讀取失敗時顯示失敗提示與重試建議。

### 流暢度

Qt 版把清單改成 model/view 之後，列數不再影響成本：

- **捲動**：只有可見列會被繪製，5,000 列的捲動仍是毫秒級。
- **勾選**：只重繪受影響的矩形（`dataChanged`），不是重建清單。
- **動效**：`QPropertyAnimation` 走 Qt 的動畫框架（硬體加速），托盤與進度面板
  都是原生補間。
- **主題切換**：一次 `setStyleSheet` 套用整份 QSS，不重建元件樹。

Tk 版（`--tk`）仍保留上述的增量更新與交錯淡入，在中小型清單下可用。

### 跨頁面批量選取與單個選取

這是重點功能，兩種粒度都支援：

- **單個選取**：勾選任一項；每一列右側也有「下載」，只抓那一項，不必先勾選。
- **整個資料夾**：勾選資料夾＝連同底下全部內容；列上會標示「N 個項目」讓你預期份量。
- **跨頁面批量選取**：勾選後切換到別的資料夾、上層或其他路徑繼續勾選，
  選取狀態會累積保留（切換課程才重置）。底部托盤會即時顯示
  「已選 N 個項目 · 約 M 個檔案 · 含 K 個資料夾 · 來自 P 個位置」。
- **檢視選取**：按「檢視選取」可以看到跨資料夾的完整清單（依所在路徑分組），
  可逐一移除，或直接下載。
- **正確的父子關係**：勾了資料夾後再取消其中一個子項目，程式會自動把資料夾
  換成「其餘子項目」，也就是檔案總管的行為，不會出現重複下載或漏抓。
- 選取狀態會存在 `localStorage`，關掉視窗再開仍在。

### 下載與進度

- 下載在**背景執行緒**進行，介面不會凍結，可繼續瀏覽與勾選。
- 右側「下載進度」面板顯示每個工作的進度條、檔案數、位元組數、目前處理中的項目。
- **可取消**：進行中的工作按「取消」會在下一個項目邊界停止，已下載的檔案保留。
- 完成後按「開啟資料夾」直接開啟輸出目錄。
- 輸出結構與 CLI 完全相同，兩種方式可以混用而不會產生兩套樹狀結構。

### 帳號與密碼

第一次開啟若還沒有憑證，會彈出連線視窗（站台／帳號／密碼／保存方式）。
密碼預設用 **Windows DPAPI 加密**存放，綁定你的 Windows 帳號。
已經設定過的話，開啟即自動登入並載入課程，不必輸入任何東西。

---

## 2. 互動式檢查模式（回報介面問題用）

覺得某個地方「看起來怪」但講不清楚時，用這個模式直接點給程式看：

```powershell
python -m bbpull gui --inspect
```

視窗右下角會出現一個紫色小面板。**之後你點的任何位置都會被記錄下來**，包含：

| 記錄內容 | 說明 |
| --- | --- |
| 點到的元件 | 類別、`objectName`、尺寸、文字、是否啟用 |
| 完整樣式狀態 | 字體（家族／像素大小／粗體）、調色盤各色、該元件的 inline stylesheet |
| 動態屬性 | 例如 chip 的 `state="ok"/"dim"/"off"` |
| 祖先鏈 | 往上 8 層的元件，看得出它被誰包住 |
| 應用程式狀態 | 是否已載入課程、是否載入中、主題、選取數、狀態列文字 |
| **放大截圖** | 該位置 4× 與 2× 的裁切圖，並附「點擊點在裁切圖中的座標」 |

也可以按面板上的「**框選區域**」拖曳一個範圍，會存成 3× 放大圖。
全部寫到 `_debug/`：

```
_debug/
├── clicks.jsonl          # 每次點擊一筆 JSON
└── shots/
    ├── click-0001-4x.png # 該點的 4 倍放大圖
    ├── click-0001-2x.png # 該點的 2 倍放大圖（含周邊脈絡）
    └── region-0002.png   # 框選的區域
```

**為什麼要做這個**：先前用截圖來回推測介面問題，兩張裁切圖尺寸與位移不同，
讓我追了一個「量出來是 0 像素差異」的假問題；更糟的是有一輪我一直在看
**另一個版本的介面**（Tk 而非 Qt），因為兩者視窗外觀不好區分。
讓程式直接記錄「你點的是哪個元件、它是什麼樣式」比看圖猜測可靠得多。

現在視窗標題會標明版本：`bbpull — Blackboard 課程下載器（Qt）` 或 `（Tk）`。

讀取記錄的摘要工具：

```powershell
python _read_inspect.py --list      # 列出所有記錄
python _read_inspect.py --id 3      # 看第 3 筆的完整內容
python _read_inspect.py --last 5    # 看最後 5 筆
```

> `_read_inspect.py` 是本 repo 唯一刻意保留的診斷工具；其他開發時期的臨時腳本都已刪除。

---

## 3. 它到底抓了什麼

| 來源 API（Learn REST v1） | 抓下來的東西 |
| --- | --- |
| `GET /courses/{id}/contents` | 課程大綱根層項目 |
| `GET /courses/{id}/contents/{cid}/children` | 遞迴展開每一層資料夾 / 學習模組 |
| `GET /courses/{id}/contents/{cid}/attachments` | 檔案型項目的真正檔名 + 下載點 |
| `GET .../attachments/{aid}/download` | 附件位元組 |
| BBML `body` 內的 `bbcswebdav` 連結 | Ultra 文件內嵌的檔案與圖片 |
| `GET /courses/{id}/announcements` | 課程公告（含分頁全部抓完） |

每個項目同時輸出：

- `<名稱>.md` — 標題、metadata、內文轉 Markdown，**內嵌檔案連結會改寫成本機相對路徑**
- `<名稱>.json` — 原始 API 回應（完整保存，日後要重新處理不必再連線）
- 附件實體檔

---

## 4. 安裝（唯一需要動手的一步）

需要 Python 3.9+（已在 Python 3.13 驗證）。

**建議用專案虛擬環境**，一行就好：

```powershell
cd Z:\PROGRAMMING\雜項\bb-ultra-pull
python -m bbpull venv --create
```

這會建立 `.venv/` 並安裝 `requests`、`PySide6-Essentials`（Qt 介面）、
`customtkinter`（Tk 降級介面）。首次約下載 80 MB。

> **為什麼不裝在全域？** 這台機器有多個 Python（anaconda base、3.13、3.12、
> 3.11、3.10）。`pip install` 只會裝進**當下那一個** Python 的環境，其他 Python
> 完全看不到。之前把 PySide6 裝進其中一個全域環境，結果你用 anaconda base 執行時
> 找不到它，程式就**默默降級成 Tk**——而我還以為你在看 Qt。
> 專案專屬的 `.venv` 從根本上消除這個不確定性。

不想用虛擬環境、只想裝進目前的 Python：

```powershell
python -m pip install -r requirements.txt
```

若你只用 CLI，不裝 GUI 套件也能運作，只有 `bbpull gui` 會提示缺少什麼。

**不用建立任何設定檔。** 站台、帳號、密碼、課程、輸出路徑全部用問的，
或在桌面介面裡填。

---

## 5. 指令列使用

### 3.1 就是打開它（推薦）

```powershell
python -m bbpull
```

或直接雙擊資料夾裡的 `bbpull.cmd`。

**第一次執行**會進入引導流程，把需要的資訊一次問完（密碼不會顯示在畫面上）：

```
==============================================================================
歡迎使用 bbpull
==============================================================================
這是第一次執行，我只需要幾個資訊，之後就可以直接抓課程。
（全程按 Ctrl-C 可隨時取消，不會留下任何檔案）

[1/5] Blackboard 站台
      站台網址 [https://twc.blackboard.com]:
      檢查連線 https://twc.blackboard.com ...
      可連線: HTTP 200

[2/5] 你的帳號
      帳號（學號）:

[3/5] 密碼與登入測試
      密碼只會用來登入，輸入時不會顯示在畫面上。
      密碼: (input hidden)
      登入中 ...
      登入成功！使用者 id: _21652_1

[4/5] 預設要抓的課程
      讀到 91 筆選課記錄。
這支帳號共有 91 門課，請先輸入關鍵字縮小範圍（輸入 * 列出全部）。
關鍵字（可留空取消）: 12529

  1  _12529_1       Student      [2026/27-1] NUR2051/NUR2046 Nursing Practicum I

選擇編號（可用 1,3 或 1-4，q 取消）: 1
      已選擇: [2026/27-1] NUR2051/NUR2046 Nursing Practicum I (_12529_1)

[5/5] 下載位置
      輸出資料夾 [output]:

密碼要怎麼保存？
  1) 加密儲存（推薦）— 用 Windows DPAPI 綁定你的 Windows 帳號
  2) 存進 .env（明文；方便但請勿分享該檔案）
  3) 不要儲存 — 每次執行時再問我
選擇 [1-3]（預設 1）: 1
```

之後每次執行就直接進主選單：

```
==============================================================================
bbpull 1.0.0 — Blackboard Ultra 課程下載器
==============================================================================
  站台    : https://twc.blackboard.com
  課程    : _12529_1
  帳號    : 23002220 / 密碼已設定
  輸出    : Z:\...\output
  預設抓取: 內容=on 公告=on 附件=on

請選擇操作：
  1) 開啟桌面應用程式（推薦：像檔案總管一樣瀏覽與勾選下載）
  2) 抓取課程內容 + 公告（可互動選課）
  3) 挑選多門課程一次抓取
  4) 只看課程大綱，不下載
  5) 列出我的所有課程
  6) 檢查設定與連線狀態
  7) 把密碼改成加密儲存（從 .env 明文搬過來）
  8) 重新設定（站台 / 帳密 / 預設課程 / 輸出）
  9) 離線自我測試（不連網、不需帳密）
 10) 離開
```

選 2 進入抓取流程時，若沒釘住課程會列出課程讓你挑，接著逐項確認：

```
抓取設定 — [2026/27-1] NUR2051/NUR2046 Nursing Practicum I
  抓取 class content（課程內容）？ [Y/n]:
  抓取 announcements（公告）？ [Y/n]:
  下載附件與內嵌檔案？ [Y/n]:
  重新下載已存在的檔案？ [y/N]:
  輸出根目錄（每門課會在其下建立專屬資料夾） [output]:
```

選課支援 `1`、`1,3`、`1-4`、`all`；鍵入 `q` 或 `0` 可取消。游標在任何提示下按 Ctrl-C
都是安全取消，不會留下半截設定檔。

### 3.2 密碼儲存方式

| 選項 | 行為 | 適用 |
| --- | --- | --- |
| **加密儲存（推薦）** | 用 Windows DPAPI 加密存在 `.state/credentials.dat`，**只有你這台電腦的這個 Windows 帳號能解密**。`.env` 不會有密碼。 | 自己的電腦 |
| 存進 .env | 明文寫入 `.env`（該檔已在 `.gitignore` 內） | 想手動管理設定 |
| 不要儲存 | 完全不落地，每次執行時再問一次（不會重跑整個精靈，只問密碼） | 共用電腦 |

已經有明文密碼在 `.env` 的話，可從主選單選 6、或執行 `python -m bbpull secure`
把它搬進加密儲存並清空明文。

### 3.3 指令式（可排程、可自動化）

```powershell
# 0) 先確認程式本身沒問題（離線，195 個測試，不需要網路與帳密）
python -m bbpull selftest

# 1) 檢查設定與能否登入（會列出解析後的設定，密碼以 *** 顯示）
python -m bbpull doctor

# 2) 列出你所有課程（課程名稱會快取，第二次執行只要 0.7 秒）
python -m bbpull courses

# 3) 抓指定課程
python -m bbpull pull --course-id _12529_1

# 4) 互動選課，但用旗標控制其餘行為
python -m bbpull pull --course-id auto
python -m bbpull pull-all --pick
```

### 3.4 非互動環境（排程器 / CI / 代理程式）

互動功能會**主動拒絕而不是卡住**：

- `--no-input` 或 `BB_NONINTERACTIVE=1` → 任何需要輸入的環節直接報錯結束。
- stdin 不是真正的終端機時（管線、重導向、`NUL`），互動命令會以 exit code 2 結束並說明原因。

> 這是實測得來的要求：本機 Python 3.13.1 上 `sys.stdin.isatty()` 對
> `subprocess.DEVNULL` 竟然回傳 **True**。若只信 `isatty()`，管線中會誤啟動互動提示而卡死。
> 因此 Windows 上改用 `GetConsoleMode` 判斷是否為真主控台（見 `wizard.stdin_is_console`）。

`bbpull.cmd` 是等價的啟動器（會自動設定 `PYTHONPATH`）：

```powershell
.\bbpull.cmd pull --course-id _12529_1
```

常用選項：

| 選項 | 作用 |
| --- | --- |
| `--out <路徑>` | 指定輸出資料夾（預設 `.\output\<課程id>_<課程名>`） |
| `--no-files` | 只抓文字與 metadata，不下載附件 |
| `--no-announcements` / `--no-content` | 只抓其中一半 |
| `--overwrite` | 重新下載已存在的檔案（預設會跳過，可重複執行續抓） |
| `-i` / `--interactive` | 抓取前先互動確認各項選項 |
| `--pick` | （`pull-all`）互動挑選多門課程 |
| `--no-input` | 絕不提示，需要輸入時直接失敗 |
| `--force` | 忽略快取的 session，重新登入 |
| `--quiet` | 只印錯誤 |

`.env` 裡的 `BB_INCLUDE_CONTENT` / `BB_INCLUDE_ANNOUNCEMENTS` / `BB_DOWNLOAD_FILES` /
`BB_OVERWRITE` 是「沒給旗標時」的預設值；旗標只能把功能關掉或把覆蓋打開，
所以設定檔裡的每個選項都一定有意義，不會被默默忽略。

### 輸出結構

```
output/_12529_1_課程名稱/
├── index.md                      # 人類看的入口：公告連結 + 完整大綱樹
├── _manifest.json                # 本次抓取的統計與失敗清單
├── content/
│   ├── 01_Week 1/                # 資料夾依 position 排序並加序號，避免同名互蓋
│   │   ├── 01_Lecture notes.md
│   │   ├── 01_Lecture notes.json
│   │   ├── notes.pdf             # data-bbfile 的 linkName → 正確檔名
│   │   └── xid-303_1             # 內嵌圖片（bbcswebdav 的原始檔名）
│   └── 02_Syllabus.pdf
└── announcements/
    ├── announcements.md          # 全部公告一覽（新到舊）
    ├── announcements.json
    ├── 2026-02-01_001_Welcome.md
    └── attachments/notes.pdf
```

每次抓取都會寫 `_manifest.json`；有失敗項目時 exit code 為 **5**，可直接用於排程判斷。

---

## 6. Exit codes

| code | 意義 |
| --- | --- |
| 0 | 成功 |
| 2 | 設定錯誤（缺帳密、課程 id 找不到…） |
| 3 | 登入失敗 |
| 4 | API 錯誤（403 = 你的帳號權限讀不到該資源） |
| 5 | 完成但有部分檔案失敗（詳見 `_manifest.json`） |

---

## 7. 做不到的事（誠實揭露）

1. **不是所有東西都是檔案。** Ultra 的測驗、作業、討論區、LTI 工具、SCORM 套件是互動式元件，
   REST 只回傳「這是一個測驗」的 metadata 與 UI 連結，抓不到題庫或互動內容。
   bbpull 會保留項目、寫下型別與 UI 連結，不會假裝下載成功。
2. **自適應發布（adaptive release）未開放的內容**，學生帳號看不到，API 也不會回傳。
3. **沒有提交作業、沒有下載成績。** 這支工具只做「讀取」，全程只有 GET，不會修改任何東西。
4. **下載是循序的**（一堂課接一堂課、一個檔案接一個檔案），刻意不對學校伺服器併發請求。
   為此程式沒有提供 `--workers` 這類選項——不提供無效的設定，比提供一個不會生效的旋鈕誠實。
5. **PDF / PPTX 只下載原檔，不做內容抽取**；內文是 HTML（BBML）的項目才會轉成 Markdown。

---

## 8. 疑難排解

### 開啟的是 Tk 介面而不是 Qt

先看引擎診斷：

```powershell
python -m bbpull gui --check
```

若顯示「PySide6 尚未安裝」，而你確定裝過，那就是**裝進了另一個 Python**——
這台機器有多個。最省事的解法是建立專案環境：

```powershell
python -m bbpull venv --create
```

之後不管你用哪個 Python 呼叫 `python -m bbpull`，都會自動切換到 `.venv`。

### 想確認自己開的是哪個版本

看**視窗標題**：`bbpull — Blackboard 課程下載器（Qt）` 或 `（Tk）`。

或看啟動訊息，會直接列出使用的直譯器：

```
正在開啟桌面應用程式（Qt）…  [Z:\...\.venv\Scripts\python.exe]
```

### 環境壞掉了

```powershell
python -m bbpull venv --create --force   # 重裝相依套件
```

**`login error: all login strategies failed`**
你的學校可能不是原生帳密表單，而是 SSO（Shibboleth / Azure AD / 校園入口轉導）。
此時帳密表單不在 Blackboard 這台機器上，任何程式都無法直接送帳密。改用 cookie 匯入：

1. 用瀏覽器正常登入 Blackboard。
2. 匯出 cookie（DevTools → Application → Cookies，或用瀏覽器擴充套件匯出成 JSON）。
3. 存成 `cookies.json`，格式：`{"base_url":"https://twc.blackboard.com","cookies":[{"name":"...","value":"...","domain":"twc.blackboard.com","path":"/"}]}`
4. 設定 `BB_COOKIE_FILE=C:\path\to\cookies.json` 後再執行 `doctor`。

**`api error: HTTP 403`**
該端點不開放給學生角色（部分課程工具僅教師可讀）。內容大綱與公告不受影響。

**檔名很奇怪（例如 `xid-303_1`）**
那是 Blackboard 內容庫的原始檔名——該檔案沒有提供人類可讀名稱，硬猜會猜錯，所以照實呈現。
多數附件會從 `data-bbfile` 的 `linkName` 取得正確檔名（如 `notes.pdf`）。

**中文檔名亂碼**
程式全程使用 UTF-8，`os.path` 亦無編碼轉換；若在舊版 cmd 看到亂碼，請用 Windows Terminal
或設定 `chcp 65001`，檔案本身沒問題。

**憑證安全**
- 預設把密碼交給 **Windows DPAPI 加密**（`.state/credentials.dat`），
  綁定你的 Windows 帳號，`.env` 不需要也不會有密碼。
- 若選擇明文存放，`.env` 已在 `.gitignore` 內。
- **`.env.example` 是可分享的範本，不該放真帳密。** `bbpull doctor` 會偵測並警告；
  偵測訊息本身不會印出密碼內容。
- `.state/` 整個目錄（cookie 與加密憑證）都在 `.gitignore` 內。
- 憑證檔在類 Unix 系統會設為 `0600`。
- 若 `.env` 被其他使用者可讀，`doctor` 會主動警告。
- 程式只會連線到 `BB_BASE_URL` 的網域與 Blackboard 官方網域，其他網域一律拒絕（避免 cookie 外洩）。
- 寫入 `.env` 與憑證檔都採「原子寫入」（先寫暫存檔再置換），中斷不會截斷你的檔案。

---

## 9. 專案結構與測試

```
bb-ultra-pull/
├── bbpull/
│   ├── gui_select.py     # 引擎選擇與診斷（絕不靜默降級）
│   ├── venv_tools.py     # 專案虛擬環境：建立、診斷、自動切換
│   ├── healthcheck.py    # 打包版本的執行檔自我檢查
│   ├── gui_qt/           # 桌面應用程式（PySide6，預設）
│   │   ├── window.py     #   主視窗：導覽、選取、下載、進度、篩選、對話框
│   │   ├── models.py     #   QAbstractListModel 虛擬化（效能核心）+ 選取語意
│   │   ├── delegates.py  #   自繪列（圖示/文字/checkbox/按鈕），無 widget
│   │   ├── workers.py    #   QThread + signals 背景工作、工作輪詢
│   │   └── theme.py      #   QSS 樣式表 + QPainter 向量圖示
│   ├── gui/              # 桌面應用程式（Tk 降級版）
│   │   ├── app.py        #   主視窗
│   │   ├── widgets.py    #   高效能元件（純 tk，增量更新）
│   │   ├── anim.py       #   動效：Tween / Spinner / Shimmer
│   │   ├── courses.py    #   學年學期解析、篩選與分組（兩個 UI 共用）
│   │   └── theme.py      #   Tk 字體、縮放與 canvas 圖示
│   ├── palette.py        # 共用設計 token（兩個 UI 同一份配色）
│   ├── cli.py            # 子命令、主選單、首次引導、輸出目錄、manifest
│   ├── wizard.py         # 文字互動層：提示引擎、選課、.env 寫入
│   ├── catalog.py        # 瀏覽模型：大綱樹、數量統計、JSON 快取
│   ├── selective.py      # 選擇性下載引擎 + 可取消的背景工作管理
│   ├── secrets_store.py  # 密碼加密儲存（Windows DPAPI / keyring）
│   ├── logging_util.py   # 主控台編碼安全輸出
│   ├── session.py        # 登入（三種策略）、cookie 快取、重試、下載
│   ├── course.py         # 大綱遞迴、Markdown 產生、附件下載
│   ├── announcements.py  # 公告抓取與彙整
│   ├── bbml.py           # BBML → Markdown / 純文字、內嵌檔案 URL 抽取
│   ├── paths.py          # 跨平台安全檔名、去重、長路徑
│   ├── config.py         # CLI > 環境變數 > .env > 加密儲存 的設定合併
│   └── errors.py
└── tests/
    ├── test_bbml.py      # BBML 轉換、檔名規則
    ├── test_course.py    # 大綱遞迴、目錄命名、公告、冪等性
    ├── test_wizard.py    # 文字互動層、首次引導、憑證儲存、API 形狀
    ├── test_browse.py    # 瀏覽模型、選擇規劃、選擇性下載引擎、工作管理
    ├── test_courses.py   # 學年學期解析、篩選、分組（純邏輯）
    ├── test_anim.py      # 補間、緩動、顏色混合、排程（純邏輯）
    ├── test_gui_select.py # GUI 引擎選擇與降級規則（純邏輯）
    ├── test_venv.py       # 虛擬環境與啟動檔完整性（含 CRLF 迴歸測試）
    ├── test_secret_scan.py # 機密掃描閘門（含正／負控制組）
    ├── test_installer.py  # 圖示、進入點、spec、健康檢查、凍結路徑
    ├── test_gui_qt.py    # Qt 介面：模型、選取、delegate 幾何、效能（需 PySide6）
    └── test_gui.py       # Tk 介面（需 Tk）
```

```powershell
# 主要測試（538 個，離線，不連網）
python -m unittest discover -s tests -t .
python -m bbpull selftest

# 介面測試（需圖形環境，預設不納入 discovery）
python -m unittest tests.test_gui_qt    # 35 個（Qt）
python -m unittest tests.test_gui       # 66 個（Tk）
```

> 為什麼 GUI 測試要分開跑：它們會建立真正的視窗／Qt 應用程式，在
> `unittest discover`（也就是 `bbpull selftest`）的同一個行程裡做這件事，會產生
> Tk／Qt 的拆除雜訊。要一起跑可以設 `BBPULL_RUN_GUI_TESTS=1`（Tk）或
> `BBPULL_RUN_QT_TESTS=1`（Qt）。Qt 測試預設使用 `QT_QPA_PLATFORM=offscreen`，
> 不需要真的桌面。

測試涵蓋：

- **內容抓取**：BBML→Markdown 轉換、內嵌連結改寫、檔名安全性（Windows 保留字 / 中文 / 長度）、
  大綱遞迴與目錄命名、重複執行冪等性、公告排序與附件。
- **互動層**：選課語法（`1,3` / `1-4` / `all`）、驗證重問、Ctrl-C/EOF 安全取消、
  設定精靈寫入的每個鍵都真的被讀回、密碼不外洩、`.env.example` 誤填密碼的偵測、
  首次引導的完整流程（含登入失敗重試、長清單關鍵字過濾）。
- **瀏覽與選擇性下載**：資料夾項目數統計、父子選取收斂（勾了資料夾又勾子項目只下一次）、
  跨分支選取、公告與內容分流、只建立必要的上層資料夾、**選擇性下載的輸出路徑必須與
  完整下載完全一致**（同一組檔案路徑），工作進度與取消、失敗不中斷。
- **學年／學期篩選**：用真實課名驗證解析（`[2026/27-1] 課名`、只有學年的 `[2023/24]`、
  沒有前綴的 `Library`、以及取不到名稱的 403 課程），年/學期下拉選項、兩者交集、
  切換學年時不可能存在的學期要自動重置、分組排序。
- **動效**：緩動函式的端點與單調性、顏色混合（含非法色值不崩潰）、
  以明確時間戳逐格推進的補間、交錯延遲在長清單要收斂、排程 token 的取消與清理。
- **桌面介面**：調色盤與圖示、**checkbox 三種狀態的實際 canvas 繪製結果**、
  **已選列必須畫出強調色**（這條是為了抓「勾了看不到勾」的回歸）、提示頁必須有真實幾何
  且在非捲動父層、清單載入後要恢復捲動區、主題切換不遺失選取、
  **效能回歸**（勾選不得重建列：25 次勾選須在 2 秒內完成；同一列物件必須在勾選後仍然存活）。
- **實測得到的真實 API 形狀**（以下每一條都是實測後才寫成測試的，不是猜的）：
  - `/users/me/courses` 回傳的是**扁平 membership**（沒有巢狀 `course` 物件、沒有課名），
    課名只能逐一查 `/courses/{id}` 並快取。
  - 舊課程對學生帳號會回 **403**，因此失敗結果也要快取，否則每次啟動都重打約 90 次無效請求。
  - `/v2/users/me/courses` 是 404；`/courses?courseId=a,b` 與重複參數形式都回 0 筆，
    所以沒有批次取名的捷徑。

### 實測過程中修掉的真實問題

| 問題 | 症狀 | 修正 |
| --- | --- | --- |
| `isatty()` 不可信 | 本機 Python 3.13.1 對 `subprocess.DEVNULL` 回傳 **True**，管線中會誤啟動互動提示 | Windows 改用 `GetConsoleMode` 判斷真主控台 |
| 扁平 membership | 課程清單恆為 0 筆，選課畫面永遠空白 | 解析頂層 `courseId`，課名另查 `/courses/{id}` 並快取 |
| 403 未快取 | 每次啟動重打約 90 次注定失敗的請求（5.2s → 0.7s） | 失敗結果一併快取 |
| 主控台編碼崩潰 | 課名含 U+2011（非斷行連字號）→ `UnicodeEncodeError` 讓整個抓取中斷 | `logging_util` 全路徑降級輸出，檔案仍寫完整 UTF-8 |
| 遞迴自我執行 | 選單編號異動後，測試誤觸 `selftest` 導致無限遞迴 | 加裝 re-entrancy 防護（模組旗標，不用環境變數） |
| 環境變數汙染測試 | 防護用的環境變數洩漏進測試環境，改變測試結果 | 改用模組層級旗標 |
| **清單勾選嚴重卡頓** | 每次勾選都銷毀重建全部列：120 列時 25 次勾選要 9562 ms | Tk 版改為增量更新（同一情境 34 ms）；**根本解法是改用 Qt model/view**，列數不再影響成本 |
| **Tk 架構無法擴展** | 每列一個 widget，而 Tk 每個 widget 都是一個 OS 視窗：91 張課程卡 = 594 個 widget，5,000 列約 14 秒 | 改用 PySide6 `QTreeView` + model + delegate，只繪製可見列（5,000 列 9.1 ms） |
| **Qt 版面從 placeholder 卡住** | `set_catalog()` 只重置狀態、沒有填充列資料；畫面剛好因為接著呼叫 `set_folder()` 才正常，但單獨呼叫就會是空的 | `set_catalog()` 內呼叫 `_rebuild()`，並補上直接呼叫 `set_catalog()` 的測試 |
| **左邊欄文字互相重疊** | 用固定像素偏移排版：標題帶 y 32..62、副標題帶 y 40..54，**整段疊在一起**；兩行折行的標題還會超出卡片 | 改成由字體實際度量堆疊（`course_card_geometry`），並加 12 個涵蓋 1.25/1.5/2.0 DPI 的「不得重疊」測試 |
| **偷偷降級到 Tk（最嚴重）** | PySide6 只裝在 4 個 Python 之中的一個；`ImportError` 被 catch 後**靜默**改用 Tk，使用者以為在看 Qt 版，導致一整輪除錯都在描述錯的介面 | 引擎選擇一律回報原因；`--qt` 讓缺 Qt 直接失敗；Tk 降級時主控台與視窗狀態列都顯示警告；視窗標題標明版本；新增 `--check` 與 22 個決策邏輯測試 |
| **套件裝進全域直譯器** | `pip install PySide6` 裝進了 Python 3.13 的 user site-packages，而你實際用的是 anaconda base——其他 Python 完全看不到，也不該去污染系統環境 | 建立專案專屬 `.venv`（`bbpull venv --create`）；`python -m bbpull` 偵測到專案環境會**自動重新以它執行**，所以從哪個 Python 啟動結果都一致 |
| **改壞了 `bbpull.cmd`** | 用會把行尾正規化成 LF 的編輯器重寫 `.bat`，而 **cmd.exe 遇到裸 LF 會把 REM 註解片段當指令執行**（5 行裡 3 行報錯） | 以 CRLF 重寫，加上 `.gitattributes`（`*.cmd text eol=crlf`）強制，並補上「不得有裸 LF」的迴歸測試 |
| **顯示的安裝指令無法貼上使用** | 產生的提示是 `pip install requests>=2.31.0`，未加引號的 `>` 在 shell 裡是**重導向**——pip 只收到 `requests`，並產生一個名為 `=2.31.0` 的檔案 | `shell_command()` 對含 shell 特殊字元的參數加引號，並測試每個危險字元 |
| **打包後 `selftest` 崩潰** | `unittest` 被我放進 PyInstaller 的 excludes，而打包版本又沒有 `tests/` 目錄（解壓到 `_MEI…` 暫存夾），`unittest discover` 直接拋 `Start directory is not importable` | 打包版本改跑**執行檔自我檢查**（相依套件、GUI 引擎、Qt 外掛、目錄可寫性）；`unittest` 不再排除 |
| **打包後登入狀態會消失** | `PROJECT_ROOT` 在凍結後指向 PyInstaller 的 `_MEI…` 暫存夾，`.state/` 與 `.env` 都寫在那裡，程式結束就被刪掉 | 凍結時改用 `%LOCALAPPDATA%\bbpull\`；並在自我檢查中加入「state 目錄不得位於暫存區」的斷言 |
| **進入點相對匯入** | PyInstaller 以**頂層模組**執行指定的腳本，`from .cli import main` 會拋 `attempted relative import with no known parent package`——建置成功，啟動即死 | 新增 `installer/entry.py` 只用絕對匯入 |
| **建置時誤刪路徑** | `Path(SPECPATH).resolve().parent` 多退了一層目錄，PyInstaller 找不到腳本 | 改為 `Path(SPECPATH).resolve()`，並加測試防止再犯 |
| **視窗版本誤判為失敗** | PyInstaller 的 bootloader 會在**子行程**執行應用程式，`Popen` 拿到的 pid 只有隱藏視窗，用 pid 比對永遠找不到 GUI | 改用視窗標題比對，並用 `taskkill /T` 連子行程一起清掉 |
| **建置被執行中的 exe 卡住** | Windows 不允許刪除執行中的 `.exe`，前一次 smoke test 留下的行程讓建置在 COLLECT 階段 `PermissionError` | 建置前先 `taskkill`，並在無法清空 `dist/` 時給出明確訊息 |
| **`QBuffer` 造成存取違反** | `QBuffer(QByteArray())` 的暫存物件會被 Python 回收，Qt 仍在寫入 → 行程以 `0xC0000005` 直接崩潰，不是拋例外 | 保留 `QByteArray` 的參考直到寫完 |
| **`packaging/` 目錄名稱衝突** | 這個名字與 PyPI 的 `packaging`（pip 相依）同名，`import packaging.make_icon` 會抓到別人的套件 | 改名為 `installer/` |
| **機密掃描閘門本身失效** | 第一版在乾淨的樹上報了 **180 筆**假警報（`BB_OUT_DIR=output` 讓每個 docstring 都命中；`password: str = ""` 等程式碼也被當成硬編碼密碼）——100% 誤報率等於沒有閘門 | 規則收窄成「精確 token 形狀」「只有名稱含 PASSWORD/SECRET/TOKEN 的 .env 值」「必須是引號字串常值」；並補上**正控制組**（種入假密碼必須被抓到）與負控制組測試 || **截圖看到舊畫面** | GDI 螢幕抓取在被遮擋時回傳過期像素（Windows 不會重繪被遮住的視窗），害我一度誤判版面壞掉 | 改用 `QWidget.grab()` 由 Qt 同步渲染，影像必定對應真實狀態 |
| **delegate 繪製中途崩潰** | `self.parent()` 是視窗不是 view，拿它找 model 觸發 `AttributeError`，而且發生在 `painter.save()` 與 `restore()` 之間，導致 painter 狀態失衡 | 把 node 直接傳進繪製函式，不再往上找 widget 樹 |
| **`Animator.after()` token 不可雜湊** | 用 dict 當 token 放進 set → `TypeError: unhashable type: 'dict'` | 改用 `OneShot` 類別 |
| **幽靈按鈕讓整個視窗建不起來** | `CanvasButton` 用了 Tk 不存在的顏色名 `transparent`，`TclError: unknown color name` | 幽靈按鈕以所在底色填色（視覺相同），並加上實際建構視窗的測試 |
| **列高異常** | 沒有按鈕的列被撐到約 300px，因為空的 `CTkFrame` 預設高度是 200 | 有按鈕才建立固定高度的按鈕容器 |
| **打勾的核取方塊看不見** | `IconPainter.check()` 先 `delete("all")`，把剛畫好的強調色方塊一起刪掉，只剩白色勾勾浮在列背景上 | 元件建立一次、改用 `itemconfigure` 更新狀態，勾勾疊在底色之上 |
| **空白提示頁跑到畫面外** | `place()` 不會對父層貢獻所需尺寸，放在可捲動區裡的提示頁把父層壓成 1px 高，整塊落在 y = −132 | 改放在非捲動的父層，並用上下彈性空白以 `pack` 置中 |
| **勾選後 checkbox 仍顯示空白** | `render_content` 直接設定 `row.selected`，checkbox 從未收到狀態，勾了也看不到勾（靠取樣截圖像素才發現） | `ItemRow.apply()` 同步驅動 checkbox，並補上斷言「已選列的 checkbox 必須畫出強調色」的測試 |
| **錯誤處理常式會殺掉事件幫浦** | `_handle()` 一拋例外就跳出 `while`，佇列不再排空，之後所有結果（含載入完成的課程結構）都被靜默丟棄，外觀看起來像「卡住」 | 逐一包住每個訊息的處理，記錄並顯示錯誤但不中斷排空 |
| **圖示按鈕 32% 是死區** | 圖示是疊在上面的子 canvas，`<Button-1>` 只綁在父層，點在看得見的圖示上完全沒反應 | 父層與圖示都綁事件，並用 `winfo_containing` 正確處理 hover 進出 |
| **DPI 雙重縮放** | customtkinter 自己做一次 DPI 縮放，與 `SetProcessDpiAwareness` 疊加；13pt 標籤要求 42px，旁邊的 canvas 卻是字面像素 | 釘住 customtkinter 縮放為 1.0，並把所有字體改為**像素**尺寸（負數） |

---

## 10. 自行打包執行檔

```powershell
python -m bbpull venv --create        # 先建立環境（含 PyInstaller 以外的相依）
.\.venv\Scripts\python -m pip install pyinstaller
python tools\build_exe.py             # 產生 dist\bbpull\ 與 release zip
python tools\build_exe.py --no-zip    # 只產生資料夾
```

產出：

```
dist/bbpull/bbpull.exe          # 主控台版本
dist/bbpull/bbpull-gui.exe      # 視窗版本（雙擊不跳命令視窗）
dist/bbpull-<版本>-windows-x64.zip
```

用**資料夾模式**而不是單一檔案：PySide6 的單檔版本每次啟動都要把約 200 MB
解壓到暫存目錄，開一次要等好幾秒。所以發佈的是壓縮檔。

`tools/build_exe.py` 不是只跑 PyInstaller，它會**驗證產物**：

1. 用應用程式自己的調色盤重新產生 `.ico`；
2. 執行 PyInstaller；
3. **實際執行**兩個執行檔 —— `--help`、`selftest`、`gui --check`、`venv`，
   並且確認視窗版本**真的開出視窗**（只看「行程還活著」不算：匯入失敗時
   PyInstaller 會彈出錯誤對話框，行程也活著）；
4. 打包成 zip。

> 為什麼要驗證：PyInstaller 可以「建置成功」卻做出一個少掉某個動態匯入模組的
> 執行檔——看起來沒問題，直到有人用到那個功能。這正是最糟的發佈失敗。

---

## 11. 使用須知

抓下來的教材仍受著作權與校方規範約束，僅供個人離線閱讀使用，請勿再散布。
本工具只做讀取（全部是 GET 請求），不會送出任何修改。
