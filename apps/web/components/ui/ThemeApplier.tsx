"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { applyTheme, currentTheme, defaultThemeFor, savedTheme } from "@/lib/theme";

/*
  Keeps the surface-default theme correct across CLIENT-SIDE navigations.
  The pre-paint script in app/layout.tsx only runs on full document loads;
  when the user SPA-navigates marketing → /login (or back), this re-applies
  the surface default — unless they made an explicit choice via ThemeToggle,
  which always wins. Renders nothing; safe outside any i18n provider.
*/
export function ThemeApplier() {
  const pathname = usePathname();

  useEffect(() => {
    if (savedTheme()) return; // explicit user choice wins everywhere
    const wanted = defaultThemeFor(pathname);
    if (currentTheme() !== wanted) applyTheme(wanted);
  }, [pathname]);

  return null;
}
