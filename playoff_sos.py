#!/usr/bin/env python3
"""
playoff_sos.py — how tough is each team's schedule during fantasy playoffs.

THE GAP THIS CLOSES
  Bye weeks aside, nothing else in this project looks at the calendar. A
  receiver on a great offense can still be the wrong pick if his fantasy
  playoff weeks line up against three of the league's stingiest defenses —
  a real, common draft-day argument ("good player, brutal playoff slate")
  that had no data behind it here.

METHOD
  Take last season's actual player-game stats, score them with this
  project's own half-PPR formula, and group by (defense faced, offensive
  position) to get how many fantasy points each defense allowed per game to
  QBs / RBs / WRs / TEs. Rank each defense against the other 31 at that
  position -> a percentile, 100 = allowed the most (easiest matchup), 0 =
  allowed the least (toughest). Then pull the real 2026 schedule, find each
  team's opponents in the target playoff weeks, and average those
  opponents' allowed-percentile at the position in question.

WHAT IT DELIBERATELY DOES NOT DO
  Feed into the core ranking, or even into ADP-style survival math. This is
  one season of games-allowed data — thinner evidence than this project
  demands before trusting a player's OWN rate (see profiles.py's shrinkage),
  so trusting it to move a defense's projected strength even further out
  from what we know now would be worse. It's a badge sitting next to the
  real value, same spirit as the ADP-vs-model gap — informative, not
  authoritative. A defense that struggled against TEs last year is not
  guaranteed to struggle again; coaches, personnel, and schemes change.

USAGE
    python playoff_sos.py                    # -> playoff_sos.csv, weeks 15-17
    python playoff_sos.py --weeks 15,16,17    # match your league's actual playoff weeks
"""

import argparse
import os
import sys

CACHE = "nflverse_cache"
LAST = 2025
NOW = 2026
POS = ["QB", "RB", "WR", "TE"]

SC = dict(rec=0.5, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6, passYd=0.04, passTD=4, int=-2)


def log(m):
    print(m, file=sys.stderr)


def load():
    import nflreadpy as nfl
    import pandas as pd

    os.makedirs(CACHE, exist_ok=True)

    # profiles.py already caches full weekly player-stats history under this
    # name for its own PRIOR_SEASONS window — reuse it if last season is in
    # there instead of downloading the same thing twice under a new name.
    hist_path = os.path.join(CACHE, "stats_hist.parquet")
    if os.path.exists(hist_path):
        stats = pd.read_parquet(hist_path)
        if LAST in stats["season"].unique():
            log("  cache hit: stats_hist (reused for %d)" % LAST)
            stats = stats[stats["season"] == LAST].copy()
        else:
            stats = None
    else:
        stats = None
    if stats is None:
        path = os.path.join(CACHE, "stats_playoff_sos_%d.parquet" % LAST)
        if os.path.exists(path):
            log("  cache hit: stats %d" % LAST)
            stats = pd.read_parquet(path)
        else:
            log("  downloading: player stats %d" % LAST)
            stats = nfl.load_player_stats([LAST]).to_pandas()
            stats.to_parquet(path)

    sched_path = os.path.join(CACHE, "schedules_%d.parquet" % NOW)
    if os.path.exists(sched_path):
        log("  cache hit: schedules %d" % NOW)
        sched = pd.read_parquet(sched_path)
    else:
        log("  downloading: schedule %d" % NOW)
        sched = nfl.load_schedules([NOW]).to_pandas()
        sched.to_parquet(sched_path)

    return stats, sched


def points_allowed(stats):
    """Fantasy points allowed per game, by (defense, offensive position).
    Higher percentile = defense allows more = easier matchup for that position."""
    s = stats[(stats["season_type"] == "REG") & stats["position"].isin(POS)].copy()
    for c in ["passing_yards", "passing_tds", "passing_interceptions", "rushing_yards",
              "rushing_tds", "receptions", "receiving_yards", "receiving_tds"]:
        if c not in s.columns:
            s[c] = 0.0
    s["pts"] = (
        s["passing_yards"].fillna(0) * SC["passYd"] + s["passing_tds"].fillna(0) * SC["passTD"]
        + s["passing_interceptions"].fillna(0) * SC["int"]
        + s["rushing_yards"].fillna(0) * SC["rushYd"] + s["rushing_tds"].fillna(0) * SC["rushTD"]
        + s["receptions"].fillna(0) * SC["rec"] + s["receiving_yards"].fillna(0) * SC["recYd"]
        + s["receiving_tds"].fillna(0) * SC["recTD"]
    )
    by_week = s.groupby(["opponent_team", "position", "week"])["pts"].sum().reset_index()
    per_game = by_week.groupby(["opponent_team", "position"])["pts"].mean().reset_index()
    per_game.columns = ["team", "pos", "pts_allowed_pg"]
    per_game["pctl"] = per_game.groupby("pos")["pts_allowed_pg"].rank(pct=True) * 100
    return per_game


