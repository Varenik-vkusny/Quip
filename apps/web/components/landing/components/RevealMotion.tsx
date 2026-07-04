"use client";

import { motion, useReducedMotion } from "motion/react";
import type { ReactNode } from "react";

const EASE = [0.23, 1, 0.32, 1] as const; // strong ease-out (emil)

/*
  Motion implementation behind `Reveal` (see Reveal.tsx, which loads this module via
  next/dynamic so `motion/react` ships as its own lazily-fetched chunk instead of
  bloating the bundle every below-the-fold section needs). Loaded with the default
  `ssr: true`, so this still renders — children included — on the server; only the
  JS chunk is deferred, never the markup.

  Single fade-up on scroll-in. Motivated: draws the eye to each block in reading order.
*/
export function Reveal({
  children,
  className = "",
  delay = 0,
  y = 18,
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
  y?: number;
}) {
  const reduce = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={reduce ? false : { opacity: 0, y }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, amount: 0.25 }}
      transition={{ duration: 0.6, delay, ease: EASE }}
    >
      {children}
    </motion.div>
  );
}
