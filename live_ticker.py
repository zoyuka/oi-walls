#!/usr/bin/env python3
"""Live quote loop (runs in GitHub Actions during market hours).

Every ~75s: fetch latest prices + session VWAP for SPX/SPY/QQQ and force-push
a tiny live.json to the orphan `live` branch. The dashboard's client JS polls
that file via the GitHub contents API (CORS-enabled) and updates price
in-place — no page rebuilds, no Pages deploys.
"""
import datetime as dt
import json
import os
import subprocess
import time
from zoneinfo import ZoneInfo

import yfinance as yf

ET = ZoneInfo("America/New_York")
SYMS = [("^GSPC", "SPX"), ("SPY", "SPY"), ("QQQ", "QQQ"),
        ("NVDA", "NVDA"), ("TSLA", "TSLA"), ("AAPL", "AAPL"), ("META", "META"),
        ("ES=F", "ES"), ("NQ=F", "NQ")]  # futures quote ~23h/day — overnight/premarket feed

from collections import deque
import urllib.request

HIST = {}  # label -> deque[(epoch, px)] — rolling ~45 min so fresh page loads can
           # replay the gap between the last static build and now into candles

# ---- cloud level watcher: push wall/band touches to the phone even when the
# ---- app is closed, via ntfy (subscribe to this topic in the free ntfy app)
NTFY = "https://ntfy.sh/levels-drk-56c5e740"
LEVELS = {}      # label -> [(price, description)]
PREV = {}        # label -> previous px
FIRED = {}       # (label, price) -> last fired epoch
LV_TS = 0
FSC = {}         # label -> futures scale (ES->SPX, NQ->QQQ) scraped from the site,
                 # so overnight futures moves can be watched against cash levels
RATIO = {}       # "SPX" -> SPX/SPY spot ratio from the same build: ^GSPC barely
                 # streams on the websocket, so SPX rides SPY's sub-second ticks

# ---- native Web Push: the installed Home-Screen app's own push channel (iOS
# ---- 16.4+). Tapping a push opens the app itself. Subscriptions arrive on a
# ---- public dead-drop topic (page re-posts each open) and persist in live.json;
# ---- pushes are useless without the VAPID private key held in Actions secrets.
WPSUBS = []
APP_URL = "https://zoyuka.github.io/oi-walls/"

def _wp_send(subs, title, body):
    if not subs or not os.environ.get("VAPID_PRIVATE"):
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 0
    ok, dead = 0, []
    for s in subs:
        try:
            webpush(subscription_info=s,
                    data=json.dumps({"title": title, "body": body, "url": APP_URL}),
                    vapid_private_key=os.environ["VAPID_PRIVATE"].strip(),
                    vapid_claims={"sub": "mailto:derekyz123@gmail.com"},
                    ttl=300)
            ok += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(s)          # subscription expired/revoked
            print("webpush failed:", code, str(e)[:120], flush=True)
        except Exception as e:
            print("webpush failed:", repr(e)[:150], flush=True)
    for s in dead:
        try:
            WPSUBS.remove(s)
        except ValueError:
            pass
    if ok:
        print("webpush sent to", ok, "device(s):", title, flush=True)
    return ok

