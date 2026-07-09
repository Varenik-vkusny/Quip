import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "accent";
export type ButtonSize = "sm" | "md" | "lg";

const base =
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md font-semibold " +
  "select-none transition duration-200 ease-snappy focus-visible:outline-none " +
  "disabled:pointer-events-none disabled:opacity-50 active:translate-y-0";

const variantClass: Record<ButtonVariant, string> = {
  // primary CTA: ink fill + bg-colored text (theme-inverted, AA-safe in both themes).
  // Accent stays scarce (mark, highlight, hook plate), never load-bearing button text.
  // Hover keeps the ink fill (ink/90 softens it a step in both themes) — a surface
  // token here would invert the button against its own text in one of the two themes.
  primary: "bg-ink text-bg shadow-card hover:bg-ink/90 hover:shadow-card-hover hover:-translate-y-px",
  secondary:
    "bg-surface-2 text-ink border border-line hover:border-line-strong hover:bg-surface-3 hover:-translate-y-px",
  ghost: "text-muted hover:text-ink hover:bg-surface-2",
  // accent CTA for in-product moments (run, apply, retry). text-on-accent is the
  // token pair for accent fills (AA in both themes); use sparingly per DESIGN.md
  // (one or two per view). Lifts on hover, settles on press.
  // Disabled = neutral elevated surface (NOT 50%-opacity accent, which composites to a
  // muddy tone over the canvas and reads "broken" rather than "waiting for input").
  accent:
    "bg-accent text-on-accent shadow-card " +
    "hover:bg-accent-2 hover:shadow-card-hover hover:-translate-y-px " +
    "disabled:opacity-100 disabled:bg-surface-2 disabled:text-faint disabled:shadow-none",
};

const sizeClass: Record<ButtonSize, string> = {
  sm: "h-9 px-3.5 text-sm",
  md: "h-11 px-5 text-[0.95rem]",
  lg: "h-[3.25rem] px-6 text-base",
};

/** Shared styling for buttons and link-buttons (apply to <button> or <Link>). */
export function buttonVariants(opts: { variant?: ButtonVariant; size?: ButtonSize } = {}): string {
  const { variant = "primary", size = "md" } = opts;
  return cn(base, variantClass[variant], sizeClass[size]);
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  className,
  children,
  disabled,
  ...props
}: ButtonProps) {
  return (
    <button
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled ?? loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}
