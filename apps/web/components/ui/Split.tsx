import { cn } from "@/lib/cn";

// Asymmetric rail + canvas layout — the calibrated-workspace alternative to the
// centered-card stack. Stacks on mobile, splits at lg. Class strings are literal so
// Tailwind can statically extract the arbitrary grid templates.
export type SplitVariant = "main-rail" | "rail-main" | "wide-narrow" | "balanced";

// ⚠️ Base (mobile) MUST be an explicit single 1fr column, NOT the default.
// Bug 2026-07-14: with only `lg:grid-cols-[…]` and no base cols, below lg the grid had
// `grid-template-columns: none` → ONE `auto` track sized to the widest child's max-content. On the
// dashboard that child was the redesigned SourceForm + long recent-project filenames (nowrap
// `truncate`, which only clamps against a CONSTRAINED width) → the whole page blew out to 913px at a
// 390px viewport (523px of horizontal scroll). `grid-cols-1` = `minmax(0,1fr)` (a shrinkable track)
// and `[&>*]:min-w-0` lets each grid ITEM shrink below its content so `truncate`/wrapping engage.
const BASE = "grid items-start grid-cols-1 [&>*]:min-w-0";

const variantCols: Record<SplitVariant, string> = {
  // content + a focused right rail (dashboard / account)
  "main-rail": "lg:grid-cols-[minmax(0,1fr)_340px]",
  // a left rail + the working canvas
  "rail-main": "lg:grid-cols-[300px_minmax(0,1fr)]",
  // wide content + a narrow secondary column
  "wide-narrow": "lg:grid-cols-[minmax(0,1fr)_300px]",
  "balanced": "lg:grid-cols-2",
};

export function Split({
  variant = "main-rail",
  gap = "gap-8 lg:gap-12",
  className,
  children,
}: {
  variant?: SplitVariant;
  gap?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return <div className={cn(BASE, variantCols[variant], gap, className)}>{children}</div>;
}
