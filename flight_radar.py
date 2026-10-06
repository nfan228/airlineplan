#!/usr/bin/env python3
"""
外站票雷達 v2 — 真正的四腿中停票

找的是這種票：
    香港 → 台北（中停 N 天）→ 巴黎 → 台北（中停 N 天）→ 香港
由同一家台灣籍航空（長榮／華航／星宇）承運，台北⇄歐美那兩段直飛。

做法分兩階段：
  第一階段（Travelpayouts，免費，每天全掃）
      掃「台北 ⇄ 目的地」的直飛來回票，累積價格歷史、算出常見價，
      挑出目前明顯偏低的目的地與日期。沒有台北直飛的目的地會自動略過。
  第二階段（SerpApi / Google Flights 多段查詢，有免費額度限制）
      拿第一階段的前幾名，實際去查四腿票的真實票價，
      指定只要長榮／華航／星宇、只要直飛，再跟「台北直接來回」比價。
      另外加上「台北→外站」的單程機票（你得先飛去外站才能開這張票）。

環境變數：
  TP_TOKEN            Travelpayouts API token（必填，免費）
  SERPAPI_KEY         SerpApi 金鑰（第二階段必填，沒有就只跑第一階段）
  TELEGRAM_BOT_TOKEN  Telegram 機器人 token（選填）
  TELEGRAM_CHAT_ID    Telegram 聊天 ID（選填）

用法：
  python flight_radar.py            # 正式執行
  python flight_radar.py --demo     # 用假資料試跑，不需金鑰、不連網
  python flight_radar.py --no-notify
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    import requests
except ImportError:          # demo 模式不需要
    requests = None

BASE = Path(__file__).resolve().parent
TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_URL = "https://serpapi.com/search.json"
TW = dt.timezone(dt.timedelta(hours=8))


# ---------------- 資料結構 ----------------

@dataclass
class Quote:
    """台北 ⇄ 目的地 的一筆直飛來回報價"""
    dest: str
    depart: str
    ret: str
    price: float
    airline: str = ""
    link: str = ""

    @property
    def days(self) -> int:
        return (dt.date.fromisoformat(self.ret) - dt.date.fromisoformat(self.depart)).days

    @property
    def month(self) -> str:
        return self.depart[:7]


@dataclass
class Candidate:
    """第一階段挑出來、值得進一步查四腿票的目的地與日期"""
    q: Quote
    region: str
    baseline: float | None
    baseline_src: str
    fallback: bool = False       # 沒達到折扣門檻、只是「目前最便宜」

    @property
    def pct_below(self) -> float:
        if not self.baseline:
            return 0.0
        return (1 - self.q.price / self.baseline) * 100


@dataclass
class FourLeg:
    """第二階段查到的四腿票實際報價"""
    cand: Candidate
    outstation: str
    dates: list                  # [D1, D2, D3, D4]
    price: float | None = None
    airlines: list = field(default_factory=list)
    all_nonstop: bool = True
    positioning: float | None = None   # 台北→外站 單程（去開票）
    error: str = ""

    @property
    def total(self) -> float | None:
        if self.price is None:
            return None
        return self.price + (self.positioning or 0)

    @property
    def saving(self) -> float | None:
        if self.total is None:
            return None
        return self.cand.q.price - self.total


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fmt(n):
    return f"NT${n:,.0f}"


# ---------------- 第一階段：Travelpayouts ----------------

def _get_json(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                log(f"  ! 請求失敗: {e}")
                return None
            time.sleep(3)
    return None


def tp_roundtrips(token, origin, dest, month, currency, delay, direct=True):
    """台北 ⇄ 目的地 的來回票，預設只要直飛"""
    js = _get_json(TP_URL, {
        "origin": origin, "destination": dest, "departure_at": month,
        "one_way": "false", "direct": "true" if direct else "false",
        "sorting": "price", "currency": currency, "limit": 1000, "page": 1,
        "token": token,
    })
    time.sleep(delay)
    out = []
    for d in (js or {}).get("data", []) or []:
        dep, ret = (d.get("departure_at") or "")[:10], (d.get("return_at") or "")[:10]
        if not dep or not ret or not d.get("price"):
            continue
        link = d.get("link") or ""
        out.append(Quote(dest, dep, ret, float(d["price"]), d.get("airline", ""),
                         f"https://www.aviasales.com{link}" if link else ""))
    return out


def tp_oneway_min(token, origin, dest, month, currency, delay):
    """台北 → 外站 的單程最低價（你得先飛過去才能開那張票）"""
    js = _get_json(TP_URL, {
        "origin": origin, "destination": dest, "departure_at": month,
        "one_way": "true", "direct": "false", "sorting": "price",
        "currency": currency, "limit": 200, "page": 1, "token": token,
    })
    time.sleep(delay)
    prices = [float(d["price"]) for d in (js or {}).get("data", []) or [] if d.get("price")]
    return min(prices) if prices else None


# ---------------- 第二階段：SerpApi 多段查詢 ----------------

def serp_multicity(key, legs, carriers, nonstop, currency):
    """legs = [(from, to, date), ...]　回傳 (價格, [航空公司], 是否全程直飛, 錯誤訊息)"""
    params = {
        "engine": "google_flights",
        "type": "3",                                   # 3 = 多個城市
        "multi_city_json": json.dumps(
            [{"departure_id": a, "arrival_id": b, "date": d} for a, b, d in legs]),
        "include_airlines": ",".join(carriers),
        "currency": currency.upper(),
        "hl": "zh-TW", "gl": "tw",
        "api_key": key,
    }
    if nonstop:
        params["stops"] = "1"                          # 1 = 只要直飛

    js = _get_json(SERP_URL, params, tries=2)
    if not js:
        return None, [], True, "查詢失敗"
    if js.get("error"):
        return None, [], True, str(js["error"])

    options = (js.get("best_flights") or []) + (js.get("other_flights") or [])
    options = [o for o in options if isinstance(o.get("price"), (int, float))]
    if not options:
        return None, [], True, "查無符合條件的組合"

    best = min(options, key=lambda o: o["price"])
    segs = best.get("flights") or []
    airlines = []
    for s in segs:
        a = s.get("airline")
        if a and a not in airlines:
            airlines.append(a)
    # 四段行程若中間有轉機，segments 會多於 4 段
    return float(best["price"]), airlines, len(segs) <= len(legs), ""


# ---------------- 資料庫 ----------------

def open_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tpe_direct(
            run_date TEXT, dest TEXT, depart TEXT, ret TEXT,
            price REAL, airline TEXT);
        CREATE INDEX IF NOT EXISTS idx_tpe ON tpe_direct(dest, run_date);
        CREATE TABLE IF NOT EXISTS fourleg(
            run_date TEXT, outstation TEXT, dest TEXT,
            d1 TEXT, d2 TEXT, d3 TEXT, d4 TEXT,
            price REAL, airlines TEXT, tpe_rt REAL, positioning REAL);
        CREATE TABLE IF NOT EXISTS notified(key TEXT PRIMARY KEY, run_date TEXT);
    """)
    return db


