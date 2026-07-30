#!/usr/bin/env python3
"""
validate.py — sanity-check the generated data before you trust it.

WHY THIS EXISTS
  Every feature added to this project so far has shipped with a real bug in
  it: a percentile that exceeded 100, a scoring sign flipped backwards, stat
  columns transposed so a running back showed 431 passing touchdowns, a team
  code that silently failed to join and broke a feature nobody was checking.
  Every one of those was caught by hand, by someone happening to look at the
  right player. That does not scale and it will not catch the next one.

  This script is the automatic version of "does this number look sane." It
  is not a model of correctness — it cannot tell you a projection is RIGHT,
  only that it is not obviously broken. Run it after every regeneration.

USAGE
    python validate.py
    python validate.py --strict     # exit non-zero on any warning, not just errors
"""

import argparse
import sys

VALID_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO",
    "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}
POS = {"QB", "RB", "WR", "TE"}


def log(m):
    print(m, file=sys.stderr)


class Findings:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def report(self, name):
        print()
        print("=" * 70)
        print("  %s" % name)
        print("=" * 70)
        if not self.errors and not self.warnings:
            print("  clean — nothing flagged")
            return
        for e in self.errors:
            print("  [ERROR]   %s" % e)
        for w in self.warnings:
            print("  [WARNING] %s" % w)


def check_war_room(path="war_room_import.csv"):
    """The file the draft tool actually imports. These checks matter most —
    this is what you're staring at on draft day."""
    import pandas as pd
    f = Findings()

    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        f.err("%s does not exist — run profiles.py" % path)
        return f

    # duplicate players
    dupes = df["Player"][df["Player"].duplicated()].tolist()
    if dupes:
        f.err("duplicate player rows: %s" % ", ".join(dupes[:10]))

    # team codes — this is exactly the bug class that silently broke
    # opening_pct for 8 teams before anyone noticed
    bad_teams = sorted(set(df["Team"].dropna()) - VALID_TEAMS)
    if bad_teams:
        f.err("unrecognized team codes (not in the standard 32): %s" % ", ".join(bad_teams))

    # positions
    bad_pos = sorted(set(df["POS"].dropna()) - POS)
    if bad_pos:
        f.err("unrecognized positions: %s" % ", ".join(bad_pos))

    stat_cols = ["Pass Yds", "Pass TDs", "INT", "Rush Yds", "Rush TDs", "REC", "Rec Yds", "Rec TDs"]

    # negative production — nonsensical for any of these regardless of position
    for c in stat_cols:
        neg = df[df[c] < 0]
        for _, r in neg.iterrows():
            f.err("%s (%s): negative %s = %.1f" % (r["Player"], r["POS"], c, r[c]))

    # position-appropriate stats. Generous thresholds — a WR can throw a
    # trick-play touchdown, a QB can catch a gadget pass. The bug this catches
    # is columns transposed wholesale (hundreds of yards in the wrong slot),
    # not the occasional real trick play worth a few yards.
    checks = [
        (df["POS"] != "QB", "Pass Yds", 60, "non-QB with real passing volume"),
        (df["POS"] != "QB", "Pass TDs", 1, "non-QB with more than 1 passing TD"),
        (df["POS"] == "QB", "Rec Yds", 60, "QB with real receiving volume"),
        (df["POS"] == "QB", "REC", 3, "QB with more than 3 projected receptions"),
        (df["POS"].isin(["WR", "TE"]), "Rush Yds", 150, "WR/TE with real rushing volume"),
    ]
    for mask, col, thresh, desc in checks:
        bad = df[mask & (df[col] > thresh)]
        for _, r in bad.iterrows():
            f.warn("%s: %s (%.1f %s > %s threshold)" % (desc, r["Player"], r[col], col, thresh))

    # a sanity ceiling — no realistic single-season projection should clear this
    # under any normal scoring system; catches gross multiplication errors
    approx_pts = (df["Pass Yds"] * 0.04 + df["Pass TDs"] * 4 + df["INT"] * -2
                  + df["Rush Yds"] * 0.1 + df["Rush TDs"] * 6
                  + df["REC"] * 0.5 + df["Rec Yds"] * 0.1 + df["Rec TDs"] * 6)
    absurd = df[approx_pts > 500]
    for _, r in absurd.iterrows():
        f.err("%s: approx %.0f half-PPR points — implausibly high, check for a scaling bug"
              % (r["Player"], approx_pts[r.name]))

    n = len(df)
    if n < 400:
        f.warn("only %d players in the file — expected 500+" % n)
    if n > 900:
        f.warn("%d players in the file — expected under 700, check for accidental duplication" % n)

    return f


def check_profiles(path="profiles.csv"):
    """The audit file — has the context_mult / context_note columns the war-room
    export does not, so bugs in the context-adjustment layer show up here even
    when they wouldn't be visible in the final component stats alone."""
    import pandas as pd
    f = Findings()

    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        f.warn("%s not found — skipping (only produced by profiles.py)" % path)
        return f

    if "context_mult" in df.columns:
        bad = df[(df["context_mult"] < 0.80) | (df["context_mult"] > 1.20)]
        for _, r in bad.iterrows():
            f.err("%s: context_mult %.3f is outside the intended +/-15%% cap "
                  "(some slack allowed for rounding, but this is well past it)"
                  % (r["name"], r["context_mult"]))

        rb_qb = df[df["pos"].isin(["RB", "QB"])]
        missing = rb_qb[rb_qb["context_note"].isna() | (rb_qb["context_note"] == "")]
        if len(missing):
            f.warn("%d RB/QB rows have no context_note — did they silently skip "
                   "the context step? (%s)" % (len(missing), ", ".join(missing["name"].head(5))))

        # any "N pctl" mention in context_note should be 0-100. This is exactly
        # the class of bug already caught once (a share-weighting error let
        # some rookies' OL scores read "153 pctl").
        import re
        for _, r in df.iterrows():
            note = r.get("context_note")
            if not isinstance(note, str):
                continue
            for m in re.findall(r"(-?\d+(?:\.\d+)?)\s*pctl", note):
                v = float(m)
                if v < 0 or v > 100:
                    f.err("%s: context_note cites a %.0f percentile — out of the 0-100 "
                          "range, the underlying math is producing an impossible value "
                          "(context_note: %s)" % (r["name"], v, note))

    if "games" in df.columns:
        bad = df[(df["games"] < 0) | (df["games"] > 17)]
        for _, r in bad.iterrows():
            f.err("%s: %.1f projected games — a season is 17 games" % (r["name"], r["games"]))

    dupes = df["name"][df["name"].duplicated()].tolist() if "name" in df.columns else []
    if dupes:
        f.err("duplicate player rows: %s" % ", ".join(dupes[:10]))

    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="exit non-zero on warnings too, not just errors")
    a = ap.parse_args()

    wr = check_war_room()
    wr.report("war_room_import.csv")
    pr = check_profiles()
    pr.report("profiles.csv")

    n_err = len(wr.errors) + len(pr.errors)
    n_warn = len(wr.warnings) + len(pr.warnings)
    print()
    print("%d error(s), %d warning(s)" % (n_err, n_warn))

    if n_err or (a.strict and n_warn):
        sys.exit(1)


if __name__ == "__main__":
    main()
