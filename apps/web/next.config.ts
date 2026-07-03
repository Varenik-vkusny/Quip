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
};

// next-intl cookie mode: wires i18n/request.ts (reads the NEXT_LOCALE cookie) into the
// build so getTranslations / useTranslations / getLocale resolve per request.
const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

export default withNextIntl(nextConfig);
