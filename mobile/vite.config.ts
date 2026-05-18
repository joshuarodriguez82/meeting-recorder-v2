import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// relative base so the bundle works when loaded from the Capacitor
// WebView (capacitor://localhost) and from `vite preview` alike.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
});
