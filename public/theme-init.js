// Applies the saved appearance before first paint, so a dark theme never
// flashes white while the app loads. A plain file (not an inline script):
// the webview's CSP allows scripts from 'self' only. Mirrors
// applyTheme() in src/lib/theme.ts — keep the two in step.
(function () {
  try {
    var raw = localStorage.getItem("mr-theme");
    var t = raw ? JSON.parse(raw) : {};
    var accents = ["ocean", "forest", "ember", "graphite"];
    var root = document.documentElement;
    // Nothing saved means light: the look the app has always had, so an
    // upgrade never switches someone's theme behind their back.
    var dark = t.mode === "dark" ||
      (t.mode === "system" &&
       window.matchMedia("(prefers-color-scheme: dark)").matches);
    if (dark) root.classList.add("dark");
    if (accents.indexOf(t.accent) !== -1) root.setAttribute("data-accent", t.accent);
  } catch (e) {
    // Nothing saved or storage blocked: the light teal default stands.
  }
})();
