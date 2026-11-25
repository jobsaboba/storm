#!/usr/bin/env python3
"""
storm.py — ⛈️ STORM win-only engine

The same Sandstorm selection sheet (UK & Irish all-weather handicaps,
course-&-distance winners dropping below their last winning mark, ran
inside 28 days, priced 3/1–33/1, shortest first, max three a race),
re-settled as a one-percent WIN-ONLY bankroll using the actual starting
prices shown on Sporting Life's result pages.

Money rules:
  * bank starts at £10.00
  * one stake per horse = 1% of the bank at the start of its race,
    rounded to the penny
  * win pays the SP (profit); every other finishing position loses the
    stake. There is no place return, no each-way split.
  * every Sandstorm qualifier is eligible; the 8+ runner rule was an
    each-way requirement and does not apply to a win-only book
  * non-runners / withdrawn / abandoned / void = no bet
  * dead heats for first are settled at the proper share of the stake
  * all picks in one race use the same pre-race bank; the bank moves
    after the race, not after each horse

Historical picks are Sandstorm's stored `drafts-28` selections. Their
result-page `current_odds` are the SP shown on the result page. Morning
picks are published before the race and settle on later refreshes.

Data output (served by the PWA):
  data/<date>.json      day payload
  data/latest.json      latest day
  data/index.json       small app index
  data/season.json      season summary, months, chart

Usage
-----
  python3 pipeline/storm.py --rebuild          # first full build
  python3 pipeline/storm.py --settle --publish # cron: settle + push
  python3 pipeline/storm.py --publish          # morning: push day
  python3 pipeline/storm.py --summary          # print the season
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent.parent
# The compact result-page cache is shared with Sandstorm 1%: both books read
# the same Sporting Life pages, and there is no need to store them twice.
CACHE = Path(os.environ.get("STORM_CACHE",
                            Path.home() / "projects/sandstorm1/pipeline/cache"))
DATA = BASE / "data"
DRAFT_DIR = Path.home() / "wonderland" / "drafts-28"
WON_CACHE = Path.home() / "wonderland" / "cache"
SL = "https://www.sportinglife.com"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

BANK_START = 10.0
STAKE_PCT = 0.01
START_DATE = "2026-01-01"
COMMIT_NAME = "jobsaboba"
COMMIT_EMAIL = "160133851+jobsaboba@users.noreply.github.com"

_LAST_FETCH = [0.0]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def london_today() -> str:
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()


def now_iso() -> str:
    return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%dT%H:%M:%S%z")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def namekey(s: str) -> str:
    """Horse-name key: ignore case, country suffix and punctuation."""
    s = re.sub(r"\s*\([a-z]{2,3}\)\s*$", "", norm(s))
    return re.sub(r"[^a-z0-9]+", "", s)


def r2(x: float) -> float:
    """Money rounded to the penny, half-up (not banker's rounding)."""
    try:
        return float(Decimal(str(x)).quantize(Decimal("0.01"),
                                              rounding=ROUND_HALF_UP))
    except Exception:  # noqa: BLE001
        return round(x, 2)


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_odds(txt):
    """'9/2' -> 4.5 (profit multiple). Also handles EVS / decimals / '7/4F'."""
    if txt is None:
        return None
    s = str(txt).strip().upper().replace("£", "")
    if not s:
        return None
    if s in ("EVS", "EVENS", "EVEN"):
        return 1.0
    s = re.sub(r"[A-Z]+$", "", s)          # 7/4F -> 7/4
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            return float(a) / float(b)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d.isoformat()
        d += timedelta(days=1)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_json(path: Path, obj, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(obj, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")


def write_if_changed(path: Path, obj: dict, pretty: bool = False) -> bool:
    """Write only when the payload really changed, so quiet refreshes are
    no-ops for git. The `updated` stamp is ignored in the comparison and
    preserved on disk when nothing else moved."""
    old = read_json(path)
    if isinstance(old, dict) and isinstance(obj, dict):
        a = {k: v for k, v in obj.items() if k != "updated"}
        b = {k: v for k, v in old.items() if k != "updated"}
        if a == b:
            obj["updated"] = old.get("updated", obj.get("updated"))
            return False
    write_json(path, obj, pretty=pretty)
    return True


# --------------------------------------------------------------------------
# Sporting Life result pages
# --------------------------------------------------------------------------
def next_data(html: str) -> dict:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if not m:
        raise ValueError("no __NEXT_DATA__ payload")
    return json.loads(m.group(1))


def canonical_results_url(race_id) -> str | None:
    """Use the morning card's canonical URL so the result slug is exact."""
    p = WON_CACHE / f"won-{race_id}.html"
    if not p.exists():
        return None
    try:
        html = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    if not m:
        return None
    return m.group(1).replace("/racing/racecards/", "/racing/results/")


def race_urls(race: dict) -> list[str]:
    rid = race.get("race_id")
    dstr = race.get("date")
    meeting = race.get("meeting") or ""
    name = race.get("race_name") or ""
    urls = []
    canon = canonical_results_url(rid)
    if canon:
        urls.append(canon)
    urls.append(f"{SL}/racing/results/{dstr}/{slug(meeting)}/{rid}/{slug(name)}")
    urls.append(f"{SL}/racing/racecards/{dstr}/{slug(meeting)}/racecard/{rid}/x")
    out = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def fetch_html(url: str) -> str:
    headers = {"User-Agent": UA, "Accept-Language": "en-GB,en"}
    wait = 0.3 - (time.time() - _LAST_FETCH[0])
    if wait > 0:
        time.sleep(wait)
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=40)
            _LAST_FETCH[0] = time.time()
            if (r.status_code == 200 and len(r.text) > 19_000
                    and "__NEXT_DATA__" in r.text):
                return r.text
            last_err = f"HTTP {r.status_code} ({len(r.text)}B)"
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def compact_path(race_id) -> Path:
    return CACHE / f"race-{race_id}.json"


def load_compact(race_id):
    return read_json(compact_path(race_id))


def save_compact(c: dict) -> bool:
    """Save a compact result only when something other than the fetch
    timestamp changed, so live polling does not churn the repo."""
    path = compact_path(c["race_id"])
    old = read_json(path)
    if isinstance(old, dict):
        a = {k: v for k, v in c.items() if k != "fetched_at"}
        b = {k: v for k, v in old.items() if k != "fetched_at"}
        if a == b:
            return False
    write_json(path, c)
    return True


def compact_from_race(race: dict, source: str) -> dict:
    rs = race.get("race_summary") or {}
    stage = str(rs.get("race_stage") or "").upper()
    rides = []
    for rd in race.get("rides") or []:
        horse = rd.get("horse") or {}
        betting = rd.get("betting") or {}
        cd = None
        for stat in rd.get("race_history_stats") or []:
            if stat.get("type") == "CourseDistance":
                cd = as_int(stat.get("value"))
                break
        rides.append({
            "name": horse.get("name", ""),
            "status": (rd.get("ride_status") or "RUNNER").upper(),
            "pos": rd.get("finish_position"),
            "sp": betting.get("current_odds"),
            "hist": betting.get("historical_odds") or [],
            "jockey": (rd.get("jockey") or {}).get("name", ""),
            "or": rd.get("official_rating"),
            "cd": cd,
            "days": horse.get("last_ran_days"),
            "casualty": rd.get("casualty") or {},
        })
    places = as_int(race.get("number_of_placed_rides")) or 3
    finished = sum(1 for r in rides
                   if r["status"] == "RUNNER" and (as_int(r["pos"]) or 0) > 0)
    # RESULT can be provisional (only the first few placings populated), so
    # wait until every paid place is visible; WEIGHEDIN is always final.
    final = (stage in ("WEIGHEDIN", "FINAL")
             or (stage == "RESULT" and finished >= places))
    abandoned = stage in ("ABANDONED", "VOID", "CANCELLED")
    return {
        "race_id": race.get("race_summary_reference", {}).get("id")
                    if isinstance(race.get("race_summary_reference"), dict)
                    else None,
        "stage": stage,
        "places": race.get("number_of_placed_rides"),
        "forecast": race.get("betting_forecast") or "",
        "rides": rides,
        "final": bool(final),
        "abandoned": bool(abandoned),
        "source": source,
        "fetched_at": now_iso(),
    }


def compact_from_html(race_id):
    for name in (f"won-res-{race_id}.html", f"won-{race_id}.html"):
        p = WON_CACHE / name
        if not p.exists():
            continue
        try:
            html = p.read_text(encoding="utf-8", errors="ignore")
            race = next_data(html)["props"]["pageProps"].get("race")
            if not race:
                continue
            c = compact_from_race(race, name)
            c["race_id"] = race_id
            save_compact(c)
            return c
        except Exception:  # noqa: BLE001
            continue
    return None


def ensure_compact(race: dict, refresh: bool = False) -> dict:
    rid = str(race.get("race_id"))
    c = load_compact(rid)
    if c:
        if c.get("final") or c.get("abandoned"):
            return c
        if not refresh:
            return c

    # No compact yet: reuse the big morning/result HTML caches first.
    if not c:
        c = compact_from_html(rid)
        if c and (c.get("final") or c.get("abandoned") or not refresh):
            return c

    # Fetch the result page (or the racecard fallback).
    last_err = None
    for url in race_urls(race):
        try:
            html = fetch_html(url)
            payload = next_data(html)["props"]["pageProps"].get("race")
            if not payload:
                raise RuntimeError("no race payload")
            c = compact_from_race(payload, "live")
            c["race_id"] = rid
            c["fetched_at"] = now_iso()
            save_compact(c)
            return c
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    print(f"  ! result {rid} {race.get('meeting')} {race.get('time')}: "
          f"{last_err}", file=sys.stderr)
    if c:
        return c
    c = {
        "race_id": rid, "stage": "UNKNOWN", "places": None, "forecast": "",
        "rides": [], "final": False, "abandoned": False,
        "source": "unavailable", "fetched_at": now_iso(),
    }
    save_compact(c)
    return c


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------
def find_ride(compact: dict, horse_name: str):
    wanted = namekey(horse_name)
    if not wanted:
        return None
    for rd in compact.get("rides") or []:
        if namekey(rd.get("name")) == wanted:
            return rd
    return None


def forecast_map(text: str) -> dict:
    out = {}
    for m in re.finditer(r"([^,()]+?)\s*\(([^)]+)\)", text or ""):
        out[norm(m.group(1))] = m.group(2).strip()
    return out


def sp_for(compact: dict, ride, draft_odds):
    if ride and ride.get("sp"):
        v = parse_odds(ride.get("sp"))
        if v is not None:
            return v, str(ride.get("sp"))
    if ride:
        fm = forecast_map(compact.get("forecast") or "")
        txt = fm.get(norm(ride.get("name")))
        if txt:
            v = parse_odds(txt)
            if v is not None:
                return v, txt
    # Never settle on the morning pick price — the app is an SP book.
    return None, None


def stake_for(bank: float) -> float:
    if bank <= 0:
        return 0.0
    return r2(bank * STAKE_PCT)


def settle_race(race: dict, compact: dict, bank: float):
    """Return (selection payloads, race meta, bank after the race).

    Win-only: a winner pays SP; every other runner loses the stake. There
    is no place component, so finishing second or third is still a loss.
    """
    sels = race.get("selections") or []
    runners = [r for r in (compact.get("rides") or [])
               if str(r.get("status") or "RUNNER").upper() == "RUNNER"]
    n_runners = len(runners)
    places = as_int(compact.get("places"))
    if places is None or places <= 0:
        places = 4 if n_runners >= 16 else 3 if n_runners >= 8 else 2 if n_runners >= 5 else 1

    pos_counts = Counter()
    for rd in runners:
        p = as_int(rd.get("pos"))
        if p and p > 0:
            pos_counts[p] += 1

    stage = str(compact.get("stage") or "").upper()
    pending = not compact.get("final") and not compact.get("abandoned")
    abandoned = (bool(compact.get("abandoned"))
                 or stage in ("ABANDONED", "VOID", "CANCELLED")
                 or (not pending and n_runners == 0))
    no_rides = not compact.get("rides")

    rows = []
    for s in sels:
        rd = find_ride(compact, s.get("horse", ""))
        sp, sp_txt = sp_for(compact, rd, s.get("odds"))
        pos = as_int(rd.get("pos")) if rd else None
        status = str(rd.get("status") or "RUNNER").upper() if rd else None
        stake = 0.0
        ret = 0.0
        res = None
        note = None

        if abandoned:
            res, note = "VOID", "Race abandoned — no bet"
        elif status in ("NONRUNNER", "WITHDRAWN"):
            res, note = "NR", "Non-runner — no bet"
        elif stake_for(bank) <= 0:
            res, note = "NOBET", "Bank too small for a 1% stake"
        elif no_rides:
            stake = stake_for(bank)
            res, note = None, "Awaiting result"
        elif pending:
            stake = stake_for(bank)
            res, note = None, "Awaiting result"
        elif rd is None:
            res, note = "UNKNOWN", "Not found on result page"
        elif sp is None:
            res, note = "NOSP", "No starting price"
        else:
            stake = stake_for(bank)
            if pos in (None, 0):
                res, ret = "LOSE", -stake
                note = "Did not finish — loss"
            elif pos == 1:
                res = "WIN"
                ret = sp * stake
                if pos_counts.get(1, 1) > 1:
                    # Split stake: the winning share pays full SP, the rest loses.
                    n = pos_counts[1]
                    ret = stake * (sp - (n - 1)) / n
                    note = f"Dead heat for 1st ({n})"
            else:
                res, ret = "LOSE", -stake
                if pos <= places:
                    note = f"Finished {ordinal(pos)} — win-only, no place return"
                else:
                    note = f"Finished {ordinal(pos)} — unplaced"

        # Only settled, runnable selections have a real SP to show.
        show_sp = None if (pending or abandoned or status in
                           ("NONRUNNER", "WITHDRAWN", None)) else sp
        show_sp_txt = None if (pending or abandoned or status in
                               ("NONRUNNER", "WITHDRAWN", None)) else sp_txt
        rows.append({
            "rank": s.get("rank"),
            "horse": s.get("horse", ""),
            "jockey": s.get("jockey", ""),
            "cd": s.get("cd_history"),
            "days": s.get("days"),
            "mark": s.get("mark"),
            "or": s.get("or"),
            "ref_bha": s.get("ref_bha"),
            "odds": s.get("odds"),
            "sp": show_sp,
            "sp_txt": show_sp_txt,
            "pos": pos if pos not in (None, 0) else None,
            "placed": bool(pos and 1 < pos <= places),
            "res": res,
            "stake": r2(stake),
            "ret": r2(ret),
            "bank": None,
            "note": note,
            "dh": bool(pos and pos_counts.get(pos, 1) > 1) if pos else False,
        })

    bets = [x for x in rows if x["res"] in ("WIN", "LOSE") and x["stake"] > 0]
    total_ret = r2(sum(x["ret"] for x in bets))
    bank_after = r2(bank + total_ret)
    for x in rows:
        x["bank"] = bank_after

    meta = {
        "runners": n_runners,
        "place_terms": places,
        "settled": bool(compact.get("final")) and not abandoned,
        "void": abandoned,
        # A race can be unresolved while every pick is already a no-bet
        # (bank exhausted). Only call it pending if a stake is actually open.
        "pending": any(x["res"] is None for x in rows),
        "staked": r2(sum(x["stake"] for x in bets)),
        "profit": total_ret,
        "bets": len(bets),
        "wins": sum(1 for x in bets if x["res"] == "WIN"),
        "losses": sum(1 for x in bets if x["res"] == "LOSE"),
        "placed": sum(1 for x in rows if x.get("placed")),
        "nobets": sum(1 for x in rows if x["res"] == "NOBET"),
        "voids": sum(1 for x in rows if x["res"] == "VOID"),
        "nrs": sum(1 for x in rows if x["res"] == "NR"),
        "pending_sels": sum(1 for x in rows if x["res"] is None),
        "bank_after": bank_after,
    }
    return rows, meta, bank_after


# --------------------------------------------------------------------------
# day + season build
# --------------------------------------------------------------------------
def empty_day(dstr: str, bank: float) -> dict:
    return {
        "date": dstr,
        "updated": now_iso(),
        "bank": {"start": r2(bank), "stake": stake_for(bank), "end": r2(bank)},
        "summary": {
            "bets": 0, "wins": 0, "losses": 0, "placed": 0, "nobets": 0,
            "voids": 0, "nrs": 0, "pending": 0, "staked": 0.0,
            "profit": 0.0, "bank_start": r2(bank), "bank_end": r2(bank),
        },
        "races": [],
    }


def build_day(dstr: str, bank: float, refresh: bool = False):
    jf = DRAFT_DIR / dstr / "selections.json"
    drafts = read_json(jf) if jf.exists() else []
    if not drafts:
        return empty_day(dstr, bank), bank

    drafts = sorted(drafts, key=lambda r: (r.get("time") or "",
                                           r.get("meeting") or ""))
    day_start = bank
    races = []
    totals = Counter()
    staked = 0.0
    profit = 0.0

    for race in drafts:
        sels = race.get("selections") or []
        if not sels:
            continue
        compact = ensure_compact(race, refresh=refresh)
        rows, meta, bank = settle_race(race, compact, bank)
        races.append({
            "time": race.get("time"),
            "meeting": race.get("meeting"),
            "name": race.get("race_name"),
            "class": race.get("class"),
            "distance": race.get("distance"),
            "riders": race.get("riders"),
            "runners": meta["runners"],
            "place_terms": meta["place_terms"],
            "settled": meta["settled"],
            "void": meta["void"],
            "pending": meta["pending"],
            "bank_after": meta["bank_after"],
            "sel": rows,
        })
        totals["bets"] += meta["bets"]
        totals["wins"] += meta["wins"]
        totals["losses"] += meta["losses"]
        totals["placed"] += meta["placed"]
        totals["nobets"] += meta["nobets"]
        totals["voids"] += meta["voids"]
        totals["nrs"] += meta["nrs"]
        totals["pending"] += meta["pending_sels"]
        staked += meta["staked"]
        profit += meta["profit"]

    summary = {
        "bets": totals["bets"],
        "wins": totals["wins"],
        "losses": totals["losses"],
        "placed": totals["placed"],
        "nobets": totals["nobets"],
        "voids": totals["voids"],
        "nrs": totals["nrs"],
        "pending": totals["pending"],
        "staked": r2(staked),
        "profit": r2(profit),
        "bank_start": r2(day_start),
        "bank_end": r2(bank),
    }
    return {
        "date": dstr,
        "updated": now_iso(),
        "bank": {"start": r2(day_start), "stake": stake_for(day_start),
                 "end": r2(bank)},
        "summary": summary,
        "races": races,
    }, bank


def build_season(days: list[dict]) -> dict:
    bets = sum(d["summary"]["bets"] for d in days)
    wins = sum(d["summary"]["wins"] for d in days)
    losses = sum(d["summary"]["losses"] for d in days)
    placed = sum(d["summary"]["placed"] for d in days)
    nobets = sum(d["summary"]["nobets"] for d in days)
    voids = sum(d["summary"]["voids"] for d in days)
    nrs = sum(d["summary"]["nrs"] for d in days)
    pending = sum(d["summary"]["pending"] for d in days)
    staked = r2(sum(d["summary"]["staked"] for d in days))
    profit = r2(sum(d["summary"]["profit"] for d in days))
    bank_end = r2(days[-1]["bank"]["end"]) if days else BANK_START

    peak = BANK_START
    max_bank = BANK_START
    min_bank = BANK_START
    max_dd = 0.0
    bust_date = None
    for d in days:
        if bust_date is None and d["bank"]["end"] < 0.50:
            bust_date = d["date"]
        for rc in d.get("races") or []:
            b = rc.get("bank_after")
            if b is None:
                continue
            peak = max(peak, b)
            max_bank = max(max_bank, b)
            min_bank = min(min_bank, b)
            max_dd = max(max_dd, peak - b)

    months = {}
    for d in days:
        mk = d["date"][:7]
        m = months.setdefault(mk, {
            "month": mk, "bets": 0, "wins": 0, "losses": 0, "placed": 0,
            "nobets": 0, "voids": 0, "nrs": 0, "pending": 0,
            "staked": 0.0, "profit": 0.0, "bank_end": d["bank"]["end"],
            "days": 0,
        })
        for k in ("bets", "wins", "losses", "placed", "nobets", "voids",
                  "nrs", "pending"):
            m[k] += d["summary"][k]
        m["staked"] = r2(m["staked"] + d["summary"]["staked"])
        m["profit"] = r2(m["profit"] + d["summary"]["profit"])
        m["bank_end"] = d["bank"]["end"]
        m["days"] += 1

    month_rows = [months[k] for k in sorted(months)]
    active_days = [d for d in days if d["summary"]["bets"]]
    active_months = [m for m in month_rows if m["bets"]]
    best_day = max(active_days, key=lambda d: d["summary"]["profit"], default=None)
    worst_day = min(active_days, key=lambda d: d["summary"]["profit"], default=None)
    best_month = max(active_months, key=lambda m: m["profit"], default=None)
    worst_month = min(active_months, key=lambda m: m["profit"], default=None)

    summary = {
        "bets": bets, "wins": wins, "losses": losses, "placed": placed,
        "nobets": nobets, "voids": voids, "nrs": nrs, "pending": pending,
        "staked": staked, "profit": profit,
        "roi": round(100 * profit / staked, 2) if staked else 0.0,
        "win_rate": round(100 * wins / bets, 1) if bets else 0.0,
        "placed_rate": round(100 * placed / bets, 1) if bets else 0.0,
        "bank_start": BANK_START, "bank_end": bank_end,
        "max_bank": r2(max_bank), "min_bank": r2(min_bank),
        "max_drawdown": r2(max_dd),
        "bust_date": bust_date,
    }
    return {
        "year": "2026",
        "updated": now_iso(),
        "start": days[0]["date"] if days else START_DATE,
        "end": days[-1]["date"] if days else london_today(),
        "bank_start": BANK_START,
        "bank_end": bank_end,
        "meta": {
            "stake_pct": STAKE_PCT,
            "stake_model": "1% win-only: win pays SP; no place returns",
            "field_rule": "all Sandstorm qualifiers",
            "source": "Sporting Life result pages (SP/current odds)",
        },
        "summary": summary,
        "months": month_rows,
        "chart": [{"date": d["date"], "bank": d["bank"]["end"]} for d in days],
        "days": [d["date"] for d in days],
        "records": {
            "best_day": ({"date": best_day["date"], "profit": best_day["summary"]["profit"]}
                         if best_day else None),
            "worst_day": ({"date": worst_day["date"], "profit": worst_day["summary"]["profit"]}
                          if worst_day else None),
            "best_month": ({"month": best_month["month"], "profit": best_month["profit"]}
                           if best_month else None),
            "worst_month": ({"month": worst_month["month"], "profit": worst_month["profit"]}
                            if worst_month else None),
        },
    }


def build_all(refresh: bool = False) -> dict:
    end = london_today()
    start = date.fromisoformat(START_DATE)
    days = []
    bank = BANK_START
    total = (date.fromisoformat(end) - start).days + 1
    done = 0
    changed = 0
    for dstr in daterange(start, date.fromisoformat(end)):
        done += 1
        payload, bank = build_day(dstr, bank, refresh=refresh)
        days.append(payload)
        if write_if_changed(DATA / f"{dstr}.json", payload):
            changed += 1
        if done % 30 == 0 or done == total:
            s = payload["summary"]
            print(f"  {done}/{total} days · bank £{bank:.2f} · "
                  f"today {s['bets']} bets / {s['pending']} pending",
                  flush=True)

    season = build_season(days)
    latest = days[-1] if days else empty_day(end, BANK_START)
    if write_if_changed(DATA / "latest.json", latest):
        changed += 1
    if write_if_changed(DATA / "season.json", season):
        changed += 1
    if write_if_changed(DATA / "index.json", {
        "latest": latest["date"],
        "first": days[0]["date"] if days else START_DATE,
        "updated": season["updated"],
        "summary": season["summary"],
        "days": season["days"],
    }):
        changed += 1
    print(f"  {changed} app data file(s) changed.", flush=True)
    return season


# --------------------------------------------------------------------------
# git publish
# --------------------------------------------------------------------------
def run_git(*args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "-C", str(BASE), *args],
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return (p.stdout + p.stderr).strip()


def publish(no_push: bool = False) -> int:
    dirty = run_git("status", "--porcelain", "--", "data")
    if not dirty:
        print("App data unchanged — nothing to push.")
        return 0
    run_git("add", "data")
    latest = read_json(DATA / "latest.json") or {}
    run_git("-c", f"user.name={COMMIT_NAME}",
            "-c", f"user.email={COMMIT_EMAIL}",
            "commit", "-m",
            f"storm: settle {latest.get('date', '')} (auto)")
    print("Committed app data.")
    if no_push:
        return 0
    last_exc = None
    for attempt in range(3):
        try:
            run_git("push")
            print("Pushed to GitHub — the phone app pulls fresh on ⟳.")
            return 0
        except RuntimeError as exc:
            last_exc = exc
            time.sleep(3 * (attempt + 1))
    sys.stderr.write("PUSH FAILED after 3 tries — run manually:\n"
                     f"  cd {BASE} && git push\n\n{last_exc}\n")
    return 1


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def print_summary(season: dict) -> None:
    s = season["summary"]
    print(f"\n=== ⛈️ STORM · WIN ONLY · {season['start']} → {season['end']} ===")
    print(f"{s['bets']} bets · W {s['wins']} · L {s['losses']} · "
          f"win {s['win_rate']}% · placed {s['placed']} (unpaid)")
    print(f"staked £{s['staked']:.2f} · P/L £{s['profit']:+.2f} · "
          f"ROI {s['roi']:+.2f}% · bank £{s['bank_end']:.2f}")
    print(f"{s['nobets']} no-bets · {s['voids']} void · {s['nrs']} NR · "
          f"{s['pending']} pending · max drawdown £{s['max_drawdown']:.2f}")
    print(f"start £{s['bank_start']:.2f} · peak £{s['max_bank']:.2f} · "
          f"low £{s['min_bank']:.2f} · next stake £{stake_for(s['bank_end']):.2f}")
    if s.get("bust_date"):
        print(f"BANK EXHAUSTED {s['bust_date']} — below the 50p needed for a 1p stake")
    for m in season.get("months") or []:
        print(f"  {m['month']}  {m['bets']:4d} bets  "
              f"W{m['wins']:3d}/L{m['losses']:4d}  "
              f"P/L £{m['profit']:+8.2f}  bank £{m['bank_end']:.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Storm win-only bankroll engine")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild all days from caches, fetching missing results")
    ap.add_argument("--settle", action="store_true",
                    help="rebuild and refetch unresolved result pages")
    ap.add_argument("--publish", action="store_true", help="commit + push app data")
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    ap.add_argument("--refresh", action="store_true",
                    help="force refetch of result pages")
    ap.add_argument("--summary", action="store_true", help="print the season summary")
    args = ap.parse_args(argv)

    if args.summary and not (args.rebuild or args.settle or args.publish):
        season = read_json(DATA / "season.json")
        if not season:
            print("No season data yet — run --rebuild first.", file=sys.stderr)
            return 1
        print_summary(season)
        return 0

    refresh = args.refresh or args.settle
    season = build_all(refresh=refresh)
    print_summary(season)
    if args.publish:
        return publish(no_push=args.no_push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
