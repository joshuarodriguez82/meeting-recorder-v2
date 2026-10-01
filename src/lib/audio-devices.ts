/**
 * Keeping the selected device selected across a device re-scan.
 *
 * The backend re-scans audio hardware so a headset connected after
 * launch shows up without restarting the app (core/audio_capture.py
 * `refresh_devices`, field report 2026-09-23). A re-scan renumbers
 * devices, and the app selects devices by index — so after a refresh the
 * old index can point at a DIFFERENT device. The selection is carried
 * across by name, which is stable.
 */

export type NamedDevice = { index: number; name: string };

/** The index, in `next`, of the device that `prevIdx` meant in `prev`.
 *  Falls back to `fallbackName` (the saved choice), then to the first
 *  device, then to null when there is nothing to pick. */
export function reselectByName(
  prev: NamedDevice[],
  prevIdx: number | null | undefined,
  next: NamedDevice[],
  fallbackName?: string | null,
): number | null {
  const name = prev.find((d) => d.index === prevIdx)?.name ?? fallbackName;
  const match = name ? next.find((d) => d.name === name) : undefined;
  if (match) return match.index;
  return next.length > 0 ? next[0].index : null;
}
