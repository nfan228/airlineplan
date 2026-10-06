# 外站票雷達 v2

專門找這種票：

```
香港 → 台北（中停 2 天）→ 巴黎 → 台北（中停 2 天）→ 香港
```

由同一家台灣籍航空（長榮 BR／華航 CI／星宇 JX）承運，**台北⇄歐美那兩段直飛**。
你住台北，所以中間那段就是你真正要飛的旅程，前後兩段是為了把票價壓下來。

程式每天掃描，找出「哪個目的地、哪些日期、從哪個外站開票」最划算，
並且誠實地把「你得先飛去外站才能開票」的成本算進去。

---

## 運作邏輯

**第一階段（Travelpayouts，免費，每天全掃）**

掃「台北 ⇄ 目的地」的**直飛**來回票，累積價格歷史、算出各航線的常見價（中位數），
挑出目前明顯偏低的目的地與日期組合。

這一步同時有個副作用很好用：查不到直飛的目的地會自動略過。
所以你不需要自己維護「哪些城市台北有直飛」的清單，航線開了或停了程式會自己跟上。

**第二階段（SerpApi / Google Flights 多段查詢）**

拿第一階段的前幾名，實際去查四腿票的真實票價：

- 用 Google Flights 的多城市搜尋（`type=3`），一次送四段行程
- 指定 `include_airlines=BR,CI,JX`，只看得出台北中停的航空
- 指定 `stops=1`，四段全部只要直飛
- 再加上「台北 → 外站」的單程機票成本（你得先飛過去才能開那張票）
- 最後跟「台北直接來回」同日期的價格對比

報告長這樣：

```
💺 香港 → 台北（停2天）→ 巴黎 → 台北（停2天）→ 香港
  01-10 HKG→TPE ｜ 01-12 TPE→CDG ｜ 01-26 CDG→TPE ｜ 01-28 TPE→HKG
  中華航空，全程直飛
  四腿票 NT$19,400 ＋ 台北→香港單程 NT$3,200（去開票） ＝ NT$22,600
  台北直接來回同日期 NT$28,500 → 省 NT$5,900
```

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

### 3. 申請金鑰