def load_subs():
    """Merge persisted subscriptions (previous shift's live.json) with anything
    new on the dead-drop; welcome-push genuinely new devices so enrollment is
    self-confirming end to end."""
    found = {}
    try:
        d = json.load(urllib.request.urlopen(
            f"https://raw.githubusercontent.com/zoyuka/oi-walls/live/live.json?x={int(time.time())}",
            timeout=15))
        for s in d.get("wps") or []:
            if isinstance(s, dict) and s.get("endpoint"):
                found[s["endpoint"]] = s
        if not QUIET:   # carry overnight held-alerts across shift restarts
            QUIET[:] = [q for q in (d.get("npend") or [])
                        if isinstance(q, list) and len(q) == 3
                        and time.time() - q[0] < 16 * 3600]
            if QUIET:
                print("restored", len(QUIET), "held overnight alerts", flush=True)
    except Exception:
        pass
    known = set(found) | {x.get("endpoint") for x in WPSUBS}
    try:
        raw = urllib.request.urlopen(NTFY + "-reg/json?poll=1&since=13h",
                                     timeout=15).read().decode()
        for ln in raw.splitlines():
            try:
                m = json.loads(ln)
                if m.get("event") != "message":
                    continue
                s = (json.loads(m.get("message", "")) or {}).get("sub") or {}
                if s.get("endpoint") and s.get("keys"):
                    found[s["endpoint"]] = s
            except Exception:
                continue
    except Exception as e:
        print("sub drop read failed:", e, flush=True)
    fresh = [s for ep, s in found.items() if ep not in known]
    for x in WPSUBS:                      # keep anything already live in memory
        found.setdefault(x.get("endpoint"), x)
    WPSUBS[:] = [s for s in found.values() if s]
    if WPSUBS:
        print("webpush subs:", len(WPSUBS), flush=True)
    if fresh:
        _wp_send(fresh, "Levels — native alerts armed",
                 "Tap this — it should open the Levels app directly. "
                 "Wall/band alerts arrive like this from now on.")

# ---- tick stream: same Yahoo websocket the page uses, run server-side so the
# ---- ntfy watcher reacts to individual prints instead of 45s-old 1m bars
WS_LAB = {"^GSPC": "SPX", "SPY": "SPY", "QQQ": "QQQ", "NVDA": "NVDA", "TSLA": "TSLA",
          "AAPL": "AAPL", "META": "META", "ES=F": "ES", "NQ=F": "NQ"}
TICK = {}        # label -> (epoch, px) latest accepted websocket print
TICK_N = {}      # label -> tick count since start (diagnostics)

def _pvar(u, i):
    v, s = 0, 0
    while True:
        b = u[i]; i += 1
        v |= (b & 127) << s
        if not (b & 128):
            break
        s += 7
        if s > 63 or i >= len(u):
            break
    return v, i

def _pdec(u):
    """Tolerant protobuf walk (port of the page's decoder): f1 id, f2 price."""
    import struct
    i, out = 0, {}
    while i < len(u):
        try:
            key, i = _pvar(u, i)
        except IndexError:
            break
        f, w = key >> 3, key & 7
        if w == 0:
            _, i = _pvar(u, i)
        elif w == 2:
            ln, i = _pvar(u, i)
            if f == 1:
                out["id"] = u[i:i + ln].decode("utf-8", "replace")
            i += ln
        elif w == 5:
            if i + 4 > len(u):
                break
            if f == 2:
                out["price"] = struct.unpack_from("<f", u, i)[0]
            i += 4
        elif w == 1:
            i += 8
        else:
            break
    return out

def on_tick(lab, px):
    prev = TICK.get(lab)
    ref = prev[1] if prev else (HIST[lab][-1][1] if HIST.get(lab) else None)
    if ref and abs(px / ref - 1) > 0.1:      # reject garbage decodes
        return
    TICK[lab] = (time.time(), px)
    TICK_N[lab] = TICK_N.get(lab, 0) + 1
    now = dt.datetime.now(ET)
    px = round(px, 2)
    if lab in ("ES", "NQ"):
        tgt = "SPX" if lab == "ES" else "QQQ"
        if FSC.get(tgt):
            watch(tgt, round(px * FSC[tgt], 2), now, fut=lab)
        return
    if lab == "SPX" and RATIO.get("SPX"):
        return                                # SPY-derived path owns SPX watching
    watch(lab, px, now, ext=True)
    if lab == "SPY" and RATIO.get("SPX"):     # SPX rides SPY's tick rate
        eq = round(px * RATIO["SPX"], 2)
        TICK["SPX"] = (time.time(), eq)
        watch("SPX", eq, now, ext=True)

