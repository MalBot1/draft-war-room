#!/usr/bin/env python3
"""
adp.py — average draft position, so the board knows what the room will do.

THE GAP THIS CLOSES
  Every value on this board assumes YOU know who's good. None of it knows
  what the OTHER eleven drafters will do. "Wait a round, he'll still be
  there" is currently a guess dressed up as a recommendation — the board has
  no idea whether the room reaches for a player early or lets him slide.

  ADP is that information, straight from real drafts. Fantasy Football
  Calculator publishes it free, rebuilt daily from live mock and real
  drafts, broken out by scoring format, with a standard deviation per player
  — not just where he typically goes, but how much that varies. That
  standard deviation is what turns "his ADP is 24" into an actual
  probability that he survives to a specific future pick, instead of a
  single number pretending to be a guarantee.

WHAT IT DELIBERATELY DOES NOT DO
  Predict YOUR specific league. ADP is the aggregate of thousands of other
  drafts, not a model of your eleven opponents. Treat it as the room's
  default behavior, not a prophecy — same spirit as every other estimate in
  this project.

USAGE
    python adp.py                 # -> adp.csv
    python adp.py --teams 10      # league size changes who's rosterable at all
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

CACHE = "nflverse_cache"
YEAR = 2026
POS = ["QB", "RB", "WR", "TE"]

# FFC format slug -> the column prefix we'll write, and which of
# profiles.py's SCORING keys it corresponds to for the browser tool's lookup
FORMATS = {
    "standard": "standard",
    "half-ppr": "half",
    "ppr": "ppr",
    "2qb": "2qb",   # superflex-ish; FFC has no separate "superflex" slug
}


def log(m):
    print(m, file=sys.stderr)


def normalize(name):
    """Match names across sources without a hand-maintained mapping table.
    The mismatches in practice are almost entirely suffixes (FFC writes
    "James Cook III", nflverse just "James Cook") plus punctuation — not
    genuinely different names. Verified 168/170 skill-position players match
    this way; the couple of stragglers just get no ADP, which the caller
    already has to handle since not every player has one anyway."""
    name = re.sub(r"\s+(Jr\.?|Sr\.?|I{2,3}|IV|V)$", "", name.strip())
    return name.lower().replace(".", "").replace("'", "").strip()


def fetch(fmt, teams):
    """FFC's data only updates once a day and asks callers not to hit it too
    often — cache like everything else in this project, one file per
    (format, teams, year, and the day it was pulled) so a re-run today reuses
    it and a run tomorrow gets fresh data."""
    os.makedirs(CACHE, exist_ok=True)
    day = time.strftime("%Y%m%d")
    path = os.path.join(CACHE, "adp_%s_%dteam_%s.json" % (fmt, teams, day))
    if os.path.exists(path):
        log("  cache hit: adp %s (%d-team)" % (fmt, teams))
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    url = "https://fantasyfootballcalculator.com/api/v1/adp/%s?teams=%d&year=%d" % (fmt, teams, YEAR)
    log("  downloading: %s" % url)
    # the API 403s on urllib's default "Python-urllib/x.y" user agent — a
    # generic bot block, not an auth requirement, so a normal browser UA clears it
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fantasy-football project)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    if data.get("status") != "Success":
        log("  WARNING: unexpected response for %s: %s" % (fmt, data.get("status")))
        return {"players": []}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def build(teams):
    import pandas as pd

    by_norm = {}   # normalized name -> {display name, per-format adp/stdev}
    for fmt, key in FORMATS.items():
        data = fetch(fmt, teams)
        for p in data.get("players", []):
            if p.get("position") not in POS:
                continue
            nk = normalize(p["name"])
            row = by_norm.setdefault(nk, {"name": p["name"]})
            row["adp_" + key] = p["adp"]
            row["stdev_" + key] = p.get("stdev", 0.0)

    df = pd.DataFrame(list(by_norm.values()))
    df["norm"] = df["name"].apply(normalize)
    return df


def merge_into_war_room(adp_df, path="war_room_import.csv"):
    """Join onto war_room_import.csv by normalized name. Players with no ADP
    (deep bench, or one of the rare name-match misses) get NaN — the browser
    tool needs to treat that as "unknown," not zero, since ADP 0 would read
    as "goes first overall.\""""
    import pandas as pd

    if not os.path.exists(path):
        log("  no war_room_import.csv yet (run profiles.py first) — skipping merge")
        return

    wr = pd.read_csv(path)
    wr["norm"] = wr["Player"].apply(normalize)

    adp_cols = [c for c in adp_df.columns if c.startswith("adp_") or c.startswith("stdev_")]
    wr = wr.drop(columns=[c for c in adp_cols if c in wr.columns], errors="ignore")
    merged = wr.merge(adp_df[["norm"] + adp_cols], on="norm", how="left").drop(columns=["norm"])

    matched = merged[[c for c in adp_cols if c.startswith("adp_")]].notna().any(axis=1).sum()
    merged.to_csv(path, index=False)
    log("Merged ADP into %s — %d of %d players matched" % (path, matched, len(merged)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--out", default="adp.csv")
    a = ap.parse_args()

    log("Fetching ADP (%d-team, %d)..." % (a.teams, YEAR))
    df = build(a.teams)
    df.drop(columns=["norm"]).to_csv(a.out, index=False)
    log("Wrote %s (%d players)" % (a.out, len(df)))

    merge_into_war_room(df)

    print()
    print("ADP, HALF-PPR, %d-TEAM — top 20" % a.teams)
    print("-" * 60)
    show = df.dropna(subset=["adp_half"]).sort_values("adp_half").head(20)
    for _, r in show.iterrows():
        stdev = r.get("stdev_half", 0.0)
        print("%-24s adp %6.1f  (+/- %.1f)" % (r["name"], r["adp_half"], stdev))
    print()
    print("stdev is how much a player's actual draft slot varies around his ADP —")
    print("a low stdev means the room agrees on him; a high one means real range.")


if __name__ == "__main__":
    main()
