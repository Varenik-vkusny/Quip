import type { ClipOut, VideoMap } from "@/lib/types";

// Hand-authored Cardi B interview fixture for the /dev/video-map cockpit harness (founder's
// no-reprocess preview). NOT machine-captured — literals shaped to exercise every cockpit path:
// ~6 chapters covering 0→5163s, 18 moments across varied `kind`s, and 10 clips whose [start,end]
// OVERLAP exactly 10 of those moments — so classifyMoment() marks 10 "clipped" (● Open clip) and 8
// "untapped" (○ Extract). The narrative carries a [[clip:clip_03]] token plus linkable/plain
// [mm:ss] timecodes. `previewUrl` points at the bundled sample in public/ (content need not match —
// this is a layout/interaction demo, not a real render).

const JOB_ID = "cardi-b-interview-demo";

/** Small helper so every clip fills the required ClipOut fields without repeating the boilerplate. */
function clip(
  id: string,
  start: number,
  end: number,
  type: ClipOut["type"],
  score: number,
  energy: number,
  hook: string,
  keyQuote: string,
  whyWorks: string,
  reason: string,
): ClipOut {
  return {
    id,
    start,
    end,
    duration: Math.round((end - start) * 10) / 10,
    type,
    score,
    energy,
    hook,
    key_quote: keyQuote,
    why_works: whyWorks,
    reason,
    tone: "candid",
    transcript: whyWorks,
    video_url: "/media/demo.mp4",
    words: [],
  };
}

const CLIPS: ClipOut[] = [
  clip(
    "clip_01",
    34,
    70,
    "hook",
    0.92,
    0.81,
    "She flips the ambush question in one line",
    "You thought you had me — you don't.",
    "Cold-open confrontation with an instant reversal — the strongest possible first three seconds.",
    "High-tension opener; the reversal is a natural hook.",
  ),
  clip(
    "clip_02",
    210,
    248,
    "strong_quote",
    0.86,
    0.64,
    "The line that became a headline",
    "I say what everybody's thinking and nobody's saying.",
    "A self-contained, quotable thesis statement that captions itself.",
    "Clean quotable moment, standalone meaning.",
  ),
  clip(
    "clip_03",
    540,
    580,
    "hook",
    0.94,
    0.88,
    "The moment everything changed",
    "One video and my whole life was different by morning.",
    "The origin-story beat — the exact turning point viewers came for; ideal lead clip.",
    "Narrative peak; going-viral turning point.",
  ),
  clip(
    "clip_04",
    940,
    986,
    "emotional_peak",
    0.83,
    0.72,
    "Where she almost gave up",
    "I was one 'no' away from quitting for good.",
    "Vulnerable admission with a clear before/after — earns the save.",
    "Emotional low that resolves; strong retention.",
  ),
  clip(
    "clip_05",
    1360,
    1402,
    "complete_thought",
    0.79,
    0.6,
    "Her whole theory of hustle in 40 seconds",
    "You don't wait for the door — you are the door.",
    "A tight, complete argument that stands alone without setup.",
    "Self-contained thesis on hustle.",
  ),
  clip(
    "clip_06",
    1900,
    1940,
    "complete_thought",
    0.8,
    0.66,
    "Why she reads every contract herself",
    "Nobody protects your name harder than you do.",
    "Practical, screenshot-able advice with a memorable button line.",
    "Actionable, quotable advice beat.",
  ),
  clip(
    "clip_07",
    2280,
    2322,
    "strong_quote",
    0.85,
    0.7,
    "The tax rant everyone clipped",
    "I pay the government a whole Lambo and I can't even see it.",
    "Funny + relatable outrage — the comment-section engine.",
    "Relatable outrage; high shareability.",
  ),
  clip(
    "clip_08",
    3020,
    3064,
    "strong_quote",
    0.87,
    0.75,
    "Where the money actually goes",
    "Show me the roads. Show me ONE road.",
    "Escalating, rhythmic delivery that peaks on a punchline.",
    "Punchy escalation with a hard button.",
  ),
  clip(
    "clip_09",
    3520,
    3560,
    "complete_thought",
    0.78,
    0.58,
    "The rule she gives every new artist",
    "Own your masters or you own nothing.",
    "One-sentence mentorship line that travels on its own.",
    "Advice line, standalone value.",
  ),
  clip(
    "clip_10",
    4100,
    4144,
    "emotional_peak",
    0.9,
    0.83,
    "Motherhood changed the whole game",
    "The day she was born, I stopped being scared of everything.",
    "The emotional centre of the interview — the beat people re-share.",
    "Peak emotional payoff; the reel's heart.",
  ),
];

