#!/usr/bin/env python3
"""
teams.py — the layer between team and player.

WHY THIS EXISTS
  The veteran model assumes last year's role continues. The rookie model only
  knows what percentage of a depth chart is vacant. Neither knows who a player
  is actually competing with, or how big the pie is on his team.

  Fantasy opportunity is a two-step: the team generates a fixed pool of targets
  and carries, then the depth chart divides it. This builds both halves.

WHAT IT PRODUCES
  team_profiles.csv   pace, pass tendency, offensive quality, line play, volume pools
  player_context.csv  every skill player's depth rank and who is ahead of him
  ol_grades.csv        each team's run blocking, graded separately left / middle / right

A NOTE ON THE LINE
  Sack rate is a joint quarterback-and-line statistic, not a clean measure of
  either. A quarterback who holds the ball inflates it. Treat it as a proxy.

  You do not need to pay for PFF grades to separate the line from the runner.
  Play-by-play already tags every run with where it went (run_location: left,
  middle, right) and who carried it. That gives you two free, real signals:
    - the offense's success rate by direction, which is the line and scheme,
      not any one runner
    - a runner's own historical split of carries by direction, so his OL grade
      is weighted toward the gaps he actually hits, not a blind team average
  See ol_direction_grades() and rusher_direction_shares() below.

USAGE
    python teams.py
    python teams.py --team KC
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

CACHE = "nflverse_cache"
LAST = 2025
NOW = 2026
SKILL = ["QB", "RB", "WR", "TE"]


def log(m):
    print(m, file=sys.stderr)


def load():
    import nflreadpy as nfl
    import pandas as pd
    os.makedirs(CACHE, exist_ok=True)

    def cached(name, fn, cols=None):
        p = os.path.join(CACHE, name + ".parquet")
        if os.path.exists(p):
            return pd.read_parquet(p)
        log("  downloading %s" % name)
        d = fn().to_pandas()
        if cols:
            d = d[[c for c in cols if c in d.columns]]
        d.to_parquet(p)
        return d

    # "pbp_slim2": the direction/rusher columns below were added after pbp_slim was
    # first cached, and a stale cache would silently lack them. New name forces one
    # fresh download instead of a confusing KeyError.
    pbp = cached("pbp_slim2", lambda: nfl.load_pbp([LAST]),
                 ["posteam", "season_type", "play_type", "pass_attempt", "rush_attempt",
                  "sack", "qb_hit", "epa", "success", "pass_oe", "qb_dropback",
                  "game_id", "wp", "half_seconds_remaining", "score_differential",
                  "run_location", "run_gap", "rusher_player_id", "rusher_player_name",
                  "yards_gained"])
    dc = cached("depth26", lambda: nfl.load_depth_charts([NOW]))
    stats = cached("stats", lambda: nfl.load_player_stats([LAST]))
    ros = cached("roster_now", lambda: nfl.load_rosters([NOW]))
    # load_rosters() (unlike load_pbp/load_depth_charts) returns "AZ" for
    # Arizona -- everything else here says "ARI". Same failure class as the
    # documented LA/LAR mismatch; normalize at the point it enters the pipeline.
    ros["team"] = ros["team"].replace({"AZ": "ARI"})
    return pbp, dc, stats, ros


# ---------------------------------------------------------------------------

def team_profiles(pbp, stats):
    import pandas as pd
    import numpy as np

    p = pbp[(pbp["season_type"] == "REG") & pbp["posteam"].notna()].copy()
    for c in ("pass_attempt", "rush_attempt", "sack", "qb_hit", "epa", "success",
              "pass_oe", "qb_dropback"):
        if c in p.columns:
            p[c] = pd.to_numeric(p[c], errors="coerce")

    # neutral game script only — trailing teams pass, leading teams run, and
    # neither tells you what the offense wants to do
    neutral = p[(p["wp"].between(0.20, 0.80)) & (p["score_differential"].abs() <= 14)]

    games = p.groupby("posteam")["game_id"].nunique()
    plays = p[(p["pass_attempt"] == 1) | (p["rush_attempt"] == 1)].groupby("posteam").size()
    drop = p.groupby("posteam")["qb_dropback"].sum()
    sacks = p.groupby("posteam")["sack"].sum()
    hits = p.groupby("posteam")["qb_hit"].sum()
    epa = p.groupby("posteam")["epa"].mean()
    poe = neutral.groupby("posteam")["pass_oe"].mean()
    rush = p[p["rush_attempt"] == 1].groupby("posteam")["epa"].mean()
    pas = p[p["pass_attempt"] == 1].groupby("posteam")["epa"].mean()

    t = pd.DataFrame({
        "games": games, "plays": plays, "dropbacks": drop,
        "sacks": sacks, "qb_hits": hits,
        "off_epa": epa, "pass_epa": pas, "rush_epa": rush, "pass_oe": poe,
    }).reset_index().rename(columns={"posteam": "team"})

    t["plays_pg"] = (t["plays"] / t["games"]).round(1)
    t["sack_rate"] = (100 * t["sacks"] / t["dropbacks"].replace(0, np.nan)).round(1)
    t["hit_rate"] = (100 * t["qb_hits"] / t["dropbacks"].replace(0, np.nan)).round(1)
    for c in ("off_epa", "pass_epa", "rush_epa", "pass_oe"):
        t[c] = t[c].round(3)

    # the pie: how much volume this offense actually generated
    s = stats[stats["season_type"] == "REG"].copy()
    for c in ("targets", "carries"):
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0)
    pool = s.groupby("team", as_index=False).agg(
        team_targets=("targets", "sum"), team_carries=("carries", "sum"))
    t = t.merge(pool, on="team", how="left")

    # percentile ranks make these readable at a glance
    for c, hi_good in [("plays_pg", True), ("off_epa", True), ("pass_epa", True),
                       ("rush_epa", True), ("sack_rate", False), ("hit_rate", False)]:
        t[c + "_pct"] = (t[c].rank(pct=True, ascending=hi_good) * 100).round(0)
    return t.sort_values("off_epa", ascending=False).reset_index(drop=True)


def ol_direction_grades(pbp):
    """Grade run blocking by direction instead of as one blended team number.

    A team's overall rush EPA mixes the runner's talent in with the line's. It
    also hides that a line can be excellent to one side and a sieve to the
    other — real, common, and invisible to a single number. Splitting by
    run_location (left / middle / right) isolates the line and scheme from any
    one back, which is the free substitute for paying for individual OL grades.
    """
    import pandas as pd

    r = pbp[(pbp["season_type"] == "REG") & (pbp["rush_attempt"] == 1)
            & pbp["run_location"].notna() & pbp["posteam"].notna()].copy()
    for c in ("epa", "success", "yards_gained"):
        r[c] = pd.to_numeric(r[c], errors="coerce")

    g = r.groupby(["posteam", "run_location"], as_index=False).agg(
        attempts=("epa", "size"), epa=("epa", "mean"),
        success=("success", "mean"), ypc=("yards_gained", "mean"))
    g = g.rename(columns={"posteam": "team", "run_location": "direction"})
    for c in ("epa", "success", "ypc"):
        g[c] = g[c].round(3)
    # percentile within each direction, so "left side" only competes against
    # other teams' left sides
    g["epa_pct"] = (g.groupby("direction")["epa"].rank(pct=True) * 100).round(0)
    return g.sort_values(["direction", "epa"], ascending=[True, False]).reset_index(drop=True)


def rusher_direction_shares(pbp):
    """Each rusher's own career-to-date split of carries by direction.

    Used to weight a team's directional OL grades toward the gaps a specific
    runner actually hits, instead of assuming he runs an equal mix of left,
    middle, and right. A player with no carries in this window (rookie, new
    team) has no row here — callers should fall back to an even split.
    """
    import pandas as pd

    r = pbp[(pbp["season_type"] == "REG") & (pbp["rush_attempt"] == 1)
            & pbp["run_location"].notna() & pbp["rusher_player_id"].notna()].copy()
    g = r.groupby(["rusher_player_id", "run_location"]).size().reset_index(name="n")
    g["share"] = g["n"] / g.groupby("rusher_player_id")["n"].transform("sum")
    return g.rename(columns={"run_location": "direction", "rusher_player_id": "player_id"})


def depth_and_competition(dc, stats, ros):
    """Current depth chart, plus how much production each player is competing with."""
    import pandas as pd

    d = dc[dc["pos_abb"].isin(SKILL)].copy()
    d["dt"] = pd.to_datetime(d["dt"], errors="coerce", utc=True)
    # keep only the newest snapshot per team/position/slot
    d = d.sort_values("dt").groupby(["team", "pos_abb", "pos_rank"], as_index=False).last()
    d = d[["team", "pos_abb", "pos_rank", "player_name"]].dropna()
    d["pos_rank"] = pd.to_numeric(d["pos_rank"], errors="coerce")
    d = d.dropna(subset=["pos_rank"])
    # a player can appear at several slots; keep his best
    d = d.sort_values("pos_rank").drop_duplicates(["team", "pos_abb", "player_name"])

    # last season's production, to weigh the competition rather than just count it
    s = stats[stats["season_type"] == "REG"].copy()
    for c in ("targets", "carries"):
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0)
    prod = s.groupby("player_display_name", as_index=False).agg(
        py_targets=("targets", "sum"), py_carries=("carries", "sum"))

    d = d.merge(prod, left_on="player_name", right_on="player_display_name", how="left")
    d[["py_targets", "py_carries"]] = d[["py_targets", "py_carries"]].fillna(0)

    rows = []
    for (team, pos), g in d.groupby(["team", "pos_abb"]):
        g = g.sort_values("pos_rank")
        vol = "py_carries" if pos == "RB" else "py_targets"
        total = g[vol].sum()
        for _, r in g.iterrows():
            ahead = g[g["pos_rank"] < r["pos_rank"]]
            rows.append({
                "player": r["player_name"], "pos": pos, "team": team,
                "depth_rank": int(r["pos_rank"]),
                "ahead_count": len(ahead),
                "ahead_volume": int(ahead[vol].sum()),
                "own_volume": int(r[vol]),
                "room_volume": int(total),
                "share_of_room": round(100 * r[vol] / total, 1) if total else 0.0,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default=None)
    a = ap.parse_args()

    log("Loading...")
    pbp, dc, stats, ros = load()
    log("Building team profiles...")
    t = team_profiles(pbp, stats)
    t.to_csv("team_profiles.csv", index=False)
    log("Building depth chart context...")
    c = depth_and_competition(dc, stats, ros)
    c.to_csv("player_context.csv", index=False)
    log("Grading run blocking by direction...")
    ol = ol_direction_grades(pbp)
    ol.to_csv("ol_grades.csv", index=False)
    log("Wrote team_profiles.csv, player_context.csv, and ol_grades.csv")

    print()
    print("TEAM OFFENSE, %d  — sorted by EPA per play" % LAST)
    print("-" * 92)
    print("%-5s %8s %9s %9s %9s %8s %8s %9s %8s" %
          ("TEAM", "PLAYS/G", "OFF EPA", "PASS EPA", "RUSH EPA", "PASS OE", "SACK%", "TARGETS", "CARRIES"))
    for _, r in t.iterrows():
        print("%-5s %8.1f %9.3f %9.3f %9.3f %8.1f %8.1f %9.0f %8.0f" %
              (r["team"], r["plays_pg"], r["off_epa"], r["pass_epa"], r["rush_epa"],
               r["pass_oe"] if r["pass_oe"] == r["pass_oe"] else 0,
               r["sack_rate"], r["team_targets"], r["team_carries"]))

    print()
    print("  PASS OE above zero means the team throws more than the situation calls for.")
    print("  SACK% is a joint quarterback-and-line number — a proxy for protection, not a")
    print("  measurement of it. High sack rate with good pass EPA usually means a")
    print("  quarterback who holds the ball, not a broken line.")

    print()
    print("RUN BLOCKING BY DIRECTION  — percentile within each direction, not overall")
    print("-" * 92)
    print("%-5s %-8s %8s %8s %8s %6s" % ("TEAM", "DIRECTION", "EPA", "EPA%ILE", "SUCCESS%", "YPC"))
    for team in sorted(ol["team"].unique()):
        sub = ol[ol["team"] == team].set_index("direction")
        for d in ("left", "middle", "right"):
            if d not in sub.index:
                continue
            row = sub.loc[d]
            print("%-5s %-8s %8.3f %7.0f%% %7.1f%% %6.1f" %
                  (team if d == "left" else "", d, row["epa"], row["epa_pct"],
                   100 * row["success"], row["ypc"]))
    print()
    print("  A team strong on one side and weak on the other is common and invisible")
    print("  to a single rush-EPA number. Weight this by a back's own carry-direction")
    print("  split (rusher_direction_shares) rather than assuming an even mix.")

    if a.team:
        print()
        print("DEPTH CHART — %s" % a.team)
        print("-" * 92)
        sub = c[c["team"] == a.team].sort_values(["pos", "depth_rank"])
        print("%-4s %5s %-24s %9s %11s %12s" %
              ("POS", "RANK", "PLAYER", "OWN VOL", "AHEAD VOL", "% OF ROOM"))
        for _, r in sub.iterrows():
            print("%-4s %5d %-24s %9d %11d %11.1f%%" %
                  (r["pos"], r["depth_rank"], str(r["player"])[:24],
                   r["own_volume"], r["ahead_volume"], r["share_of_room"]))
    print()


if __name__ == "__main__":
    main()
