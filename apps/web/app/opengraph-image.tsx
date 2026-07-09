import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ImageResponse } from "next/og";
import { siteConfig } from "@/lib/site";

// Site-wide social card. Generated at build with a local TTF (no network fetch).
// NB: Satori can't read CSS custom properties, so the «Signal Gold» studio
// palette is inlined here BY DESIGN (dark studio bg, warm ink, gold accent).
// Keep in sync with globals.css dark tokens.
export const runtime = "nodejs";
export const alt = siteConfig.ogImageAlt;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  const font = readFileSync(join(process.cwd(), "public/libass/fonts/Montserrat.ttf"));

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          background: "#0d0c0a",
          color: "#f4f2ec",
          padding: "72px",
          fontFamily: "Quip",
        }}
      >
        {/* logo: gold rounded square, ink play triangle */}
        <div style={{ display: "flex", alignItems: "center", gap: "18px" }}>
          <div
            style={{
              width: "56px",
              height: "56px",
              borderRadius: "14px",
              background: "#ffb224",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <div
              style={{
                width: 0,
                height: 0,
                borderLeft: "22px solid #1c1a17",
                borderTop: "14px solid transparent",
                borderBottom: "14px solid transparent",
                marginLeft: "6px",
              }}
            />
          </div>
          <div style={{ fontSize: "42px", fontWeight: 700, letterSpacing: "-0.02em" }}>Quip</div>
        </div>

        {/* headline: one gold phrase, rest ink */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            fontSize: "82px",
            fontWeight: 700,
            lineHeight: 1.04,
            letterSpacing: "-0.03em",
            maxWidth: "1010px",
          }}
        >
          <span>Don&rsquo;t just get clips.&nbsp;</span>
          <span style={{ color: "#ffb224" }}>Know why&nbsp;</span>
          <span>they&rsquo;re worth posting.</span>
        </div>

        {/* tagline (plain, no decorative dot) */}
        <div style={{ display: "flex", fontSize: "27px", color: "#a29b8d" }}>
          Explainable AI clips: a hook, a confidence score, and the reason it works.
        </div>
      </div>
    ),
    { ...size, fonts: [{ name: "Quip", data: font, style: "normal", weight: 700 }] },
  );
}
