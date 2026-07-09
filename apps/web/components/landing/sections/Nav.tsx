"use client";

import { useState } from "react";
import {
  useScroll,
  useMotionValueEvent,
  AnimatePresence,
  motion,
  useReducedMotion,
} from "motion/react";
import { List, X } from "@phosphor-icons/react/dist/ssr";
import Image from "next/image";
import { useLocale } from "next-intl";
import { LocaleSwitcher } from "@/components/i18n/LocaleSwitcher";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import { resolveLocale } from "@/i18n/locale";
import { getLandingContent, ROUTES } from "@/lib/landingContent";
import { PrimaryCTA } from "../components/CTA";

export function Logo() {
  return (
    <a href="#top" className="flex items-center gap-2.5" aria-label="Quip home">
      <Image
        src="/icon.png"
        alt=""
        width={28}
        height={28}
        className="shrink-0 rounded-[7px]"
        aria-hidden
      />
      <span className="text-[19px] font-bold tracking-[-0.02em] text-ink">Quip</span>
    </a>
  );
}

/*
  Nav — a fixed bar that SETTLES into a floating frosted panel once the reader
  scrolls past the hero. The settle animates ONLY composited/paint properties
  (background, border, shadow, backdrop-blur, transform) — never width/height/
  margin/padding — so it can't reflow the page (the old max-width+height morph
  flickered and juddered on scroll). A scroll hysteresis (frost on at 80px, off
  at 36px) stops the state flapping when you hover near the threshold.
  motion/react only drives the mobile disclosure.
*/
export function Nav({ authed = false }: { authed?: boolean }) {
  const { nav, openApp } = getLandingContent(resolveLocale(useLocale()));
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const { scrollY } = useScroll();
  const reduce = useReducedMotion();
  useMotionValueEvent(scrollY, "change", (y) =>
    setScrolled((prev) => (prev ? y > 36 : y > 80)),
  );

  const solid = scrolled || open;

  return (
    <header className="fixed inset-x-0 top-0 z-50">
      <div className="mx-auto w-full max-w-[1200px] px-4 sm:px-6">
        <nav
          className={`flex h-16 items-center justify-between gap-6 rounded-2xl border px-3 transition-[background-color,border-color,box-shadow,transform,backdrop-filter] duration-300 ease-[var(--ease-out)] will-change-transform sm:px-4 ${
            solid
              ? "translate-y-2.5 border-line bg-surface/80 shadow-card backdrop-blur-xl"
              : "translate-y-0 border-transparent bg-transparent"
          }`}
        >
          <Logo />

          <div className="hidden items-center gap-7 md:flex">
            {nav.items.map((item) => (
              <a
                key={item.href}
                href={item.href}
                className="text-[14px] text-muted transition-colors duration-150 hover:text-ink"
              >
                {item.label}
              </a>
            ))}
          </div>

          <div className="flex items-center gap-2.5">
            <LocaleSwitcher className="hidden sm:inline-flex" />
            <ThemeToggle className="hidden sm:inline-flex" />
            {!authed && (
              <a
                href={ROUTES.login}
                className="hidden text-[14px] text-muted transition-colors duration-150 hover:text-ink sm:inline"
              >
                {nav.signIn}
              </a>
            )}
            <PrimaryCTA
              href={authed ? ROUTES.app : ROUTES.signup}
              arrow={false}
              className="h-9 px-4 text-[14px]"
            >
              {authed ? openApp : nav.tryFree}
            </PrimaryCTA>
            <button
              onClick={() => setOpen((v) => !v)}
              aria-label={open ? "Close menu" : "Open menu"}
              aria-expanded={open}
              className="grid size-9 place-items-center rounded-[8px] border border-line text-ink md:hidden"
            >
              {open ? (
                <X weight="bold" className="size-4" />
              ) : (
                <List weight="bold" className="size-4" />
              )}
            </button>
          </div>
        </nav>

        {/* mobile disclosure — a frosted panel floating below the bar */}
        <AnimatePresence>
          {open && (
            <motion.div
              initial={reduce ? false : { height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={reduce ? undefined : { height: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.2, 0.7, 0.2, 1] }}
              className="overflow-hidden md:hidden"
            >
              <div className="mt-2 flex flex-col gap-1 rounded-2xl border border-line bg-surface/90 p-3 shadow-card backdrop-blur-xl">
                {nav.items.map((item) => (
                  <a
                    key={item.href}
                    href={item.href}
                    onClick={() => setOpen(false)}
                    className="py-2.5 text-[15px] text-muted transition-colors hover:text-ink"
                  >
                    {item.label}
                  </a>
                ))}
                {!authed && (
                  <a
                    href={ROUTES.login}
                    onClick={() => setOpen(false)}
                    className="border-t border-line py-2.5 pt-3.5 text-[15px] text-muted transition-colors hover:text-ink"
                  >
                    {nav.signIn}
                  </a>
                )}
                <div className="flex items-center gap-3 border-t border-line pt-3.5">
                  <LocaleSwitcher />
                  <ThemeToggle />
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </header>
  );
}
