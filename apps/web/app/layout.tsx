import { Analytics } from "@vercel/analytics/next";
import { SpeedInsights } from "@vercel/speed-insights/next";
import type { Metadata } from "next";
import { IBM_Plex_Mono, Onest } from "next/font/google";
import { YandexMetrika } from "@/components/analytics/YandexMetrika";
import { FeedbackWidgetLoader } from "@/components/app/FeedbackWidgetLoader";
import { NavProgress } from "@/components/ui/NavProgress";
import { ThemeApplier } from "@/components/ui/ThemeApplier";
import { DEFAULT_LOCALE } from "@/i18n/locale";
import { siteConfig } from "@/lib/site";
import "./globals.css";

// Quip UI fonts. Onest (display + body) matches the live brand at quip.ink;
// IBM Plex Mono carries timecodes / confidence scores / prices (tabular figures).
const onest = Onest({
  variable: "--font-onest",
  subsets: ["latin", "cyrillic"],
  display: "swap",
});
const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(siteConfig.url),
  title: {
    default: `${siteConfig.name} — ${siteConfig.tagline}`,
    template: `%s · ${siteConfig.name}`,
  },
  description: siteConfig.description,
  applicationName: siteConfig.name,
  keywords: [
    "AI video clipper",
    "podcast to shorts",
    "repurpose long videos",
    "vertical clips",
    "explainable AI clips",
    "clip generator",
  ],
  authors: [{ name: siteConfig.name }],
  openGraph: {
    type: "website",
    siteName: siteConfig.name,
    title: `${siteConfig.name} — ${siteConfig.tagline}`,
    description: siteConfig.description,
    url: siteConfig.url,
    // Default landing copy is English; Russian use-case pages set ru_RU themselves.
    locale: "en_US",
    alternateLocale: ["ru_RU"],
  },
  twitter: {
    card: "summary_large_image",
    title: `${siteConfig.name} — ${siteConfig.tagline}`,
    description: siteConfig.description,
    creator: siteConfig.twitterHandle,
  },
  robots: { index: true, follow: true },
  // Search-engine ownership verification. Set the env vars in Vercel once
  // (Google Search Console + Yandex.Webmaster give you the token). Yandex.Webmaster
  // is a P0 for the Russian market — Yandex is ~73% of RU search. Undefined env =
  // tag omitted (no empty meta).
  //
  // Bing has no first-class key in Next's metadata API, so it goes through `other`,
  // which renders <meta name="msvalidate.01" content="…"> verbatim. Spread it in only
  // when the token exists — an undefined value inside `other` would emit an empty meta.
  verification: {
    google: siteConfig.googleVerification,
    yandex: siteConfig.yandexVerification,
    ...(siteConfig.bingVerification
      ? { other: { "msvalidate.01": siteConfig.bingVerification } }
      : {}),
  },
  icons: {
    icon: [
      { url: "/favicon.ico", type: "image/x-icon" },
      { url: "/icon.png", type: "image/png" },
    ],
    apple: "/icon.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // The root layout must NOT read the NEXT_LOCALE cookie: doing so (via getLocale()/
  // getMessages()) is a Dynamic API in the ROOT, which opts EVERY route out of static
  // generation — including the hardcoded-English SEO pages (/pricing, /terms, /privacy,
  // /use-case/*) that have zero t() calls. So the <NextIntlClientProvider> lives in the
  // NESTED layouts that actually need client translations ((app), (auth), (home)); the
  // (marketing) group sits under no cookie-reading provider and stays statically rendered.
  //
  // Tradeoff: <html lang> is therefore the static default ("en") for all routes. RU
  // visitors on the bilingual home/app see lang="en" in the document element even though
  // the copy is Russian. Acceptable: making it dynamic would require the root to read the
  // cookie again, re-breaking static generation for the whole marketing/SEO surface.
  return (
    <html
      lang={DEFAULT_LOCALE}
      className={`${onest.variable} ${plexMono.variable} h-full`}
      suppressHydrationWarning
    >
      <body className="min-h-full bg-bg text-ink antialiased">
        {/* Pre-paint bootstrap: (1) mark JS active so scroll-reveal only hides
            content when it can animate it back in; (2) resolve the THEME before
            first paint (no flash). Theme rule: explicit user choice (localStorage
            "quip-theme") wins everywhere; otherwise the surface default — the app
            side (/dashboard, /account, /edit, auth, /dev) opens as the dark
            «studio», marketing opens as the light «stage». Deliberately NO cookie:
            reading one in the root layout would opt the whole static marketing/SEO
            surface out of SSG (see the locale comment above). SPA navigations are
            handled by <ThemeApplier/> (client), which re-applies the surface
            default on route change while respecting the saved choice. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){var d=document.documentElement;d.classList.add('js');var t=null;try{t=localStorage.getItem('quip-theme')}catch(e){}if(t!=='light'&&t!=='dark'){t=/^\\/(dashboard|account|edit|login|signup|auth|dev)(\\/|$)/.test(location.pathname)?'dark':'light'}d.dataset.theme=t})()",
          }}
        />
        <ThemeApplier />
        {/* NavProgress + FeedbackWidgetLoader use no next-intl APIs, so they render fine
            outside any provider (verified: neither imports next-intl). */}
        <NavProgress />
        {children}
        <FeedbackWidgetLoader />
        {/* Vercel Web Analytics — НИЧЕГО не рисует, только шлёт pageview/событие в Vercel
            (смотреть на дашборде проекта, вкладка Analytics). На Vercel надо один раз включить
            Analytics в настройках проекта; в локальном dev — no-op. */}
        <Analytics />
        {/* Vercel Speed Insights — измеряет производительность страниц на реальных устройствах
            пользователей (Core Web Vitals). Включается в настройках проекта на Vercel; в dev — no-op. */}
        <SpeedInsights />
        {/* Yandex.Metrica — поведенческие факторы для ранжирования в Яндексе (Вебвизор). */}
        <YandexMetrika />
      </body>
    </html>
  );
}
