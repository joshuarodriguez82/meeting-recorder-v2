// Lightweight update-availability check.
//
// Deliberately NOT a signed in-place updater (no Tauri updater plugin,
// no key management, no CI signing — builds are unsigned). The model:
//
//   1. App launch → query GitHub Releases API for the latest tag.
//   2. Compare to the running app version.
//   3. If newer: toast / show "Update available" in Settings.
//   4. User clicks Download → we open the correct installer asset for
//      their OS directly (download starts immediately, no release page
//      to dig through), in the real browser.
//
// A true download-and-install-in-place updater needs the Tauri updater
// plugin + a signing keypair + signed update manifests in CI, which
// also fights the current unsigned/Gatekeeper setup — a separate,
// larger piece of work. This keeps maintenance at zero while making
// "Download" actually work and land the right file.

import { openExternal } from "@/lib/api";

const GITHUB_API_URL =
  "https://api.github.com/repos/joshuarodriguez82/meeting-recorder-v2/releases/latest";
const RELEASE_PAGE_URL =
  "https://github.com/joshuarodriguez82/meeting-recorder-v2/releases/latest";

export type ReleaseAsset = { name: string; url: string };

export type LatestRelease = {
  tag: string;
  version: string;
  url: string;
  body: string;
  publishedAt: string;
  assets: ReleaseAsset[];
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
    assets?: { name?: string; browser_download_url?: string }[];
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
    const assets: ReleaseAsset[] = (data.assets || [])
      .filter((a) => a.name && a.browser_download_url)
      .map((a) => ({
        name: a.name as string,
        url: a.browser_download_url as string,
      }));
    // The release object exists the instant the tag is pushed, but the
    // Tauri build uploads the .exe/.msi/.zip ~10-20 min later. Don't
    // prompt while there's no installer for this OS yet — otherwise
    // "Download" dead-ends on a page with no files. A later launch
    // (once assets are attached) will surface it normally.
    if (!pickInstallerAsset(assets)) {
      return { kind: "up-to-date", currentVersion };
    }
    return {
      kind: "available",
      currentVersion,
      release: {
        tag,
        version: tag.replace(/^v/, ""),
        url: data.html_url || RELEASE_PAGE_URL,
        body: data.body || "",
        publishedAt: data.published_at || "",
        assets,
      },
    };
  }
  return { kind: "up-to-date", currentVersion };
}

/**
 * Pick the installer asset for the running OS so the download lands the
 * right file instead of dumping the user on a release page. Asset names
 * follow the build's convention:
 *   Windows → Meeting.Recorder_X.Y.Z_x64-setup.exe / ..._x64_*.msi
 *   macOS   → Meeting.Recorder_X.Y.Z_universal.zip
 */
export function pickInstallerAsset(
  assets: ReleaseAsset[],
): ReleaseAsset | null {
  if (!assets || assets.length === 0) return null;
  const ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
  const isWin = /Windows/i.test(ua);
  const isMac = /Mac OS X|Macintosh|Mac_PowerPC/i.test(ua);
  const byExt = (re: RegExp) =>
    assets.find((a) => re.test(a.name.toLowerCase())) || null;
  if (isWin) {
    return byExt(/\.exe$/) || byExt(/\.msi$/) || null;
  }
  if (isMac) {
    return (
      assets.find((a) => /universal.*\.zip$/i.test(a.name)) ||
      byExt(/\.dmg$/) ||
      byExt(/\.zip$/) ||
      null
    );
  }
  return null;
}

/**
 * Start the update download. Opens the matched installer asset URL
 * directly in the real browser (the download begins immediately — no
 * release page to navigate), via the OS opener (window.open does
 * nothing inside the Tauri webview). Falls back to the release page
 * only when no matching asset is found.
 *
 * Returns the asset name we kicked off, or null if we fell back to the
 * release page, so the UI can tell the user what's downloading.
 */
export async function downloadUpdate(
  release: Pick<LatestRelease, "url" | "assets">,
): Promise<string | null> {
  const asset = pickInstallerAsset(release.assets || []);
  await openExternal(asset?.url || release.url || RELEASE_PAGE_URL);
  return asset?.name ?? null;
}

/** Open the GitHub release page in the user's default browser. */
export async function openReleaseInBrowser(
  url: string = RELEASE_PAGE_URL,
): Promise<void> {
  await openExternal(url);
}
