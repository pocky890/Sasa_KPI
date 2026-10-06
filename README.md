# Sasa_KPI — 採購分析報告產生器

讀取採購明細 Excel/CSV，產生 HTML 分析報告（下單量、採購人員達交率、毛利率）。
原本是 n8n workflow，現改為本機執行。

## 使用方式

### 視窗版（Windows exe）
雙擊 `採購分析報告產生器.exe`（見 Releases），選資料檔 → 設定年度 → 產生報告。

### 從原始碼執行
```bash
pip install -r requirements.txt
python app.py                         # 視窗版
python analyze.py "資料.xlsx" -o out  # 命令列版
```
輸出：`採購分析報告.html`、`analysis.json`。

## 資料格式
只計「狀態」= `已確認` 的列。需要的欄位：
狀態、採購日期、標準交貨日期、廠商交貨日期、料件編號、採購供應商、採購人員、品名、
分批採購數量、採購未稅金額、銷售金額(未稅)。
欄位名稱不符時會被視為沒有資料。

## 設定
年度、下單量定義（`lines`/`amount`/`qty`）可在視窗調整；其他參數（波動門檻 `MOM_THRESHOLD`、
前幾名等）在 `analyze.py` 開頭。

## 打包 exe
```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name 採購分析報告產生器 --exclude-module matplotlib --exclude-module scipy --exclude-module IPython app.py
```

> 採購資料不放進 repo（`data/`、`output/` 已被忽略）。
