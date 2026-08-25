#!/usr/bin/env python3
"""
espn_history.py — pull one ESPN fantasy season and measure what it can honestly tell you.

Answers three questions with your own league's data:

  1. Where was replacement level, really?      -> calibrates the draft model's baseline
  2. How big was the gap at each position?     -> where scarcity actually bit
  3. Whose projections held up?                -> where early picks are safe vs. where to wait

Question 3 is the one most people skip and the one that should change your draft.
A position where projections are accurate is a position where spending an early
pick is low-risk. A position where they are noise is one where you wait and take
volume, because nobody — including you — can tell the hits from the misses.

USAGE
  pip install espn-api
  python espn_history.py --demo                    # synthetic data, see the output shape
  python espn_history.py --pull                    # fetch your real season
  python espn_history.py --analyze espn_raw.json   # re-run analysis on saved data

CREDENTIALS
  ESPN has no official API. The v3 endpoints need your league ID and two cookies.
  Preferred: set them as environment variables, never in a file you might share:

    export ESPN_LEAGUE_ID=123456
    export ESPN_YEAR=2025
    export ESPN_S2='...'          # long token, no braces
    export ESPN_SWID='{...}'      # braces included

  Alternative: a local .env file (KEY=value, one per line) in this folder.
  Already covered by .gitignore so it can't get committed, but it's still a
  plaintext file sitting on disk -- only use this if you're the only one
  with access to this machine, and don't zip/share this folder afterward
  without deleting it first.

  Find the cookies: log into fantasy.espn.com, open developer tools,
  Application (Chrome) or Storage (Firefox) -> Cookies -> fantasy.espn.com.

  Treat espn_s2 like a password. It is a live session token for your ESPN account.
"""

import argparse
import json
import math
import os
import random
import statistics
import sys
from collections import defaultdict

VAL_POS = ["QB", "RB", "WR", "TE"]
FLEX_ELIG = ["RB", "WR", "TE"]


