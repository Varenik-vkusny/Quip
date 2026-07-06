import type { Metadata, Viewport } from "next";
import { Onest, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";

const onest = Onest({
  subsets: ["latin"],
  variable: "--font-onest",
  weight: ["400", "500", "600", "700", "800"],
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  variable: "--font-plex-mono",
  weight: ["400", "500", "600"],
  display: "swap",
});

const SITE = "https://quip.ink";
const DESCRIPTION =
  "Quip turns long videos into short vertical clips, and tells you why each one will land. Explainable AI clips with a hook, a confidence score, and the reason it works.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: {
    default: "Quip: Know why your clips are worth posting",
    template: "%s · Quip",
  },
  description: DESCRIPTION,
  applicationName: "Quip",
  keywords: [
    "AI video clips",
    "podcast clips",
    "vertical clips",
    "clip confidence score",
    "explainable AI clipping",
    "OpusClip alternative",
  ],
  authors: [{ name: "Quip" }],
  openGraph: {
    type: "website",
    url: SITE,
    siteName: "Quip",
    title: "Quip: Know why your clips are worth posting",
    description: DESCRIPTION,
    images: [{ url: "/media/demo-poster.jpg", width: 1876, height: 964, alt: "Quip cutting clips, with a confidence score on each." }],
  },
  twitter: {
    card: "summary_large_image",
    title: "Quip: Know why your clips are worth posting",
    description: DESCRIPTION,
    images: ["/media/demo-poster.jpg"],
  },
  icons: {
    icon: "/icon.png",
    apple: "/apple-icon.png",
    shortcut: "/favicon.ico",
  },
};

export const viewport: Viewport = {
  themeColor: "#0c0b09",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${onest.variable} ${plexMono.variable} grain`}>
      <body>{children}</body>
    </html>
  );
}
