"use client";

import { MoonStars, Sun } from "@phosphor-icons/react";
import { useSyncExternalStore } from "react";
import { cn } from "@/lib/cn";
import { applyTheme, type Theme } from "@/lib/theme";

/*
  Light/dark switch for the dual-theme «Studio» system. Persists the choice
  (localStorage "quip-theme") so it wins over surface defaults everywhere.
  The applied theme lives on <html data-theme> (an external system), so it's
  read via useSyncExternalStore: SSR renders a neutral placeholder (server
  snapshot = null), and a MutationObserver keeps the glyph in sync even when
  ThemeApplier flips the theme on a route change. No next-intl dependency —
  safe on the SSG marketing pages; the aria-label ships bilingual.
*/

function subscribe(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}

function getSnapshot(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function getServerSnapshot(): Theme | null {
  return null; // unknown until the client applies the pre-paint script
}

export function ThemeToggle({ className }: { className?: string }) {
  const theme = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const next: Theme = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      aria-label="Switch theme · Переключить тему"
      title={theme === "dark" ? "Light theme" : "Dark theme"}
      onClick={() => applyTheme(next, { persist: true })}
      className={cn(
        "inline-flex size-9 shrink-0 items-center justify-center rounded-pill",
        "border border-line-strong text-muted",
        "transition duration-150 ease-snappy hover:border-ink hover:text-ink active:scale-95",
        className,
      )}
    >
      {/* both glyphs render once mounted; before that a neutral dot keeps layout stable */}
      {theme === null ? (
        <span aria-hidden className="size-2 rounded-pill bg-line-strong" />
      ) : theme === "dark" ? (
        <Sun size={16} weight="bold" />
      ) : (
        <MoonStars size={16} weight="bold" />
      )}
    </button>
  );
}
