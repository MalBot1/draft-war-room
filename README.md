# Draft War Room

Two redraft leagues, ESPN, snake, drafting in two to four weeks.

Everything here runs on your machine. No accounts, no services, no build step.
The nflverse data is cached in this folder, so the scripts work offline.

---

## Quick start

```bash
pip install -r requirements.txt
```

Then open `draft-war-room.html` in a browser, go to the **Data** tab, and upload
`war_room_import.csv`. That replaces the demo players with real projections and
the tool is live.

Everything below is about regenerating and improving that file.

---

## The pieces

| File | What it does |
|---|---|
| `draft-war-room.html` | The draft tool. Open in a browser, works offline. |
| `profiles.py` | Veteran + rookie projections, merged, context-adjusted. Writes `war_room_import.csv` |
| `rookies.py` | Rookie projection engine (draft capital + vacated opportunity). `profiles.py` imports this directly |
| `teams.py` | Team context, O-line grading, depth chart competition |
| `adp.py` | Real average draft position + variance + bye week, from Fantasy Football Calculator. Appends onto `war_room_import.csv` after `profiles.py` runs |
| `playoff_sos.py` | How tough each team's fantasy-playoff-week schedule is, by position. Appends onto `war_room_import.csv` |
| `validate.py` | Sanity-checks the generated files — run after every regeneration |
| `espn_history.py` | Pulls your ESPN league to measure real replacement level |

| Data file | Contents |
|---|---|
| `war_room_import.csv` | **The one you import.** ~630 players (veterans + rookies), component stats + ADP + bye + playoff SOS |
| `profiles.csv` | Full output with all inputs and context notes, for auditing |
| `rookies.csv` | 2026 rookies, standalone comp-based report (same numbers, before merge) |
| `team_profiles.csv` | 32 offenses: pace, EPA, pass tendency, line, volume pools |
| `player_context.csv` | 913 players: depth rank and volume ahead of them |
| `adp.csv` | ADP + standard deviation + bye, per scoring format (standard/half/PPR/2QB) |
| `ol_grades.csv` | Run blocking graded left / middle / right, separately per team |
| `playoff_sos.csv` | Points allowed per game by position, and each team's playoff-week opponents + rating |

---

## How the projections work

Fantasy points from last season are a bad input. They bake in touchdown luck and
efficiency, and both regress hard. What carries forward is **opportunity**.

```
projected points  =  volume  x  efficiency  x  expected games
```

Each estimated separately, because each behaves differently:

- **Volume** — targets, carries, snap share. Stable year to year. The age curve is
  applied here, because age acts on role before it acts on talent.
- **Efficiency** — yards per target, yards per carry. Noisy. Regressed toward the
  positional mean, weighted by how much evidence the player actually has.
  Touchdown rate gets the harshest regression, because it is the single noisiest
  thing in fantasy football.
- **Games** — a function of age and position.

That three-factor model is blind to who else is on the field. A second pass
adjusts for that, capped at +/-15% so it nudges rather than dominates:

- **RB** — graded against his own offensive line, but not as one team-wide
  number. `teams.py` splits run blocking left / middle / right from
  play-by-play (`ol_grades.csv`) — free, no PFF subscription needed — and
  weights those three grades by the back's own history of which gaps he
  actually hits (`rusher_direction_shares`). Blended with the team's passing
  efficiency, since a credible pass game keeps boxes light for the run.
- **QB** — graded against the strength of his own current WR/TE corps (their
  own opportunity-based projections, summed) and team-level pass protection
  (sack% and hit%, `team_profiles.csv`).
- **WR/TE** — graded against their own team's QB (his own raw projection,
  same idea as "his supply line" one position removed from a RB's line) and
  team passing efficiency. Computed from the same pre-adjustment snapshot
  QB's weapons score uses, so the two don't wait on each other despite
  leaning on each other's numbers.

