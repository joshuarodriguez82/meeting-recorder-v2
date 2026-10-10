// Speakers as the reader counts them.
//
// Lines the diarizer could not place in anyone's turn are kept under
// one label, shown as "Unattributed". They are several people at once
// (mostly "yeah" / "okay" between turns), so they are not a speaker:
// counting them is one way a 12-person call listed 14 speakers.
// Must match UNATTRIBUTED_LABEL in backend/core/speaker_merge.py.
export const UNATTRIBUTED_LABEL = "SPEAKER_UNKNOWN";

export function isUnattributed(speakerId: string): boolean {
  return speakerId === UNATTRIBUTED_LABEL;
}

/** How many people a session's speaker map represents. */
export function countPeople(speakers: Record<string, unknown> | null | undefined): number {
  return Object.keys(speakers ?? {}).filter((id) => !isUnattributed(id)).length;
}
