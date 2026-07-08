/*
  Quip landing - content layer (single source of truth for copy + data), BILINGUAL.

  Voice: measured, precise, a little dry. Honest. No fabricated proof.
  ZERO em-dashes anywhere (brand + taste rule).

  i18n (2026-06-30): the landing is fully RU/EN. Like lib/useCases.ts, the copy is a
  typed `Record<Locale, ...>` rather than next-intl messages, because the landing is
  structured data (clips, plans, FAQ arrays) that ICU message catalogs model poorly.
  The active locale comes from the SAME source as the rest of the app — the NEXT_LOCALE
  cookie via next-intl — resolved per render with `getLocale()` (server sections) or
  `useLocale()` (the two client sections, Nav + Faq). Call `getLandingContent(locale)`.

  Locale-invariant media (clip src/poster/timecode/confidence/type) lives once in
  CLIP_MEDIA; only the human text (hook/reason/caption) is per-locale, merged by the
  builder — so the EN and RU example clips can never drift to different media.
*/

import type { Locale } from "@/i18n/locale";

export type MomentType = "hook" | "peak" | "thought" | "quote";

export type Clip = {
  id: string;
  src?: string; // compressed mp4 in /public/clips
  poster?: string;
  hook: string;
  type: MomentType;
  /** Localized moment-type badge label (resolved by getLandingContent). */
  typeLabel: string;
  confidence: number; // 0-100
  reason: string;
  timecode: string; // source range
  caption?: string; // the burned-in line visible in the still, for alt text
};

export type Plan = {
  id: "free" | "starter" | "pro" | "studio";
  name: string;
  price: string;
  cadence: string;
  blurb: string;
  allowance: string;
  features: string[];
  cta: string;
  href: string;
  recommended?: boolean;
};

export const ROUTES = {
  signup: "/signup",
  app: "/dashboard",
  login: "/login",
  terms: "/terms",
  privacy: "/privacy",
};

// ── Locale-invariant example-clip media (text overlaid per locale below) ──
type ClipMedia = {
  id: string;
  src?: string;
  poster?: string;
  type: MomentType;
  confidence: number;
  timecode: string;
};

type ClipKey = "hero" | "second" | "third" | "fourth";

const CLIP_MEDIA: Record<ClipKey, ClipMedia> = {
  hero: {
    id: "03 / 23",
    src: "/clips/clip-1.mp4",
    poster: "/clips/poster-1.jpg",
    type: "thought",
    confidence: 94,
    timecode: "00:14:22 → 00:14:49",
  },
  second: {
    id: "07 / 23",
    src: "/clips/clip-2.mp4",
    poster: "/clips/poster-2.jpg",
    type: "thought",
    confidence: 88,
    timecode: "00:31:08 → 00:31:36",
  },
  third: {
    id: "11 / 23",
    poster: "/clips/frame-1.jpg",
    type: "hook",
    confidence: 91,
    timecode: "00:42:55 → 00:43:19",
  },
  fourth: {
    id: "16 / 23",
    poster: "/clips/frame-5.jpg",
    type: "peak",
    confidence: 76,
    timecode: "00:51:30 → 00:52:02",
  },
};

type ClipText = { hook: string; reason: string; caption?: string };

// ── The runtime shape consumed by the section components ──
export interface LandingContent {
  /** Shared CTA for logged-in visitors ("Open the app"). */
  openApp: string;
  typeLabel: Record<MomentType, string>;
  nav: {
    items: { label: string; href: string }[];
    signIn: string;
    dashboard: string;
    tryFree: string;
  };
  hero: {
    eyebrow: string;
    headlinePre: string;
    headlineMid: string;
    headlineAccent: string;
    headlinePost: string;
    sub: string;
    primary: string;
    secondary: string;
    trust: string;
  };
  heroClip: Clip;
  clips: Clip[];
  demo: { eyebrow: string; heading: string; sub: string; stages: string[] };
  costAnchor: {
    eyebrow: string;
    cells: { label: string; value: string; unit: string; accent?: boolean }[];
  };
  audience: {
    eyebrow: string;
    heading: string;
    sub: string;
    grindLabel: string;
    gainLabel: string;
    privacyLabel: string;
    roles: {
      id: "editor" | "studio" | "creator";
      who: string;
      role: string;
      grind: string;
      gain: string;
    }[];
    trust: string;
  };
  howItWorks: {
    eyebrow: string;
    heading: string;
    sub: string;
    steps: { n: string; title: string; body: string }[];
  };
  why: {
    eyebrow: string;
    heading: string;
    sub: string;
    payload: { id: "hook" | "whyItWorks" | "confidence" | "momentType"; label: string; body: string }[];
  };
  /** Pinned horizontal film-strip: one long video unfolded into a map of moments. */
  momentMap: {
    heading: string;
    sub: string;
    cards: { tc: string; type: MomentType; title: string; confidence: number; poster: string }[];
    more: { count: string; note: string };
  };
  /** Pinned mid-page pivot line; words reveal under the scrub. gold marks the accent word. */
  manifesto: { words: { t: string; gold?: boolean }[] };
  /** The dark «studio» window: the app shown as a product shot inside the light stage. */
  studio: {
    heading: string;
    sub: string;
    url: string;
    createTitle: string;
    createSub: string;
    placeholder: string;
    cut: string;
    drop: string;
    dropStrong: string;
    month: string;
    planTag: string;
    videos: string;
    minutes: string;
    upgrade: string;
  };
  craft: {
    eyebrow: string;
    heading: string;
    features: { id: "speaker" | "noFlash" | "captions" | "ratios"; title: string; body: string }[];
  };
  comparison: {
    eyebrow: string;
    heading: string;
    sub: string;
    cols: { quip: string; them: string };
    rows: { k: string; quip: string; them: string }[];
    kept: { label: string; score: number };
    oneVideoNote: string;
    scoredTooLow: string;
    rejected: { label: string; score: number }[];
  };
  pricing: {
    eyebrow: string;
    heading: string;
    sub: string;
    recommended: string;
    plans: Plan[];
    payg: { name: string; price: string; unit: string; note: string };
    footnote: string;
  };
  faq: { eyebrow: string; heading: string; items: { q: string; a: string }[] };
  finalCta: {
    eyebrow: string;
    heading: string;
    sub: string;
    primary: string;
    secondary: string;
    trust: string;
  };
  footer: {
    tagline: string;
    cols: { title: string; links: { label: string; href: string }[] }[];
    support: string;
    stripe: string;
    honesty: string;
  };
}

