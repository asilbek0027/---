#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EURUSD · Фибоначчи сигнал боти (H1)
===================================
Телеграмга СИГНАЛ юборади (ўзи савдо қилмайди) ва стратегияни тарихий маълумотда текширади (бэктест).
Талаб: фақат Python 3.8+ (pip ва қўшимча кутубхона керак эмас).

Буйруқлар:
  python fib_bot.py backtest [кунлар]          тарихий маълумотда текшириш (ЭНГ АВВАЛ шуни қилинг)
  python fib_bot.py backtest --csv data.csv    ўз маълумотингиз билан (time,open,high,low,close)
  python fib_bot.py chatid                     Телеграм chat_id ни топиш
  python fib_bot.py test                       Телеграмга синов хабари юбориш
  python fib_bot.py scan                       бир марта текшириш
  python fib_bot.py live                       ҳар соат автоматик текшириш

ДИҚҚАТ: бу молиявий маслаҳат эмас. Ҳеч қандай бот фойда ёки 90% аниқликни кафолатламайди.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ======================= СОЗЛАМАЛАР =======================
TELEGRAM_TOKEN = "8642251411:AAH6_Jx0_D6vu7iH9SxG5vcnDX9NTtiowrA"      # @BotFather берган токен
TELEGRAM_CHAT_ID = "7863808115"  # `chatid` буйруғи топиб беради

YAHOO_SYMBOL = "EURUSD=X"
PIP = 0.0001
SPREAD_PIPS = 1.0                      # бэктестда ҳар бир савдодан ушланади

EMA_FAST, EMA_SLOW, ATR_N = 50, 200, 14
SWING_K = 5                            # swing нуқта: ҳар икки томонда 5 шам
FIB_ENTRY = 0.618                      # кириш даражаси (0.5 / 0.618 / 0.786)
TP1_EXT, TP2_EXT = 0.27, 0.68          # Fib экстенсия: -0.27 ва -0.68
MIN_LEG_ATR, MAX_LEG_ATR = 3.0, 12.0   # импульс узунлиги (ATR бирлигида)
MAX_AGE = 48                           # swing high энг кўпи билан шунча шам олдин бўлсин
LEG_LOOKBACK = 60                      # swing low ни H дан олдин шунча шам ичида излаймиз
SL_BUFFER_ATR = 0.2                    # SL: swing low дан ATR*0.2 пастроққа
MIN_RR = 1.5                           # TP1 бўйича минимал фойда/зарар нисбати
ORDER_TTL = 24                         # лимит ордер амал қилиш муддати (шам)
MAX_HOLD = 96                          # очиқ савдо чегараси (шам), ундан кейин бозор нархида ёпилади
WARMUP = EMA_SLOW + 50

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADES_CSV = os.path.join(HERE, "backtest_trades.csv")


