#!/usr/bin/env bash
# Regenerate every data file. Run from the project root.
set -e

# On Windows, "python"/"python3" can both resolve to a Microsoft Store stub
# that exists on disk (so `command -v` finds it) but fails when actually run,
# printing an install-from-store nag instead of doing anything. Check that the
# candidate really runs, not just that a file with that name exists on PATH.
PY=python3
"$PY" --version >/dev/null 2>&1 || PY=python
"$PY" --version >/dev/null 2>&1 || { echo "No working Python found on PATH. See README's Quick start." >&2; exit 1; }

echo "== team context =="
$PY teams.py
echo
echo "== rookies (rookies.csv, standalone report) =="
$PY rookies.py
echo
echo "== veteran projections + rookie merge + context adjustment =="
# profiles.py is the one script that writes war_room_import.csv. It pulls
# rookies in itself (as a Python import, not by reading rookies.csv) so they
# pass through the same O-line/weapons context step every veteran gets.
$PY profiles.py
echo
echo "== ADP =="
# runs after profiles.py on purpose — it appends adp_*/stdev_* columns onto
# the file profiles.py already wrote, rather than feeding into projections
$PY adp.py
echo
echo "== sanity check =="
$PY validate.py
echo
echo "Done. Import war_room_import.csv into the Data tab."
