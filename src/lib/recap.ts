// The in-call recap is short markdown from the model: "## " headings
// and "- " bullets. This turns it into blocks the recap panel renders,
// so it reads as headings and lists rather than raw markdown, without
// pulling a markdown library into the record screen.

export type RecapBlock =
  | { kind: "heading"; text: string }
  | { kind: "bullet"; text: string }
  | { kind: "text"; text: string };

export function parseRecap(markdown: string): RecapBlock[] {
  const blocks: RecapBlock[] = [];
  for (const raw of (markdown || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      blocks.push({ kind: "heading", text: stripEmphasis(heading[1]) });
      continue;
    }
    const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      blocks.push({ kind: "bullet", text: stripEmphasis(bullet[1]) });
      continue;
    }
    blocks.push({ kind: "text", text: stripEmphasis(line) });
  }
  return blocks;
}

function stripEmphasis(text: string): string {
  return text.replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1").trim();
}

/** 42:15 / 1:02:03 for a position in the call. */
export function callClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

/** Text with a `[mm:ss] ` stamp inserted at the caret, and where the
 *  caret should go after it. A stamp goes at the start of a line. */
export function insertStamp(
  text: string, caret: number, seconds: number,
): { text: string; caret: number } {
  const at = Math.min(Math.max(0, caret), text.length);
  const before = text.slice(0, at);
  const lead = before && !before.endsWith("\n") ? "\n" : "";
  const stamp = `${lead}[${callClock(seconds)}] `;
  return { text: before + stamp + text.slice(at), caret: at + stamp.length };
}
