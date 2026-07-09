import Link from "next/link";
import { cn } from "@/lib/cn";

/** Quip wordmark: Signal Gold rounded square + ink play triangle (inline SVG so
 *  it follows the theme tokens; matches the landing Nav mark). The old coral Q
 *  PNG (/icon.png) remains only as the favicon until the asset is regenerated. */
export function Logo({
  className,
  href = "/",
  size = "md",
}: {
  className?: string;
  href?: string | null;
  size?: "sm" | "md";
}) {
  const inner = (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden
        className={cn(
          "grid shrink-0 place-items-center rounded-[9px] bg-accent",
          "shadow-[inset_0_1px_0_rgba(255,255,255,0.35),inset_0_-1px_0_rgba(0,0,0,0.12)]",
          size === "sm" ? "size-6 rounded-[7px]" : "size-[30px]",
        )}
      >
        <svg
          width={size === "sm" ? 9 : 11}
          height={size === "sm" ? 11 : 13}
          viewBox="0 0 12 14"
          fill="none"
        >
          <path
            d="M1.5 1.9c0-.9 1-1.5 1.8-1L11 5.8c.8.5.8 1.7 0 2.2l-7.7 4.9c-.8.5-1.8-.1-1.8-1V1.9Z"
            fill="var(--t-on-accent)"
          />
        </svg>
      </span>
      <span
        className={cn(
          "font-display font-bold tracking-tight text-ink",
          size === "sm" ? "text-base" : "text-lg",
        )}
      >
        Quip
      </span>
    </span>
  );

  if (href) {
    return (
      <Link href={href} aria-label="Quip — home" className={cn("inline-flex items-center", className)}>
        {inner}
      </Link>
    );
  }
  return <span className={cn("inline-flex items-center", className)}>{inner}</span>;
}