def _load_dotenv(path=".env"):
    """Optional local-only alternative to `export`-ing credentials in a shell.
    Only sets a variable if it isn't already in the real environment, so a
    real `export`/`$env:` always wins. Already covered by .gitignore -- this
    file is meant to never leave your machine."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# ----------------------------------------------------------------------------
# PULL
# ----------------------------------------------------------------------------

def pull_season():
    """Fetch a season from ESPN into a plain dict. Requires the espn-api package."""
    try:
        from espn_api.football import League
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install espn-api")

    league_id = os.environ.get("ESPN_LEAGUE_ID")
    year = os.environ.get("ESPN_YEAR")
    s2 = os.environ.get("ESPN_S2")
    swid = os.environ.get("ESPN_SWID")

    missing = [k for k, v in [("ESPN_LEAGUE_ID", league_id), ("ESPN_YEAR", year)] if not v]
    if missing:
        sys.exit("Set these environment variables first: " + ", ".join(missing))

    kwargs = {"league_id": int(league_id), "year": int(year)}
    if s2 and swid:
        kwargs["espn_s2"] = s2
        kwargs["swid"] = swid
    else:
        print("No cookies set — this only works if your league is public.", file=sys.stderr)

    try:
        lg = League(**kwargs)
    except Exception as e:
        sys.exit(
            "ESPN rejected the request: %s\n\n"
            "Usual causes: cookies expired (log in again and re-copy them), wrong league ID,\n"
            "or the league is private and you did not supply ESPN_S2 and ESPN_SWID." % e
        )

    slots = {}
    for slot, count in (getattr(lg.settings, "position_slot_counts", {}) or {}).items():
        if count:
            slots[slot] = count

    out = {
        "league_id": lg.league_id,
        "year": lg.year,
        "name": getattr(lg.settings, "name", ""),
        "teams": len(lg.teams),
        "roster_slots": slots,
        "weeks": [],
        "draft": [],
    }

    for p in getattr(lg, "draft", []) or []:
        out["draft"].append({
            "round": getattr(p, "round_num", None),
            "pick": getattr(p, "round_pick", None),
            "name": getattr(p, "playerName", ""),
            "team": getattr(getattr(p, "team", None), "team_name", ""),
        })

    # Regular season only. Playoff weeks have partial rosters and skew everything.
    last_week = getattr(lg.settings, "reg_season_count", 14)
    for wk in range(1, last_week + 1):
        entries = []
        try:
            boxes = lg.box_scores(wk)
        except Exception as e:
            print("Week %d unavailable (%s), skipping." % (wk, e), file=sys.stderr)
            continue
        for box in boxes:
            for lineup in (box.home_lineup, box.away_lineup):
                for pl in lineup:
                    entries.append({
                        "name": pl.name,
                        "pos": pl.position,
                        "slot": pl.slot_position,
                        "actual": float(pl.points or 0),
                        "projected": float(pl.projected_points or 0),
                    })
        out["weeks"].append({"week": wk, "entries": entries})
        print("  week %d: %d player-entries" % (wk, len(entries)), file=sys.stderr)

    return out


# ----------------------------------------------------------------------------
# ANALYZE
# ----------------------------------------------------------------------------

def analyze(data):
    teams = data["teams"]
    slots = data.get("roster_slots") or {}

    # ---- season totals and per-week actual/projected pairs ----
    totals = defaultdict(float)
    pos_of = {}
    pairs = defaultdict(list)          # pos -> [(projected, actual), ...]
    weekly = defaultdict(lambda: defaultdict(list))   # week -> pos -> [actual]
    season_pairs = defaultdict(dict)   # pos -> name -> {proj, act, n}

    for wk in data["weeks"]:
        for e in wk["entries"]:
            pos = e["pos"]
            if pos not in VAL_POS:
                continue
            pos_of[e["name"]] = pos
            totals[e["name"]] += e["actual"]
            weekly[wk["week"]][pos].append(e["actual"])
            if e.get("projected"):
                pairs[pos].append((e["projected"], e["actual"]))
                sp = season_pairs[pos].setdefault(e["name"], {"proj": 0.0, "act": 0.0, "n": 0})
                sp["proj"] += e["projected"]
                sp["act"] += e["actual"]
                sp["n"] += 1

    by_pos = defaultdict(list)
    for name, tot in totals.items():
        by_pos[pos_of[name]].append((name, round(tot, 1)))
    for pos in by_pos:
        by_pos[pos].sort(key=lambda x: -x[1])

    # ---- starters per position, flex allocated greedily by who is actually better ----
    base = {p: teams * slots.get(p, 0) for p in VAL_POS}
    flex_count = teams * (slots.get("RB/WR/TE", 0) + slots.get("FLEX", 0))
    for _ in range(flex_count):
        best, best_pts = None, -1e9
        for p in FLEX_ELIG:
            lst = by_pos.get(p, [])
            if base[p] < len(lst) and lst[base[p]][1] > best_pts:
                best_pts, best = lst[base[p]][1], p
        if not best:
            break
        base[best] += 1

    # ---- realized replacement level and spread ----
    replacement, spread = {}, {}
    for pos in VAL_POS:
        lst = by_pos.get(pos, [])
        if not lst:
            continue
        idx = min(base.get(pos, 0), len(lst) - 1)
        rep = lst[idx][1]
        replacement[pos] = {
            "starters_in_league": base.get(pos, 0),
            "replacement_player": lst[idx][0],
            "replacement_points": rep,
            "pool_size": len(lst),
        }
        top5 = statistics.mean([p for _, p in lst[:5]]) if len(lst) >= 5 else lst[0][1]
        spread[pos] = {
            "best": lst[0][1],
            "best_over_replacement": round(lst[0][1] - rep, 1),
            "top5_over_replacement": round(top5 - rep, 1),
        }

    # ---- projection accuracy ----
    # Two different questions live here and they must not be mixed:
    #   season-level  -> "can projections tell good players from bad ones?"  (drafting)
    #   weekly        -> "can projections tell good weeks from bad ones?"    (start/sit)
    # Weekly correlation is swamped by week-to-week variance, so using it to
    # decide draft strategy would understate how knowable a position is.
    accuracy = {}
    weeks_played = len(data["weeks"]) or 1
    for pos in VAL_POS:
        pr = [(p, a) for p, a in pairs.get(pos, []) if p > 0]
        if len(pr) < 30:
            continue
        errs = [abs(a - p) for p, a in pr]
        mean_actual = statistics.mean([a for _, a in pr])

        # season level: only players present for most of the year, so partial
        # seasons from injuries and waiver churn do not masquerade as bad forecasts
        full = [(n, s) for n, s in season_pairs.get(pos, {}).items() if s["n"] >= 0.6 * weeks_played]
        season_r = season_mae = None
        if len(full) >= 8:
            sp = [s["proj"] for _, s in full]
            sa = [s["act"] for _, s in full]
            season_r = spearman(sp, sa)
            season_mae = statistics.mean([abs(a - p) for p, a in zip(sp, sa)])

        accuracy[pos] = {
            "weekly_sample": len(pr),
            "mean_weekly_points": round(mean_actual, 1),
            "weekly_mae_pct": round(100 * statistics.mean(errs) / mean_actual, 1) if mean_actual else None,
            "weekly_correlation": round(correlation([p for p, _ in pr], [a for _, a in pr]) or 0, 3),
            "season_sample": len(full),
            "season_mae": round(season_mae, 1) if season_mae is not None else None,
            "season_rank_correlation": round(season_r, 3) if season_r is not None else None,
        }

    # ---- did draft order predict finish? the most decision-relevant thing here ----
    draft_roi = {}
    picks = data.get("draft") or []
    if picks:
        seen = defaultdict(list)
        for i, pk in enumerate(picks):
            nm = pk.get("name")
            if nm in totals:
                seen[pos_of[nm]].append((i + 1, totals[nm], nm))
        for pos, rows in seen.items():
            if len(rows) < 6:
                continue
            rows.sort(key=lambda r: r[0])
            r = spearman([x[0] for x in rows], [-x[1] for x in rows])
            n = max(3, teams // 3)
            early = set(nm for _, _, nm in rows[:n])
            finish = [nm for nm, _ in by_pos.get(pos, [])[:n]]
            draft_roi[pos] = {
                "drafted": len(rows),
                "order_vs_finish": round(r, 3) if r is not None else None,
                "top_n": n,
                "hit_rate": round(100.0 * len(early & set(finish)) / n, 0),
                "busts": [nm for nm in early if nm not in set(finish)][:4],
            }

    return {
        "league": data.get("name", ""),
        "year": data.get("year"),
        "teams": teams,
        "starters": base,
        "replacement": replacement,
        "spread": spread,
        "accuracy": accuracy,
        "draft_roi": draft_roi,
        "top10": {p: by_pos.get(p, [])[:10] for p in VAL_POS},
    }


def spearman(xs, ys):
    """Rank correlation — robust to the outlier seasons that distort a raw Pearson."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos + 1.0
        return r
    return correlation(ranks(xs), ranks(ys))


