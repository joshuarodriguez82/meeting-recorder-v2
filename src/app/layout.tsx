import type { Metadata } from "next";
import { Inter, Geist_Mono } from "next/font/google";
import { Toaster } from "@/components/ui/sonner";
import { ThemeSync } from "@/components/theme-sync";
import "./globals.css";

// Inter for everything a person reads: it holds up at the small sizes a
// dense desktop app lives at, and its heavier weights carry the page
// titles without a second display face. Self-hosted at build time by
// next/font, so it works offline and inside the webview's CSP. Geist_Mono
// stays for timestamps/IDs, which rely on tabular alignment.
const interSans = Inter({
  variable: "--font-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Meeting Recorder",
  description: "AI-powered meeting transcription, diarization, and summarization",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${interSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        {/* Applies the saved light/dark + colour theme before first paint
            (see the file). A blocking script on purpose: deferring it is
            exactly the white flash it exists to prevent. */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/theme-init.js" />
      </head>
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <ThemeSync />
        {children}
        <Toaster position="top-right" richColors />
      </body>
    </html>
  );
}