// Per-locale strings + the per-clip text overlay (media merged in by the builder).
type LandingStrings = Omit<LandingContent, "heroClip" | "clips"> & {
  clipText: Record<ClipKey, ClipText>;
};

const STRINGS: Record<Locale, LandingStrings> = {
  en: {
    openApp: "Open the app",
    typeLabel: {
      hook: "Hook",
      peak: "Emotional peak",
      thought: "Complete thought",
      quote: "Strong quote",
    },
    nav: {
      items: [
        { label: "How it works", href: "#how-it-works" },
        { label: "Why Quip", href: "#why" },
        { label: "Pricing", href: "#pricing" },
        { label: "FAQ", href: "#faq" },
      ],
      signIn: "Sign in",
      dashboard: "Dashboard",
      tryFree: "Try it free",
    },
    hero: {
      eyebrow: "// CLIP STUDIO",
      headlinePre: "Don't just get clips.",
      headlineMid: "Know ",
      headlineAccent: "why",
      headlinePost: " they're worth posting.",
      sub: "Drop a podcast, interview, or stream. Quip cuts the few clips worth posting and reports why each one lands, so you spend your time on the edit, not the search.",
      primary: "Upload a video",
      secondary: "See how it works",
      trust: "No card. 1 free video / month.",
    },
    clipText: {
      hero: {
        hook: "McConaughey's dad's surprising reaction to quitting law school",
        reason:
          "Opens a loop in the title, then pays it off with a real turn. A complete story that stands on its own.",
        caption: "His dad's surprising reaction.",
      },
      second: {
        hook: "How Matthew McConaughey earned his dad's support",
        reason:
          "A complete story with a clean payoff: he earned it by going all in, never by asking permission. It lands on its own.",
        caption: "He didn't half-ass it.",
      },
      third: {
        hook: "The two seconds that decide everything",
        reason:
          "A scroll-stopping cold open. States the stakes before the viewer can look away.",
        caption: "Hook",
      },
      fourth: {
        hook: "Where it almost fell apart",
        reason:
          "Genuine tension in the voice. Strong on emotion, lighter on a standalone payoff.",
        caption: "Emotional peak",
      },
    },
    demo: {
      eyebrow: "A real run, end to end",
      heading: "Here's how it cuts, and why exactly like that.",
      sub: "One long video in. Quip finds the moments, cuts clean vertical clips, and reports the reason each one will land: a hook, a confidence score, and the cut explained.",
      stages: ["Upload", "Find the moments", "Cut vertical clips"],
    },
    costAnchor: {
      eyebrow: "// THE MATH",
      cells: [
        { label: "Editor retainer", value: "$500 - 3,000", unit: "/ mo" },
        { label: "Per freelance clip", value: "$50 - 500", unit: "each" },
        { label: "To start on Quip", value: "$0", unit: "no card", accent: true },
      ],
    },
    audience: {
      eyebrow: "WHO IT'S FOR",
      heading: "For the people who cut for a living.",
      sub: "Finding the moments, the vertical reframe, the first pass of captions: that is the slow eighty percent of the job. Quip does that part and hands back the reason each clip works, so you take on more work instead of drowning in it.",
      grindLabel: "The grind today",
      gainLabel: "With Quip",
      privacyLabel: "Privacy",
      roles: [
        {
          id: "editor",
          who: "Video editors",
          role: "Take on more clients",
          grind: "Hours scrubbing a two-hour podcast for the moments worth cutting, before the real edit even starts.",
          gain: "Quip finds and reframes them first. You skip the search, keep the craft, and fit more clients into the same week.",
        },
        {
          id: "studio",
          who: "Studios and agencies",
          role: "Approve faster, revise less",
          grind: "Every clip turns into a debate: the client asks why this moment, and you defend it from a gut feeling.",
          gain: "Each clip ships with a hook, a confidence score, and a plain reason. Forward that, and the client signs off on the first pass.",
        },
        {
          id: "creator",
          who: "Creators and podcasters",
          role: "Post daily, solo",
          grind: "You record long, then the clips never happen because cutting them yourself eats the whole day.",
          gain: "Drop the episode, get a handful of clips you can stand behind, and post every day without hiring an editor.",
        },
      ],
      trust: "The AI that picks and explains your clips reads only the transcript text, never your video itself. Your footage stays yours.",
    },
    howItWorks: {
      eyebrow: "// HOW IT WORKS",
      heading: "Long video in. Clips you can trust out.",
      sub: "Three steps, a couple of minutes.",
      steps: [
        {
          n: "01",
          title: "Upload your video",
          body: "Drop your video: podcast, interview, stream, or lecture. Up to 90 minutes.",
        },
        {
          n: "02",
          title: "Quip finds and explains",
          body: "It transcribes, picks the strongest moments, writes a hook, scores confidence, and reports why each one works.",
        },
        {
          n: "03",
          title: "Polish and post",
          body: "Smooth 9:16 reframe that follows the speaker, captions with punch, your style. Export and post with intent.",
        },
      ],
    },
    why: {
      eyebrow: "Why Quip",
      heading: "Every clip comes with its reasons.",
      sub: "Most AI clippers hand you thirty clips and a shrug. Quip hands you fewer, each with a hook, a confidence score, the moment type, and a plain reason it'll land. You post with intent, not hope, and when a client asks why this clip, the answer is already written.",
      payload: [
        { id: "hook", label: "Hook", body: "A scroll-stopping top line, written for that exact moment." },
        { id: "whyItWorks", label: "Why it works", body: "The real reason: open loop, payoff, tension, in one sentence." },
        { id: "confidence", label: "Confidence", body: "An honest 0 to 100 score, so you know which clip to post first." },
        { id: "momentType", label: "Moment type", body: "Hook, emotional peak, complete thought, or strong quote." },
      ],
    },
    momentMap: {
      heading: "One video. The whole map of moments.",
      sub: "Keep scrolling: Quip unfolds an hour of conversation into a strip of strong moments, each with a timecode and a score.",
      cards: [
        { tc: "04:12", type: "hook", title: "The two seconds that decide everything", confidence: 91, poster: "/clips/frame-1.jpg" },
        { tc: "11:03", type: "quote", title: "He didn't half-ass it", confidence: 94, poster: "/clips/clip-1.webp" },
        { tc: "17:48", type: "peak", title: "Where the voice cracks", confidence: 88, poster: "/clips/clip-3.webp" },
        { tc: "26:30", type: "thought", title: "A complete argument in 40 seconds", confidence: 86, poster: "/clips/poster-1.jpg" },
        { tc: "38:15", type: "hook", title: "His dad's surprising reaction", confidence: 85, poster: "/clips/clip-4.webp" },
        { tc: "49:52", type: "quote", title: "Discipline beats talent's excuses", confidence: 82, poster: "/clips/poster-2.jpg" },
      ],
      more: { count: "+12", note: "more moments down the map. Open any of them and pull the clip in one click." },
    },
    manifesto: {
      words: [
        { t: "Stop" },
        { t: "guessing" },
        { t: "what" },
        { t: "to" },
        { t: "post." },
        { t: "The" },
        { t: "reason", gold: true },
        { t: "beats" },
        { t: "luck." },
      ],
    },
    studio: {
      heading: "Inside: the dark studio",
      sub: "The marketing is light; the workspace is dark, like an edit bay. Both themes switch in one click, everywhere on the site.",
      url: "quip.ink/dashboard",
      createTitle: "Create clips",
      createSub: "Paste a link or upload a video. Quip finds the moments and explains each one.",
      placeholder: "Paste a YouTube link",
      cut: "Make clips",
      drop: "Drag a video here, or",
      dropStrong: "choose a file",
      month: "This month",
      planTag: "FREE",
      videos: "Videos",
      minutes: "Source minutes",
      upgrade: "Get more limits",
    },
    craft: {
      eyebrow: "// THE CRAFT",
      heading: "The cut is the easy part. We sweat the rest.",
      features: [
        {
          id: "speaker",
          title: "Follows the speaker",
          body: "Active-speaker detection centers the right face, even in a multi-cam interview.",
        },
        {
          id: "noFlash",
          title: "No flash frames",
          body: "Frame-accurate scene detection. The crop only moves on a real cut, never mid-shot.",
        },
        {
          id: "captions",
          title: "Captions that pop",
          body: "The active word flares as it's spoken, burned in pixel for pixel. The preview is the export.",
        },
        {
          id: "ratios",
          title: "Your hook, four ratios",
          body: "A branded hook plate on top, and 9:16, 1:1, 4:5, or 16:9 for every platform.",
        },
      ],
    },
    comparison: {
      eyebrow: "// THE WEDGE",
      heading: "More clips was never the problem.",
      sub: "Knowing which moment is worth your audience's attention is.",
      cols: { quip: "Quip", them: "Volume clippers" },
      rows: [
        { k: "Clips per video", quip: "A few, ranked by confidence", them: "30+ to scroll through" },
        { k: "Why post each one", quip: "Hook, score, reason and type", them: "No idea, you guess" },
        { k: "Vertical reframe", quip: "Follows the speaker, zero flash frames", them: "Jumpy, often off-center" },
        { k: "Captions", quip: "Active word pops, your style", them: "Generic, one look" },
        { k: "Pricing", quip: "One credit = one video, shown up front", them: "Credit casino, surprise paywalls" },
        { k: "The result", quip: "Post with intent", them: "Post and hope" },
      ],
      kept: { label: "Made the cut", score: 94 },
      oneVideoNote: "One video gave 23 candidates. This is the one worth posting first.",
      scoredTooLow: "Scored too low to ship",
      rejected: [
        { label: "Throat-clear intro", score: 38 },
        { label: "Tangent, no payoff", score: 44 },
        { label: "Talked over", score: 29 },
        { label: "Mid-sentence cut", score: 51 },
        { label: "Repeats the last point", score: 47 },
      ],
    },
    pricing: {
      eyebrow: "Pricing",
      heading: "Simple plans. No credit casino.",
      sub: "One credit is one video, up to 60 minutes. No tokens to ration, no surprise paywalls. Your limit is shown up front, and pay-as-you-go credits never expire.",
      recommended: "Recommended",
      plans: [
        {
          id: "free",
          name: "Free",
          price: "$0",
          cadence: "forever",
          blurb: "See it work, no card required.",
          allowance: "1 video / mo · 60 min",
          features: [
            "Explainable clips with hook, score and reason",
            "Smooth 9:16 reframe that tracks the speaker",
            "Full editor and every caption style",
            "1080p export with a Quip watermark",
          ],
          cta: "Start free",
          href: ROUTES.signup,
        },
        {
          id: "starter",
          name: "Starter",
          price: "$15",
          cadence: "/ mo",
          blurb: "For creators shipping weekly.",
          allowance: "10 videos / mo · 600 min",
          features: [
            "Everything in Free",
            "No watermark, full 1080p",
            "All aspect ratios: 9:16, 1:1, 4:5, 16:9",
            "Top up anytime with pay-as-you-go",
          ],
          cta: "Choose Starter",
          href: ROUTES.signup,
        },
        {
          id: "pro",
          name: "Pro",
          price: "$35",
          cadence: "/ mo",
          blurb: "For heavy repurposing.",
          allowance: "30 videos / mo · 1,800 min",
          features: [
            "Everything in Starter",
            "Priority processing",
            "Early access to new features",
            "Pay-as-you-go when you need more",
          ],
          cta: "Choose Pro",
          href: ROUTES.signup,
          recommended: true,
        },
        {
          id: "studio",
          name: "Studio",
          price: "$49",
          cadence: "/ mo",
          blurb: "For studios and agencies shipping daily.",
          allowance: "42 videos / mo · 2,520 min",
          features: [
            "Everything in Pro",
            "42 videos a month for high-volume teams",
            "Priority processing",
            "Pay-as-you-go when you need more",
          ],
          cta: "Choose Studio",
          href: ROUTES.signup,
        },
      ],
      payg: {
        name: "Pay-as-you-go",
        price: "$3",
        unit: "/ video",
        note: "No subscription. One video, never expires. Top up anytime.",
      },
      footnote: "Prices in USD. Cancel anytime. Monthly credits reset. Pay-as-you-go never expires.",
    },
    faq: {
      eyebrow: "// FAQ",
      heading: "Questions, answered straight.",
      items: [
        {
          q: "How is Quip different from OpusClip or Vizard?",
          a: "Most clippers optimize for volume: thirty clips you have to sift through. Quip optimizes for confidence: fewer clips, each with a hook, a confidence score, the moment type, and a plain reason it works. Add reframe with no flash frames and pricing without credit games, and you post with intent instead of hope.",
        },
        {
          q: "What kind of videos work best?",
          a: "Talking-heavy long-form: podcasts, interviews, streams, lectures, and webinars, up to 90 minutes. The more speech, the better the moments.",
        },
        {
          q: "Which languages does it support?",
          a: "Language is auto-detected. English and Russian work today, with more on the way.",
        },
        {
          q: "Do I keep my clips?",
          a: "Yes. Download them and they're yours. Paid plans export without a watermark.",
        },
        {
          q: "What happens to my video?",
          a: "The AI that picks moments and writes hooks reads only the transcript text, never the video itself. Your footage is processed to cut and reframe the clips, then stored in your account for you to download. Nothing is posted anywhere on your behalf.",
        },
        {
          q: "What is a credit?",
          a: "One credit is one video, up to 60 minutes of source. Longer videos use minutes proportionally (a 90-minute upload counts as 1.5). Your plan includes a set number of video-credits per month, so you always know your limit. Pay-as-you-go credits cost $3 each and never expire.",
        },
        {
          q: "Is the free plan actually free?",
          a: "Yes. 1 video a month, that's 60 minutes of source to use however you like, no card required. Free exports carry a small watermark; paid plans drop it and export at full 1080p.",
        },
      ],
    },
    finalCta: {
      eyebrow: "// START",
      heading: "Stop guessing which clip to post.",
      sub: "Drop in a video and get a handful of clips you can stand behind, each with the reason it works and a score you can trust.",
      primary: "Try Quip free",
      secondary: "See pricing",
      trust: "No card. 1 free video / month. Cancel anytime.",
    },
    footer: {
      tagline:
        "Fewer clips, but you know why to post them. Explainable AI clips from your long videos.",
      cols: [
        {
          title: "Product",
          links: [
            { label: "How it works", href: "#how-it-works" },
            { label: "Why Quip", href: "#why" },
            { label: "Pricing", href: "#pricing" },
            { label: "FAQ", href: "#faq" },
          ],
        },
        {
          title: "Get started",
          links: [
            { label: "Create account", href: ROUTES.signup },
            { label: "Sign in", href: ROUTES.login },
            { label: "Open the app", href: ROUTES.app },
          ],
        },
        {
          title: "Legal",
          links: [
            { label: "Terms", href: ROUTES.terms },
            { label: "Privacy", href: ROUTES.privacy },
          ],
        },
      ],
      support: "Support and refunds: ceo@quip.ink",
      stripe: "© Quip. All rights reserved.",
      honesty: "Honest pricing. No credit casino. No surprise paywalls.",
    },
  },

  ru: {
    openApp: "Открыть приложение",
    typeLabel: {
      hook: "Хук",
      peak: "Пик эмоций",
      thought: "Законченная мысль",
      quote: "Сильная цитата",
    },
    nav: {
      items: [
        { label: "Как это работает", href: "#how-it-works" },
        { label: "Почему Quip", href: "#why" },
        { label: "Тарифы", href: "#pricing" },
        { label: "Вопросы", href: "#faq" },
      ],
      signIn: "Войти",
      dashboard: "Дашборд",
      tryFree: "Попробовать бесплатно",
    },
    hero: {
      eyebrow: "// СТУДИЯ КЛИПОВ",
      headlinePre: "Не просто получайте клипы.",
      headlineMid: "Знайте, ",
      headlineAccent: "почему",
      headlinePost: " их стоит постить.",
      sub: "Загрузите подкаст, интервью или стрим. Quip нарежет те несколько клипов, что стоит выложить, и объяснит, почему каждый сработает — чтобы вы тратили время на монтаж, а не на поиск моментов.",
      primary: "Загрузить видео",
      secondary: "Как это работает",
      trust: "Без карты. 1 видео в месяц бесплатно.",
    },
    clipText: {
      hero: {
        hook: "Неожиданная реакция отца Макконахи на уход из юршколы",
        reason:
          "Создаёт интригу в заголовке и закрывает её настоящим поворотом. Законченная история, которая работает сама по себе.",
        caption: "Неожиданная реакция его отца.",
      },
      second: {
        hook: "Как Мэттью Макконахи заслужил поддержку отца",
        reason:
          "Законченная история с чистой развязкой: он добился своего, выложившись по полной, а не выпрашивая разрешения. Работает сама по себе.",
        caption: "Он не делал вполсилы.",
      },
      third: {
        hook: "Две секунды, которые решают всё",
        reason:
          "Холодное начало, на котором останавливают скролл. Заявляет ставки прежде, чем зритель успеет отвернуться.",
        caption: "Хук",
      },
      fourth: {
        hook: "Момент, где всё чуть не развалилось",
        reason:
          "Настоящее напряжение в голосе. Сильно по эмоциям, слабее по самостоятельной развязке.",
        caption: "Пик эмоций",
      },
    },
    demo: {
      eyebrow: "Реальный прогон, от и до",
      heading: "Вот как он режет — и почему именно так.",
      sub: "На входе одно длинное видео. Quip находит моменты, режет чистые вертикальные клипы и объясняет, почему каждый сработает: хук, оценка уверенности и разбор нарезки.",
      stages: ["Загрузка", "Поиск моментов", "Нарезка вертикали"],
    },
    costAnchor: {
      eyebrow: "// МАТЕМАТИКА",
      cells: [
        { label: "Монтажёр на абонементе", value: "$500 - 3,000", unit: "/ мес" },
        { label: "Клип у фрилансера", value: "$50 - 500", unit: "за клип" },
        { label: "Старт на Quip", value: "$0", unit: "без карты", accent: true },
      ],
    },
    audience: {
      eyebrow: "// ДЛЯ КОГО",
      heading: "Для тех, кто монтирует на жизнь.",
      sub: "Поиск моментов, вертикальный рефрейм, черновые субтитры — это медленные восемьдесят процентов работы. Quip берёт эту часть на себя и возвращает причину, почему каждый клип сработает, чтобы вы брали больше заказов, а не тонули в рутине.",
      grindLabel: "Рутина сейчас",
      gainLabel: "С Quip",
      privacyLabel: "Приватность",
      roles: [
        {
          id: "editor",
          who: "Монтажёры",
          role: "Берите больше клиентов",
          grind: "Часы уходят на то, чтобы вручную пересмотреть двухчасовой подкаст и найти моменты для нарезки, ещё до начала настоящего монтажа.",
          gain: "Quip находит и кадрирует их первым. Вы пропускаете поиск, оставляете себе ремесло и вмещаете больше клиентов в ту же неделю.",
        },
        {
          id: "studio",
          who: "Студии и агентства",
          role: "Аппрув быстрее, правок меньше",
          grind: "Каждый клип превращается в спор: клиент спрашивает, почему именно этот момент, а вы защищаете его на ощущениях.",
          gain: "Каждый клип идёт с хуком, оценкой уверенности и понятной причиной. Перешлите это клиенту, и он согласует с первого раза.",
        },
        {
          id: "creator",
          who: "Креаторы и подкастеры",
          role: "Постите каждый день в одиночку",
          grind: "Вы пишете вдолгую, а клипы так и не выходят, потому что нарезать их самому — это весь день.",
          gain: "Загрузите выпуск, получите несколько клипов, за которые не стыдно, и постите каждый день без найма монтажёра.",
        },
      ],
      trust: "ИИ, который отбирает и объясняет клипы, читает только текст расшифровки, а не само видео. Отснятый материал остаётся вашим.",
    },
    howItWorks: {
      eyebrow: "// КАК ЭТО РАБОТАЕТ",
      heading: "На входе — длинное видео. На выходе — клипы, которым можно доверять.",
      sub: "Три шага, пара минут.",
      steps: [
        {
          n: "01",
          title: "Загрузите видео",
          body: "Перетащите видео: подкаст, интервью, стрим или лекцию. До 90 минут.",
        },
        {
          n: "02",
          title: "Quip находит и объясняет",
          body: "Транскрибирует, выбирает сильнейшие моменты, пишет хук, ставит оценку уверенности и объясняет, почему каждый сработает.",
        },
        {
          n: "03",
          title: "Доработайте и опубликуйте",
          body: "Плавный рефрейм 9:16 за спикером, сочные субтитры, ваш стиль. Экспорт — и в ленту осознанно.",
        },
      ],
    },
    why: {
      eyebrow: "Почему Quip",
      heading: "Каждый клип идёт со своими причинами.",
      sub: "Большинство ИИ-нарезчиков выдают тридцать клипов и пожимают плечами. Quip даёт меньше — но каждый с хуком, оценкой уверенности, типом момента и понятной причиной, почему он сработает. Вы постите осознанно, а не на удачу, а когда клиент спросит «почему этот клип», ответ уже написан.",
      payload: [
        { id: "hook", label: "Хук", body: "Цепляющая верхняя строка, написанная под этот конкретный момент." },
        { id: "whyItWorks", label: "Почему работает", body: "Настоящая причина: интрига, развязка, напряжение — в одном предложении." },
        { id: "confidence", label: "Уверенность", body: "Честная оценка от 0 до 100, чтобы понять, какой клип постить первым." },
        { id: "momentType", label: "Тип момента", body: "Хук, эмоциональный пик, законченная мысль или сильная цитата." },
      ],
    },
    momentMap: {
      heading: "Одно видео. Вся карта моментов.",
      sub: "Листайте дальше: Quip разворачивает час разговора в ленту сильных моментов — у каждого таймкод и оценка.",
      cards: [
        { tc: "04:12", type: "hook", title: "Две секунды, которые решают всё", confidence: 91, poster: "/clips/frame-1.jpg" },
        { tc: "11:03", type: "quote", title: "Он не делал вполсилы", confidence: 94, poster: "/clips/clip-1.webp" },
        { tc: "17:48", type: "peak", title: "Момент, где ломается голос", confidence: 88, poster: "/clips/clip-3.webp" },
        { tc: "26:30", type: "thought", title: "Законченный аргумент за 40 секунд", confidence: 86, poster: "/clips/poster-1.jpg" },
        { tc: "38:15", type: "hook", title: "Неожиданная реакция его отца", confidence: 85, poster: "/clips/clip-4.webp" },
        { tc: "49:52", type: "quote", title: "Дисциплина сильнее оправданий", confidence: 82, poster: "/clips/poster-2.jpg" },
      ],
      more: { count: "+12", note: "моментов дальше по карте. Откройте любой и вытащите клип в один клик." },
    },
    manifesto: {
      words: [
        { t: "Хватит" },
        { t: "гадать," },
        { t: "что" },
        { t: "постить." },
        { t: "Причина", gold: true },
        { t: "сильнее" },
        { t: "удачи." },
      ],
    },
    studio: {
      heading: "Внутри: тёмная студия",
      sub: "Маркетинг светлый, рабочее пространство тёмное, как монтажка. Обе темы переключаются в один клик — на всём сайте.",
      url: "quip.ink/dashboard",
      createTitle: "Создать клипы",
      createSub: "Вставьте ссылку или загрузите видео. Quip найдёт моменты и объяснит каждый.",
      placeholder: "Вставьте ссылку на YouTube",
      cut: "Нарезать",
      drop: "Перетащите видео сюда или",
      dropStrong: "выберите файл",
      month: "Этот месяц",
      planTag: "FREE",
      videos: "Видео",
      minutes: "Минуты исходника",
      upgrade: "Расширить лимиты",
    },
    craft: {
      eyebrow: "// РЕМЕСЛО",
      heading: "Нарезка — это просто. Мы потеем над остальным.",
      features: [
        {
          id: "speaker",
          title: "Следует за спикером",
          body: "Детекция активного спикера держит в центре нужное лицо — даже в мультикам-интервью.",
        },
        {
          id: "noFlash",
          title: "Без кадров-вспышек",
          body: "Покадрово точная детекция склеек. Кадр сдвигается только на реальной склейке, никогда посреди шота.",
        },
        {
          id: "captions",
          title: "Субтитры, которые цепляют",
          body: "Активное слово вспыхивает в такт речи, вшито пиксель в пиксель. Превью = экспорт.",
        },
        {
          id: "ratios",
          title: "Ваш хук, четыре формата",
          body: "Брендовая плашка хука сверху и 9:16, 1:1, 4:5 или 16:9 под любую площадку.",
        },
      ],
    },
    comparison: {
      eyebrow: "// СУТЬ",
      heading: "Проблема никогда не была в количестве клипов.",
      sub: "Проблема — понять, какой момент достоин внимания вашей аудитории.",
      cols: { quip: "Quip", them: "Массовые нарезчики" },
      rows: [
        { k: "Клипов на видео", quip: "Несколько, по оценке уверенности", them: "30+ на пролистывание" },
        { k: "Почему постить каждый", quip: "Хук, оценка, причина и тип", them: "Непонятно, гадаете" },
        { k: "Вертикальный рефрейм", quip: "Следует за спикером, без вспышек", them: "Дёрганый, часто не по центру" },
        { k: "Субтитры", quip: "Активное слово вспыхивает, ваш стиль", them: "Шаблонные, один вид" },
        { k: "Цена", quip: "Кредит = одно видео, видно сразу", them: "Токены-казино, скрытые пейволлы" },
        { k: "Итог", quip: "Постите осознанно", them: "Постите и надеетесь" },
      ],
      kept: { label: "Прошёл отбор", score: 94 },
      oneVideoNote: "Одно видео дало 23 кандидата. Вот тот, что стоит выложить первым.",
      scoredTooLow: "Слишком низкая оценка для публикации",
      rejected: [
        { label: "Прокашливание на старте", score: 38 },
        { label: "Уход в сторону без развязки", score: 44 },
        { label: "Перебили", score: 29 },
        { label: "Обрыв на полуслове", score: 51 },
        { label: "Повтор прошлой мысли", score: 47 },
      ],
    },
    pricing: {
      eyebrow: "Тарифы",
      heading: "Простые планы. Без токенов-казино.",
      sub: "Кредит — это одно видео, до 60 минут. Никаких токенов на распределение, никаких скрытых пейволлов. Лимит виден сразу, а кредиты pay-as-you-go не сгорают.",
      recommended: "Рекомендуем",
      plans: [
        {
          id: "free",
          name: "Free",
          price: "$0",
          cadence: "навсегда",
          blurb: "Посмотрите, как работает. Без карты.",
          allowance: "1 видео / мес · 60 мин",
          features: [
            "Объяснимые клипы: хук, оценка и причина",
            "Плавный рефрейм 9:16 за спикером",
            "Полный редактор и все стили субтитров",
            "Экспорт 1080p с вотермаркой Quip",
          ],
          cta: "Начать бесплатно",
          href: ROUTES.signup,
        },
        {
          id: "starter",
          name: "Starter",
          price: "$15",
          cadence: "/ мес",
          blurb: "Для авторов, что постят каждую неделю.",
          allowance: "10 видео / мес · 600 мин",
          features: [
            "Всё из Free",
            "Без вотермарки, полное 1080p",
            "Все форматы: 9:16, 1:1, 4:5, 16:9",
            "Пополнение в любой момент через pay-as-you-go",
          ],
          cta: "Выбрать Starter",
          href: ROUTES.signup,
        },
        {
          id: "pro",
          name: "Pro",
          price: "$35",
          cadence: "/ мес",
          blurb: "Для тех, кто переупаковывает много.",
          allowance: "30 видео / мес · 1 800 мин",
          features: [
            "Всё из Starter",
            "Приоритетная обработка",
            "Ранний доступ к новым фичам",
            "Pay-as-you-go, когда нужно больше",
          ],
          cta: "Выбрать Pro",
          href: ROUTES.signup,
          recommended: true,
        },
        {
          id: "studio",
          name: "Studio",
          price: "$49",
          cadence: "/ мес",
          blurb: "Для студий и агентств, что постят каждый день.",
          allowance: "42 видео / мес · 2 520 мин",
          features: [
            "Всё из Pro",
            "42 видео в месяц для команд с большим объёмом",
            "Приоритетная обработка",
            "Pay-as-you-go, когда нужно больше",
          ],
          cta: "Выбрать Studio",
          href: ROUTES.signup,
        },
      ],
      payg: {
        name: "Pay-as-you-go",
        price: "$3",
        unit: "/ видео",
        note: "Без подписки. Одно видео, не сгорает. Пополняйте в любой момент.",
      },
      footnote: "Цены в USD. Отмена в любой момент. Месячные кредиты обновляются. Pay-as-you-go не сгорает.",
    },
    faq: {
      eyebrow: "// ВОПРОСЫ",
      heading: "Вопросы — и прямые ответы.",
      items: [
        {
          q: "Чем Quip отличается от OpusClip или Vizard?",
          a: "Большинство нарезчиков гонятся за количеством: тридцать клипов на разбор. Quip — за уверенностью: меньше клипов, но у каждого хук, оценка уверенности, тип момента и понятная причина, почему он работает. Добавьте рефрейм без вспышек и цену без игр с кредитами — и вы постите осознанно, а не на удачу.",
        },
        {
          q: "Какие видео подходят лучше всего?",
          a: "Длинные говорящие форматы: подкасты, интервью, стримы, лекции и вебинары — до 90 минут. Чем больше речи, тем сильнее моменты.",
        },
        {
          q: "Какие языки поддерживаются?",
          a: "Язык определяется автоматически. Английский и русский работают уже сейчас, остальные на подходе.",
        },
        {
          q: "Клипы остаются моими?",
          a: "Да. Скачали — они ваши. На платных планах экспорт без вотермарки.",
        },
        {
          q: "Что происходит с моим видео?",
          a: "ИИ, который выбирает моменты и пишет хуки, читает только текст расшифровки, а не само видео. Материал обрабатывается, чтобы нарезать и кадрировать клипы, затем хранится в вашем аккаунте для скачивания. Ничего никуда не публикуется от вашего имени.",
        },
        {
          q: "Что такое кредит?",
          a: "Кредит — это одно видео, до 60 минут исходника. Видео длиннее тратят минуты пропорционально (90-минутная загрузка считается за 1,5). В плане — фиксированное число видео-кредитов в месяц, так что лимит всегда понятен. Кредиты pay-as-you-go стоят $3 за штуку и не сгорают.",
        },
        {
          q: "Бесплатный план правда бесплатный?",
          a: "Да. 1 видео в месяц — это 60 минут исходника, как угодно, без карты. На бесплатном экспорт идёт с небольшой вотермаркой; платные её убирают и экспортируют в полном 1080p.",
        },
      ],
    },
    finalCta: {
      eyebrow: "// СТАРТ",
      heading: "Хватит гадать, какой клип постить.",
      sub: "Загрузите видео и получите несколько клипов, за которые не стыдно — каждый с причиной, почему он работает, и с оценкой, которой можно верить.",
      primary: "Попробовать Quip бесплатно",
      secondary: "Смотреть тарифы",
      trust: "Без карты. 1 видео в месяц бесплатно. Отмена в любой момент.",
    },
    footer: {
      tagline:
        "Меньше клипов — но вы знаете, почему их постить. Объяснимые ИИ-клипы из ваших длинных видео.",
      cols: [
        {
          title: "Продукт",
          links: [
            { label: "Как это работает", href: "#how-it-works" },
            { label: "Почему Quip", href: "#why" },
            { label: "Тарифы", href: "#pricing" },
            { label: "Вопросы", href: "#faq" },
          ],
        },
        {
          title: "Начать",
          links: [
            { label: "Создать аккаунт", href: ROUTES.signup },
            { label: "Войти", href: ROUTES.login },
            { label: "Открыть приложение", href: ROUTES.app },
          ],
        },
        {
          title: "Правовое",
          links: [
            { label: "Условия", href: ROUTES.terms },
            { label: "Конфиденциальность", href: ROUTES.privacy },
          ],
        },
      ],
      support: "Поддержка и возвраты: ceo@quip.ink",
      stripe: "© Quip. Все права защищены.",
      honesty: "Честные цены. Без токенов-казино. Без скрытых пейволлов.",
    },
  },
};

/** Merge locale-invariant media with the per-locale text into one runtime Clip. */
function buildClip(key: ClipKey, s: LandingStrings): Clip {
  const media = CLIP_MEDIA[key];
  return {
    ...media,
    ...s.clipText[key],
    typeLabel: s.typeLabel[media.type],
  };
}

/** The landing copy + data for a locale. The single entry point for every section. */
export function getLandingContent(locale: Locale): LandingContent {
  const s = STRINGS[locale];
  const heroClip = buildClip("hero", s);
  const clips: Clip[] = [
    heroClip,
    buildClip("second", s),
    buildClip("third", s),
    buildClip("fourth", s),
  ];
  return {
    openApp: s.openApp,
    typeLabel: s.typeLabel,
    nav: s.nav,
    hero: s.hero,
    heroClip,
    clips,
    demo: s.demo,
    costAnchor: s.costAnchor,
    audience: s.audience,
    howItWorks: s.howItWorks,
    why: s.why,
    momentMap: s.momentMap,
    manifesto: s.manifesto,
    studio: s.studio,
    craft: s.craft,
    comparison: s.comparison,
    pricing: s.pricing,
    faq: s.faq,
    finalCta: s.finalCta,
    footer: s.footer,
  };
}
