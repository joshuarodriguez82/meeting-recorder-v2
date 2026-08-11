import type { Metadata } from "next";
import { Figtree, Geist_Mono } from "next/font/google";
import { Toaster } from "@/components/ui/sonner";
import "./globals.css";

// Figtree for body/UI copy — pairs with the Zoom-Workplace-inspired
// visual refresh. Geist_Mono stays for timestamps/IDs (see below) since
// those rely on tabular figure alignment that a humanist sans doesn't
// guarantee.
const figtreeSans = Figtree({
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
      className={`${figtreeSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        {children}
        <Toaster position="top-right" richColors />
      </body>
    </html>
  );
}