Every adjustment is logged in `context_note` (in `profiles.csv`) so it can be
audited, not just trusted — e.g. Jonathan Taylor's line reads `OL by his own
gap mix: 88 pctl (L26/M42/R32 split) + team pass game 66 pctl`. This still
isn't a per-lineman grade — that's PFF-only — but directional run grading is
the free substitute: it separates the line and scheme from the runner using
data that already exists in play-by-play.

**Rookies** have no NFL history, so they run on draft capital and vacated
opportunity instead, calibrated against every skill-position rookie since 2012.

---

## Regenerating the data

```bash
python profiles.py                  # -> profiles.csv, war_room_import.csv (veterans + rookies, merged)
python profiles.py --scoring ppr    # if a league is full PPR
python rookies.py                   # -> rookies.csv (standalone report only)
python teams.py                     # -> team_profiles.csv, player_context.csv, ol_grades.csv
python teams.py --team KC           # one team's depth chart
python adp.py                       # -> adp.csv, appends adp_*/stdev_*/bye onto war_room_import.csv
python adp.py --teams 10            # match your league size — who's rosterable shifts with it
python playoff_sos.py               # -> playoff_sos.csv, appends playoff_sos_pctl/label onto war_room_import.csv
python playoff_sos.py --weeks 15,16,17   # match your league's actual fantasy-playoff weeks
python validate.py                  # sanity-check everything above
```

Or `./run_all.sh` for all of it, validate.py included.

`profiles.py` is the only script that writes `war_room_import.csv`. It imports `rookies.py` directly (as Python, not by reading `rookies.csv`) so the draft class gets merged in *before* the O-line/weapons context step — a rookie RB landing on a good line gets credit for it exactly like a veteran would. `adp.py` and `playoff_sos.py` both append onto it afterward, so re-run them any time you re-run `profiles.py` or they'll be wiped along with everything else.

First run downloads from nflverse. After that it reads the local cache. Delete
`nflverse_cache/` to force a refresh.

**Re-run within a few days of your draft.** Depth charts in July are nearly
meaningless below the obvious starters — teams list players by contract or
alphabet. They firm up in late August, right in your draft window.

---

## Your ESPN league

```bash
export ESPN_LEAGUE_ID=...
export ESPN_YEAR=2025          # last season, not this one
export ESPN_S2='...'
export ESPN_SWID='{...}'
python espn_history.py --pull
```

Cookies come from your browser: log into fantasy.espn.com, developer tools,
Application or Storage, Cookies, fantasy.espn.com.

`espn_s2` is a live session token for your ESPN account. Treat it like a
password. It is in `.gitignore` for a reason, and it expires — if the pull works
today and fails in two weeks, that is why.

This measures what the last starting QB, RB, WR, and TE in *your* league actually
scored. That number is what the draft model's baseline should be calibrated
against, and right now it is estimated rather than measured.

---

## What the rookie history says

Every skill-position rookie, 2012 through 2025:

| Profile | Median pts | Startable season | Bust |
|---|---|---|---|
| Top-10 RB | 221 | 100% (n=8) | 0% |
| Rest of Rd 1 RB | 138 | 55% | 9% |
| Top-10 WR | 154 | 63% | 16% |
| Rest of Rd 1 WR | 106 | 31% | 12% |
| Round 3 WR | 43 | 8% | 35% |
| Rd 4-5 WR | 11 | 3% | 72% |
| Rd 6-7 WR | 2 | 1% | 87% |

**In redraft, first-round backs and top-10 receivers are the only rookies worth a
real pick.** A third-round receiver produces a startable season 8% of the time.

That 100% on top-10 backs is eight players. Real signal — nobody spends a top-10
pick on a back and then benches him — but it is not a guarantee.

---

## Schedule

T-minus, so it works whether the draft is two weeks out or four.

### T-14 — data
- [ ] Request the FantasyPros API key. Free for personal use, approval takes time.
- [ ] Import `war_room_import.csv`. Delete the demo set.
- [ ] Run `espn_history.py --pull`.
- [ ] Build both league configs. Export a backup.

### T-7 — model
- [ ] Replace estimated replacement level with your measured ESPN numbers.
- [ ] Re-run `adp.py` — ADP moves fast in the week before a draft as mocks pile up.
- [ ] Research the twenty or thirty players whose situation changed. The model
      flags team changes and depth competition; it cannot judge them.

### T-3 — rehearsal, then stop
- [ ] Two full mock drafts using the tool. You are testing yourself, not the math.
- [ ] **Feature freeze.** No new code after this.

### T-1 — refresh only
- [ ] Re-run `profiles.py` and `teams.py`. Depth charts will have moved.
- [ ] Re-import. Export a backup. Print the cheat sheet.

---

## Draft-morning checklist

- [ ] Projections regenerated today, not last week
- [ ] Correct league selected in the dropdown
- [ ] Draft slot correct — confirm in ESPN, it changes
- [ ] Roster slots match ESPN exactly
- [ ] Backup exported
- [ ] Cheat sheet printed
- [ ] Laptop plugged in, sleep disabled

---

## Draft-night runbook

**Layout.** Two windows side by side, not tabs. ESPN's draft room is a CPU hog.

**Marking picks.** `/` jumps to search, type three or four letters, `Enter` for
someone else's pick, `Shift+Enter` for yours, `Ctrl+Z` undoes. Practice until it
is automatic.

**Reading the tool.** The *Take now* card is the recommendation. The number is
what a player adds to your starting lineup minus what you would still get by
waiting a turn. The line under each name says which of those two is driving it.

**When you fall behind.** You will miss a pick or two. Mark what you can and keep
going. The model degrades gracefully with a few missing picks. It does not
degrade gracefully if you spend your clock on data entry.

**If the tool dies.** Use the paper sheet. Do not debug during a draft.

---

## Known gaps

- **ADP survival is a probability, not a guarantee.** `war_room_import.csv` now
  carries real ADP + standard deviation (`adp.py`, Fantasy Football Calculator,
  free), and the Board tab's "Gone before you pick again" card uses it to
  compute an actual survival probability per player instead of assuming the
  room drafts in our own value order. It's still an aggregate of *other*
  people's drafts, not a model of your specific league mates — treat it as the
  room's default behavior, not a prophecy. Falls back to the old value-order
  assumption if `adp.py` hasn't been run.
- **Context now reaches all four positions.** RB is graded against his own line
  (direction-weighted), QB against his weapons and pass protection, and WR/TE
  against their own team's QB quality plus team passing efficiency — his
  "supply line," the same idea as a RB's line, just one position removed.
  All bounded to +/-15% (`context_mult` / `context_note` in `profiles.csv`).
- **Team changes are flagged, not modeled.** `changed_team` marks a player who
  signed elsewhere. His projection still runs off his old team's volume — there's
  no public standard formula for this reallocation (checked; the credible public
  sources describe it qualitatively, not mathematically), so it needs a
  deliberate, documented judgment call, not a quick fix.
- **No depth-chart re-forecast.** The model flags that a player's competition
  changed. It does not re-forecast his role. That judgment is yours.
- **Point estimates only.** No ceiling, floor, or injury risk.
- **One season of ESPN history.** Enough for replacement level. Not enough to
  model how your specific leaguemates draft.
- **No kickers or defenses** — they are noise, take them last.
- **Playoff schedule strength is a badge, not a ranking input.** `playoff_sos.py`
  grades each team's fantasy-playoff-week opponents (weeks 15-17 by default —
  pass `--weeks` to match your league) by how much they allowed to that
  position last season, shown on the board as "Playoff Easy/Tough." Built from
  one season of points-allowed data, same thin-evidence problem `profiles.py`
  already treats carefully for players — so it's deliberately never folded
  into VOR or the recommendation score. A defense that struggled against TEs
  last year has no obligation to do it again.

Rookies used to be a gap here — they're merged into `war_room_import.csv` now,
through the same context step veterans get. `validate.py` catches the class of
bug that kept showing up while building this (transposed columns, bad team
codes, out-of-range percentiles) — run it after any manual edit to these scripts,
not just after a normal regeneration.

---

## Data sources

| Source | What | Access |
|---|---|---|
| nflverse | Play-by-play since 1999, snaps, targets, depth charts, draft picks | Free, `nflreadpy`, cached here |
| ESPN v3 | Your league history and settings | Undocumented, league ID plus two cookies |
| Fantasy Football Calculator | ADP from live mock drafts | Free REST API, no key |
| FantasyPros | Consensus projections, tiers, expert spread | Free key for personal use, request required |

---

## After the draft

The season is seventeen weeks; the draft is three hours.

**The key in-season insight:** past fantasy points barely predict future fantasy
points. Opportunity does — snap share, route participation, target share, carries
inside the ten. All of it is already in the cache.

**And the engine already exists.** In-season, replacement level is just the best
player on waivers. Same math, different baseline. That one substitution gives you
waiver priority, FAAB bids, trade evaluation, and start/sit from what is built.

**Worth doing once the season starts:** the backtest. Feed the model last season's
preseason projections and compare its ordering to what actually happened. If it
did not beat straight ADP, that is worth knowing before you trust it again next
August.
