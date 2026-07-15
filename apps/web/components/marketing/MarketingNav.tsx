import { MarketingNavAuth } from "@/components/marketing/MarketingNavAuth";
import { Container } from "@/components/ui/Container";
import { Logo } from "@/components/ui/Logo";
import { ThemeToggle } from "@/components/ui/ThemeToggle";

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

        <div className="flex items-center gap-2">
          {/* theme switch lives beside the CTAs (mirrors the landing Nav); on mobile it moves into
              the MobileMenu dropdown so the bar stays uncluttered. Hidden via a WRAPPER — a per-item
              `hidden sm:inline-flex` is a no-op (ThemeToggle hardcodes `inline-flex` + cn() is a plain
              joiner, no tailwind-merge → `hidden` lost the CSS-source-order fight, it showed anyway). */}
          <div className="hidden sm:flex">
            <ThemeToggle />
          </div>
          <MarketingNavAuth links={links} />
        </div>
      </Container>
    </header>
  );
}
