"use client";

import { RotateCcw, Scissors } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, memo, useEffect, useMemo, useRef, useState } from "react";
import { IconButton } from "@/components/ui/IconButton";
import { Badge } from "@/components/ui/Badge";
import { Checkbox } from "@/components/ui/Checkbox";
import { Eyebrow } from "@/components/ui/Eyebrow";
import { Numeral } from "@/components/ui/Numeral";
import { Select } from "@/components/ui/Select";
import { Switch } from "@/components/ui/Switch";
import type { CaptionReply, CaptionStyle, ClipEdit, HighlightStyle, Word } from "@/lib/types";
import { PresetStrip } from "../PresetStrip";
import { CAPTION_FONTS, ColorField, DebouncedSlider } from "./StyleControls";
import { originalReplyText } from "./replyUtils";

// ────────────────────────────────────────────────────────────────────────────
// SubtitlesTab — ОДИН таб для субтитров: и ТЕКСТ (что говорится), и СТИЛЬ (как
// выглядит). Раньше это были два таба (Captions / Style), которые резали ОДИН
// объект (CaptionTrack) надвое и путали юзера. Теперь: сверху строки (правка/
// вырезать/вернуть), ниже — пресеты и оформление. Семантика правок не изменилась
// (те же колбэки), это чисто реорганизация UI.
// Saved style TEMPLATES больше НЕ здесь: это кросс-панельное действие над ВСЕМ
// «look» → вынесено в глобальный header-popover (TemplatesPanel / EditorHeader).
// ────────────────────────────────────────────────────────────────────────────

// Order preserved; labels resolve from editor.subtitles.animations.<value>.
const ANIMATION_VALUES: NonNullable<HighlightStyle["animation"]>[] = [
  "karaoke_fill",
  "color_sweep",
  "blur_in",
  "spring",
  "pop",
  "punch",
  "bounce",
  "fade",
  "drop_in",
  "glow_pulse",
  "shake",
  "slide_up",
  "flash",
  "none",
];

// Primary group title (tier 1): Onest semibold ink, NOT uppercase, with a hairline rule —
// the loud structural anchor. Eyebrow stays for tier-2 minor sub-captions inside a group.
function GroupTitle({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="flex items-baseline gap-2 border-t border-line pt-4">
      <h3 className="text-sm font-semibold text-ink">{children}</h3>
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </div>
  );
}