def baseline_for(db, dest, run_date, window, min_samples, batch):
    """常見價＝中位數。說明文字會如實標示目前累積了幾天的資料"""
    since = (dt.date.fromisoformat(run_date) - dt.timedelta(days=window)).isoformat()
    rows, days = [], set()
    for price, rd in db.execute(
            "SELECT price, run_date FROM tpe_direct WHERE dest=? AND run_date>=?",
            (dest, since)):
        rows.append(price)
        days.add(rd)
    if len(rows) >= min_samples:
        if len(days) <= 1:
            return statistics.median(rows), f"僅今日 {len(rows)} 筆，尚無歷史"
        return statistics.median(rows), f"{len(days)} 天 / {len(rows)} 筆"
    if len(batch) >= 3:
        return statistics.median(q.price for q in batch), f"本次 {len(batch)} 筆"
    return None, ""


# ---------------- Demo 假資料 ----------------

def fake_sources():
    rng = random.Random(7)
    # 哪些目的地「有台北直飛」——用來驗證自動略過的邏輯
    nonstop = {"VIE", "AMS", "FRA", "MUC", "CDG", "LHR", "FCO", "MXP", "PRG",
               "LAX", "SFO", "SEA", "ONT", "JFK", "EWR", "YVR", "IAH", "ORD", "BNE"}
    center = {"歐洲": 34000, "北美": 29000, "大洋洲": 26000}

    def rt(token, origin, dest, month, currency, delay, direct=True, region=None):
        if dest not in nonstop:
            return []
        y, m = map(int, month.split("-"))
        c = center.get(region, 30000)
        out = []
        for _ in range(22):
            dep = dt.date(y, m, rng.randint(1, 28))
            ret = dep + dt.timedelta(days=rng.randint(6, 32))
            p = c * rng.uniform(0.88, 1.3)
            if rng.random() < 0.06:
                p = c * rng.uniform(0.62, 0.8)
            out.append(Quote(dest, dep.isoformat(), ret.isoformat(), round(p, -2),
                             rng.choice(["CI", "BR", "JX"])))
        return out

    def ow(token, origin, dest, month, currency, delay):
        return round({"HKG": 3400, "BKK": 5200, "ICN": 5600}.get(dest, 5000)
                     * rng.uniform(0.8, 1.3), -2)

    def mc(key, legs, carriers, nonstop_flag, currency, tpe_rt=30000):
        # 外站開票通常比台北來回便宜 1～4 成
        if rng.random() < 0.15:
            return None, [], True, "查無符合條件的組合"
        p = round(tpe_rt * rng.uniform(0.6, 0.95), -2)
        return p, [rng.choice(["長榮航空", "中華航空", "星宇航空"])], True, ""

    return rt, ow, mc


