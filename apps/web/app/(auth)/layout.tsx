import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";
import { Logo } from "@/components/ui/Logo";

export default async function AuthLayout({ children }: { children: React.ReactNode }) {
  const t = await getTranslations("auth");
  // The root layout no longer mounts a provider (that would force every route dynamic),
  // so the auth client components (AuthForm → useTranslations("auth")) get their provider
  // here. Only the "auth" namespace is shipped to the client — the rest of the catalog
  // never reaches these pages.
  const locale = await getLocale();
  const messages = await getMessages();
  return (
    <NextIntlClientProvider locale={locale} messages={{ auth: messages.auth }}>
      <div className="relative flex min-h-dvh flex-col items-center justify-center overflow-hidden px-5 py-12">
        <div
          aria-hidden
          className="pointer-events-none absolute left-1/2 top-0 size-[600px] -translate-x-1/2 -translate-y-1/3 rounded-full bg-[radial-gradient(circle,var(--t-accent-tint),transparent_62%)]"
        />
        <div className="relative w-full max-w-sm">
          <div className="mb-8 flex justify-center">
            <Logo />
          </div>
          {children}
          <p className="mt-6 text-center text-xs leading-relaxed text-faint">
            {t.rich("agree", {
              terms: (chunks) => (
                <a href="/terms" className="underline hover:text-muted">
                  {chunks}
                </a>
              ),
              privacy: (chunks) => (
                <a href="/privacy" className="underline hover:text-muted">
                  {chunks}
                </a>
              ),
            })}
          </p>
        </div>
      </div>
    </NextIntlClientProvider>
  );
}
