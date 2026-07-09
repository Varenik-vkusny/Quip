import Image from "next/image";
import Link from "next/link";
import { cn } from "@/lib/cn";

export function Logo({
  className,
  href = "/",
  size = "md",
}: {
  className?: string;
  href?: string | null;
  size?: "sm" | "md";
}) {
  const iconSize = size === "sm" ? 20 : 28;
  const inner = (
    <span className="inline-flex items-center gap-2">
      <Image
        src="/icon.png"
        alt=""
        width={iconSize}
        height={iconSize}
        className="shrink-0 rounded-[7px]"
        aria-hidden
      />
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