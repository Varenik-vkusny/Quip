"use client";

import { useEffect, useRef } from "react";
import type { MomentType } from "@/lib/landingContent";

/*
  WaveformRiver — the landing's signature element. A canvas waveform of a long
  conversation flows under the hero; a playhead sweeps it and, on reaching a
  moment, the bars around it IGNITE in gold and a typed chip pops above the
  wave. It is the product's job, drawn: Quip listens to an hour of talk and
  catches the few moments worth posting.

  Craft notes:
  - Deterministic bars (seeded PRNG, speech-like bursts) — no hydration drift,
    no Math.random in render.
  - Canvas repaints only transform-cheap state (playhead, glow); DOM chips are
    positioned once and revealed via [data-lit] (transform/opacity only).
  - Colors come from the live theme tokens (recomputed when <html data-theme>
    flips), so the river works on both the light stage and the dark studio.
  - prefers-reduced-motion → static wave, every moment pre-lit, no rAF loop.
  - rAF pauses when the strip is offscreen or the tab is hidden.
*/

export type RiverMoment = {
  /** position across the source, 0..1 */
  at: number;
  type: MomentType;
  label: string;
};

const BAR_COUNT = 220;
const SWEEP_SECONDS = 38; // one full pass of the conversation
const IGNITE_RADIUS = 0.012; // how much of the wave flares around a moment

