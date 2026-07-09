/*
  Theme plumbing for the dual-theme «Studio» system (light stage / dark studio).

  Rules (single source of truth, mirrored by the pre-paint script in app/layout.tsx):
  - An EXPLICIT user choice (localStorage "quip-theme") wins on every surface.
  - Without a choice, the surface decides: app-side routes open dark (a pro video
    tool), marketing opens light (the trust-building stage).
  - No cookies anywhere in this file: the marketing/SEO surface must stay SSG
    (a theme cookie read in the root layout would make every route dynamic).
*/

export type Theme = "light" | "dark";

const STORAGE_KEY = "quip-theme";

/* App-side prefixes that default to the dark studio. Keep in sync with the
   inline pre-paint script in app/layout.tsx. */
const DARK_DEFAULT = /^\/(dashboard|account|edit|login|signup|auth|dev)(\/|$)/;

export function defaultThemeFor(pathname: string): Theme {
  return DARK_DEFAULT.test(pathname) ? "dark" : "light";
}

export function savedTheme(): Theme | null {
  try {
    const t = localStorage.getItem(STORAGE_KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}

/** Current applied theme (what the pre-paint script or a later apply set). */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/**
 * Apply a theme to <html>. `persist` records it as the user's explicit choice
 * (theme toggle); route-change re-defaults pass persist=false so they never
 * overwrite a choice the user actually made.
 */
export function applyTheme(theme: Theme, opts: { persist?: boolean; animate?: boolean } = {}) {
  const el = document.documentElement;
  if (opts.animate !== false && el.dataset.theme !== theme) {
    // One transition beat (globals.css html.theming) so the switch reads as a
    // deliberate cross-fade, not a repaint glitch.
    el.classList.add("theming");
    window.setTimeout(() => el.classList.remove("theming"), 350);
  }
  el.dataset.theme = theme;
  if (opts.persist) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* storage unavailable (private mode) — theme still applies for the session */
    }
  }
}
