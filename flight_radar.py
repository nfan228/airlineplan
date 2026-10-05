#!/usr/bin/env python3
"""
外站票雷達 Flight Radar
- 從外站（香港/曼谷/首爾…）出發，掃描到北美、歐洲、阿曼、中南美的來回票
- 價格低於該航線常見價一定比例時，列為好價
- 自動加上「台北⇄外站」的估算成本，並和台北直接出發比價
- 可選：用 SerpApi (Google Flights) 即時複查
- 結果寫入 reports/latest.md，並可推播到 Telegram

環境變數：
  TP_TOKEN            Travelpayouts API token（必填，免費申請）
  SERPAPI_KEY         SerpApi 金鑰（選填，用來即時複查）
  TELEGRAM_BOT_TOKEN  Telegram 機器人 token（選填）
  TELEGRAM_CHAT_ID    Telegram 聊天 ID（選填）

用法：
  python flight_radar.py            # 正式執行
  python flight_radar.py --demo     # 用假資料試跑，不需任何金鑰、不連網
  python flight_radar.py --no-notify
"""
from __future__ import annotations

import argparse
import datetime as dt
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
except ImportError:  # demo 模式不需要 requests
    requests = None

BASE = Path(__file__).resolve().parent
TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_URL = "https://serpapi.com/search.json"
TW = dt.timezone(dt.timedelta(hours=8))


@dataclass
class Quote:
    origin: str
    dest: str
    depart: str
    ret: str
    price: float
    airline: str = ""
    transfers_out: int = 0
    transfers_back: int = 0
    link: str = ""

    @property
    def days(self) -> int:
        return (dt.date.fromisoformat(self.ret) - dt.date.fromisoformat(self.depart)).days

    @property
    def month(self) -> str:
        return self.depart[:7]


@dataclass
class Deal:
    q: Quote
    region: str
    baseline: float
    baseline_src: str
    positioning: float | None = None
    tpe_price: float | None = None
    live_price: float | None = None
    typical_range: list | None = None
    price_level: str | None = None
    notes: list = field(default_factory=list)

    @property
    def pct_below(self) -> float:
        return (1 - self.q.price / self.baseline) * 100

    @property
    def total(self) -> float | None:
        return self.q.price + self.positioning if self.positioning else None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------- 資料來源 ----------------

def _get_json(url, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=40)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                log(f"  ! 請求失敗 {params.get('origin', '')}->{params.get('destination', '')}: {e}")
                return None
            time.sleep(3)
    return None


def fetch_travelpayouts(token, origin, dest, month, currency, delay):
    params = {
        "origin": origin, "destination": dest, "departure_at": month,
        "one_way": "false", "direct": "false", "sorting": "price",
        "unique": "false", "currency": currency, "limit": 1000, "page": 1,
        "token": token,
    }
    js = _get_json(TP_URL, params)
    time.sleep(delay)
    out = []
    for d in (js or {}).get("data", []) or []:
        dep, ret = (d.get("departure_at") or "")[:10], (d.get("return_at") or "")[:10]
        if not dep or not ret or not d.get("price"):
            continue
        link = d.get("link") or ""
        out.append(Quote(origin, dest, dep, ret, float(d["price"]), d.get("airline", ""),
                         int(d.get("transfers") or 0), int(d.get("return_transfers") or 0),
                         f"https://www.aviasales.com{link}" if link else ""))
    return out


def fake_fetcher():
    """Demo 用：產生看起來合理的假價格"""
    rng = random.Random(42)
    base = {"北美": 26000, "歐洲": 30000, "阿曼": 22000, "中南美": 42000}

    def f(token, origin, dest, month, currency, delay, region=None):
        if region is None:  # 台北⇄外站
            center = {"HKG": 4500, "BKK": 7000, "ICN": 7500}.get(dest, 6000)
        else:
            center = base[region] * (1.15 if origin == "TPE" else 1.0)
        y, m = map(int, month.split("-"))
        out = []
        for _ in range(25):
            day = rng.randint(1, 28)
            dep = dt.date(y, m, day)
            ret = dep + dt.timedelta(days=rng.randint(5, 35))
            p = center * rng.uniform(0.85, 1.25)
            if rng.random() < 0.04:
                p = center * rng.uniform(0.55, 0.75)  # 偶爾出現好價
            out.append(Quote(origin, dest, dep.isoformat(), ret.isoformat(), round(p, -2),
                             rng.choice(["CX", "TG", "KE", "CI", "BR", "EK", "QR", "TK"]),
                             rng.randint(0, 2), rng.randint(0, 2), ""))
        return out
    return f