# ======================= МАЪЛУМОТ =======================
def http_json(url, params=None, data=None, timeout=30):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def load_yahoo(days=700):
    """H1 шамлар: [(unix_вақт, open, high, low, close), ...] — UTC, эскидан янгига."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + YAHOO_SYMBOL
    res = http_json(url, {"interval": "1h", "range": "%dd" % days})["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None not in (o, h, l, c):
            rows[int(ts)] = (int(ts), float(o), float(h), float(l), float(c))
    return [rows[k] for k in sorted(rows)]


def load_csv(path):
    """CSV: time,open,high,low,close (time — '2024-01-02 03:00:00' ёки unix секунд, UTC)."""
    rows = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            raw = next(r[k] for k in ("time", "datetime", "date", "timestamp") if k in r)
            try:
                ts = int(float(raw))
            except ValueError:
                dt = datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
            rows[ts] = (ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    return [rows[k] for k in sorted(rows)]


def only_closed(bars):
    """Ҳозир шаклланаётган (ёпилмаган) шамни олиб ташлаймиз."""
    now = time.time()
    return [b for b in bars if b[0] + 3600 <= now]


# ======================= ИНДИКАТОРЛАР =======================
def ema(values, n):
    k, prev, out = 2.0 / (n + 1), None, []
    for v in values:
        prev = v if prev is None else prev + k * (v - prev)
        out.append(prev)
    return out


def atr(H, L, C, n):
    prev, out = None, []
    for i in range(len(C)):
        if i == 0:
            tr = H[i] - L[i]
        else:
            tr = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
        prev = tr if prev is None else prev + (tr - prev) / n
        out.append(prev)
    return out


def swing_flags(hi, lo, k):
    """Swing high/low. i-чи нуқта фақат i+k шамдан кейин тасдиқланади (келажакка қарамаймиз)."""
    n = len(hi)
    swh, swl = [False] * n, [False] * n
    for i in range(k, n - k):
        if hi[i] > max(hi[i - k:i]) and hi[i] >= max(hi[i + 1:i + k + 1]):
            swh[i] = True
        if lo[i] < min(lo[i - k:i]) and lo[i] <= min(lo[i + 1:i + k + 1]):
            swl[i] = True
    return swh, swl


class View:
    """Бир йўналиш учун 'LONG кўриниши'. SHORT учун нархлар кўзгудек (минус билан) алмаштирилади,
    шунда битта код иккала йўналишга ишлайди. Ҳақиқий нарх = d * кўриниш нархи."""

    def __init__(self, bars, d):
        self.d = d
        self.T = [b[0] for b in bars]
        H = [b[2] for b in bars]
        L = [b[3] for b in bars]
        C = [b[4] for b in bars]
        self.hi = H if d == 1 else [-x for x in L]
        self.lo = L if d == 1 else [-x for x in H]
        self.cl = C if d == 1 else [-x for x in C]
        self.ef = [d * x for x in ema(C, EMA_FAST)]
        self.es = [d * x for x in ema(C, EMA_SLOW)]
        self.atr = atr(H, L, C, ATR_N)
        self.swh, self.swl = swing_flags(self.hi, self.lo, SWING_K)


# ======================= СТРАТЕГИЯ =======================
def find_setup(v, t):
    """t-чи шам ёпилганда Фибоначчи сетап борми? Фақат t гача бўлган маълумотдан фойдаланади.

    Қоида (LONG): тренд юқорига (close > EMA200 ва EMA50 > EMA200); энг сўнгги тасдиқланган
    swing high (H) ва ундан олдинги swing low (L) орасидаги импульс; нарх H дан ўтмаган ва
    Fib 0.618 даражасига ҳали тегмаган. Кириш — 0.618 даги лимит ордер, SL — L дан пастда,
    TP1/TP2 — H дан юқорида (экстенсия -0.27 / -0.68). SHORT — кўзгудек."""
    if t < WARMUP:
        return None
    if not (v.cl[t] > v.es[t] and v.ef[t] > v.es[t]):
        return None
    ih = next((i for i in range(t - SWING_K, t - MAX_AGE - 1, -1) if v.swh[i]), None)
    if ih is None:
        return None
    il = next((i for i in range(ih - 1, ih - LEG_LOOKBACK - 1, -1) if v.swl[i]), None)
    if il is None:
        return None
    top, bottom = v.hi[ih], min(v.lo[il:ih + 1])
    leg = top - bottom
    a = v.atr[ih]
    if not (MIN_LEG_ATR * a <= leg <= MAX_LEG_ATR * a):
        return None
    entry = top - FIB_ENTRY * leg
    if max(v.hi[ih + 1:t + 1]) > top or min(v.lo[ih + 1:t + 1]) <= entry:
        return None  # H ёки кириш даражаси аллақачон бузилган
    sl = bottom - SL_BUFFER_ATR * v.atr[t]
    tp1, tp2 = top + TP1_EXT * leg, top + TP2_EXT * leg
    if (tp1 - entry) / (entry - sl) < MIN_RR:
        return None
    return {"d": v.d, "time": v.T[t], "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
            "h": top, "key": "%d:%d:%d" % (v.d, v.T[il], v.T[ih])}


def simulate(v, s, start):
    """Битта сетап тақдирини start-чи шамдан бошлаб 'LONG кўриниши'да кузатади.
    Консерватив қоидалар: бир шамда SL ва TP бирга тегса — SL биринчи; ордер бажарилган шамда
    фақат SL текширилади; TP1 тегган шамда кириш нархига қайтса — қолган ярми нолда ёпилади.
    Натижа R (1R = кириш-SL масофаси) бирлигида. TP1 да позициянинг 50% ёпилади, SL кириш нархига
    сурилади (breakeven), қолган 50% TP2 гача."""
    n = len(v.hi)
    entry, sl, tp1, tp2, top = s["entry"], s["sl"], s["tp1"], s["tp2"], s["h"]
    risk = entry - sl
    r1, r2 = (tp1 - entry) / risk, (tp2 - entry) / risk
    fill, half, banked = None, False, 0.0

    def done(j, r, why):
        return {"status": "closed", "fill": fill, "end": j, "r": r, "why": why}

    for j in range(start, n):
        if fill is None:                       # ордер ҳали кутяпти
            if j >= start + ORDER_TTL:
                return {"status": "cancelled", "end": j}
            if v.lo[j] <= entry:
                fill = j
                if v.lo[j] <= sl:
                    return done(j, -1.0, "SL")
            elif v.hi[j] > top:                # бажарилмасдан нарх H дан ўтиб кетди
                return {"status": "cancelled", "end": j}
            continue
        if not half:
            if v.lo[j] <= sl:
                return done(j, -1.0, "SL")
            if v.hi[j] >= tp1:
                half, banked = True, 0.5 * r1
                if v.lo[j] <= entry:
                    return done(j, banked, "TP1+BE")
                if v.hi[j] >= tp2:
                    return done(j, banked + 0.5 * r2, "TP1+TP2")
        else:
            if v.lo[j] <= entry:
                return done(j, banked, "TP1+BE")
            if v.hi[j] >= tp2:
                return done(j, banked + 0.5 * r2, "TP1+TP2")
        if j - fill >= MAX_HOLD:
            px = (v.cl[j] - entry) / risk
            return done(j, banked + (0.5 if half else 1.0) * px, "TIME")
    if fill is None:
        return {"status": "pending", "end": n - 1}
    px = (v.cl[n - 1] - entry) / risk
    return {"status": "open", "fill": fill, "end": n - 1, "half": half,
            "r": banked + (0.5 if half else 1.0) * px, "why": "OPEN"}


# ======================= БЭКТЕСТ =======================
def run_backtest(bars):
    """Бир вақтда биттагина сигнал/савдо (худди жонли режимдагидек)."""
    views = {1: View(bars, 1), -1: View(bars, -1)}
    n, t = len(bars), WARMUP
    used, trades, signals = set(), [], 0
    while t < n - 1:
        s = None
        for d in (1, -1):
            c = find_setup(views[d], t)
            if c and c["key"] not in used:
                s = c
                break
        if s is None:
            t += 1
            continue
        used.add(s["key"])
        signals += 1
        v = views[s["d"]]
        r = simulate(v, s, t + 1)
        if r["status"] in ("closed", "open"):
            risk = s["entry"] - s["sl"]
            trades.append({
                "side": "BUY" if s["d"] == 1 else "SELL",
                "signal": v.T[t], "fill": v.T[r["fill"]], "exit": v.T[r["end"]],
                "entry": s["d"] * s["entry"], "sl": s["d"] * s["sl"],
                "tp1": s["d"] * s["tp1"], "tp2": s["d"] * s["tp2"],
                "why": r["why"], "r": r["r"] - SPREAD_PIPS * PIP / risk,
            })
        if r["status"] in ("pending", "open"):
            break
        t = max(r["end"], t + 1)
    return trades, signals


def stats(rs):
    n = len(rs)
    if n == 0:
        return None
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp, gl = sum(wins), -sum(losses)
    avg_w = gp / len(wins) if wins else 0.0
    avg_l = gl / len(losses) if losses else 0.0
    be_wr = avg_l / (avg_w + avg_l) if (avg_w + avg_l) > 0 else float("nan")
    eq = peak = mdd = 0.0
    streak = best = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        streak = streak + 1 if r <= 0 else 0
        best = max(best, streak)
    return {"n": n, "wr": len(wins) / n * 100, "be_wr": be_wr * 100, "exp": sum(rs) / n,
            "pf": gp / gl if gl > 0 else float("inf"), "tot": sum(rs), "mdd": mdd, "streak": best}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def report(trades, signals, bars):
    print(f"\n=== БЭКТЕСТ · EURUSD H1 · {iso(bars[0][0])[:10]} → {iso(bars[-1][0])[:10]} ({len(bars)} шам) ===")
    rs = [t["r"] for t in trades]
    s = stats(rs)
    if not s:
        print(f"Сигналлар: {signals}, лекин битта ҳам ордер бажарилмади. Маълумот кўпроқ бўлсин.")
        return
    tp1 = sum(1 for t in trades if t["why"].startswith("TP1")) / s["n"] * 100
    print(f"Сигналлар: {signals} · ордер бажарилди: {s['n']}")
    print(f"Win rate (R>0): {s['wr']:.1f}%   (нолга чиқиш учун керак: {s['be_wr']:.1f}%)")
    print(f"Ўртача натижа: {s['exp']:+.3f}R   Profit factor: {s['pf']:.2f}")
    print(f"Жами: {s['tot']:+.1f}R   Макс. просадка: {s['mdd']:.1f}R   Кетма-кет зарар: {s['streak']} та")
    print(f"TP1 га етган савдолар: {tp1:.0f}%")
    h = s["n"] // 2
    if h >= 5:
        a, b = stats(rs[:h]), stats(rs[h:])
        print(f"1-ярим: win rate {a['wr']:.0f}%, ўртача {a['exp']:+.2f}R  |  "
              f"2-ярим: win rate {b['wr']:.0f}%, ўртача {b['exp']:+.2f}R")
    print("(1R = Stop Loss гача бўлган хавф; спред ҳисобга олинган, комиссия — йўқ)")
    if s["n"] < 100:
        print("⚠ Савдолар сони 100 дан кам — натижа тасодифий бўлиши мумкин, унга ишонманг.")
    print("⚠ Ўтган натижа келажакни кафолатламайди. Win rate ни сунъий кўтариш осон (кичик TP, катта SL),")
    print("  лекин асосий кўрсаткичлар — ўртача R ва profit factor.")


def write_trades_csv(trades):
    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["signal_utc", "fill_utc", "exit_utc", "side", "entry", "sl", "tp1", "tp2", "result", "R_net"])
        for t in trades:
            w.writerow([iso(t["signal"]), iso(t["fill"]), iso(t["exit"]), t["side"],
                        "%.5f" % t["entry"], "%.5f" % t["sl"], "%.5f" % t["tp1"], "%.5f" % t["tp2"],
                        t["why"], "%.3f" % t["r"]])


def cmd_backtest(args):
    if "--csv" in args:
        bars = load_csv(args[args.index("--csv") + 1])
    else:
        days = int(args[0]) if args and args[0].isdigit() else 700
        print(f"Yahoo Finance дан {days} кунлик H1 маълумот юкланмоқда...")
        bars = load_yahoo(days)
    bars = only_closed(bars)
    if len(bars) < WARMUP + 100:
        print("Маълумот жуда кам.")
        return
    trades, signals = run_backtest(bars)
    report(trades, signals, bars)
    write_trades_csv(trades)
    print(f"Ҳар бир савдо алоҳида: {TRADES_CSV}")


# ======================= ТЕЛЕГРАМ =======================
def telegram_ready():
    return bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
    res = http_json(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    if not res.get("ok"):
        raise RuntimeError(str(res))


def fmt_signal(s, last_price):
    d = s["d"]
    p = lambda x: d * x
    risk = s["entry"] - s["sl"]
    side = "BUY LIMIT (олиш)" if d == 1 else "SELL LIMIT (сотиш)"
    beyond = "юқорига" if d == 1 else "пастга"
    return (
        f"📈 EURUSD · H1 · {side}\n"
        f"Кириш (Fib {FIB_ENTRY}): {p(s['entry']):.5f}\n"
        f"Stop Loss: {p(s['sl']):.5f} ({risk / PIP:.0f} пипс)\n"
        f"TP1 (Fib -{TP1_EXT}): {p(s['tp1']):.5f} → ярмини ёпинг, SL ни кириш нархига суринг\n"
        f"TP2 (Fib -{TP2_EXT}): {p(s['tp2']):.5f}\n"
        f"Фойда/зарар (TP1): {(s['tp1'] - s['entry']) / risk:.1f}\n"
        f"Ордер {ORDER_TTL} соат амал қилади. Нарх {p(s['h']):.5f} дан {beyond} чиқиб кетса — ордерни бекор қилинг.\n"
        f"Ҳозирги нарх: {last_price:.5f} · Сигнал (UTC): {iso(s['time'])}\n"
        f"⚠️ Кафолат йўқ. Аввал демо ҳисобда синанг; бир савдода депозитнинг 1–2% идан ортиғини хавфга қўйманг."
    )


def cmd_chatid():
    if not TELEGRAM_TOKEN:
        print("Аввал TELEGRAM_TOKEN ни киритинг (README.md га қаранг).")
        return
    res = http_json("https://api.telegram.org/bot%s/getUpdates" % TELEGRAM_TOKEN)
    chats = {}
    for u in res.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat")
        if c:
            chats[c["id"]] = c.get("username") or c.get("title") or c.get("first_name") or ""
    if not chats:
        print("Ҳеч нарса топилмади. Телеграмда ботингизга /start ёзиб, қайта уриниб кўринг.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


def cmd_test():
    if not telegram_ready():
        print("TELEGRAM_TOKEN ва TELEGRAM_CHAT_ID киритилмаган (README.md га қаранг).")
        return
    send_telegram("✅ EURUSD Fib бот уланди. Сигналлар шу ерга келади.")
    print("Синов хабари юборилди.")


# ======================= ЖОНЛИ РЕЖИМ =======================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("last", None)
    st.setdefault("used", [])
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def scan_once():
    bars = only_closed(load_yahoo(90))
    if len(bars) < WARMUP + 10:
        print("Маълумот етарли эмас.")
        return
    views = {1: View(bars, 1), -1: View(bars, -1)}
    st = load_state()
    last = st["last"]
    if last:  # олдинги сигнал ҳали фаол бўлса, янгисини излаймаймиз (бэктестдагидек)
        v = views[last["d"]]
        i = bisect_left(v.T, last["time"])
        if i < len(v.T) and v.T[i] == last["time"]:
            r = simulate(v, last, i + 1)
            if r["status"] in ("pending", "open"):
                print("Олдинги сигнал ҳали фаол (%s) — янги сигнал излаш тўхтатилди." % r["status"])
                return
    t = len(bars) - 1
    for d in (1, -1):
        s = find_setup(views[d], t)
        if s and s["key"] not in st["used"]:
            msg = fmt_signal(s, bars[-1][4])
            print(msg)
            if telegram_ready():
                try:
                    send_telegram(msg)
                except Exception as e:
                    print("Телеграмга юбориб бўлмади:", e)
                    return  # кейинги соатда қайта уриниб кўрамиз
            else:
                print("(Телеграм созланмаган — сигнал фақат экранга чиқарилди)")
            st["last"] = s
            st["used"] = (st["used"] + [s["key"]])[-100:]
            save_state(st)
            return
    print("Янги сигнал йўқ.")


def cmd_live():
    print("Бот ишга тушди (ҳар соат текширади). Тўхтатиш: Ctrl+C")
    while True:
        try:
            print(datetime.now().strftime("[%Y-%m-%d %H:%M]"), end=" ")
            scan_once()
        except Exception as e:
            print("Хато:", e)
        now = time.time()
        time.sleep(max(60, (int(now) // 3600 + 1) * 3600 + 90 - now))


# ======================= ИШГА ТУШИРИШ =======================
def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    try:
        if cmd == "backtest":
            cmd_backtest(a[1:])
        elif cmd == "scan":
            scan_once()
        elif cmd == "live":
            cmd_live()
        elif cmd == "test":
            cmd_test()
        elif cmd == "chatid":
            cmd_chatid()
        else:
            print(__doc__)
    except urllib.error.HTTPError as e:
        print(f"Тармоқ хатоси: HTTP {e.code}. Токенни ёки интернетни текширинг.")
    except urllib.error.URLError as e:
        print(f"Интернетга уланиб бўлмади: {e.reason}")
    except KeyboardInterrupt:
        print("\nТўхтатилди.")


if __name__ == "__main__":
    main()