const MAP: VideoMap = {
  status: "done",
  narrative:
    "Cardi B sits down for a wide-ranging interview, and the whole thing plays like a highlight " +
    "reel. The cold open is an ambush question she flips on its head [0:40], setting the tone for " +
    "the hour. She rewinds to the Bronx, then hits the moment everything changed — going viral — " +
    "which is exactly the clip you'd want to lead with [[clip:clip_03]]. From there it gets sharp: " +
    "an unfiltered riff on taxes and where the money actually goes [50:20] is instantly quotable, " +
    "and the softer turn into motherhood [68:25] gives the reel its emotional beat. There's " +
    "untapped gold too — the [13:20] tangent never became a clip, but it easily could.",
  chapters: [
    {
      title: "Cold open: the ambush question",
      start: 0,
      end: 720,
      summary:
        "The interviewer opens with a loaded question and Cardi turns it around on the spot, then " +
        "settles into her origin story.",
      clip_ids: ["clip_01", "clip_02", "clip_03"],
      moments: [
        {
          kind: "tension",
          label: "The ambush question, flipped",
          why: "She reverses a hostile opener in a single line — instant confrontation and payoff.",
          start: 40,
          end: 58,
        },
        {
          kind: "quote",
          label: "“I say what everybody's thinking”",
          why: "A self-contained thesis statement that captions itself.",
          start: 215,
          end: 232,
        },
        {
          kind: "funny",
          label: "The green-room story nobody expected",
          why: "A quick absurd aside that lands as a laugh but wasn't cut into a clip.",
          start: 400,
          end: 418,
        },
      ],
    },
    {
      title: "Coming up in the Bronx",
      start: 720,
      end: 1680,
      summary:
        "She walks through the early years — the jobs, the doubt, and the exact night the internet " +
        "found her.",
      clip_ids: ["clip_03", "clip_04", "clip_05"],
      moments: [
        {
          kind: "insight",
          label: "The lesson from her worst job",
          why: "A crisp takeaway about self-worth — untapped, but a strong standalone.",
          start: 800,
          end: 818,
        },
        {
          kind: "emotional",
          label: "One 'no' away from quitting",
          why: "Vulnerable low point with a clear turn — earns retention.",
          start: 945,
          end: 978,
        },
        {
          kind: "tension",
          label: "“You are the door”",
          why: "A tight, complete argument on hustle delivered at full intensity.",
          start: 1365,
          end: 1398,
        },
      ],
    },
    {
      title: "The break: going viral",
      start: 1680,
      end: 2760,
      summary:
        "The turning point — how one clip changed everything, and the advice she wishes she'd had.",
      clip_ids: ["clip_06", "clip_07"],
      moments: [
        {
          kind: "quote",
          label: "“Protect your name harder than anyone”",
          why: "Screenshot-able advice line — never clipped, easy win.",
          start: 1750,
          end: 1768,
        },
        {
          kind: "insight",
          label: "Why she reads every contract",
          why: "Practical, memorable advice with a clean button line.",
          start: 1905,
          end: 1935,
        },
        {
          kind: "funny",
          label: "The tax rant everyone clipped",
          why: "Relatable outrage delivered as comedy — the comment engine.",
          start: 2285,
          end: 2318,
        },
      ],
    },
    {
      title: "Money, taxes, and the government",
      start: 2760,
      end: 3840,
      summary:
        "The most-shared stretch of the interview — where the money goes, and who's really keeping " +
        "the receipts.",
      clip_ids: ["clip_08", "clip_09"],
      moments: [
        {
          kind: "emotional",
          label: "The first cheque she framed",
          why: "A warm memory with a visual — untapped, would make a soft-open clip.",
          start: 2860,
          end: 2878,
        },
        {
          kind: "quote",
          label: "“Show me ONE road”",
          why: "Rhythmic escalation peaking on a hard punchline.",
          start: 3025,
          end: 3060,
        },
        {
          kind: "tension",
          label: "“Own your masters or own nothing”",
          why: "A confrontational one-liner of mentorship — travels on its own.",
          start: 3525,
          end: 3555,
        },
      ],
    },
    {
      title: "Motherhood vs. the machine",
      start: 3840,
      end: 4620,
      summary:
        "The interview softens — balancing an empire with being a mom, and the day her priorities " +
        "flipped.",
      clip_ids: ["clip_10"],
      moments: [
        {
          kind: "insight",
          label: "How she guards her calendar now",
          why: "A concrete routine tip — untapped, practical standalone.",
          start: 3900,
          end: 3918,
        },
        {
          kind: "emotional",
          label: "“I stopped being scared of everything”",
          why: "The emotional centre of the whole interview — the beat people re-share.",
          start: 4105,
          end: 4140,
        },
        {
          kind: "funny",
          label: "The toddler-in-the-studio story",
          why: "A light, human aside — never clipped but very shareable.",
          start: 4400,
          end: 4420,
        },
      ],
    },
    {
      title: "What she'd tell her younger self",
      start: 4620,
      end: 5163,
      summary:
        "A reflective close — the advice, the regrets she's made peace with, and what comes next.",
      clip_ids: [],
      moments: [
        {
          kind: "quote",
          label: "“Slow down, it's all coming”",
          why: "A tender closing line — untapped, would make a perfect outro clip.",
          start: 4700,
          end: 4718,
        },
        {
          kind: "insight",
          label: "The one thing she'd do differently",
          why: "A clear, reflective takeaway to end on — never cut.",
          start: 4950,
          end: 4968,
        },
      ],
    },
  ],
};

export const CARDI_B_FIXTURE: {
  jobId: string;
  map: VideoMap;
  clips: ClipOut[];
  previewUrl: string;
} = {
  jobId: JOB_ID,
  map: MAP,
  clips: CLIPS,
  previewUrl: "/media/demo.mp4",
};