def playoff_opponents(sched, weeks):
    reg = sched[(sched["game_type"] == "REG") & sched["week"].isin(weeks)]
    rows = []
    for _, g in reg.iterrows():
        rows.append({"team": g["home_team"], "opp": g["away_team"], "week": g["week"]})
        rows.append({"team": g["away_team"], "opp": g["home_team"], "week": g["week"]})
    import pandas as pd
    return pd.DataFrame(rows)


def build(weeks):
    stats, sched = load()
    allowed = points_allowed(stats)
    opp = playoff_opponents(sched, weeks)

    rows = []
    for pos in POS:
        pos_allowed = allowed[allowed["pos"] == pos].set_index("team")["pctl"]
        for team, grp in opp.groupby("team"):
            grp = grp.sort_values("week")
            pctls = [pos_allowed.get(o) for o in grp["opp"] if pos_allowed.get(o) is not None]
            if not pctls:
                continue
            avg = sum(pctls) / len(pctls)
            label = "Easy" if avg >= 65 else "Tough" if avg <= 35 else "Neutral"
            rows.append({
                "team": team, "pos": pos,
                "weeks": ",".join(str(int(w)) for w in grp["week"]),
                "opponents": ",".join(grp["opp"]),
                "sos_pctl": round(avg, 1), "sos_label": label,
            })
    import pandas as pd
    return pd.DataFrame(rows)


def merge_into_war_room(sos_df, path="war_room_import.csv"):
    """Join onto war_room_import.csv by (team, position) — this is a team-
    level signal, not a per-player one, so every player on a given team's
    offense at a given position gets the same rating."""
    import pandas as pd

    if not os.path.exists(path):
        log("  no war_room_import.csv yet (run profiles.py first) — skipping merge")
        return

    wr = pd.read_csv(path)
    new_cols = ["playoff_sos_pctl", "playoff_sos_label"]
    wr = wr.drop(columns=[c for c in new_cols if c in wr.columns], errors="ignore")

    s = sos_df.rename(columns={"sos_pctl": "playoff_sos_pctl", "sos_label": "playoff_sos_label"})
    merged = wr.merge(s[["team", "pos", "playoff_sos_pctl", "playoff_sos_label"]],
                       left_on=["Team", "POS"], right_on=["team", "pos"], how="left")
    merged = merged.drop(columns=["team", "pos"])
    merged.to_csv(path, index=False)
    matched = merged["playoff_sos_pctl"].notna().sum()
    log("Merged playoff SOS into %s — %d of %d players matched" % (path, matched, len(merged)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", default="15,16,17",
                    help="comma-separated fantasy-playoff weeks for your league, e.g. 15,16,17")
    ap.add_argument("--out", default="playoff_sos.csv")
    a = ap.parse_args()

    weeks = [int(w.strip()) for w in a.weeks.split(",") if w.strip()]
    log("Computing playoff strength of schedule for weeks %s..." % weeks)
    df = build(weeks)
    df.to_csv(a.out, index=False)
    log("Wrote %s (%d rows)" % (a.out, len(df)))

    merge_into_war_room(df)

    print()
    print("EASIEST PLAYOFF MATCHUPS (weeks %s)" % ",".join(map(str, weeks)))
    print("-" * 70)
    for pos in POS:
        top = df[df["pos"] == pos].sort_values("sos_pctl", ascending=False).head(5)
        print("%s:" % pos)
        for _, r in top.iterrows():
            print("  %-4s vs %-20s sos=%5.1f  %s" % (r["team"], r["opponents"], r["sos_pctl"], r["sos_label"]))
    print()
    print("Based on one season of points allowed by position — a signal to weigh, not a guarantee.")


if __name__ == "__main__":
    main()
