"use client";

import { motion, AnimatePresence, useReducedMotion } from "motion/react";

/*
  Accordion answer body — split out of Faq.tsx so `motion/react` loads via its own
  lazily-fetched chunk (see the next/dynamic import in Faq.tsx) instead of shipping
  eagerly with the rest of the below-the-fold FAQ section.

  Loaded with the default `ssr: true`, so this still renders normally on the server:
  the first item is open by default (`open === 0` in Faq.tsx), so its answer text is
  present in the server-rendered HTML exactly as before — only the JS chunk driving
  the open/close animation is deferred, not the markup.
*/
export function FaqAnswer({ isOpen, answer }: { isOpen: boolean; answer: string }) {
  const reduce = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {isOpen && (
        <motion.div
          initial={reduce ? false : { height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={reduce ? undefined : { height: 0, opacity: 0 }}
          transition={{ duration: 0.3, ease: [0.2, 0.7, 0.2, 1] }}
          className="overflow-hidden"
        >
          <p className="max-w-[64ch] pb-6 pr-10 text-[15px] leading-relaxed text-muted">{answer}</p>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