def ws_loop():
    import base64
    try:
        import websocket
    except ImportError:
        print("websocket-client missing — tick stream off, 45s fallback only", flush=True)
        return
    while True:
        ws = None
        try:
            ws = websocket.create_connection(
                "wss://streamer.finance.yahoo.com/?version=2", timeout=30)
            ws.send(json.dumps({"subscribe": list(WS_LAB)}))
            print("ws connected", flush=True)
            ws.settimeout(60)
            while True:
                raw = ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if raw and raw[0] == "{":
                    try:
                        raw = json.loads(raw).get("message") or ""
                    except Exception:
                        continue
                if not raw:
                    continue
                try:
                    m = _pdec(base64.b64decode(raw))
                except Exception:
                    continue
                lab = WS_LAB.get(m.get("id") or "")
                px = m.get("price")
                if lab and px and px == px and px > 0:
                    on_tick(lab, float(px))
        except Exception as e:
            print("ws dropped:", repr(e), flush=True)
            try:
                ws and ws.close()
            except Exception:
                pass
            time.sleep(5)

def load_levels():
    """Fetch today's morning map (walls + day-band edges) from the repo."""
    global LEVELS, LV_TS
    for back in (0, 1, 2, 3):
        day = (dt.datetime.now(ET) - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        url = f"https://raw.githubusercontent.com/zoyuka/oi-walls/main/data/archive/{day}_open.json?x={int(time.time())}"
        try:
            d = json.load(urllib.request.urlopen(url, timeout=15))["tickers"]
        except Exception:
            continue
        try:
            if d.get("SPX", {}).get("spot") and d.get("SPY", {}).get("spot"):
                RATIO["SPX"] = d["SPX"]["spot"] / d["SPY"]["spot"]
        except Exception:
            pass
        out = {}
        for lab, t in d.items():
            lv = []
            for w in t.get("calls", []):
                lv.append((float(w["k"]), f"{w['k']:g}C wall"))
            for w in t.get("puts", []):
                lv.append((float(w["k"]), f"{w['k']:g}P wall"))
            e = (t.get("ems") or {}).get("day")
            if e:
                lv.append((e[0] - e[1], f"lower day-band edge {e[0]-e[1]:.0f}"))
                lv.append((e[0] + e[1], f"upper day-band edge {e[0]+e[1]:.0f}"))
            out[lab] = lv
        LEVELS, LV_TS = out, time.time()
        print("levels loaded from", day, "-", {k: len(v) for k, v in out.items()}, flush=True)
        _load_fsc()
        return
    print("no level map available", flush=True)
    _load_fsc()

def _load_fsc():
    """Scrape the deployed page's futures scale so ES/NQ map onto cash levels."""
    global FSC
    try:
        import re
        url = ("https://raw.githubusercontent.com/zoyuka/oi-walls/main/docs/index.html"
               f"?x={int(time.time())}")
        h = urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "replace")
        m = re.search(r'const FSC = ({[^;]+});', h)
        if m:
            FSC = json.loads(m.group(1))
            print("fsc:", FSC, flush=True)
    except Exception as e:
        print("fsc fetch failed:", e, flush=True)

