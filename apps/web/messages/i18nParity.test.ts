import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Guards the #1 i18n bug tsc CANNOT catch: a message key present in one catalog but
// missing from the other (fails only at runtime with next-intl's "missing message").
// Every key MUST exist in BOTH en.json and ru.json with an identical nested shape.

type Json = Record<string, unknown>;

function load(name: string): Json {
  return JSON.parse(readFileSync(new URL(`./${name}`, import.meta.url), "utf8")) as Json;
}

/** All leaf key paths (dotted), recursing into nested objects. Arrays aren't used in the catalogs. */
function keyPaths(obj: Json, prefix = ""): string[] {
  const out: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      out.push(...keyPaths(v as Json, path));
    } else {
      out.push(path);
    }
  }
  return out;
}

describe("message catalog parity (en ⇄ ru)", () => {
  const en = new Set(keyPaths(load("en.json")));
  const ru = new Set(keyPaths(load("ru.json")));

  it("has no keys present in en.json but missing from ru.json", () => {
    const missing = [...en].filter((k) => !ru.has(k)).sort();
    expect(missing, `Keys in en.json missing from ru.json:\n${missing.join("\n")}`).toEqual([]);
  });

  it("has no keys present in ru.json but missing from en.json", () => {
    const extra = [...ru].filter((k) => !en.has(k)).sort();
    expect(extra, `Keys in ru.json missing from en.json:\n${extra.join("\n")}`).toEqual([]);
  });
});
