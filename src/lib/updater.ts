// Lightweight update-availability check.
//
// Deliberately NOT a signed in-place updater (no Tauri updater plugin,
// no key management, no CI signing). The model here is:
//
//   1. App launch → query GitHub Releases API for the latest tag.
//   2. Compare to the running app version.
//   3. If newer: toast / show "Update available" in Settings.
//   4. User clicks → opens the GitHub release page in the default
//      browser. They download and run the installer manually.
//
// That keeps the maintenance cost at zero (no keypair to back up, no
// secrets to set in CI) while still nudging users to upgrade. For a
// small-team / personal-use tool this is the right tradeoff vs the
// full signed-updater dance. Re-evaluate when the user base grows
// or the threat model includes hostile networks.

const GITHUB_API_URL =
  "https://api.github.com/repos/joshuarodriguez82/meeting-recorder-v2/releases/latest";
const RELEASE_PAGE_URL =
  "https://github.com/joshuarodriguez82/meeting-recorder-v2/releases/latest";

export type LatestRelease = {
  tag: string;
  version: string;
  url: string;
  body: string;
  publishedAt: string;
};

export type UpdateCheckResult =
  | { kind: "available"; release: LatestRelease; currentVersion: string }
  | { kind: "up-to-date"; currentVersion: string }
  | { kind: "unknown"; reason: string; currentVersion: string };

/**
 * Compare two semver-like strings. Returns 1 if a > b, -1 if a < b,
 * 0 if equal. Handles "v2.4.0", "2.4.0", and "2.4.0-rc1" style strings.
 * Non-numeric chunks compare lexicographically.
 */
function compareVersions(a: string, b: string): number {
  const norm = (v: string): (string | number)[] =>
    v.replace(/^v/, "").split(/[.-]/).map((p) => {
      const n = parseInt(p, 10);
      return Number.isNaN(n) ? p : n;
    });
  const na = norm(a);
  const nb = norm(b);
  const len = Math.max(na.length, nb.length);
  for (let i = 0; i < len; i++) {
    const x = na[i] ?? 0;
    const y = nb[i] ?? 0;
    if (x < y) return -1;
    if (x > y) return 1;
  }
  return 0;
}

async function getCurrentVersion(): Promise<string> {
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    return await getVersion();
  } catch {
    // Running under `next dev` against a non-Tauri shell — no version.
    return "0.0.0";
  }
}

/**
 * Hit the GitHub Releases API and compare against the running version.
 * Failures (no network, rate-limited, deleted repo) return "unknown"
 * so the caller can fall back to silence rather than throwing.
 */
export async function checkForUpdate(): Promise<UpdateCheckResult> {
  const currentVersion = await getCurrentVersion();
  let res: Response;
  try {
    res = await fetch(GITHUB_API_URL, {
      headers: { Accept: "application/vnd.github+json" },
    });
  } catch (e) {
    return {
      kind: "unknown",
      currentVersion,
      reason: `Network error: ${e instanceof Error ? e.message : e}`,
    };
  }
  if (!res.ok) {
    return {
      kind: "unknown",
      currentVersion,
      reason: `GitHub API returned ${res.status}`,
    };
  }
  type ReleasePayload = {
    tag_name?: string;
    html_url?: string;
    body?: string;
    published_at?: string;
  };
  const data: ReleasePayload = await res.json();
  const tag = (data.tag_name ?? "").trim();
  if (!tag) {
    return {
      kind: "unknown",
      currentVersion,
      reason: "Latest release has no tag_name",
    };
  }
  if (compareVersions(tag, currentVersion) > 0) {
    return {
      kind: "available",
      currentVersion,
      release: {
        tag,
        version: tag.replace(/^v/, ""),
        url: data.html_url || RELEASE_PAGE_URL,
        body: data.body || "",
        publishedAt: data.published_at || "",
      },
    };
  }
  return { kind: "up-to-date", currentVersion };
}

/**
 * Open the GitHub release page in the user's default browser. Tauri's
 * webview intercepts navigations to external URLs via `window.open`
 * with a non-self target and routes them to the OS browser.
 */
export function openReleaseInBrowser(url: string = RELEASE_PAGE_URL): void {
  try {
    window.open(url, "_blank", "noopener");
  } catch {
    // Should never trip; left as a defensive fallback.
    location.href = url;
  }
}
