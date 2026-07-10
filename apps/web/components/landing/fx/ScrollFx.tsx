"use client";

import { useEffect } from "react";

/*
  ScrollFx — the landing's scroll choreography, wired once for the whole page.
  Sections stay Server Components and opt in via data-attributes; this single
  client island finds them after hydration and animates with GSAP ScrollTrigger
  on top of Lenis inertial scrolling (the "heavy, cinematic" scroll feel).

  Vocabulary (all transform/opacity only, emil-grade easing):
  - [data-para="<px>"]                hero parallax: element drifts <px> over the hero scroll
  - [data-count-to="<n>"]             mono numeral counts 0→n when it enters the viewport
  - [data-count-bar]                  sibling bar fills to the same fraction
  - [data-map-pin] > [data-map-track] pinned section: vertical scroll → horizontal film-strip pan
  - [data-mani] > .w                  pinned manifesto: words fade .18→1 with scrub
  - [data-studio-window]              3D product-shot entrance (tilt straightens as it enters)

  Simple reveals stay on the house <Reveal/> (IntersectionObserver) — GSAP here
  is only for what IO cannot do: pinning, scrubbing, parallax, inertia.

  prefers-reduced-motion: none of this runs; content is fully visible statically.
*/
export function ScrollFx() {
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let cleanup: (() => void) | undefined;
    let cancelled = false;

    (async () => {
      // dynamic import keeps gsap+lenis out of every non-landing bundle
      const [{ default: gsap }, { default: ScrollTrigger }, { default: Lenis }] =
        await Promise.all([
          import("gsap"),
          import("gsap/ScrollTrigger"),
          import("lenis"),
        ]);
      if (cancelled) return;

      gsap.registerPlugin(ScrollTrigger);

      const lenis = new Lenis({ lerp: 0.11 });
      lenis.on("scroll", ScrollTrigger.update);
      const tick = (t: number) => lenis.raf(t * 1000);
      gsap.ticker.add(tick);
      gsap.ticker.lagSmoothing(0);

      const ctx = gsap.context(() => {
        /* hero parallax: phones and the reason card drift at different depths */
        const hero = document.querySelector<HTMLElement>("[data-hero]");
        if (hero) {
          document.querySelectorAll<HTMLElement>("[data-para]").forEach((el) => {
            gsap.to(el, {
              y: Number(el.dataset.para) || 0,
              ease: "none",
              scrollTrigger: { trigger: hero, start: "top top", end: "bottom top", scrub: true },
            });
          });
        }

        /* confidence numerals count up (the AI "scores" in front of you) */
        document.querySelectorAll<HTMLElement>("[data-count-to]").forEach((el) => {
          const target = Number(el.dataset.countTo) || 0;
          const bar = el.closest("[data-count-scope]")?.querySelector<HTMLElement>("[data-count-bar]");
          const state = { v: 0 };
          ScrollTrigger.create({
            trigger: el,
            start: "top 88%",
            once: true,
            onEnter: () =>
              gsap.to(state, {
                v: target,
                duration: 1.2,
                ease: "power2.out",
                onUpdate: () => {
                  el.textContent = String(Math.round(state.v));
                  if (bar) bar.style.width = `${state.v}%`;
                },
              }),
          });
        });

        /* moment reel: fly the camera down a 3D corridor of clips (the hour of
           conversation, unfolded into depth). Pure CSS-3D transforms driven per
           frame; GSAP only owns the scrubbed "camera" value + the pin. md+ only —
           on mobile / reduced-motion the same cards stay a native swipe strip. */
        const reel = document.querySelector<HTMLElement>("[data-reel-pin]");
        const scene = reel?.querySelector<HTMLElement>("[data-reel-scene]");
        if (reel && scene && window.matchMedia("(min-width: 768px)").matches) {
          const cards = gsap.utils.toArray<HTMLElement>("[data-reel-card]");
          const STEP = 440; // scroll distance (px) the camera travels per clip
          reel.classList.add("reel-on");
          const travel = Math.max(1, cards.length - 1) * STEP;

          // Perf-safe pseudo-3D: depth is faked with translate + scale + opacity
          // ONLY (GPU-composited, cheap). No perspective / preserve-3d / rotateY —
          // those are GPU-bound and fall back to ~2fps software rendering without
          // GPU accel. The focal clip is large, centred and lit; UPCOMING moments
          // recede up-and-right toward a vanishing point (a receding trail, so they
          // never pile on the focal clip); a clip that PASSES the camera swells and
          // flies off down-left. Reads as flying forward along the reel of moments.
          const VPx = 320; // trail vanishing point, right of + above the focal clip
          const VPy = -340;
          const render = (cam: number) => {
            const p = cam / STEP; // fractional focal index
            cards.forEach((el, i) => {
              const rel = i - p; // 0 = focal, + upcoming (recede), - already passed
              let scale: number;
              let x: number;
              let y: number;
              let opacity: number;
              if (rel >= 0) {
                scale = 1.06 * Math.pow(0.68, rel);
                const t = 1 - scale / 1.06; // 0 at the focal clip → ~1 far up the trail
                x = VPx * t;
                y = VPy * t;
                opacity = Math.max(0, Math.min(1, 1 - (rel - 2.8) / 1.1));
              } else {
                const a = -rel; // how far it has flown past the camera
                scale = 1.06 * (1 + a * 0.4); // swell + peel off toward lower-left
                x = -VPx * a * 0.5;
                y = a * 74;
                opacity = Math.max(0, 1 - a / 0.7);
              }
              el.style.transform =
                `translate(calc(-50% + ${x.toFixed(1)}px), calc(-50% + ${y.toFixed(1)}px)) ` +
                `scale(${scale.toFixed(3)})`;
              el.style.opacity = opacity.toFixed(3);
              el.style.zIndex = String(3000 - Math.round(Math.abs(rel) * 100));
              el.classList.toggle("reel-focus", Math.abs(rel) < 0.42);
            });
          };
          render(0);

          const cam = { v: 0 };
          gsap.to(cam, {
            v: travel + 220,
            ease: "none",
            scrollTrigger: {
              trigger: reel,
              start: "top top",
              end: () => `+=${Math.round((travel + 220) * 0.72)}`,
              pin: true,
              scrub: 0.5,
              invalidateOnRefresh: true,
              onUpdate: () => render(cam.v),
              onRefresh: () => render(cam.v),
            },
          });
        }

        /* manifesto: words light up under the scrub (pre-split in markup) */
        const mani = document.querySelector<HTMLElement>("[data-mani]");
        if (mani) {
          gsap.to(mani.querySelectorAll(".w"), {
            opacity: 1,
            ease: "none",
            stagger: 0.08,
            scrollTrigger: {
              trigger: mani,
              start: "top top",
              end: "+=120%",
              pin: true,
              scrub: true,
            },
          });
        }

        /* studio window: product-shot entrance, tilt straightens on the way in */
        const win = document.querySelector<HTMLElement>("[data-studio-window]");
        if (win) {
          gsap.fromTo(
            win,
            { rotationX: 10, scale: 0.94, y: 60, transformPerspective: 1400, opacity: 0 },
            {
              rotationX: 0,
              scale: 1,
              y: 0,
              opacity: 1,
              ease: "none",
              scrollTrigger: { trigger: win, start: "top 92%", end: "top 40%", scrub: 0.6 },
            },
          );
        }
      });

      /* Pinned sections change the page's scroll length AFTER media/fonts load,
         so a direct hash entry (quip.ink/#pricing) can land in a pin-spacer gap.
         Re-measure on full load and re-aim the anchor through Lenis. */
      const onLoad = () => {
        ScrollTrigger.refresh();
        if (location.hash) {
          const el = document.querySelector<HTMLElement>(location.hash);
          if (el) lenis.scrollTo(el, { immediate: true, offset: -80 });
        }
      };
      if (document.readyState === "complete") onLoad();
      else window.addEventListener("load", onLoad, { once: true });

      cleanup = () => {
        window.removeEventListener("load", onLoad);
        ctx.revert();
        gsap.ticker.remove(tick);
        lenis.destroy();
      };
    })();

    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, []);

  return null;
}
