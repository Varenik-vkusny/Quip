import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";

/*
  The marketing home is self-contained: its Nav + Footer live in the page itself
  (ported "Readout" landing). So this route-group layout adds only the landing's
  film-grain overlay (.grain, a fixed pointer-events-none ::after defined in
  globals.css). The marketing sub-pages (pricing / terms / privacy / use-case)
  stay under app/(marketing) with the shared MarketingNav + Footer, untouched.

  Route-segment metadata for "/" (brief Step 9): the landing's own title +
  description + OG/Twitter, layered over the root layout's global metadata
  (metadataBase, icons, og:image). Kept em-dash-free per the house brand rule.
*/
const TITLE = "Quip: Know why your clips are worth posting";
// Keep this between ~120 and ~155 chars: Google truncates a meta description around 155-160, and the
// previous one (166) was being cut mid-sentence in the SERP. Ends on the differentiator, not a clause.
const DESCRIPTION =
  "Quip turns long videos into short vertical clips and explains why each one will land: every clip ships with a hook, a confidence score, and a reason.";

export const metadata: Metadata = {
  title: { absolute: TITLE },
  description: DESCRIPTION,
  alternates: { canonical: "/" },
  openGraph: { title: TITLE, description: DESCRIPTION },
  twitter: { title: TITLE, description: DESCRIPTION },
};

export default async function HomeLayout({ children }: { children: React.ReactNode }) {
  // The root layout no longer mounts a provider (that would force every route dynamic).
  // The landing is server-rendered bilingual (sections read the NEXT_LOCALE cookie via
  // getLocale() → getLandingContent(locale)), so this group is inherently cookie-dynamic
  // regardless. The only CLIENT translation consumer here is ConfidenceGaugeMotion
  // (useTranslations("confidence")); Nav/Faq/LocaleSwitcher only need useLocale (carried by
  // the provider's `locale` prop). So we ship just the "confidence" namespace to the client.
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <NextIntlClientProvider locale={locale} messages={{ confidence: messages.confidence }}>
      <div className="grain">{children}</div>
    </NextIntlClientProvider>
  );
}
