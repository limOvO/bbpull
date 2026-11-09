# bbpull

把 **Blackboard Learn (Ultra)** 指定課程的**教材內容與公告**抓到本機。

- 保留課程原本的資料夾結構
- 文件轉成 Markdown，附件與內嵌圖片一起下載
- 提供**桌面應用程式**（像檔案總管一樣瀏覽、勾選、下載）與**命令列**

只讀取 Blackboard，不會修改任何線上內容。

---

## 下載執行檔（不需要 Python）

到 [Releases](../../releases) 下載對應平台的壓縮檔。

### Windows

`bbpull-*-windows-x64.zip`，解壓縮後：

| 檔案 | 用途 |
| --- | --- |
| `bbpull-gui.exe` | 雙擊開啟桌面視窗（不會跳出黑色命令視窗） |
| `bbpull.exe` | 命令列版本，也能開視窗 |

第一次開啟會詢問站台／帳號／密碼。密碼以 **Windows DPAPI 加密**儲存，綁定你的
Windows 帳號，存放於 `%LOCALAPPDATA%\bbpull\`，換版本或搬動資料夾都不會遺失登入狀態。

```powershell
bbpull.exe selftest     # 確認執行檔完整可用
```

### Linux

`bbpull-*-linux-x86_64.tar.gz`，解壓縮後：

| 檔案 | 用途 |
| --- | --- |
| `bbpull` | 執行檔：`./bbpull gui` 開視窗，也支援全部命令列 |
| `bbpull-gui` | 三行啟動腳本，等同 `./bbpull gui`（給檔案總管雙擊） |
| `install.sh` | 安裝到 `~/.local`（免 root），建立 `bbpull` 指令、選單項目與桌面捷徑 |
| `bbpull.png` | 256px 圖示 |
| `README-linux.txt` | 同上內容的離線說明 |

```bash
./bbpull gui            # 直接開視窗
./bbpull selftest       # 確認執行檔完整可用
./install.sh            # 裝進 ~/.local + 建立桌面捷徑
./bbpull shortcut       # 只建立桌面捷徑（不搬動程式）
./install.sh --no-desktop   # 安裝但不建立桌面捷徑
```

Linux 版需要 Qt 平台外掛（xcb 或 Wayland），兩者都已經打包在資料夾裡，
不需要另外安裝 Qt。登入狀態與密碼存在 `$XDG_DATA_HOME/bbpull`
（預設 `~/.local/share/bbpull`）：有桌面鑰匙圈時由系統保管，沒有時退回
「只有檔案權限保護」（0600），`bbpull doctor` 會照實說明，不會假裝已加密。
`install.sh --uninstall` 會移除程式與選單項目，但保留已儲存的登入狀態。

> Tk 降級介面（`--tk`）需要系統的 Tk 套件（Arch：`tk`、Debian/Ubuntu：
> `python3-tk`）。Qt 是預設介面，沒有 Tk 也能正常使用。

**Linux 疑難排解**

| 症狀 | 原因與解法 |
| --- | --- |
| `Could not load the Qt platform plugin "xcb"` | 系統缺少 xcb 相依函式庫；Debian/Ubuntu 裝 `libxcb-cursor0`，Arch 裝 `xcb-util-cursor` |
| Wayland 下視窗或輸入異常 | 改用 X11 後端：`QT_QPA_PLATFORM=xcb ./bbpull gui` |
| 「開啟資料夾」沒反應 | 缺少 `xdg-open`（Arch：`xdg-utils`、Debian：`xdg-utils`） |
| `gui --tk` 說 tkinter 不存在 | 裝系統 Tk 套件；Qt 介面不需要它 |
| 沒有桌面環境的伺服器 | 用命令列：`./bbpull pull --course-id <id>` |

---

## 從原始碼執行

需要 Python 3.9+（已在 Windows 3.13 與 Linux 3.14 驗證）。建議用專案虛擬環境，
避免套件裝到錯的 Python：

```powershell
python -m bbpull venv --create     # 建立 .venv 並安裝相依套件
python -m bbpull gui               # 開啟桌面應用程式
```

```bash
python3 -m bbpull venv --create    # Linux / macOS
python3 -m bbpull gui
```

建立 `.venv` 之後，不管你用哪個 Python 呼叫 `python -m bbpull`，都會自動切換到該環境。
Windows 也可以直接使用 `bbpull.cmd`，Linux／macOS 使用 `./bbpull.sh`。

```powershell
python -m bbpull gui --check       # 顯示會用哪個 GUI 引擎、為什麼
python -m bbpull gui --qt          # 強制 Qt；裝不起來就報錯，不靜默降級
python -m bbpull gui --tk          # 強制使用內建 Tk 介面（需系統 Tk）
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
| **預覽項目（預設動作）** | **雙擊非資料夾的列，或按該列的「預覽」** |
| **變更下載資料夾** | **左下「變更下載資料夾…」** |
| 上一頁／下一頁／上一層 | `Alt+←` / `Alt+→` / `Alt+↑` |
| 跳到上層任一層 | 點擊路徑列 |
| 篩選目前資料夾 | 右上「篩選…」 |
| 搜尋整門課 | 範圍切成「整門課」再輸入關鍵字 |
| 全選目前可見項目 | `Ctrl+A` |
| 下載選取項目 | `Ctrl+D` |
| 重新讀取課程結構 | `F5` |
| 切換亮／暗色 | 右上月亮圖示 |

