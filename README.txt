NBA PROP COMMAND CENTER — v6 "FINAL POLISH, BUG FIXES, LOCAL SAVING & OFFLINE"
================================================================
A personal, single-user web app for tracking NBA player-prop and team bets
you've already placed elsewhere (e.g. Fanatics). It never logs into a
sportsbook, never needs a password, and never fetches betting odds — you
enter what you bet, and it shows you live progress toward it using ESPN's
free public scores/stats feed.

This is a REFINEMENT of v5, not a rebuild. Nothing from v5 was removed or
rebuilt from scratch — every existing feature (straight bets, parlays,
player props, team markets, Ladder Challenges, Bet History, Game Center)
still works the same way it did, and your existing v5 data is preserved
and automatically migrated forward (see section 8).

For iPhone-specific setup, see the separate file IPHONE-INSTRUCTIONS.txt
included in this same folder — this README focuses on Windows/desktop,
what changed, and the honest test results.

(The research/odds backend that is being built alongside this tracker lives in
backend/ and is documented in docs/ARCHITECTURE.md and backend/README.md. The
tracker below does not depend on it and works exactly as before without it.)


0. v6.1 — SETTLEMENT FIX, PERSISTED RESULTS, REBUILT TESTS, PINNED OCR
================================================================
  A. BUG FIXED: OLD BETS COULD BE SETTLED AGAINST A NEWER, UNRELATED GAME.
     Results were never saved — every screen refresh re-judged every bet
     against whatever game the live feed (today + yesterday only) currently
     had for that team/player, with no date check. Reproduced: a 3-day-old
     "BOS vs MIA" prop showed WON from today's unrelated BOS vs NYK box score.
     Bets whose game had left the feed fell back to "Needs review".
     Now (see game-binding.js):
       - A bet only ever matches a game that tips off within 30 hours
         before/after the moment it was recorded (or on the optional "Game
         date (ET)" you can set in its Edit dialog). An opponent, if entered,
         is a hard requirement.
       - The first match BINDS the bet to that exact game (ESPN event id, ET
         game date, tip-off time saved on the bet). It is never re-matched.
       - When that game is final, the result is SAVED on the bet with the
         evidence (final score / the player's final stat line), the game id
         and date, and a settledAt timestamp. It no longer depends on the live
         feed: it survives reloads, the game leaving the feed, and backup
         export/import. Parlay legs work the same way, leg by leg.
       - A bound bet whose game has already left the scoreboard is settled by
         loading that exact game directly from ESPN by its event id.
       - Editing a settled bet's line/direction/stat re-judges it from the
         saved final stat line; editing its player/team/opponent/game date
         clears the saved result and matches it afresh.
     Old bets saved by v6.0 have no saved results. They are matched only
     inside their own window, so one that can no longer be matched shows
     "Needs review" (use "Correct result…" in Bet History) instead of a wrong
     result. Nothing is deleted.
  B. Team codes from autocomplete (GSW, NYK, SAS, NOP, UTA, WAS) are now
     matched to ESPN's own codes (GS, NY, SA, NO, UTAH, WSH). Previously a
     bet entered as "GSW" could not match ESPN's "GS".
  C. ESPN's scoreboard is now requested for the US-Eastern calendar day (the
     NBA's schedule day), not this device's local day.
  D. History CSV export adds: Game ID, Game date (ET), Settled at, Result
     source (auto from final box score, or manual correction).
  E. OCR library pinned: tesseract.js 5.1.1 is vendored in
     vendor/tesseract.js-5.1.1/ (hash-checked by a test) instead of a
     floating "@5" CDN link. See that folder's VENDOR.md. OCR still needs an
     internet connection for its worker/language files (exact versions).
  F. Test suite rebuilt (the tests/ folder described in section 7 below was
     not actually present in the repository). See section 7.
  G. Data schema 4 -> 5 (same storage key nbaCC.v5); new fields are
     backfilled automatically, nothing is removed.


1. WHAT'S NEW / FIXED IN v6 — PLAIN-ENGLISH SUMMARY
================================================================
  A. ON-COURT STATUS REMOVED ENTIRELY. v4/v5 showed on-court status as
     always "Unknown." v6 removes the concept completely — there is no
     on-court indicator, label, or filter anywhere. Game Center's
     "Starters"/"Bench" grouping is a FACTUAL box-score classification
     (who started the game, per the box score) — never a claim about who
     is on the floor right now. When the feed doesn't supply a starter
     flag for a game, it falls back to one plain "Players" list rather
     than guessing.

  B. OVER/UNDER LINE HANDLING — now ONE shared, tested function
     (line-rules.js) used everywhere a line is evaluated: straight bets,
     parlay legs, manual entries, and screenshot imports. A whole-number
     line (e.g. "Over 17") can push (refund) if the result lands exactly
     on it; a half-point line (e.g. "Over 17.5") never pushes. This was
     always the correct behavior, but it's now centralized in one place
     instead of being duplicated across files, with a short explanatory
     line shown next to every line/direction field in the app so it's
     clear up front how a push would be handled.

  C. AUTOCOMPLETE FIXED. All 30 NBA teams now resolve correctly by full
     name, city, nickname, or common abbreviation/alias (e.g. "GS" or
     "GSW" or "Warriors" or "Golden State Warriors" all resolve to the
     same team). The specific reported bug — a player who could be
     manually entered and matched correctly against live stats, but never
     showed up in the autocomplete list — is fixed: the app now also
     remembers every player it has actually seen in a live box score (a
     "seen players" cache, persisted locally) and merges that into
     autocomplete, so even if the team-roster feed is down or stale,
     anyone who's shown up in a live game you're tracking is still
     searchable. Picking a suggestion now stores the real ESPN player ID
     on the form, so matching against live stats is exact, not just a
     text-name guess.

  D. SAFE BET-HISTORY MANAGEMENT. Bet History now has "Remove Selected"
     and "Clear All History" controls, with a confirmation and a record
     count shown before anything is deleted. Only ARCHIVED tickets (ones
     that have been fully settled AND sat in History for the 30-minute
     window) can be selected for removal — an active ticket, an open
     parlay, or anything still linked to an in-progress ladder step can
     never be deleted this way. If a removed ticket was linked to a
     ladder, the ladder's own accounting (balance, settled result, net
     profit) is completely untouched — only the ticket's own History row
     disappears, and the ladder step keeps a saved snapshot of what it
     was ("Jayson Tatum (BOS) — Points O 20 (removed from History)")
     instead of losing its description.

  E. RELIABLE LOCAL SAVING. Every change auto-saves immediately, with a
     visible "Saving…" / "Saved locally HH:MM:SS" / "Save failed" status
     next to the live-feed indicator at all times (never silently fails).
     A save failure (e.g. your browser's storage is full) is shown in
     plain language, not hidden. As an extra safety net, every successful
     save is also mirrored into this browser's IndexedDB; if your
     browser's regular local storage is ever found empty on reopening
     (for example after a storage error), the app automatically recovers
     your data from that backup copy and tells you it did so. This is
     still entirely local to this one browser/device — it is a resilience
     measure, not a sync mechanism.

  F. OFFLINE ACCESS. A new banner clearly tells you when you've gone
     offline, reassures you that your saved bets are all still there and
     fully editable, and reminds you this is per-device access, not sync.
     You can view and edit every saved bet, parlay, and ladder with no
     internet connection at all. Live scores obviously can't update while
     offline; a "last successful update" timestamp and a stale-data note
     make that clear rather than silently showing old numbers as if they
     were current. The moment your connection comes back, the app
     refreshes immediately rather than waiting for the next scheduled
     tick.

  G. BACKUP EXPORT/IMPORT OVERHAUL. The previous Import Backup button had
     a real bug: it silently dropped your ladders and audit log on
     import, keeping only straight bets/parlays. That's fixed. Import now
     validates the WHOLE file first (and tells you exactly what's wrong
     if it doesn't look like a real backup, changing nothing if so), then
     offers a genuine choice: MERGE (adds anything in the file that isn't
     already on this device, by each record's own stable ID — safe to
     import the same backup twice, nothing gets duplicated, and a ladder
     that already exists here gets any missing steps merged in without
     touching its existing fields) or REPLACE ALL DATA (wipes this
     device's current data first, with an explicit "this cannot be
     undone" confirmation, then loads exactly what's in the file).

  H. LIVE REFRESH SPEED + SMARTER POLLING. The live scoreboard/box-score
     refresh interval is now 15 seconds (down from 30), but only while
     this browser tab is actually visible — switching to another tab or
     app pauses polling entirely, and coming back refreshes immediately
     rather than waiting for the next tick. The separate team-roster
     cache (used for autocomplete) is completely unaffected by this and
     still only refreshes roughly every 12 hours — it was never, and is
     still never, refetched on every 15-second tick.

  I. SMARTER "ADD TO LADDER." Stake and odds are prefilled straight from
     the ticket itself (a parlay correctly uses its own COMBINED odds,
     never one leg's odds), and before anything is actually added, you
     see a compact review: the ladder name, the stake, the odds, and
     clearly separate "expected return if won" and "expected profit if
     won" figures (explicitly labeled as an estimate for planning, never
     confused with the ladder's actual settled result once the ticket is
     confirmed final). Editing a prefilled value is always honored —
     nothing is silently reset — and "Back" returns you to the exact
     values you'd already entered.

  J. SIMPLER LADDER SELECTION. Every open ladder is now listed by its own
     real name — the old "Most recent open ladder" label is gone. With
     more than one open ladder, nothing is silently preselected; you have
     to actually choose one (or explicitly choose "Create a new ladder").
     With exactly one open ladder, it's safely preselected since there's
     no real ambiguity. Closed/won/lost ladders never show up as choices.

  K. STANDARDIZED STATUS COLORS. Green always means a confirmed win, red
     always means a confirmed loss, blue always means open/live, amber
     always means pending/needs-review, and neutral gray always means
     push/void — consistently, everywhere in the app. The specific bug
     this fixes: a live "target reached" status used to share the exact
     same green as an actual confirmed win; it's now its own distinct
     blue-family color, since reaching a number while the game is still
     live is not the same as having actually won yet.

  L. "COURT SIDE" VISUAL REDESIGN. A new charcoal-and-basketball-orange
     look across the whole app — deep charcoal backgrounds, elevated
     charcoal cards, basketball orange reserved for primary actions and
     the selected nav tab (never used for a loss), warm cream text
     instead of stark white, and the standardized win/loss/live/pending/
     push-void colors from item K used consistently throughout. The
     layout itself (Overview's structure, the tabs, live-game behavior)
     is unchanged — this is a color/surface pass, not a rearrangement.

  M. NEW SETTINGS TAB. A dedicated "Settings" tab keeps the main betting
     workflows uncluttered. It has five sections: Data Management &
     Backups (Export/Import, same validated flow as the top-bar buttons);
     Local Storage & Offline Status (save status, last successful save,
     a plain-English explanation of exactly what does and doesn't survive
     — see section 7); Live Data & Refresh (the real refresh interval,
     connection status, last successful update, a manual refresh button);
     Appearance & Preferences (confirms the Court Side theme and the
     standardized status colors); and App Information & Data Integrity
     (app version, data schema version, and a "Run data integrity check"
     button that scans for malformed records or broken ladder/ticket
     relationships and reports them in plain language — it only ever
     looks, it never changes anything itself).

  N. RETROACTIVE EDITING. Every straight bet, parlay ticket (and each of its
     individual legs), ladder (and each of its steps), and Bet History row
     now has its own "Edit" button, so a mistake — a typo'd player/team
     name, the wrong line or odds, a stake entered wrong, a ladder's own
     name or goal — can be corrected after the fact, instead of having to
     delete and re-enter the whole thing. This is deliberately a DIFFERENT
     thing from Bet History's existing "Correct result…" dropdown: that
     overrides a RESULT (forces WON/LOST/PUSH/VOID); this corrects the
     bet's own recorded FACTS, and settlement still re-evaluates live from
     stats/scores using whatever's on file after you save — editing a prop
     bet's target from 20 to 18, for example, changes what counts as a win
     going forward, it doesn't declare a result itself. Specifics:
       - A straight bet's Edit button (Straight Bets tab, or from Bet
         History) opens every one of its own fields — player/team name,
         stat/market, line, direction, odds, bookmaker, stake, matchup
         label — prefilled with its current values.
       - A parlay has TWO separate edit paths: "Edit ticket" (Parlays tab)
         corrects the ticket's own name/stake/combined-odds/payout only;
         each individual leg has its own "Edit" button for that leg's
         player/team/line/odds — editing one never touches the other legs
         or the ticket-level fields.
       - A ladder's "Edit ladder" button (Ladders tab) corrects its name,
         starting amount, goal amount, progression mode, loss policy,
         target date, or notes — its steps and any linked tickets are
         completely untouched by this. Each individual STEP also has its
         own "Edit" button for that step's stake/odds/entered payout. If a
         step has already settled, editing it explicitly warns you up
         front and then recomputes that step's payout/profit using the
         exact same math the app uses everywhere else, so the ladder's
         running balance is never left stale after a correction — it never
         silently re-settles a step for you, only recalculates the numbers
         from whatever you just corrected.
       - Bet History's own "Edit" button reaches the SAME underlying
         ticket as the live tabs (not a separate copy) — including an
         already-archived one — so a past, settled bet's recorded facts
         can still be fixed without having to go dig it up elsewhere.
     Every edit is written to the audit log (the same place a manual
     result correction goes), recording exactly what changed — the old
     value and the new one, for every field touched — so a past correction
     is never silent or untraceable.


2. OVER/UNDER LINE HANDLING — EXACTLY HOW IT WORKS
================================================================
This app never changes what you actually bet — the line you enter is
always exactly what's evaluated and displayed. What v6 adds is one
consistent, tested rule for push handling, used identically whether you
typed the bet in by hand, picked a parlay leg, or imported a screenshot:

  - A WHOLE-NUMBER line (e.g. "Over 17" or "Under 17") pushes — the stake
    is refunded, no win or loss — if the final result lands exactly on
    that number. Otherwise it wins or loses normally on either side.
  - A HALF-POINT line (e.g. "Over 17.5" or "Under 17.5") can NEVER push,
    since the result can't land exactly on a half-point — it always
    settles win or lose.

A short line of helper text appears right next to the line/direction
fields on every relevant form, explaining in plain language what wins and
what pushes for the exact number you typed, so there's no ambiguity about
how a given line will be interpreted before you confirm anything.


3. BET HISTORY — SAFE DELETION, PRECISELY
================================================================
Bet History still preserves every ticket's full details (odds, stake,
payout, P&L, linked ladder, original creation time) by default. What's
new in v6 is the ability to actually clean it up when you want to, safely:

  - Only ARCHIVED tickets can be selected for removal (checkbox shown).
    A ticket only becomes archived once every game it depends on is
    confirmed FINAL and 30 real minutes have then passed — exactly the
    same rule as v5. An active ticket never gets a checkbox at all.
  - "Remove Selected" deletes only the tickets you've actually checked,
    with a confirmation showing exactly how many.
  - "Clear All History" removes every currently-archived ticket at once,
    also with a confirmation and count — and if there's nothing archived
    yet, it tells you that plainly instead of silently doing nothing.
  - "Export Backup First" is offered right in the History toolbar as a
    one-click safety copy before you delete anything.
  - Deleting a History record NEVER touches an active bet, an open
    parlay, a ladder's definition, or any ladder step's settlement
    accounting (result, net profit) — this is guaranteed by construction,
    not just by convention: a ticket can only reach "archived" after its
    linked ladder step (if any) has already fully settled, so removing it
    from History can never happen before that settlement is locked in.
    If the removed ticket was linked to a ladder, that step keeps a saved
    one-line description of what it was, instead of losing its history.


4. LOCAL SAVING, OFFLINE ACCESS & BACKUPS — READ THIS SECTION
================================================================
WHERE YOUR DATA LIVES: entirely in this one browser's local storage, on
this one device. There is no account, no cloud database, no server this
app talks to for your bet data (only ESPN's public score feed, which it
only reads, never sends your data to). A small backup mirror of the same
data is also kept in this browser's IndexedDB purely as a local safety
net — if your browser's normal storage is ever found empty when the app
reopens, it automatically restores from that mirror and tells you so.

WHAT THIS MEANS, CONCRETELY:
  - Survives: closing the browser, restarting your computer, restarting
    your phone, your browser being closed for days.
  - Does NOT survive: clearing this browser's site data/cookies for this
    page, using a different browser on the same device, using a different
    device, or a corrupted/reset browser profile.
  - Opening this same app on a second device (even "Add to Home Screen"
    on your iPhone, even using the exact same file/URL) does NOT bring
    your PC's data with it. Each install/browser keeps its own entirely
    separate local copy. See section 7 for the full explanation.

A visible status next to the live-feed indicator always shows "Saving…",
"Saved locally [time]", or "Save failed — [reason]" — it never claims a
save succeeded before it actually has, and a failure (e.g. storage full)
is shown in plain language rather than silently losing your change.

OFFLINE: you can view, add, and edit every bet, parlay, and ladder with
zero internet connection — none of that requires the network at all. An
"You're offline" banner appears automatically so it's always clear, and
reassures you nothing is lost. Live scores/stats obviously can't update
while offline (that part of the app genuinely needs ESPN's feed); the
timestamp shown always reflects when data was ACTUALLY last refreshed, so
old numbers are never presented as if they were current. The moment your
connection returns, a refresh happens automatically right away.

If you also run the app through a local web server (see section 9), its
own files (the page itself, not live scores) are cached by a service
worker so the app can still open even with no connection at all, and
"Add to Home Screen" works properly on an iPhone. This offline app-shell
caching is completely separate from, and does not replace, the local-
saving behavior above, which works identically either way (opened
directly as a file, or served locally).

BACKUPS — EXPORT:
  - "Export Backup" (top bar, or Settings → Data Management) downloads a
    dated JSON file (e.g. NBA-Prop-Command-Center-Backup-2026-10-22.json)
    containing every straight bet, parlay, ladder (with all its steps),
    the audit log, and odds history. No passwords, credentials, or
    secrets of any kind are included — it's just your tracked bets.
  - On Windows, this is a completely normal browser download — see your
    browser's regular Downloads folder, or you'll be prompted to choose
    a location depending on your browser's settings. You can move the
    file anywhere afterward (e.g. into Documents, or onto a USB drive,
    or into cloud storage you control, like OneDrive, if you want an
    extra off-device copy — this app itself never uploads it anywhere).
  - On iPhone, see IPHONE-INSTRUCTIONS.txt for the exact Share-sheet
    steps to save it into the Files app.

BACKUPS — IMPORT / RESTORE:
  - "Import Backup" (top bar, or Settings → Data Management) opens your
    device's normal file picker — browse to wherever you saved the
    export (Windows: any folder; iPhone: the Files app) and pick it.
  - The WHOLE file is validated before anything changes. If it doesn't
    look like a real backup from this app, you get a specific error
    explaining why, and nothing on your device is touched.
  - You're then asked to choose:
      MERGE (recommended) — adds anything in the file that isn't already
      on this device (matched by each record's own ID), and leaves
      everything already here completely untouched. Importing the exact
      same backup twice is safe — nothing gets duplicated.
      REPLACE ALL DATA — deletes everything currently on this device and
      loads only what's in the file. This requires an extra explicit
      confirmation because it cannot be undone (unless you have another
      backup of what was just replaced).
  - Either way, a clear summary is shown afterward (e.g. "3 new straight
    bets added, 1 already present (skipped), 1 ladder merged...").


5. GAME CENTER
================================================================
Unchanged in layout from v5: scoreboard cards grouped Live now / Upcoming
/ Final, click any card to expand a full box score — team totals, shooting
splits formatted as "made/attempted" (e.g. "10/15 FG"), per-player stats.
What's different in v6: no on-court indicator anywhere (see item A above).
When the box score's feed supplies a "started the game" flag, players are
grouped into "Starters" / "Bench" (a factual box-score classification,
explicitly NOT a claim about who's on the floor right now) plus a
separate "DNP / Not suited up" group for anyone with 0 recorded minutes;
when that flag isn't available, it falls back to one plain "Players" list
rather than guessing a split.

GAMES FROM THE LAST 24 HOURS — ALWAYS WITH A FULL BOX SCORE. Every refresh
now checks BOTH today's and yesterday's calendar day (ESPN's scoreboard
defaults to only "today," which could otherwise make a game that finished
late last night vanish the moment the date rolled over). Any game that's
finished within roughly the last 24-30 hours shows up under "Final (last
24 hrs)", sorted most-recent-first, with a "Today" or "Yesterday" label
right on its card so it's clear which day it was. Crucially: clicking any
of these finished games now ALWAYS loads its complete box score — every
player on both teams, with minutes, points, rebounds, assists, steals,
blocks, turnovers, fouls, and shooting splits, plus each team's totals —
regardless of whether you have a bet tracked on that particular game. The
previous "no box score loaded... it may not have any tracked bets on it"
limitation only still applies to a live or upcoming game that has no
tracked bet on it (where skipping the fetch genuinely saves a request for
a number nobody's watching yet); a finished game is never skipped that
way. Once a finished game's box score has loaded, it's kept in memory and
reused on every later 15-second refresh rather than re-downloaded, since
a final score can't change anymore.


6. WHAT COULD NOT BE VERIFIED (BEING HONEST WITH YOU HERE)
================================================================
- Live behavior against the REAL ESPN feed during an actual NBA game —
  this sandbox's network is on an allowlist that blocks ESPN's real
  servers (reconfirmed again this session with a direct connection
  attempt, which failed/timed out). Every test below used MOCKED network
  responses standing in for ESPN. This proves the app's own logic,
  rendering, and data handling are correct for the data shapes ESPN is
  documented to return — it is NOT the same as a live, real-game check.
  The first time you use this during real games is still your own first
  live verification, same as with v1-v5.
- Whether iOS Safari's real "Add to Home Screen" flow and Share-sheet
  "Save to Files" behave exactly as the manifest/service-worker and the
  standard iOS share flow predict — this sandbox has no physical iPhone
  to test on. What IS verified directly: the service worker registers
  and activates successfully when served over http://localhost, and does
  nothing at all (by design, no errors) when opened via file://; and the
  backup file produced by Export Backup is confirmed to be valid,
  well-formed JSON with a descriptive filename, which is what any
  standard "save to Files" flow needs.
- Whether a.espncdn.com actually serves every player's logo/headshot URL
  — unchanged from v4/v5, still not independently verifiable here.
- The IndexedDB recovery safety net is tested directly (see section 7)
  for the "browser-level storage corruption/empty-on-reopen" scenario it
  targets, but a real IndexedDB-quota or private-browsing restriction
  specific to a browser this wasn't tested in can't be fully ruled out —
  the app is written to fail silently-but-visibly in that case (the save
  status would show "Save failed") rather than to crash.


7. TESTS — WHAT EXISTS AND WHAT IT PROVES (rewritten in v6.1)
================================================================
The v6.0 version of this section listed 21 test files under tests/ and a
v5-backup-before-v6-changes/ folder. Neither was present in the repository, so
those claims could not be checked. v6.1 rebuilt a test suite from scratch.

Run (Node 20+; browser tests need Playwright with Chromium installed):
    npm run test:unit      # pure logic, no browser, no network
    npm run test:browser   # real app in headless Chromium
    npm test               # both
    npm run check          # node --check on every top-level .js file

All network responses in the browser tests are SYNTHETIC (mocked ESPN feed
built in tests/browser/harness.js). They prove the app's own logic against the
data shapes ESPN is documented to return; they are not a live-game check. The
app is served from a throwaway local HTTP server during tests.

tests/unit/game-binding.test.js — the v6.1 fix: a 3-day-old bet sees no
  candidate games; a bound bet only ever sees its bound game; settlement is
  never saved from a different game; the 30h window edges; back-to-back
  ranking; opponent as a hard constraint; explicit game date; team-code
  aliases (GS/GSW, NY/NYK, ...); US-Eastern dates incl. month rollover;
  settlement record contents and re-judging from saved evidence; no
  settlement while live/postponed/unmatched/voided; ESPN payload parsing.
tests/unit/core-logic.test.js — line push rules, prop combos and statuses,
  team markets (totals/spread/moneyline incl. pushes), parlay aggregation,
  ladder math, 30-minute archival, bet-slip OCR text parsing, team list.
tests/unit/vendor-integrity.test.js — the vendored OCR bundle's sha256; that
  app.js never loads a floating CDN copy; the service worker caches every
  script the page loads.
tests/browser/settlement-regression.test.js — in the real app: old bets and
  old parlay legs stay "Needs review" instead of settling from today's game;
  a settled result is saved with game id / ET date / settledAt and survives a
  reload after its game leaves the feed AND a newer same-team game appears;
  a bound bet outside the scoreboard window settles from its own game loaded
  by event id; GSW/GS matching; editing a settled bet; ladder accounting
  after reload; CSV export columns.
tests/browser/core-flows.test.js — schema-4 data migrates with nothing lost;
  adding a straight bet and a parlay through the forms; backup export and
  Merge import (no duplicates); data-integrity check; Game Center box score;
  the vendored OCR bundle loads without any CDN request for the library.
tests/browser/app-behaviors.test.js — save-status indicator; offline banner;
  only archived tickets removable from History with ladder accounting
  untouched; status colors (live target-reached is never the win color);
  team autocomplete aliases.

NOT covered by automated tests (unchanged limitations): live behavior against
the real ESPN feed during a real game; iOS "Add to Home Screen" / Share-sheet
flows; real OCR recognition of a screenshot (needs the CDN worker + WASM);
the 15-second refresh cadence and tab-visibility pausing.

Observed while building the tests: in automated Chromium, localStorage writes
made a split second before reloading a page opened directly as a FILE
(file://) were sometimes lost. Over http(s) this never happened in repeated
runs. If you use the app from a file, the IndexedDB mirror and the next
refresh's re-settlement cover this, but serving it over http(s) is the more
robust setup.


8. YOUR EXISTING DATA — MIGRATION & BACKUP
================================================================
- (v6.1 note) The v6.0 delivery described a v5-backup-before-v6-changes/
  folder; it is NOT in this repository. Earlier versions are recoverable
  only from git history (which starts at v6.0) or from your own copies.
  Export a backup from each device before upgrading.
- The app still stores its data under the SAME key as v5 (nbaCC.v5) — it
  was deliberately NOT renamed for this release, since this is a
  refinement, not a rebuild. Only the internal schema version number
  changed (3 → 4), and the handful of new v6 fields are backfilled
  automatically and safely onto your existing records the first time you
  open this version — nothing is ever deleted or reset in that process.
- If you're coming from v3/v4 (skipping v5 entirely), that older
  migration path still works exactly as it did before — see v5's own
  README (inside the nested backup folder) for that history if needed.


9. HOW TO RUN IT ON WINDOWS
================================================================
Easiest — just open it:
1. Unzip this folder anywhere (e.g. your Desktop).
2. Double-click index.html. It opens in your default browser. That's it.
   Your bets, parlays, and ladders save automatically as you go — look
   for "Saved locally [time]" near the top of the page to confirm.

If you want "Add to Home Screen" on your iPhone to work, or want the app
to still open with no internet connection at all (not just a slow one),
run it through a tiny local server instead (same page, same data — this
is optional, not required just to use the app):
1. Install Python if you don't have it: https://python.org/downloads
   (check "Add Python to PATH" during setup).
2. Open the unzipped folder in File Explorer, click the address bar, type
   cmd, press Enter — this opens a command prompt already in that folder.
3. Type:  python -m http.server 8000
4. Open a browser and go to:  http://localhost:8000
5. For your iPhone to reach that same local server, both devices need to
   be on the same Wi-Fi and you'd use your PC's local network address
   instead of "localhost" — this depends on your specific home network
   and wasn't set up for you here. REMEMBER either way: your iPhone and
   PC still keep SEPARATE local data (section 4) — running a local server
   does not change that.
6. Leave the command-prompt window open while you use the app this way;
   close it when you're done (Ctrl+C, or just close the window).

Either way: the app auto-refreshes live scores/stats every 15 seconds
while the tab is visible (paused automatically when it's not), or click
"Refresh now" any time. A dot in the top bar is orange/neutral normally,
amber while actively refreshing, red if it can't currently reach ESPN —
your tracked bets are never lost when that happens, only the live numbers
pause until the feed recovers. A second status line shows your local
save status at all times.


10. KNOWN LIMITATIONS / THINGS WORTH KNOWING
================================================================
- No live-odds/line-movement tracking — you enter the odds you got, and
  they're never auto-updated. Combined parlay odds are always what you
  typed in, never auto-calculated from the legs.
- On-court status does not exist in this app at all (by design — see
  item A in section 1) and will not be added without you asking again.
- Cross-device sync does not exist — see section 4, please read it if you
  use this on more than one device.
- The Bet History manual-correction audit log is recorded in your data
  (and included in Export Backup) but doesn't yet have its own dedicated
  viewer screen inside the app — you'd need to open an exported .json to
  read past correction entries directly, or ask for that viewer later.
- A ladder's "suggested next stake" in Reinvest mode is exactly the
  ladder's current balance — always a suggestion you confirm or edit
  before it's used, never assumed silently.
- Only one visual theme (Court Side) is currently available — there's no
  light-mode or alternate-theme switcher in Settings yet.
- The offline app-shell (service worker) only takes effect when the app
  is served over http/https, not when opened directly as a file — see
  section 9 if you want that.