# ---------------- 報告 ----------------

def build_report(cfg, names, run_date, four_legs, candidates, skipped, scanned,
                 has_serp, fallback_used=False):
    st = cfg["stopover"]
    lines = [f"✈️ 外站票雷達 {run_date}",
             f"掃描 {scanned} 個目的地的台北來回票，挑出 {len(candidates)} 個候選", ""]
    if fallback_used:
        lines.append("※ 今天沒有任何一筆低於常見價的門檻，以下改列「目前最便宜」的日期，")
        lines.append("  讓四腿票查詢還是有東西可比。累積幾天歷史之後折扣判斷才會準。")
        lines.append("")

    if four_legs:
        lines.append("━━━ 四腿外站票實際報價 ━━━")
        lines.append("")
    for f in four_legs:
        q, o = f.cand.q, f.outstation
        d1, d2, d3, d4 = f.dates
        lines.append(f"💺 {names[o]} → 台北（停{st['days_before']}天）→ {names[q.dest]} "
                     f"→ 台北（停{st['days_after']}天）→ {names[o]}")
        lines.append(f"  {d1[5:]} {o}→TPE ｜ {d2[5:]} TPE→{q.dest} ｜ "
                     f"{d3[5:]} {q.dest}→TPE ｜ {d4[5:]} TPE→{o}")
        if f.price is None:
            lines.append(f"  查無報價（{f.error}）")
            lines.append("")
            continue
        lines.append(f"  {'／'.join(f.airlines) or '?'}"
                     + ("，全程直飛" if f.all_nonstop else "，⚠️ 中間有轉機"))
        lines.append(f"  四腿票 {fmt(f.price)}"
                     + (f" ＋ 台北→{names[o]}單程 {fmt(f.positioning)}（去開票）"
                        f" ＝ {fmt(f.total)}" if f.positioning else ""))
        lines.append(f"  台北直接來回同日期 {fmt(q.price)}"
                     f" → {'省' if f.saving > 0 else '反而貴'} {fmt(abs(f.saving))}")
        lines.append("")

    if not has_serp:
        lines.append("※ 沒有設定 SERPAPI_KEY，所以只跑了第一階段。")
        lines.append("  四腿票的實際票價需要 Google Flights 多段查詢才問得到。")
        lines.append("")

    lines.append("━━━ 台北來回便宜票（四腿票的候選日期）━━━")
    lines.append("")
    if not candidates:
        lines.append("今天完全沒抓到資料，請檢查 TP_TOKEN 是否正常。")
        lines.append("")
    for c in candidates:
        q = c.q
        dep, ret = dt.date.fromisoformat(q.depart), dt.date.fromisoformat(q.ret)
        lines.append(f"🔥 台北 → {names[q.dest]} {q.dest}（{c.region}）"
                     f"{names.get(q.airline, q.airline) or '?'}")
        lines.append(f"  {dep:%m/%d}–{ret:%m/%d}（{q.days}天）台北來回 {fmt(q.price)}")
        if c.baseline and not c.fallback:
            lines.append(f"  比常見價 {fmt(c.baseline)} 便宜 "
                         f"{c.pct_below:.0f}%（基準：{c.baseline_src}）")
        elif c.baseline:
            lines.append(f"  目前常見價 {fmt(c.baseline)}（基準：{c.baseline_src}）")
        else:
            lines.append("  資料太少，還算不出常見價")
        if q.link:
            lines.append(f"  {q.link}")
        lines.append("")

    if skipped:
        lines.append(f"略過（{'台灣籍航空' if cfg.get('require_taiwan_carrier', True) else ''}"
                     f"查無資料）：{'、'.join(names[d] for d in skipped)}")
        lines.append("")
    lines.append("※ 價格為快取價與搜尋結果，訂票前請到航空公司官網以「多個城市」重新確認。")
    lines.append("※ 外站票的中停天數、改票退票規定依票價條件而定，開票前請先看清楚。")
    return "\n".join(lines)