export const SubtitlesTab = memo(function SubtitlesTab({
  words,
  replies,
  activeReplyIndex,
  busy,
  burn,
  onReplyTextChange,
  onCutReply,
  onSeekReply,
  onBurnChange,
  edit,
  activePresetId,
  onPresetApply,
  onError,
  onStyleChange,
  onHighlightChange,
}: {
  words: Word[];
  replies: CaptionReply[];
  activeReplyIndex: number | null;
  busy: boolean;
  burn: boolean;
  onReplyTextChange: (replyIndex: number, text: string | null) => void;
  onCutReply: (replyIndex: number) => void;
  onSeekReply: (replyIndex: number) => void;
  onBurnChange: (burn: boolean) => void;
  edit: ClipEdit;
  activePresetId: string | null;
  onPresetApply: (presetId: string) => Promise<void>;
  onError: (msg: string) => void;
  onStyleChange: (patch: Partial<CaptionStyle>) => void;
  onHighlightChange: (patch: Partial<HighlightStyle> | null) => void;
}) {
  const t = useTranslations("editor.subtitles");
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const activeRef = useRef<HTMLDivElement>(null);

  const st = edit.captions.style;
  const hl = edit.captions.highlight ?? null;

  useEffect(() => {
    if (editing !== null && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.select();
    }
  }, [editing]);

  // автоскролл к активной реплике (следим за видео)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [activeReplyIndex]);

  const startEdit = (i: number) => {
    const original = originalReplyText(replies, words, i);
    setDraft(replies[i]?.text_override ?? original);
    setEditing(i);
  };

  const commit = () => {
    if (editing === null) return;
    const i = editing;
    const original = originalReplyText(replies, words, i);
    const trimmed = draft.trim();
    setEditing(null);
    onReplyTextChange(i, trimmed && trimmed !== original ? trimmed : null);
  };

  // позиционный offset слов для каждой реплики (для отображения текста)
  const rows = useMemo(() => {
    const out: { reply: CaptionReply; i: number; group: Word[] }[] = [];
    for (let i = 0, offset = 0; i < replies.length; i++) {
      const count = replies[i].word_refs.length;
      out.push({ reply: replies[i], i, group: words.slice(offset, offset + count) });
      offset += count;
    }
    return out;
  }, [replies, words]);

  return (
    <div className="flex flex-col gap-5 pr-1">
      {/* ───────────── LINES (content) ───────────── */}
      <section className="flex flex-col gap-2">
        <Eyebrow tone="muted">{t("linesEyebrow")}</Eyebrow>

        {/* видео уже с вшитыми субтитрами → не накладывать наши (без двойных) */}
        <div className="rounded-md border border-line bg-surface-2 px-2.5 py-2">
          <Switch
            checked={!burn}
            disabled={busy}
            onChange={(e) => onBurnChange(!e.target.checked)}
            label={t("alreadyCaptioned")}
            className="w-full text-xs"
          />
        </div>

        <div
          className={`max-h-[38vh] min-h-[8rem] space-y-1 overflow-y-auto rounded-md border border-line bg-surface-2 p-2 ${
            burn ? "" : "pointer-events-none opacity-40"
          }`}
        >
          {rows.map(({ reply, i, group }) => {
            if (reply.hidden || group.length === 0) return null;
            const text = reply.text_override ?? group.map((w) => w.text).join(" ");
            const isActive = i === activeReplyIndex;
            const isEditing = editing === i;
            return (
              <div
                key={i}
                ref={isActive ? activeRef : undefined}
                className={`group/reply rounded-md border-l-2 px-2.5 py-1.5 transition ${
                  isActive
                    ? "border-l-accent bg-surface-3"
                    : "border-l-transparent hover:bg-surface-2"
                }`}
              >
                <div className="flex items-start gap-2">
                  <button
                    type="button"
                    onClick={() => onSeekReply(i)}
                    title={t("jumpToLine")}
                    className="shrink-0 pt-0.5 text-muted transition hover:text-accent"
                  >
                    <Numeral className="text-[10px]">{fmtSec(group[0].start)}</Numeral>
                  </button>

                  {isEditing ? (
                    <textarea
                      ref={textareaRef}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={commit}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          commit();
                        } else if (e.key === "Escape") {
                          e.preventDefault();
                          setEditing(null);
                        }
                      }}
                      rows={2}
                      className="w-full resize-none rounded-sm border border-accent/60 bg-bg p-1.5 text-sm text-ink outline-none transition-colors focus:border-accent"
                    />
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => startEdit(i)}
                      className="min-w-0 flex-1 text-left text-sm leading-snug text-ink transition hover:text-accent"
                    >
                      {text}
                      {reply.text_override != null && (
                        <Badge tone="neutral" className="ml-1.5 align-middle">
                          {t("edited")}
                        </Badge>
                      )}
                    </button>
                  )}

                  {!isEditing && (
                    <span className="flex shrink-0 items-center gap-1 transition focus-within:opacity-100 [@media(hover:hover)]:opacity-0 [@media(hover:hover)]:group-hover/reply:opacity-100">
                      {reply.text_override != null && (
                        <IconButton
                          size="sm"
                          tone="accent"
                          title={t("restoreOriginal")}
                          aria-label={t("restoreOriginal")}
                          onClick={() => onReplyTextChange(i, null)}
                        >
                          <RotateCcw className="size-3.5" />
                        </IconButton>
                      )}
                      <IconButton
                        size="sm"
                        tone="danger"
                        title={t("cutLine")}
                        aria-label={t("cutLine")}
                        disabled={busy}
                        onClick={() => onCutReply(i)}
                      >
                        <Scissors className="size-3.5" />
                      </IconButton>
                    </span>
                  )}
                </div>
              </div>
            );
          })}
          {rows.length === 0 && (
            <p className="px-2 py-3 text-xs text-muted">{t("noLines")}</p>
          )}
        </div>
      </section>

      {/* ───────────── STYLE (appearance) — one clear group: how the captions LOOK ───────────── */}
      <GroupTitle hint={t("styleGroupHint")}>{t("styleGroup")}</GroupTitle>

      <section className="space-y-2">
        <Eyebrow tone="muted">{t("presets")}</Eyebrow>
        <PresetStrip activePresetId={activePresetId} onApply={onPresetApply} onError={onError} />
      </section>

      <section className="space-y-3">
        <Eyebrow tone="muted">{t("colors")}</Eyebrow>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <ColorField
            label={t("textColor")}
            value={st.color ?? "#FFFFFF"}
            disabled={busy}
            onChange={(v) => onStyleChange({ color: v })}
          />
          <ColorField
            label={t("highlightColor")}
            value={hl?.color ?? "#FF5A3D"}
            disabled={busy || hl === null}
            onChange={(v) => onHighlightChange({ color: v })}
          />
          <ColorField
            label={t("outline")}
            value={st.outline_color ?? "#000000"}
            disabled={busy}
            onChange={(v) => onStyleChange({ outline_color: v })}
          />
        </div>

        <div className="space-y-2 border-t border-line pt-3">
          <Checkbox
            checked={!!st.emphasis_color}
            disabled={busy}
            onChange={(e) =>
              onStyleChange({ emphasis_color: e.target.checked ? "#FF5A3D" : null })
            }
            label={t("highlightKeywords")}
            className="text-xs"
          />
          {st.emphasis_color && (
            <ColorField
              label={t("keywordColor")}
              value={st.emphasis_color}
              disabled={busy}
              onChange={(v) => onStyleChange({ emphasis_color: v })}
            />
          )}
        </div>
      </section>

      <section className="space-y-3">
        <Eyebrow tone="muted">{t("text")}</Eyebrow>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1.5 text-xs text-muted">
            {t("font")}
            <Select
              value={st.font ?? "Montserrat"}
              disabled={busy}
              onChange={(e) => onStyleChange({ font: e.target.value })}
            >
              {CAPTION_FONTS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </Select>
          </label>
        </div>
        <DebouncedSlider
          label={t("size")}
          min={40}
          max={140}
          value={st.size ?? 90}
          disabled={busy}
          onCommit={(v) => onStyleChange({ size: v })}
        />
        <Checkbox
          checked={st.uppercase ?? true}
          disabled={busy}
          onChange={(e) => onStyleChange({ uppercase: e.target.checked })}
          label={t("uppercase")}
          className="text-xs"
        />
      </section>

      <section className="space-y-3">
        <Eyebrow tone="muted">{t("position")}</Eyebrow>
        <DebouncedSlider
          label={t("positionFromBottom")}
          min={40}
          max={1200}
          value={st.margin_v ?? 260}
          disabled={busy}
          onCommit={(v) => onStyleChange({ margin_v: v, pos_y: null })}
          hint={t("positionHint")}
        />
      </section>

      <section className="space-y-3">
        <Eyebrow tone="muted">{t("animation")}</Eyebrow>
        <label className="flex flex-col gap-1.5 text-xs text-muted">
          {t("activeWordAnimation")}
          <Select
            value={hl === null ? "off" : (hl.animation ?? "karaoke_fill")}
            disabled={busy}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "off") onHighlightChange(null);
              else onHighlightChange({ animation: v as HighlightStyle["animation"] });
            }}
          >
            {ANIMATION_VALUES.map((v) => (
              <option key={v} value={v}>
                {t(`animations.${v}`)}
              </option>
            ))}
            <option value="off">{t("highlightOff")}</option>
          </Select>
        </label>
      </section>
    </div>
  );
});

function fmtSec(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
