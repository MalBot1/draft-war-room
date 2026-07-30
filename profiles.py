#!/usr/bin/env python3
"""
profiles.py — build player profiles and projections from opportunity, not from points.

WHY THIS SHAPE
  Last year's fantasy points are a poor input. They bake in touchdown luck and
  efficiency, and both regress hard. What carries forward is OPPORTUNITY:
  target share, carry volume, snap share, air yards. Those are stable year over
  year. So the model is:

      projected points = projected volume  x  regressed efficiency  x  expected games

  Each of those three is estimated separately, because each behaves differently:
    volume      - stable, mostly a function of role and team
    efficiency  - noisy, regress hard toward the positional mean
    games       - a function of age and position

  Age and team come in as adjustments to volume, which is where they actually act.

CONTEXT: THE PLAYER DOES NOT PLAY ALONE
  The three-factor model above is blind to a real driver of fantasy outcomes:
  who's blocking for him and who else is on the field. After the base
  projection, a second pass applies a bounded adjustment (capped at +/-15%,
  so it nudges rather than dominates):

    RB  - graded against his own offensive line, but not as one team-wide
          number. teams.py splits run blocking left / middle / right from
          play-by-play (free — no PFF subscription needed) and this file
          weights those three grades by the back's own history of which
          gaps he actually hits. Blended with the team's passing efficiency,
          since a credible pass game keeps boxes light for the run.
    QB  - graded against the strength of his own current WR/TE corps (their
          own opportunity-based projections, summed) and the team's pass-
          protection rate (sack% and hit% from teams.py).

  Every adjustment is logged in context_note so it can be audited, not just
  trusted. This is still a proxy, not a snap-by-snap grade of five individual
  linemen — that requires paid PFF data. Directional run grading is the free
  substitute: it separates the line and scheme from the runner using data
  that already exists in play-by-play.

USAGE
    pip install nflreadpy pandas
    python profiles.py                      # build, cache, report
    python profiles.py --scoring ppr        # ppr | half | standard
    python profiles.py --pos RB --top 40    # focus one position
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

CACHE = "nflverse_cache"
PRIOR_SEASONS = [2024, 2025]
TARGET_SEASON = 2026
CONTEXT_CAP = 0.15   # max +/-15% swing from the context adjustment below

# ---------------------------------------------------------------------------
# Age curves. Multiplier applied to projected volume.
# Running backs fall off a cliff; receivers glide; tight ends develop late;
# quarterbacks hold a long plateau. These are coarse on purpose — they are a
# prior, not a measurement, and pretending otherwise would be false precision.
# ---------------------------------------------------------------------------
AGE_CURVE = {
    "RB": {21: 0.95, 22: 1.00, 23: 1.03, 24: 1.05, 25: 1.05, 26: 1.02, 27: 0.97,
           28: 0.90, 29: 0.82, 30: 0.73, 31: 0.64, 32: 0.55, 33: 0.46, 34: 0.38},
    "WR": {21: 0.88, 22: 0.94, 23: 1.00, 24: 1.04, 25: 1.06, 26: 1.06, 27: 1.05,
           28: 1.03, 29: 1.00, 30: 0.95, 31: 0.89, 32: 0.82, 33: 0.74, 34: 0.66,
           35: 0.58, 36: 0.50},
    "TE": {22: 0.80, 23: 0.88, 24: 0.95, 25: 1.00, 26: 1.04, 27: 1.06, 28: 1.06,
           29: 1.04, 30: 1.00, 31: 0.94, 32: 0.87, 33: 0.79, 34: 0.70, 35: 0.61,
           36: 0.52},
    "QB": {22: 0.92, 23: 0.96, 24: 1.00, 25: 1.02, 26: 1.04, 27: 1.05, 28: 1.05,
           29: 1.05, 30: 1.04, 31: 1.03, 32: 1.01, 33: 0.99, 34: 0.96, 35: 0.92,
           36: 0.87, 37: 0.81, 38: 0.74, 39: 0.66, 40: 0.58},
}

# Expected games, by position and age. Injury risk is real and mostly ignored.
BASE_GAMES = {"RB": 15.0, "WR": 15.5, "TE": 15.0, "QB": 15.5}

# Shrinkage constants: how much evidence before we believe a player's own
# efficiency over the positional average. Touchdown rate gets the harshest
# treatment because it is the noisiest thing in fantasy football.
SHRINK = {"ypt": 45, "ypc": 90, "td_rate": 130, "catch_rate": 45}

SCORING = {
    "ppr":      dict(rec=1.0, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6, passYd=0.04, passTD=4, int=-2, fl=-2),
    "half":     dict(rec=0.5, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6, passYd=0.04, passTD=4, int=-2, fl=-2),
    "standard": dict(rec=0.0, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6, passYd=0.04, passTD=4, int=-2, fl=-2),
}

POS = ["QB", "RB", "WR", "TE"]


def log(m):
    print(m, file=sys.stderr)


# ---------------------------------------------------------------------------
# LOAD
# ---------------------------------------------------------------------------

def load_all():
    import nflreadpy as nfl
    import pandas as pd

    os.makedirs(CACHE, exist_ok=True)

    def cached(name, fn):
        path = os.path.join(CACHE, name + ".parquet")
        if os.path.exists(path):
            log("  cache hit: %s" % name)
            return pd.read_parquet(path)
        log("  downloading: %s" % name)
        df = fn().to_pandas()
        df.to_parquet(path)
        return df

    # teams.py also caches a file it calls "stats" — but for a single season
    # (LAST), not these two. Same filename, different contents, would silently
    # clobber whichever script ran second. rookies.py's stats_hist already
    # spans both years we need, so reuse it instead of a colliding download.
    hist_path = os.path.join(CACHE, "stats_hist.parquet")
    if os.path.exists(hist_path):
        log("  cache hit: stats_hist (reused for %s)" % PRIOR_SEASONS)
        stats = pd.read_parquet(hist_path)
        stats = stats[stats["season"].isin(PRIOR_SEASONS)].copy()
    else:
        stats = cached("stats_prior2", lambda: nfl.load_player_stats(PRIOR_SEASONS))

    snaps = cached("snaps", lambda: nfl.load_snap_counts(PRIOR_SEASONS))
    ros_now = cached("roster_now", lambda: nfl.load_rosters([TARGET_SEASON]))
    ros_prev = cached("roster_prev", lambda: nfl.load_rosters([PRIOR_SEASONS[-1]]))
    return stats, snaps, ros_now, ros_prev


def load_expected_td_rates():
    """Expected rushing/receiving TDs per touch, from ffopportunity's play-level
    expectation model (nflreadpy's load_ff_opportunity) — a free, pre-built
    model of what a touch at that down/distance/field position should be worth.

    This is the fix for a real gap in the touchdown-rate regression below: it
    used to shrink a player's own TD rate toward the blanket POSITIONAL
    average, which treats a bell-cow goal-line back the same as a receiving
    back who never sees the 5-yard line. Shrinking toward his OWN expected
    rate instead — which already reflects exactly how often his actual usage
    put him in scoring position — is a sharper, role-aware prior. Bijan
    Robinson, 2025: 7 actual rushing TDs vs. 6.96 expected (rushing TD luck is
    a wash), but 4 actual receiving TDs vs. 2.60 expected — real signal that
    his receiving-TD rate should regress down, which a positional-average
    prior has no way to see.
    """
    import nflreadpy as nfl
    import pandas as pd

    path = os.path.join(CACHE, "ff_opportunity.parquet")
    if os.path.exists(path):
        log("  cache hit: ff_opportunity")
        d = pd.read_parquet(path)
    else:
        log("  downloading: ff_opportunity")
        d = nfl.load_ff_opportunity(seasons=PRIOR_SEASONS).to_pandas()
        d.to_parquet(path)

    d = d[d["position"].isin(POS) & d["player_id"].notna()].copy()
    # unlike load_player_stats/load_rosters, this loader returns season as a
    # string ("2024") — left alone, that silently fails to match the int keys
    # in season_w below (every weight -> 0 -> every player skipped, not an
    # error, just an empty result), which is exactly what happened on first run
    d["season"] = pd.to_numeric(d["season"], errors="coerce").astype("Int64")
    for c in ("rush_attempt", "rush_touchdown_exp", "rec_attempt", "rec_touchdown_exp"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)

    season_tot = d.groupby(["player_id", "season"], as_index=False).agg(
        rush_att=("rush_attempt", "sum"), rush_td_exp=("rush_touchdown_exp", "sum"),
        rec_att=("rec_attempt", "sum"), rec_td_exp=("rec_touchdown_exp", "sum"))

    # same recency weighting as blend(), so this prior moves with the same
    # "how much do we trust last season vs. the one before" logic as everything else
    recent, prior = PRIOR_SEASONS[-1], PRIOR_SEASONS[0]
    season_w = {recent: 0.70, prior: 0.30}
    season_tot["w"] = season_tot["season"].map(season_w).fillna(0.0)

    out = {}
    for pid, g in season_tot.groupby("player_id"):
        tw = g["w"].sum()
        if tw <= 0:
            continue
        rush_att = (g["rush_att"] * g["w"]).sum() / tw
        rec_att = (g["rec_att"] * g["w"]).sum() / tw
        entry = {}
        if rush_att > 0:
            entry["rush"] = ((g["rush_td_exp"] * g["w"]).sum() / tw) / rush_att
        if rec_att > 0:
            entry["rec"] = ((g["rec_td_exp"] * g["w"]).sum() / tw) / rec_att
        if entry:
            out[pid] = entry
    return out


def load_team_context():
    """Pull in teams.py's play-by-play-derived context: directional O-line
    grades, each rusher's own gap tendency, and team-level pass/protection
    numbers. Reuses teams.py's own caching, so this costs nothing extra once
    teams.py has been run once."""
    import teams as teams_mod

    pbp, dc, stats_1yr, ros = teams_mod.load()
    tp = teams_mod.team_profiles(pbp, stats_1yr)
    ol = teams_mod.ol_direction_grades(pbp)
    shares = teams_mod.rusher_direction_shares(pbp)
    return tp, ol, shares


# ---------------------------------------------------------------------------
# AGGREGATE
# ---------------------------------------------------------------------------

def season_aggregates(stats, snaps):
    import pandas as pd

    s = stats[stats["season_type"] == "REG"].copy()
    s = s[s["position"].isin(POS)]

    num = ["targets", "receptions", "receiving_yards", "receiving_tds", "receiving_air_yards",
           "carries", "rushing_yards", "rushing_tds",
           "passing_yards", "passing_tds", "passing_interceptions",
           "target_share", "air_yards_share"]
    for c in num:
        if c not in s.columns:
            s[c] = 0.0
        s[c] = pd.to_numeric(s[c], errors="coerce").fillna(0.0)

    # a "game" only counts if the player was actually involved
    s["played"] = ((s["targets"] > 0) | (s["carries"] > 0) | (s["passing_yards"].abs() > 0)).astype(int)

    agg = s.groupby(["player_id", "player_display_name", "position", "season"], as_index=False).agg(
        games=("played", "sum"),
        targets=("targets", "sum"),
        receptions=("receptions", "sum"),
        rec_yards=("receiving_yards", "sum"),
        rec_tds=("receiving_tds", "sum"),
        air_yards=("receiving_air_yards", "sum"),
        carries=("carries", "sum"),
        rush_yards=("rushing_yards", "sum"),
        rush_tds=("rushing_tds", "sum"),
        pass_yards=("passing_yards", "sum"),
        pass_tds=("passing_tds", "sum"),
        ints=("passing_interceptions", "sum"),
        tgt_share=("target_share", "mean"),
        ay_share=("air_yards_share", "mean"),
    )
    agg = agg[agg["games"] >= 1]

    # snap share, the cleanest single signal of role
    sn = snaps[snaps["game_type"] == "REG"].copy()
    sn["offense_pct"] = pd.to_numeric(sn["offense_pct"], errors="coerce")
    sn = sn.groupby(["player", "season"], as_index=False)["offense_pct"].mean()
    sn.columns = ["player_display_name", "season", "snap_pct"]
    # nflverse stores snap pct as a fraction in some seasons, percent in others
    if sn["snap_pct"].max() and sn["snap_pct"].max() <= 1.5:
        sn["snap_pct"] *= 100.0
    agg = agg.merge(sn, on=["player_display_name", "season"], how="left")
    return agg


def blend(agg):
    """Weight recent season heavier, and weight by games so a 4-game sample
    does not speak as loudly as a 17-game one."""
    import pandas as pd
    import numpy as np

    recent, prior = PRIOR_SEASONS[-1], PRIOR_SEASONS[0]
    season_w = {recent: 0.70, prior: 0.30}
    agg = agg.copy()
    agg["w"] = agg["season"].map(season_w) * agg["games"].clip(upper=17)

    per_game = ["targets", "receptions", "rec_yards", "rec_tds", "air_yards",
                "carries", "rush_yards", "rush_tds", "pass_yards", "pass_tds", "ints"]
    for c in per_game:
        agg[c + "_pg"] = agg[c] / agg["games"]

    rows = []
    for (pid, name, pos), g in agg.groupby(["player_id", "player_display_name", "position"]):
        tw = g["w"].sum()
        if tw <= 0:
            continue
        r = {"player_id": pid, "name": name, "pos": pos,
             "seasons": len(g), "games_sample": int(g["games"].sum()),
             "last_games": int(g[g["season"] == recent]["games"].sum())}
        for c in per_game:
            r[c] = float((g[c + "_pg"] * g["w"]).sum() / tw)
        for c in ("tgt_share", "ay_share", "snap_pct"):
            vals = g[[c, "w"]].dropna()
            r[c] = float((vals[c] * vals["w"]).sum() / vals["w"].sum()) if len(vals) and vals["w"].sum() else np.nan
        rows.append(r)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# PROJECT
# ---------------------------------------------------------------------------

def shrink(player_rate, pos_rate, n, k):
    """Pull a player's rate toward the positional mean. Small sample -> mostly mean."""
    if n <= 0 or player_rate != player_rate:
        return pos_rate
    return (n * player_rate + k * pos_rate) / (n + k)


def project(df, ros_now, ros_prev, scoring, exp_td_rates=None):
    import pandas as pd
    import numpy as np

    sc = SCORING[scoring]

    # current team and age
    rn = ros_now[["full_name", "team", "birth_date", "years_exp"]].drop_duplicates("full_name")
    rn.columns = ["name", "team_2026", "birth_date", "years_exp"]
    rp = ros_prev[["full_name", "team"]].drop_duplicates("full_name")
    rp.columns = ["name", "team_2025"]
    df = df.merge(rn, on="name", how="left").merge(rp, on="name", how="left")

    df["birth_date"] = pd.to_datetime(df["birth_date"], errors="coerce")
    df["age"] = ((pd.Timestamp("%d-09-01" % TARGET_SEASON) - df["birth_date"]).dt.days / 365.25).round(1)
    df["changed_team"] = (df["team_2026"].notna() & df["team_2025"].notna()
                          & (df["team_2026"] != df["team_2025"]))
    df["on_roster"] = df["team_2026"].notna()

    # ---- positional mean efficiency, weighted by volume ----
    means = {}
    for pos in POS:
        p = df[df["pos"] == pos]
        tg, ca = p["targets"].sum(), p["carries"].sum()
        means[pos] = {
            "ypt": (p["rec_yards"].sum() / tg) if tg else 0.0,
            "catch": (p["receptions"].sum() / tg) if tg else 0.0,
            "rec_td_rate": (p["rec_tds"].sum() / tg) if tg else 0.0,
            "ypc": (p["rush_yards"].sum() / ca) if ca else 0.0,
            "rush_td_rate": (p["rush_tds"].sum() / ca) if ca else 0.0,
        }

    exp_td_rates = exp_td_rates or {}
    out = []
    for _, r in df.iterrows():
        pos = r["pos"]
        m = means[pos]
        my_exp = exp_td_rates.get(r["player_id"], {})
        age = r["age"] if r["age"] == r["age"] else 26.0

        curve = AGE_CURVE.get(pos, {})
        if curve:
            keys = sorted(curve)
            a = int(round(min(max(age, keys[0]), keys[-1])))
            age_mult = curve.get(a, curve[keys[-1] if a > keys[-1] else keys[0]])
        else:
            age_mult = 1.0

        # volume, age-adjusted
        tgt = r["targets"] * age_mult
        car = r["carries"] * age_mult

        # efficiency, regressed on the player's own sample size
        n_t = r["targets"] * max(r["games_sample"], 1)
        n_c = r["carries"] * max(r["games_sample"], 1)
        ypt = shrink(r["rec_yards"] / r["targets"] if r["targets"] else np.nan, m["ypt"], n_t, SHRINK["ypt"])
        cr = shrink(r["receptions"] / r["targets"] if r["targets"] else np.nan, m["catch"], n_t, SHRINK["catch_rate"])
        # shrink toward HIS OWN expected TD rate (from ff_opportunity's play-level
        # model — captures his real red-zone/goal-line role) when we have it,
        # falling back to the blanket positional rate only when we don't
        rec_td_target = my_exp.get("rec", m["rec_td_rate"])
        rush_td_target = my_exp.get("rush", m["rush_td_rate"])
        rtd = shrink(r["rec_tds"] / r["targets"] if r["targets"] else np.nan, rec_td_target, n_t, SHRINK["td_rate"])
        ypc = shrink(r["rush_yards"] / r["carries"] if r["carries"] else np.nan, m["ypc"], n_c, SHRINK["ypc"])
        gtd = shrink(r["rush_tds"] / r["carries"] if r["carries"] else np.nan, rush_td_target, n_c, SHRINK["td_rate"])

        ppg = (cr * tgt * sc["rec"] + ypt * tgt * sc["recYd"] + rtd * tgt * sc["recTD"]
               + ypc * car * sc["rushYd"] + gtd * car * sc["rushTD"]
               + r["pass_yards"] * sc["passYd"] + r["pass_tds"] * sc["passTD"] + r["ints"] * sc["int"])

        games = BASE_GAMES.get(pos, 15.0)
        if age >= 30:
            games -= 1.0
        if age >= 32:
            games -= 1.0
        if r["last_games"] <= 8:
            games -= 0.5

        out.append({
            "player_id": r["player_id"],
            "name": r["name"], "pos": pos, "team": r.get("team_2026"),
            "age": age, "age_mult": round(age_mult, 3),
            "snap_pct": round(r["snap_pct"], 1) if r["snap_pct"] == r["snap_pct"] else None,
            "tgt_share": round(r["tgt_share"] * 100, 1) if r["tgt_share"] == r["tgt_share"] else None,
            "tgt_pg": round(tgt, 1), "car_pg": round(car, 1),
            "ypt": round(ypt, 2), "ypc": round(ypc, 2),
            "games": round(games, 1),
            "ppg": round(ppg, 2),
            "proj": round(ppg * games, 1),
            # floored at 0 — a player with almost no attempts can land on a
            # slightly negative blended per-game rate (e.g. sack yardage
            # outweighing a handful of completions), and "-30 passing yards
            # projected" is never a meaningful answer regardless of how the
            # arithmetic got there
            "p_rec": round(max(0.0, cr * tgt * games), 1),
            "p_rec_yds": round(max(0.0, ypt * tgt * games), 1),
            "p_rec_tds": round(max(0.0, rtd * tgt * games), 1),
            "p_rush_yds": round(max(0.0, ypc * car * games), 1),
            "p_rush_tds": round(max(0.0, gtd * car * games), 1),
            "p_pass_yds": round(max(0.0, r["pass_yards"] * games), 1),
            "p_pass_tds": round(max(0.0, r["pass_tds"] * games), 1),
            "p_ints": round(max(0.0, r["ints"] * games), 1),
            "changed_team": bool(r["changed_team"]),
            "on_roster": bool(r["on_roster"]),
            "sample_games": int(r["games_sample"]),
        })

    res = pd.DataFrame(out)
    res = res[res["on_roster"]].reset_index(drop=True)
    return res.sort_values("proj", ascending=False).reset_index(drop=True)


def apply_context(res, team_profiles, ol_grades, rusher_shares, scoring):
    """Second pass: adjust the base projection for who is blocking for him and
    who else is on the field. Bounded to +/-CONTEXT_CAP so it nudges rather
    than overrides the opportunity model above. See the module docstring."""
    import pandas as pd
    import numpy as np

    sc = SCORING[scoring]
    tp = team_profiles.set_index("team")
    shares_by_player = {
        pid: dict(zip(g["direction"], g["share"]))
        for pid, g in rusher_shares.groupby("player_id")
    }
    ol_by_team = {
        team: dict(zip(g["direction"], g["epa_pct"]))
        for team, g in ol_grades.groupby("team")
    }
    # volume-weighted (share of all carries), not a mean of each player's own
    # share — averaging shares directly overweights small-sample players (one
    # carry left = a "left share" of 1.0, same voice as a 300-carry back) and
    # the three directions can end up summing well past 1.0
    league_avg_shares = (rusher_shares.groupby("direction")["n"].sum()
                          / rusher_shares["n"].sum()).to_dict()

    weapons = res[res["pos"].isin(["WR", "TE"])].groupby("team")["proj"].sum()
    weapons_pct = (weapons.rank(pct=True) * 100) if len(weapons) else pd.Series(dtype=float)

    def clamp(x):
        return max(-1.0, min(1.0, x))

    mults, notes = [], []
    for _, r in res.iterrows():
        pos, team, pid = r["pos"], r["team"], r["player_id"]
        z, note = 0.0, None

        if pos == "RB" and team in ol_by_team:
            grades = ol_by_team[team]
            own = shares_by_player.get(pid)
            # a direction missing from his OWN record means he simply never ran
            # that way (his real shares already sum to 1 across what he did run) —
            # it must default to 0, not league average, or the weights double up
            # past 1.0 and the "percentile" can exceed 100. League average is only
            # for a player with no rushing record at all.
            my_shares = ({d: own.get(d, 0.0) for d in ("left", "middle", "right")}
                         if own else league_avg_shares)
            ol_score = sum(my_shares[d] * grades.get(d, 50.0) for d in ("left", "middle", "right"))
            pass_pct = tp.loc[team, "pass_epa_pct"] if team in tp.index else 50.0
            z = clamp(((ol_score - 50) * 0.7 + (pass_pct - 50) * 0.3) / 50)
            note = ("OL by his own gap mix: %.0f pctl (L%.0f/M%.0f/R%.0f split) "
                    "+ team pass game %.0f pctl"
                    % (ol_score, 100 * my_shares.get("left", 0), 100 * my_shares.get("middle", 0),
                       100 * my_shares.get("right", 0), pass_pct))
        elif pos == "QB" and team in tp.index:
            w_pct = weapons_pct.get(team, 50.0)
            # sack_rate_pct / hit_rate_pct are already oriented high = good
            # (team_profiles ranks them ascending=False, so a low sack rate
            # gets a high percentile) — no inversion needed here
            block_pct = (tp.loc[team, "sack_rate_pct"] + tp.loc[team, "hit_rate_pct"]) / 2
            z = clamp(((w_pct - 50) * 0.65 + (block_pct - 50) * 0.35) / 50)
            note = "weapons %.0f pctl, pass protection %.0f pctl" % (w_pct, block_pct)

        m = round(1 + CONTEXT_CAP * z, 3)
        mults.append(m)
        notes.append(note)

    res = res.copy()
    res["context_mult"] = mults
    res["context_note"] = notes

    # rescale positive production, but not interceptions — a better-blocked,
    # better-armed QB throws fewer picks, not more, so scaling ints the same
    # direction as everything else would have it backwards
    for c in ("p_rec", "p_rec_yds", "p_rec_tds", "p_rush_yds", "p_rush_tds",
              "p_pass_yds", "p_pass_tds"):
        res[c] = (res[c] * res["context_mult"]).round(1)

    res["proj"] = (res["p_rec"] * sc["rec"] + res["p_rec_yds"] * sc["recYd"]
                   + res["p_rec_tds"] * sc["recTD"] + res["p_rush_yds"] * sc["rushYd"]
                   + res["p_rush_tds"] * sc["rushTD"] + res["p_pass_yds"] * sc["passYd"]
                   + res["p_pass_tds"] * sc["passTD"] + res["p_ints"] * sc["int"]).round(1)
    res["ppg"] = (res["proj"] / res["games"]).round(2)
    return res.sort_values("proj", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scoring", default="half", choices=list(SCORING))
    ap.add_argument("--pos", default=None)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default="profiles.csv")
    a = ap.parse_args()

    log("Loading nflverse data...")
    stats, snaps, rn, rp = load_all()
    log("Aggregating seasons %s..." % PRIOR_SEASONS)
    agg = season_aggregates(stats, snaps)
    log("Blending %d player-seasons..." % len(agg))
    blended = blend(agg)
    log("Loading expected TD rates (red-zone/goal-line role, from ff_opportunity)...")
    exp_td_rates = load_expected_td_rates()
    log("Projecting %d players..." % len(blended))
    res = project(blended, rn, rp, a.scoring, exp_td_rates)

    log("Loading 2026 rookies (draft capital + vacated opportunity)...")
    import pandas as pd
    import rookies as rookies_mod
    rookie_rows = rookies_mod.component_rows(sc=SCORING[a.scoring])
    n_vets = len(res)
    res = pd.concat([res, rookie_rows], ignore_index=True, sort=False)
    log("  %d veterans + %d rookies = %d players before context" % (n_vets, len(rookie_rows), len(res)))

    log("Loading team context (O-line by direction, weapons, pass protection)...")
    tp, ol, shares = load_team_context()
    log("Applying context adjustment (capped at +/-%.0f%%)..." % (100 * CONTEXT_CAP))
    res = apply_context(res, tp, ol, shares, a.scoring)

    res.to_csv(a.out, index=False)
    log("Wrote %s (%d players)" % (a.out, len(res)))

    # war-room import format: component stats, so each league rescores them itself
    wr = res[["name", "pos", "team", "p_pass_yds", "p_pass_tds", "p_ints",
              "p_rush_yds", "p_rush_tds", "p_rec", "p_rec_yds", "p_rec_tds"]].copy()
    wr.columns = ["Player", "POS", "Team", "Pass Yds", "Pass TDs", "INT",
                  "Rush Yds", "Rush TDs", "REC", "Rec Yds", "Rec TDs"]
    wr["FL"] = 1.5
    wr.to_csv("war_room_import.csv", index=False)
    log("Wrote war_room_import.csv - paste or upload this into the Data tab")

    log("")
    log("NOT IN THIS FILE: anyone without %s snaps. That means every 2026 rookie," % "/".join(map(str, PRIOR_SEASONS)))
    log("plus players who missed both seasons. You must add them by hand.")
    show = res if not a.pos else res[res["pos"] == a.pos]
    show = show.head(a.top)
    print()
    print("%-24s %-3s %-4s %4s %6s %6s %6s %6s %7s  %s"
          % ("PLAYER", "POS", "TM", "AGE", "SNAP%", "TGT%", "TGT/G", "CAR/G", "PROJ", "FLAGS"))
    print("-" * 104)
    for _, r in show.iterrows():
        flags = []
        if r["changed_team"]:
            flags.append("NEW TEAM")
        if r["age_mult"] < 0.90:
            flags.append("age %.0f%%" % (r["age_mult"] * 100))
        if r["sample_games"] < 12:
            flags.append("thin sample")
        if r["context_mult"] >= 1.05:
            flags.append("context +%.0f%%" % (100 * (r["context_mult"] - 1)))
        elif r["context_mult"] <= 0.95:
            flags.append("context %.0f%%" % (100 * (r["context_mult"] - 1)))
        print("%-24s %-3s %-4s %4.1f %6s %6s %6.1f %6.1f %7.1f  %s"
              % (str(r["name"])[:24], r["pos"], str(r["team"])[:4], r["age"],
                 r["snap_pct"] if r["snap_pct"] else "-", r["tgt_share"] if r["tgt_share"] else "-",
                 r["tgt_pg"], r["car_pg"], r["proj"], ", ".join(flags)))
    print()
    print("  context_note in profiles.csv explains every context adjustment above —")
    print("  OL grade by the back's own gap tendency, weapons strength, pass protection.")


if __name__ == "__main__":
    main()