def notify(title, body):
    # JSON publish endpoint: whole payload is UTF-8 JSON, so em-dashes and any
    # unicode survive. (Header-based publish died on latin-1 header encoding —
    # every alert before 2026-08-20 was silently lost to that.)
    try:
        req = urllib.request.Request(
            "https://ntfy.sh",
            data=json.dumps({"topic": NTFY.rsplit("/", 1)[1], "title": title,
                             "message": body, "priority": 5,   # urgent: trader interrupt
                             "tags": ["vertical_traffic_light"],
                             "click": "https://zoyuka.github.io/oi-walls/",
                             "actions": [{"action": "view", "label": "Open Levels",
                                          "url": "https://zoyuka.github.io/oi-walls/",
                                          "clear": True}]}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print("pushed:", title, "|", body, flush=True)
    except Exception as e:
        print("ntfy failed:", e, flush=True)
    _wp_send(WPSUBS, title, body)   # native channel rides every alert

# ---- quiet hours: level detection runs all night, but the phone only makes
# ---- noise 7am-8pm ET weekdays. Overnight touches log silently and arrive as
# ---- one recap at 7am, so gap nights are documented without costing sleep.
QUIET = []   # held alerts: [epoch, title, body]

def audible(now):
    return now.weekday() < 5 and 7 * 60 <= now.hour * 60 + now.minute < 20 * 60

def route(title, body, now):
    if audible(now):
        notify(title, body)
        return
    QUIET.append([int(time.time()), title, body])
    print("quiet-held:", title, flush=True)
    try:   # silent trail in ntfy's list (priority 1 = no sound, no banner)
        req = urllib.request.Request(
            "https://ntfy.sh",
            data=json.dumps({"topic": NTFY.rsplit("/", 1)[1],
                             "title": title + " · quiet", "message": body,
                             "priority": 1}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("quiet log failed:", e, flush=True)

def flush_quiet(now):
    if not QUIET or not audible(now):
        return
    QUIET[:] = [q for q in QUIET if time.time() - q[0] < 16 * 3600]
    if QUIET:
        lines = []
        for t_, ti, b in QUIET[-8:]:
            when = dt.datetime.fromtimestamp(t_, ET).strftime("%-I:%M%p").lower()
            lines.append(f"{when} {b.split(' — ')[0]}")
        head = f"{len(QUIET)} level touch{'es' if len(QUIET) > 1 else ''} while you slept"
        notify("Levels — overnight recap", head + ": " + "; ".join(lines))
    QUIET.clear()

def watch(label, px, now, fut=None, ext=False):
    """Fire when price crosses or first touches a mapped level (armed after one loop).
    fut="ES"/"NQ": px is a futures print mapped onto cash levels — watched only
    while cash is CLOSED (overnight/premarket), with a longer cooldown.
    ext=True: tick-sourced index/ETF print — also watched through extended hours
    (4am-8pm), where the real-time tick beats the 10-min-delayed futures feed."""
    pk = label + ("~f" if fut else "")
    was = PREV.get(pk)
    PREV[pk] = px
    m = now.hour * 60 + now.minute
    rth = 570 <= m < 960
    if fut:
        ok = not rth                     # futures cover whenever cash is closed
    elif ext and label in ("SPX", "SPY", "QQQ"):
        ok = 240 <= m < 1200             # liquid tickers: pre/post market too
    else:
        ok = rth
    if was is None or not ok or now.weekday() >= 5:
        return
    for lvl, desc in LEVELS.get(label, []):
        lo, hi = min(was, px), max(was, px)
        near = abs(px / lvl - 1) <= 0.0006
        crossed = lo <= lvl <= hi
        if not (near or crossed):
            continue
        q = 5 if lvl >= 2000 else 0.5
        bk = round(lvl / q) * q
        last = max(FIRED.get((label, bk - q), 0), FIRED.get((label, bk), 0),
                   FIRED.get((label, bk + q), 0))
        if time.time() - last < (3600 if fut else 1500):
            continue
        FIRED[(label, bk)] = time.time()
        down = px < was
        arrow = "↓" if down else "↑"
        if crossed and was != px:
            move = f"broke below {desc}" if down else f"reclaimed {desc}"
        elif px >= lvl:
            move = (f"testing {desc} from above" if down else f"holding above {desc}")
        else:
            move = (f"sliding under {desc}" if down else f"testing {desc} from below")
        if fut:
            route(f"Levels — {label} {arrow} (overnight)",
                  f"{fut}→{label} {arrow} {move} at {px:,.2f} — futures, cash closed. "
                  f"Open the ladder.", now)
        else:
            route(f"Levels — {label} {arrow}",
                  f"{label} {arrow} {move} at {px:,.2f} — open the app, check the planner", now)

def seed_fired():
    """On (re)start, read the topic's own recent history so an hourly loop restart
    doesn't re-push a level that already alerted minutes ago."""
    try:
        raw = urllib.request.urlopen(NTFY + "/json?poll=1&since=45m", timeout=15).read().decode()
        msgs = [json.loads(x) for x in raw.splitlines() if x.strip()]
        n = 0
        for lab, lvs in LEVELS.items():
            for lvl, desc in lvs:
                q = 5 if lvl >= 2000 else 0.5
                bk = round(lvl / q) * q
                for m in msgs:
                    if (m.get("event") == "message" and lab in m.get("title", "")
                            and desc in m.get("message", "")):
                        if m.get("time", 0) > FIRED.get((lab, bk), 0):
                            FIRED[(lab, bk)] = m["time"]
                            n += 1
        print("seeded", n, "recent alerts from topic history", flush=True)
    except Exception as e:
        print("seed_fired failed:", e, flush=True)

def snapshot():
    out = {}
    for sym, label in SYMS:
        try:
            px = yf.Ticker(sym).history(period="1d", interval="1m")
            if px.empty:
                continue
            spot = float(px.Close.iloc[-1])
            v = px.Volume.sum()
            if v > 0:
                tp = (px.High + px.Low + px.Close) / 3
                vwap = float((tp * px.Volume).sum() / v)
            else:
                vwap = None
            tk = TICK.get(label)
            fresh_tick = tk is not None and time.time() - tk[0] < 60
            if fresh_tick:
                spot = tk[1]   # live.json carries the tick-fresh price for the page
            out[label] = {"px": round(spot, 2), "vwap": None if vwap is None else round(vwap, 2)}
            HIST.setdefault(label, deque(maxlen=40)).append((int(time.time()), round(spot, 2)))
            if fresh_tick:
                pass           # the websocket owns level-watching for this label
            elif label not in ("ES", "NQ"):
                watch(label, round(spot, 2), dt.datetime.now(ET))
            else:
                tgt = "SPX" if label == "ES" else "QQQ"
                if FSC.get(tgt):
                    watch(tgt, round(spot * FSC[tgt], 2), dt.datetime.now(ET), fut=label)
        except Exception as e:
            print(label, "failed:", e)
    if out:
        out["h"] = {k: [[t, p] for t, p in v] for k, v in HIST.items()}
        now_ = time.time()
        out["diag"] = {"wsn": sum(TICK_N.values()),
                       "age": {k: round(now_ - v[0], 1) for k, v in TICK.items()}}
    return out

def push(payload):
    open("live.json", "w").write(json.dumps(payload))
    subprocess.run(["git", "add", "live.json"], check=True)
    subprocess.run(["git", "commit", "-q", "--amend", "--no-edit"], check=True)
    subprocess.run(["git", "push", "-q", "--force", "origin", "HEAD:live"], check=True)

def globex_open(now):
    """ES/NQ trade ~23h: Sun 6pm ET through Fri 5pm, with a 5-6pm daily break."""
    wd, mins = now.weekday(), now.hour * 60 + now.minute
    if wd == 5:
        return False                        # Saturday
    if wd == 6:
        return mins >= 18 * 60              # Sunday from 6pm ET
    if wd == 4 and mins >= 17 * 60:
        return False                        # Friday after 5pm ET
    return not (17 * 60 <= mins < 18 * 60)  # daily maintenance break

def main():
    subprocess.run(["git", "config", "user.name", "walls-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "checkout", "-q", "--orphan", "livebr"], check=True)
    subprocess.run("git rm -rfq --cached . && rm -rf docs data *.py *.md *.txt .github", shell=True)
    open("live.json", "w").write("{}")
    subprocess.run(["git", "add", "live.json"], check=True)
    subprocess.run(["git", "commit", "-qm", "live"], check=True)

    load_levels()
    seed_fired()
    load_subs()
    import threading
    threading.Thread(target=ws_loop, daemon=True).start()
    start = time.time()
    while True:
        now = dt.datetime.now(ET)
        if time.time() - LV_TS > 1800:   # map refreshes after the 8:15 build lands
            load_levels()
            load_subs()
        if not globex_open(now):
            print("globex closed — exiting", flush=True); break
        if time.time() - start > 3.4 * 3600:
            print("shift over — exiting (next scheduled run takes it)"); break
        flush_quiet(now)               # 7am: deliver the overnight recap once
        snap = snapshot()
        if snap:
            snap["ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
            if WPSUBS:
                snap["wps"] = WPSUBS   # persist enrollments across shift restarts
            if QUIET:
                snap["npend"] = QUIET  # held overnight alerts survive restarts too
            try:
                push(snap)
                print(now.strftime("%H:%M:%S"), {k: v["px"] for k, v in snap.items() if k != "ts"})
            except Exception as e:
                print("push failed:", e)
        time.sleep(45)

if __name__ == "__main__":
    main()
