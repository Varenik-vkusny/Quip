import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  experimental: {
    // Tree-shake barrel imports for packages Next doesn't optimize by default.
    // @phosphor-icons/react is imported (named, via /dist/ssr) across the landing
    // (Nav, Pricing, Faq, Comparison, CTA) and is NOT in Next's built-in list —
    // without this, a named import can pull the whole icon barrel. (lucide-react is
    // already default-optimized, so it doesn't need to be listed.)
    optimizePackageImports: ["@phosphor-icons/react"],
  },
  async headers() {
    // Long-lived Cache-Control for the multi-megabyte landing demo videos/posters
    // served straight from public/ (public/demo, public/media, public/clips,
    // public/mock). These filenames are NOT content-hashed (unlike Next's
    // build-hashed static assets, which already get an immutable 1y header the
    // platform can't override) — so we deliberately do NOT use `immutable` or a
    // 1-year max-age here. If one of these files is ever swapped in place without
    // renaming, an immutable/very-long cache would pin returning visitors to the
    // stale bytes with no way to bust it short of a filename change. 7 days +
    // stale-while-revalidate gives most of the repeat-visit/back-forward win while
    // bounding how long a mistaken swap can stay wrong for someone who already
    // cached it.
    return [
      {
        source: "/:dir(demo|media|clips|mock)/:path*",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=604800, stale-while-revalidate=86400",
          },
        ],
      },
    ];
  },
};

// next-intl cookie mode: wires i18n/request.ts (reads the NEXT_LOCALE cookie) into the
// build so getTranslations / useTranslations / getLocale resolve per request.
const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

export default withNextIntl(nextConfig);
