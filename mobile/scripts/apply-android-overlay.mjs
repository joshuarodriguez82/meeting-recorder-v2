#!/usr/bin/env node
/**
 * Injects the native recording layer into the Capacitor-generated
 * `android/` project. Run AFTER `cap add android` and BEFORE
 * `cap sync` / `gradlew`. Idempotent — every mutation is marker-guarded
 * so re-running (or running on an already-patched tree) is a no-op.
 *
 * `android/` is disposable build output (gitignored); the source of
 * truth is mobile/android-overlay + this script.
 */
import { existsSync, readFileSync, writeFileSync, readdirSync, rmSync, copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const mobile = join(here, "..");
const android = join(mobile, "android");
const appMain = join(android, "app", "src", "main");
const overlayJava = join(mobile, "android-overlay", "java");

const PKG = "com.joshuarodriguez.meetingrecorder";
const PKG_PATH = PKG.replaceAll(".", "/");
const KOTLIN_VERSION = "1.9.25";

function die(msg) {
  console.error(`[overlay] ERROR: ${msg}`);
  process.exit(1);
}
function log(msg) {
  console.log(`[overlay] ${msg}`);
}

if (!existsSync(android)) {
  die("android/ not found — run `npx cap add android` first.");
}

// 1. Source files: drop our Kotlin into the app package, remove the
//    generated MainActivity.* (any language) so ours is authoritative.
const srcRoots = ["java", "kotlin"]
  .map((d) => join(appMain, d))
  .filter((d) => existsSync(d));
if (srcRoots.length === 0) die("no java/ or kotlin/ source root under app/src/main");

const pkgDir = join(srcRoots[0], PKG_PATH);
mkdirSync(pkgDir, { recursive: true });

for (const root of srcRoots) {
  const d = join(root, PKG_PATH);
  if (!existsSync(d)) continue;
  for (const f of readdirSync(d)) {
    if (/^MainActivity\.(java|kt)$/.test(f)) {
      rmSync(join(d, f));
      log(`removed generated ${f}`);
    }
  }
}
for (const f of ["MainActivity.kt", "RecorderPlugin.kt", "RecordingService.kt"]) {
  copyFileSync(join(overlayJava, PKG_PATH, f), join(pkgDir, f));
  log(`installed ${f}`);
}

// 2. AndroidManifest: permissions + the foreground service.
const manifestPath = join(appMain, "AndroidManifest.xml");
let manifest = readFileSync(manifestPath, "utf8");

const PERMS = [
  "android.permission.RECORD_AUDIO",
  "android.permission.FOREGROUND_SERVICE",
  "android.permission.FOREGROUND_SERVICE_MICROPHONE",
  "android.permission.POST_NOTIFICATIONS",
];
const permLines = PERMS.filter((p) => !manifest.includes(`"${p}"`))
  .map((p) => `    <uses-permission android:name="${p}" />`)
  .join("\n");
if (permLines) {
  manifest = manifest.replace(
    /<manifest([^>]*)>/,
    (m) => `${m}\n${permLines}`,
  );
  log(`added ${permLines.split("\n").length} permission(s)`);
}

if (!manifest.includes("RecordingService")) {
  const service =
    `        <service\n` +
    `            android:name="${PKG}.RecordingService"\n` +
    `            android:exported="false"\n` +
    `            android:foregroundServiceType="microphone" />\n`;
  manifest = manifest.replace(/(\n\s*<\/application>)/, `\n${service}$1`);
  log("registered RecordingService");
}
writeFileSync(manifestPath, manifest);

// 3. app/build.gradle: Kotlin plugin + native deps.
const appGradle = join(android, "app", "build.gradle");
let g = readFileSync(appGradle, "utf8");

if (!g.includes("kotlin-android")) {
  // Capacitor's template starts with `apply plugin: 'com.android.application'`
  g = g.replace(
    /apply plugin: 'com\.android\.application'/,
    `apply plugin: 'com.android.application'\napply plugin: 'kotlin-android'`,
  );
  log("applied kotlin-android plugin");
}
if (!g.includes("OVERLAY_DEPS")) {
  // androidx.core (NotificationCompat / ContextCompat) is already on
  // the classpath transitively via Capacitor at 1.15.0 — don't pin a
  // second, older copy. Only documentfile + the Kotlin stdlib are new.
  const deps =
    `    // OVERLAY_DEPS\n` +
    `    implementation "androidx.documentfile:documentfile:1.0.1"\n` +
    `    implementation "org.jetbrains.kotlin:kotlin-stdlib:${KOTLIN_VERSION}"\n`;
  g = g.replace(/dependencies\s*\{/, (m) => `${m}\n${deps}`);
  log("added documentfile/core-ktx/kotlin deps");
}
writeFileSync(appGradle, g);

// 4. Project build.gradle: Kotlin Gradle plugin on the buildscript
//    classpath (Capacitor 7 still uses the classic buildscript block).
const projGradle = join(android, "build.gradle");
let p = readFileSync(projGradle, "utf8");
if (!p.includes("kotlin-gradle-plugin")) {
  if (!/buildscript\s*\{[\s\S]*dependencies\s*\{/.test(p)) {
    die(
      "project build.gradle has no buildscript{dependencies{}} — Capacitor " +
        "template changed; update apply-android-overlay.mjs.",
    );
  }
  p = p.replace(
    /(buildscript\s*\{[\s\S]*?dependencies\s*\{)/,
    `$1\n        classpath "org.jetbrains.kotlin:kotlin-gradle-plugin:${KOTLIN_VERSION}"`,
  );
  writeFileSync(projGradle, p);
  log("added kotlin-gradle-plugin classpath");
}

log("done — native overlay applied.");
