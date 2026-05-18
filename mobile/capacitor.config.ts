import type { CapacitorConfig } from "@capacitor/cli";

// applicationId / package: deliberately distinct from the desktop
// Tauri identifier (com.joshuarodriguez.meeting-recorder) so the phone
// app and a hypothetical future desktop-on-Android never collide, and
// because Android package segments can't contain hyphens anyway.
const config: CapacitorConfig = {
  appId: "com.joshuarodriguez.meetingrecorder",
  appName: "Meeting Recorder",
  webDir: "dist",
  android: {
    // Sideloaded debug APK — allow the WebView to talk to itself only.
    allowMixedContent: false,
  },
};

export default config;