**預覽是預設動作**：項目列上「預覽」在主要位置（強調色），「下載」在它左邊；
雙擊非資料夾的列也是預覽。換句話說，看一眼內容不需要先下載任何東西。

**預覽**會顯示項目的類型、檔名／MIME、建立與修改時間、所在路徑，以及文件與公告的
內文（渲染成 Markdown）。內容來自已快取的課程結構，**不會下載任何檔案**；內文中的
圖片以文字佔位，也不會被抓取。檔案與測驗／工具會顯示說明與「下載此項目」或
「在瀏覽器開啟」。預覽視窗的「下載此項目」只抓那一個項目。

Ultra 文件裡內嵌的 **PDF／Excel／DOCX** 會直接以**懸掛式的子列**顯示在課程清單上
（縮排、帶 `└` 連接線、掛在文件列下方，標示「附件」），不必先開預覽對話框：

- 該列的「**下載**」會把附件存進課程輸出資料夾的正確位置（和完整下載的目錄結構一致）。
- 該列的「**預覽**」會用已登入的 session 把檔案抓到系統暫存區
  （`/tmp/bbpull-preview/<內容 id>/`，同一份不會重複下載），再用系統預設程式開啟
  ——PDF 用你的 PDF 檢視器、Excel 用 LibreOffice／OnlyOffice；圖片直接顯示。
- 清單上方會顯示「N 個項目（含 M 個附件）」；附件列沒有勾選框，因為下載托盤是以
  內容項目為單位，避免選到重複的東西。

預覽對話框裡同樣會列出這些附件，兩邊都能用。直接是檔案屬性的項目（`x-bb-file`）
也有「預覽檔案」按鈕。

Blackboard 有時把文件標題回報成無意義的 `ultraDocumentBody`，程式會改用內文標題、
主要附件檔名或第一行文字顯示，所以整排「PDF／Excel 看不到」的情況不會再出現。

**下載資料夾**預設是 `output/`（每門課在其下建立專屬資料夾）。按左下
「變更下載資料夾…」選擇後，會立即生效並寫入 `.env`（`BB_OUT_DIR`），
所以命令列與下次開啟 GUI 都會沿用同一個位置。

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

Linux／macOS 把 `python -m bbpull` 換成 `./bbpull.sh`（或用 `.venv/bin/python -m bbpull`）；
打包版則是 `./bbpull`：