| 環境變數 | 用途 | 必要性 |
|---|---|---|
| `TP_TOKEN` | Travelpayouts，第一階段的價格來源 | **必填**。免費，[個人資料頁](https://app.travelpayouts.com/profile/api-token)取得 |
| `SERPAPI_KEY` | [SerpApi](https://serpapi.com)，第二階段的外站票報價 | **這是核心功能**。免費方案每月 250 次查詢，不用信用卡，金鑰在 [manage-api-key](https://serpapi.com/manage-api-key) |
| `LINE_TOKEN` | 推播到 LINE | 選填，設定方式見下方 |
| `LINE_USER_ID` | 只推給自己（不設就用 broadcast） | 選填 |
| `TELEGRAM_BOT_TOKEN` | 推播到 Telegram | 選填 |
| `TELEGRAM_CHAT_ID` | 同上 | 選填 |

沒設 `SERPAPI_KEY` 的話程式一樣會跑，但只會告訴你「台北哪天便宜」，
查不到外站票的實際票價——而那才是你要的東西。

### 4. 執行

```bash
export TP_TOKEN="..."
export SERPAPI_KEY="..."
python flight_radar.py
```

---

## 推播到 LINE

舊的 LINE Notify 已於 2025 年 3 月底停止服務，現在要改用 Messaging API。
設定稍微麻煩，但只要做一次。台灣的免費方案每月 200 則，自用綽綽有餘
（這支程式一天最多推一兩則）。

1. 到 [LINE Developers](https://developers.line.biz/console/) 用你的 LINE 帳號登入
2. 建立一個 **Provider**（名字隨便取，例如 `personal`）
3. 在它底下建立一個 **Messaging API channel**，這會同時開一個 LINE 官方帳號
4. 進入該 channel 的 **Messaging API** 分頁：
   - 拉到最下面，**Channel access token (long-lived)** 按 Issue，複製那一長串
   - 上面有個 QR code，用手機 LINE 掃描，**把這個官方帳號加為好友**（這步不能漏，
     沒加好友的話推播會成功送出但你收不到）
5. 同一分頁把 **Auto-reply messages** 關掉，免得它每次都回罐頭訊息

然後把那串 token 設成 `LINE_TOKEN` 就好。

只設 `LINE_TOKEN` 的話程式用的是 **broadcast**，發給所有加這個官方帳號的好友——
自用情境下就是你一個人，省去查自己 userId 的麻煩。

如果這個官方帳號還有別人加好友、你想只發給自己，就到 channel 的 **Basic settings**
分頁最下面找 **Your user ID**，額外設定 `LINE_USER_ID`，程式會改成點對點推播。

LINE 和 Telegram 兩邊都設定的話會同時推。

### 通知門檻

`config.yaml` 裡的 `notify_min_saving` 預設 **3000**，意思是省不到三千就不吵你。
省一兩千的機會其實不值得特地飛去外站開票，光住宿和時間就吃掉了。
想看到所有結果就設 0，但會很吵。

不管有沒有推播，完整報告都會寫進 `reports/latest.md`。

---

## SerpApi 額度要怎麼用

免費方案每月 250 次查詢，所以 `config.yaml` 裡的 `multicity.max_per_run` 預設是 **6**，
一天 6 次、一個月 180 次，留一點餘裕給你手動測試。

每個目的地會用掉 **1 次對照組 ＋ 每個外站各 1 次** ＝ 目前共 4 次。

對照組那一次很重要：它查的是「同一批航空、同日期、從台北買」要多少。
第一階段抓到的市場最低價常常是外籍轉機票（經上海、伊斯坦堡那種），
拿那個去跟長榮的外站票比並不公平，會讓外站票永遠看起來很爛。

| max_per_run | 每天覆蓋 | 每月用量 |
|---|---|---|
| 4（預設） | 1 個目的地 | 約 120 次 |
| 8 | 2 個目的地 | 約 240 次（貼著上限） |

另一個做法：把 workflow 的 cron 改成每週跑兩三次，單次 `max_per_run` 就能放得更大，
一次看到更多目的地。

---

## 調整設定

全部在 `config.yaml`：

| 設定 | 意思 |
|---|---|
| `outstations` | 外站＝票的起訖點。預設香港、曼谷、首爾 |
| `stopover_carriers` | 可中停台北的航空。預設長榮 BR、華航 CI、星宇 JX |
| `stopover.days_before` / `days_after` | 台北中停幾天。預設前後各 2 天 |
| `destinations` | 目的地。沒有台北直飛的會自動略過，可以放寬一點 |
| `trip_days` | 歐美段去回程間隔幾天 |
| `deal_threshold_pct` | 台北直飛票比常見價便宜多少 % 才列為候選（預設 15） |
| `multicity.max_per_run` | 每次執行查幾組四腿票 |
| `multicity.nonstop_only` | 四段是否全部只要直飛（預設 true） |

---

## 幾件務必知道的事

**中停天數會影響票價，甚至影響票開不開得出來。**
很多票價條件對中停（stopover）有規定：有些只允許 24 小時內的轉機、
有些允許一次免費中停、有些中停要加錢。程式預設前後各停 2 天只是個起點，
查出來的價格請務必到航空公司官網用「多個城市」重新確認同樣的日期組合。

**你得先到外站才能開票。**
買香港出發的票，第一段是 HKG→TPE，所以你人要先在香港。
程式已經把「台北→香港」的單程最低價算進總成本，但實務上你還要考慮
那趟的時間、住宿，以及機票條件（有些票規定未搭乘第一段，後面全部作廢）。

**兩段機票不互相保護。**
「台北→香港」那張跟四腿票是兩個獨立訂位代號。如果前一段延誤導致你趕不上
HKG→TPE，航空公司沒有義務幫你處理。中間留足夠的緩衝時間。

**價格僅供參考。**
第一階段是快取價，第二階段是 Google Flights 的搜尋結果，兩者都可能在你下單前變動。
這支程式的定位是雷達，不是訂票系統，更不是消費建議。

---

## 檔案結構

```
flight-radar/
├── flight_radar.py          # 主程式
├── config.yaml              # 設定（只改這個就好）
├── requirements.txt
├── .github/workflows/radar.yml   # 每天自動執行
├── data/prices.db           # 價格歷史（累積越久，常見價越準）
└── reports/latest.md        # 最新一次報告
```