def send_telegram(token, chat_id, text):
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += block + "\n\n"
    chunks.append(cur)
    for c in chunks:
        if c.strip():
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": c,
                                "disable_web_page_preview": "true"}, timeout=30)


# ---------------- 主流程 ----------------

def months(n):
    today = dt.datetime.now(TW).date()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cur = cfg.get("currency", "twd")
    home = cfg.get("home", "TPE")
    run_date = dt.datetime.now(TW).date().isoformat()
    mlist = months(cfg.get("months_ahead", 6))
    tmin, tmax = cfg["trip_days"]["min"], cfg["trip_days"]["max"]
    delay = cfg.get("request_delay_sec", 0.7)
    st = cfg["stopover"]
    carriers = list(cfg["stopover_carriers"])

    names = {home: "台北", **cfg["outstations"], **cfg["stopover_carriers"]}
    region_of = {}
    for region, ds in cfg["destinations"].items():
        for code, name in ds.items():
            names[code] = name
            region_of[code] = region

    if args.demo:
        fake_rt, fake_ow, fake_mc = fake_sources()
        db_path = BASE / "data" / "demo.db"
        db_path.unlink(missing_ok=True)
        token, serp_key = "demo", "demo"
    else:
        token = os.environ.get("TP_TOKEN")
        if not token:
            sys.exit("請先設定環境變數 TP_TOKEN（Travelpayouts API token）")
        serp_key = os.environ.get("SERPAPI_KEY")
        db_path = BASE / "data" / "prices.db"
    db = open_db(db_path)

    # ---- 第一階段：台北 ⇄ 目的地 直飛來回 ----
    batches, skipped = {}, []
    for dcode in region_of:
        qs = []
        for m in mlist:
            qs += (fake_rt(token, home, dcode, m, cur, delay, region=region_of[dcode])
                   if args.demo else
                   tp_roundtrips(token, home, dcode, m, cur, delay,
                                 direct=cfg.get("direct_only", False)))
        qs = [q for q in qs if tmin <= q.days <= tmax]
        if cfg.get("require_taiwan_carrier", True):
            qs = [q for q in qs if q.airline in carriers]
        if not qs:
            skipped.append(dcode)
            log(f"{home}->{dcode}: 查無資料，略過")
            continue
        batches[dcode] = qs
        db.execute("DELETE FROM tpe_direct WHERE run_date=? AND dest=?", (run_date, dcode))
        db.executemany("INSERT INTO tpe_direct VALUES(?,?,?,?,?,?)",
                       [(run_date, q.dest, q.depart, q.ret, q.price, q.airline) for q in qs])
        log(f"{home}->{dcode}: {len(qs)} 筆")
    db.commit()

    # ---- 挑候選 ----
    thr = cfg["deal_threshold_pct"] / 100
    candidates, bases = [], {}
    for dcode, qs in batches.items():
        base, src = baseline_for(db, dcode, run_date, cfg["baseline_window_days"],
                                 cfg["min_samples"], qs)
        bases[dcode] = (base, src)
        if not base:
            continue
        best_per_month = {}
        for q in qs:
            if q.price <= base * (1 - thr):
                if q.month not in best_per_month or q.price < best_per_month[q.month].price:
                    best_per_month[q.month] = q
        for q in best_per_month.values():
            candidates.append(Candidate(q, region_of[dcode], base, src))
    candidates.sort(key=lambda c: -c.pct_below)

    # 沒有任何一筆達到折扣門檻時（剛開始沒歷史資料時很常見），
    # 改用「每個目的地目前最便宜的那一筆」當候選，讓第二階段還是有東西可查。
    fallback_used = False
    if not candidates and cfg.get("fallback_to_cheapest", True):
        fallback_used = True
        for dcode, qs in batches.items():
            q = min(qs, key=lambda x: x.price)
            base, src = bases.get(dcode, (None, ""))
            candidates.append(Candidate(q, region_of[dcode], base, src, fallback=True))
        # 用「相對於自己航線常見價的折扣」排序，比單純比絕對票價有意義，
        # 不然永遠只會挑到票價最低的那幾個目的地
        candidates.sort(key=lambda c: (-c.pct_below, c.q.price))

    candidates = candidates[: cfg.get("max_deals_in_report", 12)]
    log(f"候選 {len(candidates)} 筆" + ("（最便宜遞補）" if fallback_used else ""))

    # ---- 第二階段：四腿票實際報價 ----
    four_legs = []
    mc_cfg = cfg.get("multicity", {})
    has_serp = bool(serp_key)
    if mc_cfg.get("enabled") and has_serp and candidates:
        budget = mc_cfg.get("max_per_run", 3)
        pos_cache = {}
        for cand in candidates:
            if budget <= 0:
                break
            d2 = dt.date.fromisoformat(cand.q.depart)
            d3 = dt.date.fromisoformat(cand.q.ret)
            d1 = d2 - dt.timedelta(days=st["days_before"])
            d4 = d3 + dt.timedelta(days=st["days_after"])
            if d1 <= dt.datetime.now(TW).date():
                continue
            for o in cfg["outstations"]:
                if budget <= 0:
                    break
                budget -= 1
                legs = [(o, home, d1.isoformat()),
                        (home, cand.q.dest, d2.isoformat()),
                        (cand.q.dest, home, d3.isoformat()),
                        (home, o, d4.isoformat())]
                if args.demo:
                    price, airlines, nonstop, err = fake_mc(
                        serp_key, legs, carriers, mc_cfg.get("nonstop_only", True), cur,
                        cand.q.price)
                else:
                    price, airlines, nonstop, err = serp_multicity(
                        serp_key, legs, carriers, mc_cfg.get("nonstop_only", True), cur)
                    time.sleep(delay)
                f = FourLeg(cand, o, [d1.isoformat(), d2.isoformat(),
                                      d3.isoformat(), d4.isoformat()],
                            price, airlines, nonstop, None, err)
                if price is not None:
                    key = (o, d1.strftime("%Y-%m"))
                    if key not in pos_cache:
                        pos_cache[key] = (fake_ow(token, home, o, key[1], cur, delay)
                                          if args.demo else
                                          tp_oneway_min(token, home, o, key[1], cur, delay))
                    f.positioning = pos_cache[key]
                    db.execute("INSERT INTO fourleg VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (run_date, o, cand.q.dest, *f.dates, price,
                                ",".join(airlines), cand.q.price, f.positioning))
                four_legs.append(f)
                log(f"四腿 {o}->{cand.q.dest}: {price if price else err}")
        db.commit()
        four_legs.sort(key=lambda f: (f.saving is None, -(f.saving or 0)))

    report = build_report(cfg, names, run_date, four_legs, candidates,
                          skipped, len(batches), has_serp, fallback_used)
    out = BASE / "reports"
    out.mkdir(exist_ok=True)
    (out / ("demo.md" if args.demo else "latest.md")).write_text(report, encoding="utf-8")
    print(report)

    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not args.demo and not args.no_notify and tg_token and tg_chat:
        fresh = [f for f in four_legs if f.saving and f.saving > 0]
        new = []
        for f in fresh:
            k = f"{f.outstation}-{f.cand.q.dest}-{'-'.join(f.dates)}-{round(f.price, -2):.0f}"
            if not db.execute("SELECT 1 FROM notified WHERE key=?", (k,)).fetchone():
                new.append(k)
        if new:
            send_telegram(tg_token, tg_chat, report)
            db.executemany("INSERT OR IGNORE INTO notified VALUES(?,?)",
                           [(k, run_date) for k in new])
            log(f"已推播到 Telegram（{len(new)} 筆新的）")

    if not args.demo:
        db.execute("DELETE FROM tpe_direct WHERE run_date < ?",
                   ((dt.date.fromisoformat(run_date) - dt.timedelta(days=180)).isoformat(),))
        db.commit()


if __name__ == "__main__":
    main()
