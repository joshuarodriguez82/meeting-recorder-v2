import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
  // Tauri requires static export
  trailingSlash: true,
};

export default nextConfig;
