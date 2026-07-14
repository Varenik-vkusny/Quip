"use client";

import { cn } from "@/lib/cn";

// Dual-thumb range primitive built from TWO overlaid native <input type="range">
// (not div + pointermove) so keyboard (arrows/Home/End), touch, and screen-reader
// support come from the platform for free — PRODUCT.md's WCAG AA floor requires
// keyboard navigability. Each input owns one handle; a custom rail + fill span
// underneath supply the visuals, since the inputs' own native track stays invisible.
export type RangeSliderProps = {
  /** Track lower bound (e.g. 4). */
  min: number;
  /** Track upper bound (e.g. 180). */
  max: number;
  /** Step size. Defaults to 1. */
  step?: number;
  /** Minimum distance enforced between the two values (e.g. 2). */
  gap: number;
  valueMin: number;
  valueMax: number;
  onChange: (next: { min: number; max: number }, edited: "min" | "max") => void;
  disabled?: boolean;
  ariaLabelMin: string;
  ariaLabelMax: string;
  /** Formats the track-bound labels under the rail. Defaults to String(n). */
  format?: (n: number) => string;
  className?: string;
};

// The global :focus-visible rule (globals.css) draws its ring on whatever element is
// focused — fine for a normal control, but here the "element" is the invisible,
// full-width input box, so the ring would wrap the ENTIRE track instead of the 20px
// handle. That rule is written after `@import "tailwindcss"`, i.e. UNLAYERED, and an
// unlayered normal declaration beats every @layer — so a plain `shadow-none` utility
// (emitted into @layer utilities) is a NO-OP against it. Only `!important` outranks it.
// Then re-home the same ring recipe (bg + accent-line, as in Switch.tsx) onto the thumb
// pseudo-element via an arbitrary variant — there's no tailwind.config to add one to.
const THUMB_FOCUS_RING =
  "focus-visible:shadow-none! " +
  "[&:focus-visible::-webkit-slider-thumb]:shadow-[0_0_0_2px_var(--color-bg),0_0_0_4px_var(--color-accent-line)] " +
  "[&:focus-visible::-moz-range-thumb]:shadow-[0_0_0_2px_var(--color-bg),0_0_0_4px_var(--color-accent-line)]";

// Height/margin math copied from StyleControls.tsx's DebouncedSlider (h-1.5 input +
// `range-touch`'s -8px thumb margin) — that pairing is the already-proven way to
// center a 20px `range-touch` thumb on a 6px track, so we reuse it verbatim instead
// of re-deriving new offsets. It's absolutely positioned + vertically centered
// (rather than sitting in normal flow like DebouncedSlider's) so two of these can
// stack on the exact same centerline as the rail/fill beneath them.
// `bg-transparent` is a deliberate departure from range-touch's own usage note
// ("no `background: transparent` — that hid the track"): single-thumb sliders WANT
// their own background to paint as the track, but here the rail/fill divs already
// paint that role, so the inputs' native track must stay invisible.
// `pointer-events-none` on the box + `pointer-events-auto` on only the thumb means
// a click can only ever land on a 20px thumb circle, never on the shared invisible
// track underneath both inputs — so the two overlaid inputs don't fight over clicks
// that aren't actually on either handle.
const BASE_INPUT =
  "range-touch absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 m-0 bg-transparent " +
  "pointer-events-none [&::-webkit-slider-thumb]:pointer-events-auto [&::-moz-range-thumb]:pointer-events-auto " +
  "disabled:cursor-not-allowed " +
  THUMB_FOCUS_RING;

/** Dual-thumb range slider: two native `<input type="range">` overlaid on a shared
 *  rail, with a `gap` floor enforced natively (via each input's own min/max attr)
 *  rather than reimplemented in JS. See `apps/web/components/ui/RangeSlider.tsx`
 *  usage note above for why it's built this way instead of divs + pointer events. */
export function RangeSlider({
  min,
  max,
  step = 1,
  gap,
  valueMin,
  valueMax,
  onChange,
  disabled,
  ariaLabelMin,
  ariaLabelMax,
  format = String,
  className,
}: RangeSliderProps) {
  const span = max - min || 1; // guard divide-by-zero on a degenerate min === max track
  const pctMin = ((valueMin - min) / span) * 100;
  const pctMax = ((valueMax - min) / span) * 100;

  // Once the two values sit `gap` apart the hit-circles overlap almost completely, and
  // whichever input stacks on top swallows every click. Stack by WHO CAN STILL MOVE, not
  // by where the pair sits on the track: a bunched pair is only separable by dragging min
  // LEFT or max RIGHT, and min can always go left unless it's already on the floor.
  //   min > track floor → min on top  (drag it left to un-bunch — always available)
  //   min == floor      → max on top  (min has nowhere to go; max must move right)
  // This is dead-end-proof by construction. Scoring it by the pair's midpoint instead
  // (the obvious heuristic) strands the user: bunched at 58–60 the midpoint is ~31% of
  // the track, so max wins the stack — but max CANNOT move left (the gap holds it), and
  // min is buried underneath. Nothing moves.
  const minOnTop = valueMin > min;

  // ☠️ BOTH inputs MUST declare the FULL track [min,max]. A native range paints its thumb at
  // (value - input.min) / (input.max - input.min) — its OWN span. Narrowing an input's min/max
  // attrs to enforce `gap` (the obvious-looking trick) gives the two thumbs DIFFERENT
  // denominators than the rail/fill below them, so the handles drift off the bar, cross over
  // each other (default 20–60 rendered min RIGHT of max), and on 60–120 land on the exact same
  // pixel — burying the min thumb under the max one, unreachable by mouse. Enforce `gap` in
  // onChange instead; the value can be clamped, the SCALE cannot.
  const clampMin = (n: number) => Math.min(n, valueMax - gap);
  const clampMax = (n: number) => Math.max(n, valueMin + gap);

  return (
    <div className={cn("w-full", disabled && "pointer-events-none opacity-50", className)}>
      <div className="relative h-5 select-none">
        {/* rail */}
        <div className="pointer-events-none absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-surface-3" />
        {/* selected span — ink, not gold: gold is the one scarce brand accent
            (chrome/CTA/focus only per DESIGN.md), and Switch.tsx already made this
            exact call for its "on" state ("engaged/ink, NOT coral"). No transition
            on left/width: those are layout properties, not transform/opacity, and a
            slider shouldn't be animating its fill mid-drag regardless. */}
        <div
          className="pointer-events-none absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-ink"
          style={{ left: `${pctMin}%`, width: `${Math.max(0, pctMax - pctMin)}%` }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={valueMin}
          disabled={disabled}
          aria-label={ariaLabelMin}
          aria-valuetext={format(valueMin)}
          onChange={(e) => onChange({ min: clampMin(Number(e.target.value)), max: valueMax }, "min")}
          className={cn(BASE_INPUT, minOnTop ? "z-20" : "z-10")}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={valueMax}
          disabled={disabled}
          aria-label={ariaLabelMax}
          aria-valuetext={format(valueMax)}
          onChange={(e) => onChange({ min: valueMin, max: clampMax(Number(e.target.value)) }, "max")}
          className={cn(BASE_INPUT, minOnTop ? "z-10" : "z-20")}
        />
      </div>
      {/* track bounds, not the current values — a fixed frame of reference under a
          rail whose fill already shows the live selection. */}
      <div className="mt-1.5 flex items-center justify-between">
        <span className="font-mono text-[11px] tabular-nums text-faint">{format(min)}</span>
        <span className="font-mono text-[11px] tabular-nums text-faint">{format(max)}</span>
      </div>
    </div>
  );
}
