#!/usr/bin/env python3
"""
rookies.py — project rookies from draft capital and vacated opportunity.

THE ARGUMENT
  Rookies have no NFL history, so the usual opportunity model has nothing to
  chew on. What it can use instead:

    1. DRAFT CAPITAL. The strongest single predictor of rookie fantasy
       production, stronger than college stats or athletic testing. It works
       because it predicts VOLUME: teams play their investments. A first-round
       back gets carries whether he has earned them or not.

    2. VACATED OPPORTUNITY. Targets and carries that walked out the door on his
       new team. A third-round receiver behind an established WR1 and a
       third-round receiver inheriting 140 vacated targets are not the same bet.

  Rather than guess at how those combine, this calibrates on history: every
  skill-position rookie since 2012, what they actually scored, bucketed by
  draft capital. The output is an empirical distribution, not an opinion.

WHAT IT DELIBERATELY IGNORES
  Preseason box scores (compiled against backups), and hype. Buzz is already
  priced into ADP — paying for public information is how you overpay.

USAGE
    python rookies.py                 # full report
    python rookies.py --pos RB
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

CACHE = "nflverse_cache"
HIST_START = 2012
LAST_SEASON = 2025
DRAFT_YEAR = 2026
POS = ["QB", "RB", "WR", "TE"]

HALF = dict(rec=0.5, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6,
            passYd=0.04, passTD=4, interception=-2)

# Draft capital buckets. The cliff between the top of round one and the rest
# of it is real and large, which is why the first bucket is so narrow.
TIERS = [(1, 10, "Top 10"), (11, 32, "Rest of Rd 1"), (33, 64, "Round 2"),
         (65, 105, "Round 3"), (106, 175, "Rd 4-5"), (176, 300, "Rd 6-7")]


def tier_of(pick):
    for lo, hi, name in TIERS:
        if lo <= pick <= hi:
            return name
    return "Undrafted"


def log(m):
    print(m, file=sys.stderr)


# load_draft_picks() comes from PFR and uses PFR's team codes, which differ
# from the nflverse codes every other file in this project uses (rosters,
# depth charts, play-by-play, team_profiles.csv). Left unmapped, a rookie's
# team column silently fails to join against anything else in the pipeline —
# vacated()'s opening_pct lookup was doing exactly that for these 8 teams
# before this map existed.
PFR_TO_NFLVERSE_TEAM = {
    "GNB": "GB", "KAN": "KC", "LAR": "LA", "LVR": "LV",
    "NOR": "NO", "NWE": "NE", "SFO": "SF", "TAM": "TB",
}


def load():
    import nflreadpy as nfl
    import pandas as pd
    os.makedirs(CACHE, exist_ok=True)

    def cached(name, fn):
        p = os.path.join(CACHE, name + ".parquet")
        if os.path.exists(p):
            return pd.read_parquet(p)
        log("  downloading %s" % name)
        d = fn().to_pandas()
        d.to_parquet(p)
        return d

    picks = cached("draft_all", lambda: nfl.load_draft_picks())
    picks = picks.copy()
    picks["team"] = picks["team"].replace(PFR_TO_NFLVERSE_TEAM)
    hist = cached("stats_hist", lambda: nfl.load_player_stats(list(range(HIST_START, LAST_SEASON + 1))))
    ros26 = cached("roster_now", lambda: nfl.load_rosters([DRAFT_YEAR]))
    # load_rosters() returns "AZ" for Arizona; PFR/play-by-play/everything
    # else here says "ARI". Same failure class as PFR_TO_NFLVERSE_TEAM above.
    ros26 = ros26.copy()
    ros26["team"] = ros26["team"].replace({"AZ": "ARI"})
    return picks, hist, ros26


def fantasy_points(df):
    import pandas as pd
    g = lambda c: pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
    return (g("receptions") * HALF["rec"] + g("receiving_yards") * HALF["recYd"]
            + g("receiving_tds") * HALF["recTD"] + g("rushing_yards") * HALF["rushYd"]
            + g("rushing_tds") * HALF["rushTD"] + g("passing_yards") * HALF["passYd"]
            + g("passing_tds") * HALF["passTD"] + g("passing_interceptions") * HALF["interception"])


COMPONENTS = ["receptions", "receiving_yards", "receiving_tds", "rushing_yards", "rushing_tds",
              "passing_yards", "passing_tds", "passing_interceptions"]
# maps a raw nflverse column to the p_* name profiles.py uses, so rookie and
# veteran output share one shape and can sit in the same file
COMPONENT_OUT = {"receptions": "p_rec", "receiving_yards": "p_rec_yds", "receiving_tds": "p_rec_tds",
                 "rushing_yards": "p_rush_yds", "rushing_tds": "p_rush_tds",
                 "passing_yards": "p_pass_yds", "passing_tds": "p_pass_tds",
                 "passing_interceptions": "p_ints"}
# PFR's own draft-history table already carries its own career-stat columns
# (receptions, rush_yards, etc.) — prefixed so ours never collides with theirs
# in the merge below, which pandas would otherwise resolve by silently
# suffixing one side (_x/_y) rather than erroring.
HIST_COL = {c: "hist_" + c for c in COMPONENTS}


def build_history(picks, hist):
    """Every rookie since 2012 and what they actually scored — both the single
    fantasy-point total (for the report) and each raw component (so a
    projection can be rebuilt in any league's own scoring, the same contract
    profiles.py uses)."""
    import pandas as pd

    h = hist[hist["season_type"] == "REG"].copy()
    h["fp"] = fantasy_points(h)
    for c in COMPONENTS:
        if c not in h.columns:
            h[c] = 0.0
        h[c] = pd.to_numeric(h[c], errors="coerce").fillna(0.0)

    season_tot = h.groupby(["player_id", "season"], as_index=False).agg(
        fp=("fp", "sum"), rookie_games=("week", "nunique"),
        **{HIST_COL[c]: (c, "sum") for c in COMPONENTS})

    p = picks[(picks["season"] >= HIST_START) & (picks["season"] <= LAST_SEASON)].copy()
    p = p[p["position"].isin(POS)]
    p = p[p["gsis_id"].notna()]
    p["tier"] = p["pick"].apply(tier_of)

    m = p.merge(season_tot, left_on=["gsis_id", "season"], right_on=["player_id", "season"], how="left")
    m["fp"] = m["fp"].fillna(0.0)     # drafted, never produced — that is a real outcome
    m["rookie_games"] = m["rookie_games"].fillna(0)
    for c in COMPONENTS:
        hc = HIST_COL[c]
        m[hc] = m[hc].fillna(0.0)
        m[hc + "_pg"] = m[hc] / m["rookie_games"].clip(lower=1)   # 0 games -> 0 per-game, not NaN
    return m


def tier_table(hist_rookies):
    """Empirical distribution of rookie outcomes by position and draft capital.

    Alongside the single fantasy-point summary (for the console report), this
    also carries the median per-game rate for every raw component and the
    median games actually played. rate x games reconstructs a projection in
    component form — the same shape profiles.py projects veterans in — so a
    league can score a rookie under its own rules instead of being stuck with
    one fixed scoring assumption.
    """
    import numpy as np
    out = {}
    for pos in POS:
        for _, _, tname in TIERS:
            sub = hist_rookies[(hist_rookies["position"] == pos) & (hist_rookies["tier"] == tname)]
            if len(sub) < 8:
                continue
            fp = sub["fp"].values
            entry = {
                "n": len(sub),
                "median": float(np.median(fp)),
                "mean": float(np.mean(fp)),
                "p25": float(np.percentile(fp, 25)),
                "p75": float(np.percentile(fp, 75)),
                "p90": float(np.percentile(fp, 90)),
                # "useful" = roughly a startable season in a 12-team league
                "useful_rate": float((fp >= {"QB": 200, "RB": 130, "WR": 130, "TE": 90}[pos]).mean()),
                "zero_rate": float((fp < 30).mean()),
                "median_games": float(np.median(sub["rookie_games"].values)),
            }
            for c in COMPONENTS:
                entry[c + "_pg"] = float(np.median(sub[HIST_COL[c] + "_pg"].values))
            out[(pos, tname)] = entry
    return out


def vacated(hist, ros26):
    """Targets and carries on each team that are not coming back in 2026."""
    import pandas as pd

    last = hist[(hist["season"] == LAST_SEASON) & (hist["season_type"] == "REG")].copy()
    for c in ("targets", "carries"):
        last[c] = pd.to_numeric(last[c], errors="coerce").fillna(0.0)

    who = last.groupby(["player_display_name", "team"], as_index=False).agg(
        targets=("targets", "sum"), carries=("carries", "sum"))

    r = ros26[["full_name", "team"]].drop_duplicates("full_name")
    r.columns = ["player_display_name", "team_2026"]
    who = who.merge(r, on="player_display_name", how="left")

    # gone = not on a 2026 roster at all, or on a different team now
    who["gone"] = who["team_2026"].isna() | (who["team_2026"] != who["team"])
    vac = who[who["gone"]].groupby("team", as_index=False).agg(
        vac_targets=("targets", "sum"), vac_carries=("carries", "sum"))
    tot = who.groupby("team", as_index=False).agg(
        tot_targets=("targets", "sum"), tot_carries=("carries", "sum"))
    v = vac.merge(tot, on="team", how="right").fillna(0.0)
    v["pct_targets_open"] = (100 * v["vac_targets"] / v["tot_targets"].replace(0, 1)).round(1)
    v["pct_carries_open"] = (100 * v["vac_carries"] / v["tot_carries"].replace(0, 1)).round(1)
    return v


def project_rookies(picks, table, vac):
    """Comp-based baseline, nudged by how much room the landing spot actually has.

    Outputs the same p_rec / p_rec_yds / ... component columns profiles.py
    produces for veterans, not just a single point total — so a rookie can be
    scored under any league's own rules instead of being locked to one
    half-PPR assumption, and can sit in the same ranked file as everyone else.
    """
    import pandas as pd

    r = picks[(picks["season"] == DRAFT_YEAR) & (picks["position"].isin(POS))].copy()
    r["tier"] = r["pick"].apply(tier_of)
    vmap = vac.set_index("team").to_dict("index")

    rows = []
    for _, p in r.iterrows():
        key = (p["position"], p["tier"])
        base = table.get(key)
        if not base:
            continue
        team = p["team"]
        v = vmap.get(team, {})
        opening = v.get("pct_carries_open", 0) if p["position"] == "RB" else v.get("pct_targets_open", 0)

        # Opening is a modifier, not the driver. A wide-open depth chart lifts a
        # rookie's floor; a crowded one caps it. Range held to +/-25% so landing
        # spot cannot overwhelm draft capital, which is the stronger signal.
        mult = 1.0 + max(-0.25, min(0.25, (opening - 30.0) / 120.0))
        games = base["median_games"]

        row = {
            "player_id": p["gsis_id"],
            "player": p["pfr_player_name"], "pos": p["position"], "team": team,
            "pick": int(p["pick"]), "tier": p["tier"], "college": p.get("college", ""),
            "comp_n": base["n"],
            "comp_median": round(base["median"], 1),
            "comp_p75": round(base["p75"], 1),
            "comp_p90": round(base["p90"], 1),
            "useful_rate": round(100 * base["useful_rate"]),
            "bust_rate": round(100 * base["zero_rate"]),
            "opening_pct": opening,
            "median_games": round(games, 1),
        }
        for c in COMPONENTS:
            row[COMPONENT_OUT[c]] = round(base[c + "_pg"] * mult * games, 1)

        row["proj"] = round(fantasy_points_from_row(row) , 1)
        row["upside"] = round(base["p75"] * mult, 1)
        rows.append(row)
    return pd.DataFrame(rows).sort_values("proj", ascending=False).reset_index(drop=True)


def fantasy_points_from_row(row):
    """Recompute the half-PPR total from the component columns, so the console
    report and comp_median/p75 (still fp-based) stay comparable to proj."""
    return (row["p_rec"] * HALF["rec"] + row["p_rec_yds"] * HALF["recYd"] + row["p_rec_tds"] * HALF["recTD"]
            + row["p_rush_yds"] * HALF["rushYd"] + row["p_rush_tds"] * HALF["rushTD"]
            + row["p_pass_yds"] * HALF["passYd"] + row["p_pass_tds"] * HALF["passTD"]
            + row["p_ints"] * HALF["interception"])


DEFAULT_SC = dict(rec=0.5, recYd=0.1, recTD=6, rushYd=0.1, rushTD=6, passYd=0.04, passTD=4, int=-2)


def component_rows(sc=None):
    """Rookie projections reshaped into profiles.py's own row schema (same
    player_id / pos / team / p_* columns), so profiles.py can concatenate them
    into the same pool BEFORE its context-adjustment step.

    This is the point of this function: a rookie merged in *after* context is
    applied — the original design — never gets graded against his own line or
    his own team's weapons the way a veteran does. A rookie RB landing behind
    a great line should get that credit; one landing behind a bad one should
    not. Running him through the same apply_context() call is what makes that
    happen. He'll have no rushing-direction history of his own (no NFL
    carries yet), so apply_context()'s existing fallback to the league-average
    gap split kicks in automatically — the same thing that already happens
    for a veteran with an unrelated small sample.

    sc: a profiles.py-style scoring dict (keys rec/recYd/recTD/rushYd/rushTD/
    passYd/passTD/int) used only to seed an initial proj so rookies count
    correctly toward team WEAPONS totals before the real per-league rescore
    at the end of apply_context. Defaults to half PPR if not supplied.
    """
    import pandas as pd

    sc = sc or DEFAULT_SC
    picks, hist, ros26 = load()
    hr = build_history(picks, hist)
    table = tier_table(hr)
    vac = vacated(hist, ros26)
    proj = project_rookies(picks, table, vac)

    rows = []
    for _, r in proj.iterrows():
        proj_pts = (r["p_rec"] * sc["rec"] + r["p_rec_yds"] * sc["recYd"] + r["p_rec_tds"] * sc["recTD"]
                    + r["p_rush_yds"] * sc["rushYd"] + r["p_rush_tds"] * sc["rushTD"]
                    + r["p_pass_yds"] * sc["passYd"] + r["p_pass_tds"] * sc["passTD"]
                    + r["p_ints"] * sc["int"])
        rows.append({
            "player_id": r["player_id"], "name": r["player"], "pos": r["pos"], "team": r["team"],
            # NaN, not None — profiles.py's report formats age with "%4.1f",
            # which raises on None but prints fine ("nan") on a float NaN
            "age": float("nan"), "age_mult": 1.0, "snap_pct": None, "tgt_share": None,
            "tgt_pg": 0.0, "car_pg": 0.0, "ypt": 0.0, "ypc": 0.0,
            "games": r["median_games"], "ppg": round(proj_pts / max(r["median_games"], 1), 2),
            "proj": round(proj_pts, 1),
            "p_rec": r["p_rec"], "p_rec_yds": r["p_rec_yds"], "p_rec_tds": r["p_rec_tds"],
            "p_rush_yds": r["p_rush_yds"], "p_rush_tds": r["p_rush_tds"],
            "p_pass_yds": r["p_pass_yds"], "p_pass_tds": r["p_pass_tds"], "p_ints": r["p_ints"],
            "changed_team": False, "team_change_car_mult": 1.0, "team_change_tgt_mult": 1.0,
            "on_roster": True, "sample_games": 0,
            "rookie": True, "pick": r["pick"], "tier": r["tier"],
            # how often THIS comp profile actually panned out historically —
            # carried through so the board can show it, not just the point
            # estimate. A Round 3 WR's median outcome and a proven WR2's
            # median outcome look identical as a single number; they are not
            # equally trustworthy, and bust_rate is the honest difference.
            "useful_rate": r["useful_rate"], "bust_rate": r["bust_rate"],
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", default=None)
    ap.add_argument("--out", default="rookies.csv")
    a = ap.parse_args()

    log("Loading...")
    picks, hist, ros26 = load()
    log("Building rookie history %d-%d..." % (HIST_START, LAST_SEASON))
    hr = build_history(picks, hist)
    table = tier_table(hr)
    vac = vacated(hist, ros26)

    print()
    print("WHAT ROOKIES ACTUALLY DID, %d-%d  (half PPR, full season)" % (HIST_START, LAST_SEASON))
    print("-" * 88)
    print("%-4s %-14s %5s %8s %8s %8s %10s %8s" %
          ("POS", "DRAFT CAPITAL", "N", "MEDIAN", "75th", "90th", "USEFUL%", "BUST%"))
    for pos in POS:
        for _, _, t in TIERS:
            d = table.get((pos, t))
            if d:
                print("%-4s %-14s %5d %8.0f %8.0f %8.0f %9.0f%% %7.0f%%" %
                      (pos, t, d["n"], d["median"], d["p75"], d["p90"],
                       100 * d["useful_rate"], 100 * d["zero_rate"]))
        print()

    proj = project_rookies(picks, table, vac)
    proj.to_csv(a.out, index=False)
    log("Wrote %s" % a.out)
    log("(war_room_import.csv is now written by profiles.py, which merges rookies")
    log(" in before context-adjustment — run profiles.py to update the draft board.)")

    show = proj if not a.pos else proj[proj["pos"] == a.pos]
    print("2026 ROOKIES")
    print("-" * 88)
    print("%-22s %-3s %-4s %5s %-13s %7s %7s %8s %7s" %
          ("PLAYER", "POS", "TM", "PICK", "CAPITAL", "OPEN%", "PROJ", "UPSIDE", "USEFUL"))
    for _, r in show.head(28).iterrows():
        print("%-22s %-3s %-4s %5d %-13s %6.0f%% %7.0f %8.0f %6.0f%%" %
              (str(r["player"])[:22], r["pos"], str(r["team"])[:4], r["pick"],
               r["tier"], r["opening_pct"], r["proj"], r["upside"], r["useful_rate"]))
    print()
    print("PROJ is the median outcome for this profile, not a forecast for this player.")
    print("Half of comparable rookies beat it and half did not. USEFUL is how often")
    print("that profile produced a startable season at all.")
    print()


if __name__ == "__main__":
    main()