/** mulberry32 — tiny deterministic PRNG so the "speech" never changes between renders */
function mulberry32(seed: number) {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** speech-like envelope: bursts of talk separated by breaths */
function buildBars(): number[] {
  const rnd = mulberry32(20260709);
  const bars: number[] = [];
  let energy = 0.35;
  for (let i = 0; i < BAR_COUNT; i++) {
    // slow envelope drift + occasional pause (breath) + syllable jitter
    if (rnd() < 0.045) energy = 0.12 + rnd() * 0.2; // breath
    else if (rnd() < 0.1) energy = 0.45 + rnd() * 0.5; // emphasis burst
    else energy += (rnd() - 0.48) * 0.16;
    energy = Math.min(1, Math.max(0.08, energy));
    bars.push(0.15 + energy * 0.85 * (0.72 + 0.28 * Math.sin(i * 0.7 + rnd() * 2)));
  }
  return bars;
}

const BARS = buildBars();

type Tokens = {
  bar: string;
  barQuiet: string;
  gold: string;
  goldSoft: string;
};

function readTokens(el: HTMLElement): Tokens {
  const cs = getComputedStyle(el);
  const line = cs.getPropertyValue("--t-line-strong").trim() || "rgba(28,26,23,0.16)";
  const faint = cs.getPropertyValue("--t-line").trim() || "rgba(28,26,23,0.09)";
  const gold = cs.getPropertyValue("--t-accent").trim() || "#f0a400";
  const goldSoft = cs.getPropertyValue("--t-accent-tint").trim() || "rgba(240,164,0,0.12)";
  return { bar: line, barQuiet: faint, gold, goldSoft };
}

export function WaveformRiver({
  moments,
  className,
}: {
  moments: RiverMoment[];
  className?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let tokens = readTokens(wrap);
    let width = 0;
    let height = 0;
    let raf = 0;
    let running = false;
    let visible = true;
    const started = performance.now();
    const chips = Array.from(wrap.querySelectorAll<HTMLElement>("[data-chip]"));

    const resize = () => {
      const rect = wrap.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw(progress());
    };

    const progress = () =>
      reduce ? 1 : (((performance.now() - started) / 1000) % SWEEP_SECONDS) / SWEEP_SECONDS;

    const draw = (p: number) => {
      ctx.clearRect(0, 0, width, height);
      const mid = height / 2;
      const step = width / BAR_COUNT;
      const barW = Math.max(1.5, step * 0.55);

      for (let i = 0; i < BAR_COUNT; i++) {
        const x = i * step + step / 2;
        const frac = i / BAR_COUNT;
        const passed = frac <= p;

        // nearest moment influence (gold ignition around lit moments)
        let ignite = 0;
        for (const m of moments) {
          if (!reduce && m.at > p) continue; // not reached yet
          const d = Math.abs(frac - m.at);
          if (d < IGNITE_RADIUS * 3) ignite = Math.max(ignite, 1 - d / (IGNITE_RADIUS * 3));
        }

        // playhead breath: bars near the head lift slightly
        const headD = Math.abs(frac - p);
        const breath = !reduce && headD < 0.02 ? (1 - headD / 0.02) * 0.22 : 0;

        const h = BARS[i] * (height * 0.78) * (1 + breath) * (0.55 + 0.45 * (passed ? 1 : 0.55));
        ctx.fillStyle = ignite > 0.08 ? tokens.gold : passed ? tokens.bar : tokens.barQuiet;
        ctx.globalAlpha = ignite > 0.08 ? 0.45 + 0.55 * ignite : 1;
        // rounded bar
        const y = mid - h / 2;
        const r = Math.min(barW / 2, 2);
        ctx.beginPath();
        ctx.roundRect(x - barW / 2, y, barW, h, r);
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      // playhead
      if (!reduce) {
        const x = p * width;
        const grad = ctx.createLinearGradient(x, 0, x, height);
        grad.addColorStop(0, "transparent");
        grad.addColorStop(0.5, tokens.gold);
        grad.addColorStop(1, "transparent");
        ctx.fillStyle = grad;
        ctx.fillRect(x - 0.75, 0, 1.5, height);
      }

      // chips: reveal once the head passes their moment
      for (let c = 0; c < chips.length; c++) {
        const at = Number(chips[c].dataset.at || 0);
        const lit = reduce || at <= p;
        if ((chips[c].dataset.lit === "1") !== lit) {
          chips[c].dataset.lit = lit ? "1" : "0";
        }
      }
    };

    const loop = () => {
      if (!running) return;
      draw(progress());
      raf = requestAnimationFrame(loop);
    };

    const start = () => {
      if (running || reduce) return;
      running = true;
      raf = requestAnimationFrame(loop);
    };
    const stop = () => {
      running = false;
      cancelAnimationFrame(raf);
    };

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    // theme flips re-read the tokens (light stage ↔ dark studio)
    const mo = new MutationObserver(() => {
      tokens = readTokens(wrap);
      draw(progress());
    });
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    const io = new IntersectionObserver((entries) => {
      visible = entries[0]?.isIntersecting ?? true;
      if (visible && !document.hidden) start();
      else stop();
    });
    io.observe(wrap);

    const onVis = () => {
      if (document.hidden || !visible) stop();
      else start();
    };
    document.addEventListener("visibilitychange", onVis);

    if (!reduce) start();

    return () => {
      stop();
      ro.disconnect();
      mo.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [moments]);

  return (
    <div ref={wrapRef} className={`relative ${className ?? ""}`} aria-hidden>
      <canvas ref={canvasRef} className="absolute inset-0 size-full" />
      {/* typed chips pop above the wave as the playhead catches each moment */}
      {moments.map((m) => (
        <span
          key={m.at}
          data-chip
          data-at={m.at}
          data-lit="0"
          className={[
            "absolute -top-1 -translate-x-1/2 select-none rounded-[7px] px-2 py-0.5",
            "text-[10.5px] font-bold uppercase tracking-[0.02em]",
            "transition-[transform,opacity] duration-500 ease-[cubic-bezier(0.23,1,0.32,1)]",
            "data-[lit=0]:translate-y-2 data-[lit=0]:scale-90 data-[lit=0]:opacity-0",
            "data-[lit=1]:translate-y-0 data-[lit=1]:scale-100 data-[lit=1]:opacity-100",
            m.type === "hook" && "bg-hook-tint text-hook",
            m.type === "peak" && "bg-peak-tint text-peak",
            m.type === "thought" && "bg-thought-tint text-thought",
            m.type === "quote" && "bg-quote-tint text-quote",
          ]
            .filter(Boolean)
            .join(" ")}
          style={{ left: `${m.at * 100}%` }}
        >
          {m.label}
        </span>
      ))}
    </div>
  );
}
