"use client";

import { useState } from "react";

// Results-page tabs: land on the clips (as usual), switch to the interactive Video Map with one
// click — so the two views each get full focus instead of stacking down the page. The inactive
// view is unmounted (lazy) so the map's source-preview video only loads when you open the map.

export function ResultTabs({
  clipCount,
  clips,
  map,
}: {
  clipCount: number;
  clips: React.ReactNode;
  map: React.ReactNode;
}) {
  const [tab, setTab] = useState<"clips" | "map">("clips");

  const tabClass = (active: boolean) =>
    `rounded-md px-3.5 py-1.5 text-sm font-medium transition-colors duration-150 ${
      active ? "bg-surface-2 text-ink shadow-sm" : "text-muted hover:text-ink"
    }`;

  return (
    <div className="space-y-4">
      <div
        role="tablist"
        aria-label="Results view"
        className="inline-flex items-center gap-0.5 rounded-lg border border-line p-0.5"
      >
        <button
          type="button"
          role="tab"
          aria-selected={tab === "clips"}
          onClick={() => setTab("clips")}
          className={tabClass(tab === "clips")}
        >
          Clips{clipCount > 0 ? ` (${clipCount})` : ""}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "map"}
          onClick={() => setTab("map")}
          className={tabClass(tab === "map")}
        >
          Video map
        </button>
      </div>

      {tab === "clips" ? clips : map}
    </div>
  );
}
