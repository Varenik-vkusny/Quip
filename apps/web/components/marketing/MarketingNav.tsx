import { MarketingNavAuth } from "@/components/marketing/MarketingNavAuth";
import { Container } from "@/components/ui/Container";
import { Logo } from "@/components/ui/Logo";

const links = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#why", label: "Why Quip" },
  { href: "#pricing", label: "Pricing" },
  { href: "#faq", label: "FAQ" },
];

/** Sticky marketing nav. Static server shell (Logo + links); the auth-aware CTAs live
 *  in the <MarketingNavAuth> client island so this shared layout nav does NOT read the
 *  session cookie server-side — that read previously forced every marketing/SEO page to
 *  render dynamically instead of being statically generated / edge-cached. */
export function MarketingNav() {
  return (
    <header className="sticky top-0 z-50 border-b border-line/70 bg-bg/75 backdrop-blur-xl">
      <Container className="flex h-16 items-center justify-between gap-6">
        <Logo />

        <nav aria-label="Primary" className="hidden items-center gap-8 md:flex">
          {links.map((l) => (
            <a
              key={l.href}
              href={l.href}
              className="text-sm text-muted transition-colors duration-200 hover:text-ink"
            >
              {l.label}
            </a>
          ))}
        </nav>

        <MarketingNavAuth links={links} />
      </Container>
    </header>
  );
}
