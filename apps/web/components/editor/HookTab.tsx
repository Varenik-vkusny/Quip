"use client";

import { ChevronDown, ChevronRight, Loader2, RefreshCw, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { memo, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Checkbox";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Select } from "@/components/ui/Select";
import { HOOK_PRESETS } from "@/lib/hookPresets";
import type { ClipEdit, HookOverlay } from "@/lib/types";
import { CAPTION_FONTS, ColorField, DebouncedSlider } from "./StyleControls";

// ── Таб «Хук»: топ-текст клипа (T1, наш отличитель — объяснимый цепляющий заголовок) ──
// Хук = ASS-событие с верхним якорем В ТОМ ЖЕ файле, что субтитры → libass-превью
// показывает его пиксель-в-пиксель как экспорт. Правки идут через onHookChange
// (PATCH captions.hook через очередь мутаций + instant-превью). Паритет со стилем
// субтитров: галерея пресетов + цвет/плашка/контур/шрифт/размер/позиция/анимация.
// Поля HookOverlay все опциональны → создавая хук с нуля, шлём только нужное,
// pydantic дольёт дефолты.

// Labels resolve from editor.hook.animations.<value>.
const HOOK_ANIMATION_VALUES: NonNullable<HookOverlay["animation"]>[] = ["none", "pop", "fade", "bounce"];

const HOOK_DEFAULTS = {
  color: "#FFFFFF",
  box_color: "#FF5A3D" as string | null,
  box_opacity: 1,
  outline_color: "#000000",
  outline_w: 4,
  font: "Unbounded",
  size: 66,
  margin_v: 150,
  uppercase: true,
  animation: "none" as NonNullable<HookOverlay["animation"]>,
};

export const HookTab = memo(function HookTab({
  edit,
  busy,
  onHookChange,
  onRegenerate,
  regenerating = false,
}: {
  edit: ClipEdit;
  busy: boolean;
  onHookChange: (patch: Partial<HookOverlay> | null) => void;
  // W4: перегенерировать текст хука под текущий интервал клипа (узкий Gemini-вызов).
  onRegenerate?: () => void;
  regenerating?: boolean;
}) {
  const t = useTranslations("editor.hook");
  const hook = edit.captions.hook ?? null;
  const enabled = hook?.enabled ?? false;
  const fullClip = hook?.full_clip ?? true;
  // box_color: undefined в патче ≠ null. Модельный дефолт = коралл-плашка.
  const boxColor = hook?.box_color === undefined ? HOOK_DEFAULTS.box_color : hook.box_color;
  const hasPlaque = boxColor !== null;

  // локальный текст: печать не должна слать PATCH на каждый символ (как ColorField).
  // Коммит — на blur / Enter-pause (debounce). Синк с пропом — adjust-during-render.
  const [text, setText] = useState(hook?.text ?? "");
  const [prevText, setPrevText] = useState(hook?.text ?? "");
  // Style section: collapsed by default so common actions are immediately visible.
  const [styleOpen, setStyleOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  if ((hook?.text ?? "") !== prevText) {
    setPrevText(hook?.text ?? "");
    setText(hook?.text ?? "");
  }
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const commitText = (value: string) => {
    setText(value);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => onHookChange({ text: value, enabled: true }), 350);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto pr-1">
      {/* ── PRIMARY: текст + показ + регенерация + тайминг + Remove ── */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-ink">{t("topText")}</h3>
          <div className="flex items-center gap-2">
            {hook && (
              <button
                type="button"
                disabled={busy}
                onClick={() => onHookChange(null)}
                className="rounded text-xs font-medium text-bad/70 transition duration-150 ease-snappy hover:text-bad focus:outline-none focus-visible:ring-2 focus-visible:ring-bad/40 disabled:opacity-50"
              >
                {t("remove")}
              </button>
            )}
            <Checkbox
              label={t("show")}
              className="text-xs"
              checked={enabled}
              disabled={busy}
              onChange={(e) =>
                onHookChange(e.target.checked ? { enabled: true } : { enabled: false })
              }
            />
          </div>
        </div>

        <textarea
          value={text}
          disabled={busy}
          rows={2}
          maxLength={80}
          placeholder={t("placeholder")}
          onChange={(e) => commitText(e.target.value)}
          onBlur={() => {
            if (timer.current) clearTimeout(timer.current);
            onHookChange({ text, enabled: text.trim() ? true : enabled });
          }}
          className="w-full resize-none rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm font-semibold text-ink outline-none transition-colors focus:border-accent/60"
        />
        <p className="flex items-center gap-1.5 text-xs leading-snug text-muted">
          <Sparkles className="size-3 shrink-0 text-muted" />
          {t("helper")}
        </p>

        {/* W4: хук НЕ обновляется сам при сдвиге клипа — явная регенерация под новый интервал */}
        {onRegenerate && (
          <div className="space-y-1.5 pt-1">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={busy || regenerating}
              onClick={onRegenerate}
              className="w-full"
            >
              {regenerating ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <RefreshCw className="size-3.5" />
              )}
              {regenerating ? t("writingHook") : t("regenerate")}
            </Button>
            <p className="text-xs leading-snug text-muted">
              {t("regenerateHelper")}
            </p>
          </div>
        )}
      </section>

      {/* ── когда показывать ── */}
      <section className="space-y-2">
        <Eyebrow tone="muted">{t("whenToShow")}</Eyebrow>
        <div className="grid grid-cols-2 gap-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => onHookChange({ full_clip: true })}
            className={`rounded-lg border px-3 py-2.5 text-sm font-semibold transition duration-150 ease-snappy ${
              fullClip
                ? "border-accent-line bg-surface-3 text-accent"
                : "border-line bg-surface-2 text-muted hover:border-line-strong hover:text-ink"
            }`}
          >
            {t("wholeClip")}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onHookChange({ full_clip: false })}
            className={`rounded-lg border px-3 py-2.5 text-sm font-semibold transition duration-150 ease-snappy ${
              !fullClip
                ? "border-accent-line bg-surface-3 text-accent"
                : "border-line bg-surface-2 text-muted hover:border-line-strong hover:text-ink"
            }`}
          >
            {t("firstSeconds")}
          </button>
        </div>
        {!fullClip && (
          <div className="pt-1">
            {/* Reuse DebouncedSlider so the 20px range-touch thumb is centered in its own
                h-10 row (clear of the label) — a bare input here overlapped the label text. */}
            <DebouncedSlider
              label={t("showDuration")}
              min={1}
              max={15}
              value={Math.round(hook?.duration_sec ?? 4)}
              disabled={busy}
              onCommit={(v) => onHookChange({ duration_sec: v })}
            />
          </div>
        )}
      </section>

      {/* ── SECONDARY: collapsible Style section (default collapsed) ── */}
      <section className="border-t border-line pt-4">
        <button
          type="button"
          onClick={() => setStyleOpen((o) => !o)}
          aria-expanded={styleOpen}
          className="flex w-full items-center gap-1.5 text-sm font-semibold text-ink transition duration-150 ease-snappy hover:text-accent"
        >
          {styleOpen ? (
            <ChevronDown className="size-4 shrink-0 text-muted" />
          ) : (
            <ChevronRight className="size-4 shrink-0 text-muted" />
          )}
          {t("style")}
        </button>

        {styleOpen && (
          <div className="mt-3 space-y-3">
            {/* ── пресеты хука (галерея look'ов) ── */}
            <div className="space-y-2">
              <Eyebrow tone="muted">{t("presets")}</Eyebrow>
              <div className="grid grid-cols-2 gap-3 py-1">
                {HOOK_PRESETS.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    disabled={busy}
                    title={preset.name}
                    onClick={() => onHookChange({ ...preset.values, enabled: true })}
                    className="flex flex-col items-stretch gap-1.5 rounded-lg border border-line bg-surface-2 p-2 transition hover:border-line-strong focus:outline-none focus:ring-2 focus:ring-accent/50 disabled:opacity-50"
                  >
                    <HookPresetThumb preset={preset.values} />
                    <span className="truncate text-center text-xs font-semibold text-muted">
                      {preset.name}
                    </span>
                  </button>
                ))}
              </div>
            </div>

            {/* ── стиль (паритет с субтитрами) ── */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <ColorField
                label={t("textColor")}
                value={hook?.color ?? HOOK_DEFAULTS.color}
                disabled={busy}
                onChange={(v) => onHookChange({ color: v })}
              />
              <label className="flex flex-col gap-1.5 text-xs text-muted">
                {t("font")}
                <Select
                  value={hook?.font ?? HOOK_DEFAULTS.font}
                  disabled={busy}
                  onChange={(e) => onHookChange({ font: e.target.value })}
                >
                  {CAPTION_FONTS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </Select>
              </label>
            </div>

            {/* плашка vs контур */}
            <Checkbox
              checked={hasPlaque}
              disabled={busy}
              onChange={(e) =>
                onHookChange({ box_color: e.target.checked ? (boxColor ?? "#FF5A3D") : null })
              }
              label={t("backgroundPlaque")}
              className="text-xs"
            />
            {hasPlaque ? (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <ColorField
                  label={t("plaqueColor")}
                  value={boxColor ?? "#FF5A3D"}
                  disabled={busy}
                  onChange={(v) => onHookChange({ box_color: v })}
                />
                <DebouncedSlider
                  label={t("plaqueOpacity")}
                  min={0}
                  max={100}
                  value={Math.round((hook?.box_opacity ?? HOOK_DEFAULTS.box_opacity) * 100)}
                  disabled={busy}
                  onCommit={(v) => onHookChange({ box_opacity: v / 100 })}
                />
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <ColorField
                  label={t("outline")}
                  value={hook?.outline_color ?? HOOK_DEFAULTS.outline_color}
                  disabled={busy}
                  onChange={(v) => onHookChange({ outline_color: v })}
                />
                <DebouncedSlider
                  label={t("outlineWidth")}
                  min={0}
                  max={16}
                  value={hook?.outline_w ?? HOOK_DEFAULTS.outline_w}
                  disabled={busy}
                  onCommit={(v) => onHookChange({ outline_w: v })}
                />
              </div>
            )}

            <DebouncedSlider
              label={t("size")}
              min={36}
              max={120}
              value={hook?.size ?? HOOK_DEFAULTS.size}
              disabled={busy}
              onCommit={(v) => onHookChange({ size: v })}
            />

            <DebouncedSlider
              label={t("positionFromTop")}
              min={40}
              max={900}
              value={hook?.margin_v ?? HOOK_DEFAULTS.margin_v}
              disabled={busy}
              // clear pos_y so the slider regains vertical control after a free drag; keep pos_x.
              onCommit={(v) => onHookChange({ margin_v: v, pos_y: null })}
              hint={t("positionHint")}
            />

            <label className="flex flex-col gap-1.5 text-xs text-muted">
              {t("entranceAnimation")}
              <Select
                value={hook?.animation ?? HOOK_DEFAULTS.animation}
                disabled={busy}
                onChange={(e) =>
                  onHookChange({ animation: e.target.value as HookOverlay["animation"] })
                }
              >
                {HOOK_ANIMATION_VALUES.map((a) => (
                  <option key={a} value={a}>
                    {t(`animations.${a}`)}
                  </option>
                ))}
              </Select>
            </label>

            <Checkbox
              checked={hook?.uppercase ?? HOOK_DEFAULTS.uppercase}
              disabled={busy}
              onChange={(e) => onHookChange({ uppercase: e.target.checked })}
              label={t("uppercase")}
              className="text-xs"
            />
          </div>
        )}
      </section>
    </div>
  );
});

/** Мини-превью хук-пресета: слово в основном цвете на плашке/с контуром. */
function HookPresetThumb({ preset }: { preset: Partial<HookOverlay> }) {
  const color = preset.color ?? "#FFFFFF";
  const box = preset.box_color ?? null;
  const outline = preset.outline_color ?? "#000000";
  return (
    <div className="flex h-16 w-full items-center justify-center rounded-lg bg-black px-2">
      <span
        style={{
          fontFamily: "var(--font-display), system-ui, sans-serif",
          fontWeight: 900,
          fontSize: "17px",
          color: box ? "#FFFFFF" : color,
          background: box ?? undefined,
          borderRadius: box ? "4px" : undefined,
          padding: box ? "2px 6px" : undefined,
          textShadow: box ? undefined : `0 0 3px ${outline}, 0 0 3px ${outline}`,
        }}
      >
        HOOK
      </span>
    </div>
  );
}
