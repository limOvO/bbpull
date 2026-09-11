# bbpull

把 **Blackboard Learn (Ultra)** 指定課程的**教材內容與公告**抓到本機。

- 保留課程原本的資料夾結構
- 文件轉成 Markdown，附件與內嵌圖片一起下載
- 提供**桌面應用程式**（像檔案總管一樣瀏覽、勾選、下載）與**命令列**

只讀取 Blackboard，不會修改任何線上內容。

---

## 下載執行檔（不需要 Python）

到 [Releases](../../releases) 下載 `bbpull-*-windows-x64.zip`，解壓縮後：

| 檔案 | 用途 |
| --- | --- |
| `bbpull-gui.exe` | 雙擊開啟桌面視窗（不會跳出黑色命令視窗） |
| `bbpull.exe` | 命令列版本，也能開視窗 |

第一次開啟會詢問站台／帳號／密碼。密碼以 **Windows DPAPI 加密**儲存，綁定你的
Windows 帳號，存放於 `%LOCALAPPDATA%\bbpull\`，換版本或搬動資料夾都不會遺失登入狀態。

```powershell
bbpull.exe selftest     # 確認執行檔完整可用
```

> 目前只提供 Windows x64。其他平台請從原始碼執行。

---

## 從原始碼執行

需要 Python 3.9+（已在 3.13 驗證）。建議用專案虛擬環境，避免套件裝到錯的 Python：

```powershell
python -m bbpull venv --create     # 建立 .venv 並安裝相依套件
python -m bbpull gui               # 開啟桌面應用程式
```

建立 `.venv` 之後，不管你用哪個 Python 呼叫 `python -m bbpull`，都會自動切換到該環境。

```powershell
python -m bbpull gui --check       # 顯示會用哪個 GUI 引擎、為什麼
python -m bbpull gui --qt          # 強制 Qt；裝不起來就報錯，不靜默降級
python -m bbpull gui --tk          # 強制使用內建 Tk 介面
```

---

## 使用方式

### 桌面介面

```powershell
python -m bbpull gui
```

左邊選課，右邊瀏覽內容。勾選要下載的項目，底部托盤按「下載選取項目」。

| 操作 | 方式 |
| --- | --- |
| 進入資料夾 | 雙擊該列 |
| 上一頁／下一頁／上一層 | `Alt+←` / `Alt+→` / `Alt+↑` |
| 跳到上層任一層 | 點擊路徑列 |
| 篩選目前資料夾 | 右上「篩選…」 |
| 搜尋整門課 | 範圍切成「整門課」再輸入關鍵字 |
| 全選目前可見項目 | `Ctrl+A` |
| 下載選取項目 | `Ctrl+D` |
| 重新讀取課程結構 | `F5` |
| 切換亮／暗色 | 右上月亮圖示 |

勾選**資料夾**代表連同底下所有內容一起下載；可以在不同資料夾之間累積選取，
最後一次下載。左側可用學年／學期下拉選單縮小課程範圍。

回報介面問題時：

```powershell
python -m bbpull gui --inspect     # 點任何位置都會記錄元件、樣式與放大截圖
```

### 命令列

```powershell
python -m bbpull pull --course-id _12529_1           # 抓一門課
python -m bbpull pull-all                             # 抓全部已選課程
python -m bbpull courses                              # 列出課程
python -m bbpull list-content --course-id _12529_1    # 只印大綱，不下載
python -m bbpull doctor                               # 設定與連線狀態
```

常用旗標：

| 旗標 | 說明 |
| --- | --- |
| `--course-id` | 課程 id，例如 `_12529_1` |
| `--out` | 輸出目錄（預設 `output/`） |
| `--no-files` | 只寫文字與中介資料，不下載附件 |
| `--no-content` / `--no-announcements` | 只抓其中一種 |
| `--overwrite` | 重新下載已存在的檔案 |

其他子命令：`menu`（互動主選單）、`setup`（設定精靈）、`secure`（密碼改存加密儲存）、
`login`、`selftest`、`venv`。

### Exit codes

| 代碼 | 意義 |
| --- | --- |
| 0 | 成功 |
| 2 | 設定錯誤（缺帳密、課程 id 找不到…） |
| 3 | 登入失敗 |
| 4 | API 錯誤（403 = 你的帳號權限讀不到該資源） |
| 5 | 完成但有部分檔案失敗 |

---

## 專案結構

```
bbpull/
├── bbpull/
│   ├── gui_qt/          # 桌面介面（PySide6，預設）
│   │   ├── window.py    #   主視窗
│   │   ├── models.py    #   model/view 虛擬化
│   │   ├── delegates.py #   自繪列
│   │   ├── theme.py     #   QSS + 向量圖示
│   │   ├── workers.py   #   背景執行緒
│   │   └── inspect.py   #   點擊檢查模式
│   ├── gui/             # 桌面介面（Tk 降級版，同功能）
│   ├── palette.py       # 兩個介面共用的設計 token
│   ├── gui_select.py    # GUI 引擎選擇與診斷
│   ├── venv_tools.py    # 專案虛擬環境
│   ├── healthcheck.py   # 打包版本的自我檢查
│   ├── cli.py           # 子命令與主選單
│   ├── wizard.py        # 文字互動層
│   ├── session.py       # 登入、cookie 快取、下載
│   ├── course.py        # 課程大綱遞迴與附件下載
│   ├── announcements.py # 公告
│   ├── bbml.py          # BBML → Markdown
│   ├── catalog.py       # 瀏覽模型與快取
│   ├── selective.py     # 選擇性下載引擎
│   ├── config.py        # 設定合併（CLI > 環境變數 > .env > 加密儲存）
│   ├── secrets_store.py # 密碼加密儲存（DPAPI）
│   └── paths.py         # 安全檔名、長路徑
├── installer/           # 圖示產生器、凍結版本進入點
├── tools/               # 建置腳本、機密掃描閘門
├── tests/               # 單元測試
├── bbpull.spec          # PyInstaller 設定
└── bbpull.cmd           # Windows 啟動檔（優先使用 .venv）
```

輸出結構與 CLI／GUI 完全一致，兩種方式可以混用：

```
output/<課程名稱>/
├── _manifest.json       # 這次抓了什麼、成功失敗統計
├── 01_教材資料夾/
│   ├── 文件.md          # 文件轉 Markdown
│   └── 附件.pdf         # 附件原檔
└── _announcements/      # 公告
```

---

## 測試

```powershell
python -m bbpull selftest                # 538 個離線測試 + 機密掃描
python -m unittest tests.test_gui_qt     # 47 個 Qt 介面測試
python -m unittest tests.test_gui        # 66 個 Tk 介面測試
```

---

## 授權

[MIT](LICENSE)

---

更深入的內容（為什麼用 Qt、效能的實測數據、打包流程、疑難排解、開發過程中修掉的問題）
見 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。
