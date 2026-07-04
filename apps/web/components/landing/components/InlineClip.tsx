"use client";

import { useEffect, useRef } from "react";

/*
  Muted, looping vertical clip that plays only while in view (perf + battery) and
  holds on its poster when out of view or when the visitor prefers reduced motion.
*/
export function InlineClip({
  src,
  poster,
  label,
  className = "",
}: {
  src: string;
  poster?: string;
  label: string;
  className?: string;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return; // hold on poster

    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          // Warm the metadata fetch (if not already) right before we need to play.
          if (v.preload !== "metadata" && v.preload !== "auto") v.preload = "metadata";
          v.play().catch(() => {});
        } else {
          v.pause();
        }
      },
      { threshold: 0.2 }
    );
    io.observe(v);
    return () => io.disconnect();
  }, []);

  return (
    <video
      ref={ref}
      className={className}
      poster={poster}
      muted
      loop
      playsInline
      preload="none"
      aria-label={label}
    >
      <source src={src} type="video/mp4" />
    </video>
  );
}
