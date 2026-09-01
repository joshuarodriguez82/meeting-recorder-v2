/**
 * The one decision the rename UI can get wrong: is this a rename or a
 * MERGE?
 *
 * It matters because the two read differently to a user. A rename is
 * cosmetic and people accept it without thinking. A merge folds two
 * records into one and cannot be undone with a button, so the screen
 * has to say "Merge" on the button BEFORE it happens — which means the
 * frontend must reach the same verdict the backend will.
 *
 * The backend's rule lives in `services/client_admin.py::plan_rename`:
 * names are compared on a trimmed, lower-cased key, and a target whose
 * key equals the source's key is NOT a merge — "acme" -> "Acme" is one
 * record wearing better capitalisation. Getting that wrong in the UI
 * puts the word "Merge" on a button that performs a rename, or worse,
 * hides it from one that performs a merge.
 *
 * Duplicated here rather than asked of the server: the label has to be
 * correct as the user types, and a round-trip per keystroke to learn
 * what a button should say is not a trade worth making. The pair is
 * kept honest by tests on both sides asserting the same cases.
 */

/** Trim + case-fold, matching ClientConfigService's storage key. */
export function normalizeName(name: string): string {
  return (name || "").trim().toLowerCase();
}

/**
 * Whether renaming `from` to `to` folds it into an existing record.
 *
 * `existing` is the full list of current names; `from`'s own presence in
 * it is expected and ignored, since a record always collides with
 * itself.
 */
export function isMerge(
  from: string,
  to: string,
  existing: readonly string[],
): boolean {
  const target = normalizeName(to);
  if (!target) return false;
  // Same key => same record. Re-casing is a rename, and reporting it as
  // a merge would promise the user work that never happens.
  if (target === normalizeName(from)) return false;
  return existing.some((name) => normalizeName(name) === target);
}

/** Whether the typed name is a usable, changed target. */
export function isRenameable(from: string, to: string): boolean {
  const next = (to || "").trim();
  return next.length > 0 && next !== from;
}

/** "1 meeting" / "3 meetings" — the count a destructive confirm needs. */
export function pluralMeetings(n: number): string {
  return `${n} meeting${n === 1 ? "" : "s"}`;
}