def correlation(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else None


# ----------------------------------------------------------------------------
# REPORT
# ----------------------------------------------------------------------------

def report(a):
    w = lambda s="": print(s)
    w()
    w("=" * 66)
    w("  %s  %s  (%d teams)" % (a["year"], a["league"] or "your league", a["teams"]))
    w("=" * 66)

    w()
    w("REPLACEMENT LEVEL — the number your whole draft model rests on")
    w("-" * 66)
    for pos in VAL_POS:
        r = a["replacement"].get(pos)
        if not r:
            continue
        w("  %-3s  %2d started leaguewide.  Last starter by finish: %-16s %7.1f pts"
          % (pos, r["starters_in_league"], r["replacement_player"][:16], r["replacement_points"]))
    w()
    w("  Anything above these lines was real value. Below them was streamable.")

    w()
    w("POSITIONAL SPREAD — where scarcity actually bit")
    w("-" * 66)
    ranked = sorted(a["spread"].items(), key=lambda kv: -kv[1]["top5_over_replacement"])
    for pos, s in ranked:
        bar = "#" * max(1, int(s["top5_over_replacement"] / 8))
        w("  %-3s  top-5 averaged %6.1f over replacement  %s" % (pos, s["top5_over_replacement"], bar))
    w()
    w("  Widest spread = where an elite player separated you most from the field.")

    if a["accuracy"]:
        w()
        w("PREDICTABILITY — could projections tell good players from bad ones?")
        w("-" * 66)
        w("  %-4s %9s %14s %16s" % ("POS", "PLAYERS", "RANK CORREL", "SEASON AVG MISS"))
        acc = sorted(a["accuracy"].items(), key=lambda kv: -(kv[1]["season_rank_correlation"] or -1))
        for pos, m in acc:
            rc = m["season_rank_correlation"]
            w("  %-4s %9s %14s %15s"
              % (pos, m["season_sample"] or "-",
                 "%.2f" % rc if rc is not None else "n/a",
                 "%.0f pts" % m["season_mae"] if m["season_mae"] is not None else "n/a"))
        rated = [(p, m) for p, m in acc if m["season_rank_correlation"] is not None]
        if len(rated) >= 2:
            hi = rated[0][1]["season_rank_correlation"]
            lo = rated[-1][1]["season_rank_correlation"]
            w()
            if hi - lo < 0.10:
                w("  These are within %.2f of each other. One season cannot separate positions" % (hi - lo))
                w("  that close, so do not read an ordering into it. What it does say is that")
                w("  projections had real signal everywhere — none of these was a coin flip.")
            else:
                w("  Most knowable: %s (%.2f). An early pick there is least likely to be wasted." % (rated[0][0], hi))
                w("  Least knowable: %s (%.2f). Wait and take volume — if nobody can sort the" % (rated[-1][0], lo))
                w("  hits from the misses, paying a premium to try is how you lose a draft.")

        w()
        w("WEEK-TO-WEEK NOISE — a different question, for start/sit not for drafting")
        w("-" * 66)
        for pos, m in sorted(a["accuracy"].items(), key=lambda kv: kv[1]["weekly_mae_pct"] or 999):
            w("  %-4s  weekly projections missed by %.0f%% of a typical week's score"
              % (pos, m["weekly_mae_pct"] or 0))
        w()
        w("  These are always ugly. That is normal, and it is why chasing last week's")
        w("  points is a losing habit — one week barely predicts the next.")

    if a.get("draft_roi"):
        w()
        w("DRAFT ORDER vs FINISH — did your league's picks pay off?")
        w("-" * 66)
        for pos, d in sorted(a["draft_roi"].items(), key=lambda kv: -(kv[1]["order_vs_finish"] or -1)):
            w("  %-4s  correlation %5s   first %d drafted -> %.0f%% finished top %d"
              % (pos, "%.2f" % d["order_vs_finish"] if d["order_vs_finish"] is not None else "n/a",
                 d["top_n"], d["hit_rate"], d["top_n"]))
            if d["busts"]:
                w("        missed: %s" % ", ".join(d["busts"]))
        w()
        w("  A high correlation means the room drafted that position well and value was")
        w("  priced in. A low one means the position was a lottery — which is exactly")
        w("  where you can wait while everyone else pays up.")
        w("  The hit rates are over a handful of players. Treat them as anecdote, not")
        w("  evidence; the correlations use every pick and are the sturdier number.")

    w()
    w("WHAT TO ACTUALLY DO WITH THIS")
    w("-" * 66)
    w("  Trust most:  the replacement levels. They come from every roster in your")
    w("               league across the whole season, and they are what the draft")
    w("               model's baseline should be calibrated against.")
    w("  Trust some:  the positional spread. Real, but one season of scoring luck")
    w("               moves it around more than you would like.")
    w("  Trust least: anything that separates positions by a small margin, and any")
    w("               hit rate computed over four or five players.")
    w()
    w("  The single trap to avoid: this is hindsight. Last year's RB1 may have gone")
    w("  in the fourth round. Do not reweight your draft toward whichever position")
    w("  happened to score most — that is fitting to information you did not have")
    w("  on draft day, and it is the most common way people talk themselves into a")
    w("  bad strategy.")

    w()
    w("TOP FINISHERS")
    w("-" * 66)
    for pos in VAL_POS:
        lst = a["top10"].get(pos, [])[:5]
        if lst:
            w("  %-3s  %s" % (pos, ",  ".join("%s (%.0f)" % (n, p) for n, p in lst)))
    w()


# ----------------------------------------------------------------------------
# DEMO DATA — so the output shape is visible before you wire up cookies
# ----------------------------------------------------------------------------

def demo_data(seed=7):
    rnd = random.Random(seed)
    teams = 12
    # per-position: count, weekly mean for the best player, decay, and week-to-week noise.
    # Noise is the interesting knob — it is what makes a position unpredictable.
    shape = {
        "QB": (28, 22.0, 0.972, 5.5),
        "RB": (60, 19.5, 0.965, 7.5),
        "WR": (72, 18.5, 0.972, 8.0),
        "TE": (26, 14.0, 0.955, 6.5),
    }
    roster = []
    for pos, (n, top, decay, noise) in shape.items():
        for i in range(n):
            roster.append({"name": "%s %d" % (pos, i + 1), "pos": pos,
                           "true": top * (decay ** i), "noise": noise})
    weeks = []
    for wk in range(1, 15):
        entries = []
        for p in roster:
            actual = max(0.0, rnd.gauss(p["true"], p["noise"]))
            projected = max(0.0, rnd.gauss(p["true"], p["noise"] * 0.45))
            entries.append({"name": p["name"], "pos": p["pos"], "slot": p["pos"],
                            "actual": round(actual, 1), "projected": round(projected, 1)})
        weeks.append({"week": wk, "entries": entries})
    # a synthetic draft: the room drafts roughly by true talent, with real-world reaching
    order = sorted(roster, key=lambda p: -(p["true"] + rnd.gauss(0, 1.6)))[:teams * 15]
    draft = [{"round": i // teams + 1, "pick": i % teams + 1, "name": p["name"], "team": ""}
             for i, p in enumerate(order)]
    return {
        "league_id": 0, "year": 2025, "name": "DEMO (synthetic)", "teams": teams,
        "roster_slots": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "RB/WR/TE": 1},
        "weeks": weeks, "draft": draft,
    }


# ----------------------------------------------------------------------------

def main():
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pull", action="store_true", help="fetch your season from ESPN")
    ap.add_argument("--demo", action="store_true", help="run on synthetic data")
    ap.add_argument("--analyze", metavar="FILE", help="analyze a previously saved raw file")
    ap.add_argument("--out", default="espn_raw.json", help="where to save the raw pull")
    ap.add_argument("--calibration", default="calibration.json", help="where to save the summary")
    args = ap.parse_args()

    if args.demo:
        data = demo_data()
    elif args.analyze:
        with open(args.analyze) as f:
            data = json.load(f)
    elif args.pull:
        print("Pulling from ESPN...", file=sys.stderr)
        data = pull_season()
        with open(args.out, "w") as f:
            json.dump(data, f, indent=1)
        print("Raw season saved to %s" % args.out, file=sys.stderr)
    else:
        ap.print_help()
        return

    a = analyze(data)
    report(a)
    with open(args.calibration, "w") as f:
        json.dump(a, f, indent=1)
    print("Calibration summary written to %s" % args.calibration)


if __name__ == "__main__":
    main()