```bash
./bbpull.sh pull --course-id _12529_1
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
`login`、`selftest`、`venv`、`shortcut`（建立桌面捷徑）。

### 無法登入

登入失敗時，程式會**轉述站台回覆的原文**，不再只說「still anonymous」：

```
[auth] form-post rejected - The username or password you typed is incorrect. ...
[auth] direct-post rejected - 站台要求第二因素驗證（MFA／TOTP）...
```

| 訊息 | 意思 | 怎麼辦 |
| --- | --- | --- |
| `username or password ... incorrect` | 帳密不對，或帳號被鎖定／要求改密碼 | 先用瀏覽器以同一組帳密登入確認 |
| `MFA／TOTP` | 密碼正確，但要求驗證碼 | 無法自動化，改用 cookie 匯入 |
| `站台把登入導向 …（SSO）` | 學校用 SSO（Azure AD／Shibboleth） | 改用 cookie 匯入 |
| `no credential form found` | 登入頁不是原生表單 | 改用 cookie 匯入 |

**Cookie 匯入**（SSO／MFA 唯一可行的方式）：用瀏覽器正常登入 Blackboard，匯出 cookie 後
把 `BB_COOKIE_FILE`（或 `--cookie-file`）指向該檔。支援三種格式：

1. 本程式自己的 jar：`{"base_url":"https://twc.blackboard.com","cookies":[...]}`
2. 瀏覽器擴充套件匯出的 JSON 陣列：`[{"name":"...","value":"...","domain":"..."}]`
3. Netscape `cookies.txt`（`curl -c`、多數「Export cookies」擴充套件的格式）

```bash
BB_COOKIE_FILE=~/cookies.txt ./bbpull doctor
./bbpull --cookie-file ~/cookies.json doctor
```

> **Windows 已儲存的密碼不能帶到 Linux。** 密碼用 Windows DPAPI 加密，綁定 Windows
> 帳號，Linux 無法解密；Linux 版第一次使用時要重新輸入一次密碼。

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
│   ├── desktop.py       # 開資料夾、失敗對話框（Windows / macOS / Linux）
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
│   ├── secrets_store.py # 密碼加密儲存（DPAPI / 系統鑰匙圈）
│   └── paths.py         # 安全檔名、長路徑
├── installer/           # 圖示產生器、凍結版本進入點
├── tools/               # 建置腳本、機密掃描閘門
├── tests/               # 單元測試
├── bbpull.spec          # PyInstaller 設定（依平台產生 1 或 2 個執行檔）
├── bbpull.cmd           # Windows 啟動檔（優先使用 .venv）
└── bbpull.sh            # Linux／macOS 啟動檔（優先使用 .venv）
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
python -m bbpull selftest                # 681 個離線測試 + 機密掃描
python -m unittest tests.test_gui_qt     # 59 個 Qt 介面測試
python -m unittest tests.test_gui        # 66 個 Tk 介面測試（需 Tk）
```

Linux 上沒有系統 Tk 時，Tk 介面的測試會自動跳過（約 77 個），Qt 與引擎測試照常執行；
`tests/test_linux.py` 另外驗證 XDG 路徑、`xdg-open`、Qt 外掛偵測、`bbpull.sh`
與安裝腳本。

---

## 打包

```powershell
python tools\build_exe.py --version 0.1.2     # Windows：zip
```

```bash
python3 tools/build_exe.py --version 0.1.2    # Linux：tar.gz + install.sh
```

建置完會**實際執行**產物驗證（`--help`、`selftest`、`gui --check`、`venv`、
`gui --smoke`），不是只看 PyInstaller 有沒有回報成功。細節見
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#10-自行打包執行檔)。

---

## 授權

[MIT](LICENSE)

---

更深入的內容（為什麼用 Qt、效能的實測數據、打包流程、疑難排解、開發過程中修掉的問題）
見 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。
