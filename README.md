# 外站票雷達

從**香港、曼谷、首爾**出發，自動掃描到**北美、歐洲、阿曼、中南美**的來回機票，
價格低於該航線常見價一定比例時通知你，並自動加上「台北 ⇄ 外站」的機票成本，
跟「台北直接出發」比價，告訴你這張外站票到底划不划算。

---

## 五分鐘上手

### 1. 安裝

```bash
pip install -r requirements.txt
```

### 2. 先空跑一次（不需任何金鑰、不連網）

```bash
python flight_radar.py --demo
```

會用假資料跑完整流程，讓你先看報告長什麼樣、確認環境沒問題。

### 3. 申請 Travelpayouts token（免費，必填）

1. 到 <https://www.travelpayouts.com> 註冊
2. 進 <https://app.travelpayouts.com/profile/api-token> 複製 API token

### 4. 正式執行

```bash
export TP_TOKEN="你的token"
python flight_radar.py
```

報告會印在畫面上，同時寫進 `reports/latest.md`。

---

## 選配：讓它更好用

### Telegram 通知

1. 在 Telegram 找 `@BotFather`，送 `/newbot`，拿到 bot token
2. 對你的新 bot 隨便說一句話
3. 開 `https://api.telegram.org/bot<你的token>/getUpdates`，找到 `"chat":{"id":123456789}`

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="123456789"
```

### Google Flights 即時複查（SerpApi）

Travelpayouts 給的是**快取價**，拿來判斷「相對便宜」很好用，但不保證現在買得到。
設定 SerpApi 後，程式會拿排名最前的幾筆去 Google Flights 查即時價格，
報告裡會多一行「Google Flights 即時：NT$xx,xxx（常見區間 …）」。

1. 到 <https://serpapi.com> 註冊（免費方案每月約 100 次查詢）
2. `export SERPAPI_KEY="..."`

免費額度很少，所以 `config.yaml` 裡預設每次只複查前 3 筆（`verify.max_per_run`）。

### 每天自動跑（GitHub Actions，免費、不用開電腦）

1. 把這個資料夾推到一個 **private** GitHub repo
2. repo → Settings → Secrets and variables → Actions → New repository secret，
   加入 `TP_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`（`SERPAPI_KEY` 選配）
3. `.github/workflows/radar.yml` 已設好每天台灣時間早上 9 點執行，
   並會把累積的價格資料庫存回 repo（歷史越久，判斷越準）

---

## 調整設定

全部在 `config.yaml`，改完存檔即可，不用動程式。

| 設定 | 意思 |
|---|---|
| `origins` | 外站起點。目前是香港 HKG、曼谷 BKK、首爾 ICN |
| `destinations` | 目的地，依區域分組，想加城市就加一行 IATA 代碼 |
| `months_ahead` | 掃描未來幾個月的出發日期（預設 6） |
| `trip_days` | 只看幾天到幾天的來回票（預設 7～30 天） |
| `deal_threshold_pct` | 便宜多少 % 才通知（預設 20） |
| `baseline_window_days` | 用過去幾天的資料算常見價（預設 60） |
| `request_delay_sec` | 每次 API 呼叫間隔，怕被限流就調大 |

想少跑一點、快一點：把 `months_ahead` 調到 3，或砍掉幾個目的地。
目前預設約 112 條航線 × 6 個月 ≈ 690 次 API 呼叫，一次執行大約 10～15 分鐘。

---

## 報告怎麼看

```
🔥 香港 HKG → 利馬 LIM（中南美）
  01/01–01/14（13天）｜CX 轉機 去0/回1
  票價 NT$23,300，比常見價 NT$44,200 便宜 47%（基準：18 天 / 2,140 筆）
  ＋台北⇄香港 估 NT$3,200 → 總計約 NT$26,500
  台北出發同月最低 NT$35,300 → 省 NT$8,800
  💡 CI 是台灣籍航空，這張很可能經台北轉機（典型外站票，可考慮在台北停留）
```

- **常見價**：該航線近期所有日期組合的**中位數**，不是平均，比較不會被少數天價票拉歪
- **基準**：目前靠幾天、幾筆資料算出來的。第一次跑會顯示「僅今日 N 筆，尚無歷史」，
  這時判斷力最弱；累積一兩週後才會真正準
- **總計**：外站票價 ＋ 台北飛到該外站的最低價（同月）
- **台北出發同月最低**：拿來對照，如果「反而貴」就別折騰了
- **💡 台灣籍航空**：華航 CI／長榮 BR／星宇 JX 從外站出發，通常會經台北轉機，
  代表你可以在台北中停（stopover），是最典型的外站票用法

---

## 幾件要知道的事

**價格是快取價，不是即時價。**
Travelpayouts 回的是其他人近期搜到的價格，可能已經賣完或變動。
這支程式的定位是**雷達**：幫你發現「哪條航線、哪個月份現在不合理地便宜」，
真的要訂，請到航空公司官網或訂票網站確認。

**真正的外站票要自己組。**
典型外站票長這樣：`香港 → 台北（停留 N 天）→ 巴黎 → 台北 → 香港`。
這種多段票大部分 API 查不到，程式只能查「外站 → 目的地」的來回票。
所以流程是：程式幫你找到便宜的起點和月份 → 你到華航／長榮官網用「多個城市」
把台北中停組進去，實際票價常常比程式顯示的還好。

**別忘了外站票的隱形成本。**
除了台北飛外站的機票，還有簽證（有些目的地從不同起點規定不同）、
轉機住宿、行李規定、以及萬一班機延誤時兩段機票不同訂位代號不會互相保護。
程式算的「省 NT$X」只涵蓋機票本身。

**這不是投資或消費建議**，價格判斷僅供參考，下單前請自行確認條件與退改規定。

---

## 檔案結構

```
flight-radar/
├── flight_radar.py          # 主程式
├── config.yaml              # 設定（只改這個就好）
├── requirements.txt
├── .github/workflows/radar.yml   # 每天自動執行
├── data/prices.db           # 價格歷史（自動產生，越久越準）
└── reports/latest.md        # 最新一次報告
```