def serpapi_verify(key, deal: Deal, currency):
    q = deal.q
    params = {
        "engine": "google_flights", "departure_id": q.origin, "arrival_id": q.dest,
        "outbound_date": q.depart, "return_date": q.ret, "type": "1",
        "currency": currency.upper(), "hl": "zh-TW", "gl": "tw", "api_key": key,
    }
    js = _get_json(SERP_URL, params, tries=2)
    if not js:
        return
    flights = (js.get("best_flights") or []) + (js.get("other_flights") or [])
    prices = [f["price"] for f in flights if isinstance(f.get("price"), (int, float))]
    deal.live_price = min(prices) if prices else None
    pi = js.get("price_insights") or {}
    deal.typical_range = pi.get("typical_price_range")
    deal.price_level = pi.get("price_level")


# ---------------- 資料庫 ----------------

def open_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS quotes(
            run_date TEXT, origin TEXT, dest TEXT, depart TEXT, ret TEXT,
            price REAL, airline TEXT, transfers_out INT, transfers_back INT);
        CREATE INDEX IF NOT EXISTS idx_route ON quotes(origin, dest, run_date);
        CREATE TABLE IF NOT EXISTS notified(key TEXT PRIMARY KEY, run_date TEXT);
    """)
    return db


def save_quotes(db, run_date, quotes):
    db.execute("DELETE FROM quotes WHERE run_date=? AND origin=? AND dest=?",
               (run_date, quotes[0].origin, quotes[0].dest)) if quotes else None
    db.executemany("INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)",
                   [(run_date, q.origin, q.dest, q.depart, q.ret, q.price, q.airline,
                     q.transfers_out, q.transfers_back) for q in quotes])


def baseline_for(db, origin, dest, run_date, window, min_samples, batch):
    """回傳 (常見價, 說明)。常見價＝中位數。
    累積天數越多越可靠，說明文字會如實標示目前是靠幾天的資料。"""
    since = (dt.date.fromisoformat(run_date) - dt.timedelta(days=window)).isoformat()
    rows, days = [], set()
    for price, rd in db.execute(
            "SELECT price, run_date FROM quotes WHERE origin=? AND dest=? AND run_date>=?",
            (origin, dest, since)):
        rows.append(price)
        days.add(rd)
    if len(rows) >= min_samples:
        if len(days) <= 1:
            return statistics.median(rows), f"僅今日 {len(rows)} 筆，尚無歷史"
        return statistics.median(rows), f"{len(days)} 天 / {len(rows)} 筆"
    if len(batch) >= 5:
        return statistics.median(q.price for q in batch), f"本次 {len(batch)} 筆"
    return None, ""


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


def fmt(n):
    return f"NT${n:,.0f}"


def build_report(deals, cfg, names, run_date, scanned):
    lines = [f"✈️ 外站票雷達 {run_date}",
             f"掃描 {scanned} 條航線，找到 {len(deals)} 筆低於常見價 {cfg['deal_threshold_pct']}% 以上的票", ""]
    if not deals:
        lines.append("今天沒有特別便宜的票。")
    for d in deals:
        q = d.q
        dep, ret = dt.date.fromisoformat(q.depart), dt.date.fromisoformat(q.ret)
        lines.append(f"🔥 {names[q.origin]} {q.origin} → {names[q.dest]} {q.dest}（{d.region}）")
        lines.append(f"  {dep:%m/%d}–{ret:%m/%d}（{q.days}天）｜{q.airline or '?'} 轉機 去{q.transfers_out}/回{q.transfers_back}")
        lines.append(f"  票價 {fmt(q.price)}，比常見價 {fmt(d.baseline)} 便宜 {d.pct_below:.0f}%（基準：{d.baseline_src}）")
        if d.total:
            lines.append(f"  ＋台北⇄{names[q.origin]} 估 {fmt(d.positioning)} → 總計約 {fmt(d.total)}")
        if d.tpe_price:
            if d.total:
                diff = d.tpe_price - d.total
                word = "省" if diff > 0 else "反而貴"
                lines.append(f"  台北出發同月最低 {fmt(d.tpe_price)} → {word} {fmt(abs(diff))}")
            else:
                lines.append(f"  台北出發同月最低 {fmt(d.tpe_price)}")
        if d.live_price is not None or d.typical_range:
            s = f"  Google Flights 即時：{fmt(d.live_price) if d.live_price else '查無'}"
            if d.typical_range and len(d.typical_range) == 2:
                s += f"（常見區間 {d.typical_range[0]:,}–{d.typical_range[1]:,}"
                s += f"，{d.price_level}）" if d.price_level else "）"
            lines.append(s)
        for n in d.notes:
            lines.append(f"  💡 {n}")
        if q.link:
            lines.append(f"  {q.link}")
        lines.append("")
    lines.append("※ 掃描價格為快取價，訂票前請以航空公司官網為準。外站多段票請到官網用「多個城市」組合查詢。")
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
                          data={"chat_id": chat_id, "text": c, "disable_web_page_preview": "true"},
                          timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--demo", action="store_true", help="用假資料試跑")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cur = cfg.get("currency", "twd")
    home = cfg.get("home", "TPE")
    run_date = dt.datetime.now(TW).date().isoformat()
    mlist = months(cfg.get("months_ahead", 6))
    tmin, tmax = cfg["trip_days"]["min"], cfg["trip_days"]["max"]
    delay = cfg.get("request_delay_sec", 0.4)

    names = {home: "台北", **cfg["origins"]}
    region_of = {}
    for region, ds in cfg["destinations"].items():
        for code, name in ds.items():
            names[code] = name
            region_of[code] = region

    if args.demo:
        fetch = fake_fetcher()
        db_path = BASE / "data" / "demo.db"
        db_path.unlink(missing_ok=True)
        token = "demo"
    else:
        token = os.environ.get("TP_TOKEN")
        if not token:
            sys.exit("請先設定環境變數 TP_TOKEN（Travelpayouts API token）")
        fetch = None
        db_path = BASE / "data" / "prices.db"
    db = open_db(db_path)

    def get(origin, dest, month):
        if args.demo:
            return fetch(token, origin, dest, month, cur, delay, region_of.get(dest))
        return fetch_travelpayouts(token, origin, dest, month, cur, delay)

    # 1) 台北⇄外站 的估算成本（每月最低）
    positioning = {}
    for o in cfg["origins"]:
        for m in mlist:
            qs = [q for q in get(home, o, m) if q.days >= 3]
            if qs:
                positioning[(o, m)] = min(q.price for q in qs)
    log(f"台北⇄外站成本：取得 {len(positioning)} 筆")

    # 2) 掃描所有航線（外站起點 + 台北，用來比價）
    batches = {}
    for o in list(cfg["origins"]) + [home]:
        for dcode in region_of:
            qs = []
            for m in mlist:
                qs += get(o, dcode, m)
            qs = [q for q in qs if tmin <= q.days <= tmax]
            batches[(o, dcode)] = qs
            if qs:
                save_quotes(db, run_date, qs)
            log(f"{o}->{dcode}: {len(qs)} 筆")
    db.commit()

    # 3) 找好價
    thr = cfg["deal_threshold_pct"] / 100
    deals = []
    for (o, dcode), qs in batches.items():
        if o == home or not qs:
            continue
        base, src = baseline_for(db, o, dcode, run_date, cfg["baseline_window_days"],
                                 cfg["min_samples"], qs)
        if not base:
            continue
        best_per_month = {}
        for q in qs:
            if q.price <= base * (1 - thr):
                if q.month not in best_per_month or q.price < best_per_month[q.month].price:
                    best_per_month[q.month] = q
        tpe = batches.get((home, dcode), [])
        for q in best_per_month.values():
            d = Deal(q, region_of[dcode], base, src)
            d.positioning = positioning.get((o, q.month))
            same_month = [t.price for t in tpe if t.month == q.month]
            d.tpe_price = min(same_month) if same_month else None
            if q.airline in cfg.get("taiwan_carriers", []):
                d.notes.append(f"{q.airline} 是台灣籍航空，這張很可能經台北轉機（典型外站票，可考慮在台北停留）")
            deals.append(d)

    # 4) 去掉已通知過的
    fresh = []
    for d in deals:
        key = f"{d.q.origin}-{d.q.dest}-{d.q.depart}-{d.q.ret}-{round(d.q.price, -2):.0f}"
        if db.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone():
            continue
        fresh.append((key, d))
    fresh.sort(key=lambda kd: -kd[1].pct_below)
    fresh = fresh[: cfg.get("max_deals_in_report", 15)]

    # 5) 即時複查
    serp_key = os.environ.get("SERPAPI_KEY")
    if cfg.get("verify", {}).get("enabled") and serp_key and not args.demo:
        for _, d in fresh[: cfg["verify"].get("max_per_run", 3)]:
            serpapi_verify(serp_key, d, cur)

    report = build_report([d for _, d in fresh], cfg, names, run_date, len(batches))
    out = BASE / "reports"
    out.mkdir(exist_ok=True)
    (out / ("demo.md" if args.demo else "latest.md")).write_text(report, encoding="utf-8")
    print(report)

    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not args.demo and not args.no_notify and tg_token and tg_chat and fresh:
        send_telegram(tg_token, tg_chat, report)
        log("已推播到 Telegram")

    if not args.demo:
        db.executemany("INSERT OR IGNORE INTO notified VALUES(?,?)", [(k, run_date) for k, _ in fresh])
        db.execute("DELETE FROM quotes WHERE run_date < ?",
                   ((dt.date.fromisoformat(run_date) - dt.timedelta(days=180)).isoformat(),))
        db.commit()


if __name__ == "__main__":
    main()
