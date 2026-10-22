// Pure 30-minute archival logic. No DOM, no storage, no setTimeout/setInterval —
// the whole point is that this is a comparison against a PERSISTED timestamp and
// the current clock, so it is correct immediately after the app is reopened, and
// never depends on a browser timer having run continuously in the background.

const ARCHIVE_DELAY_MS = 30 * 60 * 1000;

// gameStates: one entry per game the ticket actually depends on — 'pre'|'in'|
// 'halftime'|'post', or null/undefined if that leg's game couldn't be identified
// at all (unmatched). A manually-voided selection should pass 'post' for its own
// slot (voiding is already a terminal, settled state) — app.js does that mapping.
//
// Returns the finalizedAt timestamp to persist: null if the ticket isn't (yet)
// fully final, or the FIRST moment all its games were observed final (never
// recomputed forward once set, so the 30-minute window starts at the right time
// even if this function is called again later).
function computeFinalizedAt(gameStates, existingFinalizedAt, nowMs) {
  // Once a ticket has been finalized, that timestamp must never be reverted —
  // not even if its game(s) briefly look unmatched/unresolved (e.g. right after
  // a page reload, before this session has (re)fetched its own live-game list).
  // Checking this FIRST, before looking at gameStates at all, is what lets
  // archival survive a reload: a transiently-empty/unmatched gameStates array
  // must never be allowed to erase an already-finalized ticket's timestamp.
  if (existingFinalizedAt) return existingFinalizedAt;
  if (!gameStates || !gameStates.length) return null; // no games identified -> never finalize (stays "needs review", not silently archived)
  const allFinal = gameStates.every(s => s === 'post');
  if (!allFinal) return null; // any game missing/unmatched/not final -> not finalized yet
  return nowMs;
}

function isArchived(finalizedAt, nowMs) {
  if (!finalizedAt) return false;
  return (nowMs - finalizedAt) >= ARCHIVE_DELAY_MS;
}

function minutesRemaining(finalizedAt, nowMs) {
  if (!finalizedAt) return null;
  const remaining = ARCHIVE_DELAY_MS - (nowMs - finalizedAt);
  return remaining <= 0 ? 0 : Math.ceil(remaining / 60000);
}

if (typeof module !== 'undefined') module.exports = { ARCHIVE_DELAY_MS, computeFinalizedAt, isArchived, minutesRemaining };
