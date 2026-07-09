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

        /* moment map: pin the stage, pan the film-strip horizontally.
           md+ only — on mobile the strip is a native swipe gallery instead. */
        const pin = document.querySelector<HTMLElement>("[data-map-pin]");
        const track = pin?.querySelector<HTMLElement>("[data-map-track]");
        if (pin && track && window.matchMedia("(min-width: 768px)").matches) {
          const distance = () => Math.max(0, track.scrollWidth - window.innerWidth);
          const cards = gsap.utils.toArray<HTMLElement>("[data-map-card]");

          // coverflow depth: the card nearest the viewport centre is full size and
          // bright; its neighbours recede in scale and fade, so the strip reads as a
          // focused timeline moving THROUGH the frame instead of a flat slide.
          const applyDepth = () => {
            const cx = window.innerWidth / 2;
            const reach = window.innerWidth * 0.44;
            for (const card of cards) {
              const r = card.getBoundingClientRect();
              const d = Math.min(1, Math.abs(r.left + r.width / 2 - cx) / reach);
              gsap.set(card, {
                scale: 1.06 - d * 0.26, // focused card pops above 1.0, neighbours recede
                opacity: 1 - d * 0.62,
                yPercent: -(1 - d) * 4, // a gentle arc: the card in focus lifts a touch
                transformOrigin: "center 60%",
              });
            }
          };

          gsap.to(track, {
            x: () => -distance(),
            ease: "none",
            scrollTrigger: {
              trigger: pin,
              start: "top top",
              end: () => `+=${distance()}`,
              pin: true,
              scrub: 1,
              invalidateOnRefresh: true,
              onUpdate: applyDepth,
              onRefresh: applyDepth,
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
