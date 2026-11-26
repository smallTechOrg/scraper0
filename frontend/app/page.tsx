import Link from "next/link";
import { AccessRequestForm } from "@/components/access-request-form";
import { Logo } from "@/components/shell";
import { ButtonLink, CodeBlock } from "@/components/ui";

const steps = [
  {
    n: "1",
    title: "Read once with AI",
    body: "Point scraper0 at a page. An LLM reads it and extracts the fields of your entity — names, dates, addresses, links.",
    visual: "ai",
  },
  {
    n: "2",
    title: "Learn the layout",
    body: "It records where each field lives on that kind of page and stores the layout as a reusable recipe for the domain.",
    visual: "learn",
  },
  {
    n: "3",
    title: "Scrape the rest without AI",
    body: "Every similar page is extracted with the recipe — fast, deterministic and free. AI is only used again when a layout changes.",
    visual: "fast",
  },
] as const;

const features = [
  {
    title: "Any entity, defined as data",
    body: "Describe the fields you want — text, dates, URLs, images, numbers, enums, addresses — and scraper0 extracts them. No code.",
  },
  {
    title: "Events built in",
    body: "A ready-made event entity with date parsing, past/online filtering, de-duplication and address checks.",
  },
  {
    title: "Uploads",
    body: "Drop in posters and images, PDFs, CSV, JSON or ICS files, or paste text. Vision models read the flyers for you.",
  },
  {
    title: "Site connectors",
    body: "Purpose-built connectors for sites that need logins, forms or a browser, behind the same job API.",
  },
  {
    title: "Scheduled sources",
    body: "Save a crawl or connector as a source and run it every 15 minutes or once a week — with history for every run.",
  },
  {
    title: "Change feed",
    body: "Results are stored and de-duplicated. A cursor-based feed returns only items that are new or changed since you last asked.",
  },
  {
    title: "Bring your own AI key",
    body: "Use the shared provider, or plug in your own Groq, Anthropic or OpenAI-compatible key. Every call is metered against a budget.",
  },
  {
    title: "Multi-tenant",
    body: "Separate workspaces with their own members, API keys, entities, sources and spend caps.",
  },
];

const curl = `curl -X POST https://scraper0.example.com/v1/jobs \\
  -H "X-API-Key: s0_your_key" \\
  -H "Content-Type: application/json" \\
  -d '{
    "kind": "crawl",
    "input": {
      "entity": "event",
      "urls": ["https://example.org/whats-on"],
      "options": { "max_detail_pages": 25, "allow_llm": true, "learn": true }
    }
  }'

# → 202 {"data": {"id": "job_…", "status": "queued", …}}
# then poll GET /v1/jobs/{id} or stream GET /v1/jobs/{id}/events`;

function StepVisual({ kind }: { kind: "ai" | "learn" | "fast" }) {
  // Small schematic illustrations; decorative.
  return (
    <svg viewBox="0 0 160 90" className="h-24 w-full" aria-hidden>
      <rect x="1" y="1" width="158" height="88" rx="10" fill="var(--surface-2)" stroke="var(--border)" />
      {kind === "ai" && (
        <>
          <rect x="14" y="16" width="62" height="58" rx="5" fill="var(--surface)" stroke="var(--border-strong)" />
          <rect x="22" y="24" width="40" height="5" rx="2" fill="var(--border-strong)" />
          <rect x="22" y="35" width="46" height="4" rx="2" fill="var(--border)" />
          <rect x="22" y="44" width="34" height="4" rx="2" fill="var(--border)" />
          <rect x="22" y="53" width="42" height="4" rx="2" fill="var(--border)" />
          <path d="M84 45h18" stroke="var(--accent)" strokeWidth="2" strokeDasharray="3 3" />
          <circle cx="124" cy="45" r="20" fill="var(--accent-soft)" stroke="var(--accent)" />
          <text x="124" y="50" textAnchor="middle" fontSize="13" fontWeight="700" fill="var(--accent)">
            AI
          </text>
        </>
      )}
      {kind === "learn" && (
        <>
          <rect x="14" y="16" width="62" height="58" rx="5" fill="var(--surface)" stroke="var(--border-strong)" />
          <rect x="20" y="22" width="50" height="10" rx="2" fill="none" stroke="var(--accent)" strokeDasharray="3 2" />
          <rect x="20" y="38" width="50" height="8" rx="2" fill="none" stroke="var(--accent)" strokeDasharray="3 2" />
          <rect x="20" y="52" width="36" height="8" rx="2" fill="none" stroke="var(--accent)" strokeDasharray="3 2" />
          <path d="M84 45h14" stroke="var(--border-strong)" strokeWidth="2" />
          <rect x="104" y="22" width="44" height="46" rx="5" fill="var(--surface)" stroke="var(--accent)" />
          <rect x="111" y="31" width="30" height="4" rx="2" fill="var(--accent)" />
          <rect x="111" y="41" width="24" height="4" rx="2" fill="var(--accent)" opacity="0.6" />
          <rect x="111" y="51" width="28" height="4" rx="2" fill="var(--accent)" opacity="0.4" />
        </>
      )}
      {kind === "fast" && (
        <>
          {[0, 1, 2, 3].map((i) => (
            <rect
              key={i}
              x={12 + i * 9}
              y={18 + i * 6}
              width="46"
              height="40"
              rx="4"
              fill="var(--surface)"
              stroke="var(--border-strong)"
            />
          ))}
          <path d="M92 45h14" stroke="var(--accent)" strokeWidth="2" />
          <path d="M102 40l6 5-6 5" fill="none" stroke="var(--accent)" strokeWidth="2" />
          {[0, 1, 2, 3].map((i) => (
            <rect key={i} x="114" y={20 + i * 13} width="34" height="8" rx="2" fill="var(--success)" opacity={0.85 - i * 0.15} />
          ))}
        </>
      )}
    </svg>
  );
}

export default function LandingPage() {
  return (
    <div className="min-h-dvh">
      <header className="border-b border-border">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
          <Link href="/" aria-label="scraper0 home">
            <Logo />
          </Link>
          <nav aria-label="Primary" className="flex items-center gap-1 sm:gap-3">
            <a href="#how" className="hidden rounded px-2 py-1 text-sm text-muted hover:text-fg sm:inline">
              How it works
            </a>
            <a href="#features" className="hidden rounded px-2 py-1 text-sm text-muted hover:text-fg sm:inline">
              Features
            </a>
            <a href="#api" className="hidden rounded px-2 py-1 text-sm text-muted hover:text-fg sm:inline">
              API
            </a>
            <ButtonLink href="/login" variant="ghost" size="sm">
              Sign in
            </ButtonLink>
            <a
              href="#access"
              className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-sm font-medium text-accent-fg hover:bg-accent-hover"
            >
              Request access
            </a>
          </nav>
        </div>
      </header>

      <main>
        {/* Hero */}
        <section className="mx-auto max-w-6xl px-4 pb-16 pt-16 sm:px-6 sm:pt-24">
          <div className="max-w-3xl">
            <p className="mb-4 inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted">
              <span className="size-1.5 rounded-full bg-accent" aria-hidden />
              Invite-only beta
            </p>
            <h1 className="text-4xl font-semibold tracking-tight sm:text-5xl md:text-6xl">
              Turn any website, poster or spreadsheet into <span className="text-accent">structured data</span>
            </h1>
            <p className="mt-6 max-w-2xl text-lg text-muted">
              scraper0 reads a page once with AI, learns its layout, and then extracts every similar page without AI.
              Define the entity you need, point it at sources, and get a clean feed of new and changed items over an API.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <a
                href="#access"
                className="inline-flex h-11 items-center rounded-md bg-accent px-5 text-sm font-medium text-accent-fg shadow-sm hover:bg-accent-hover"
              >
                Request access
              </a>
              <a
                href="#how"
                className="inline-flex h-11 items-center rounded-md border border-border-strong bg-surface px-5 text-sm font-medium hover:bg-surface-2"
              >
                See how it works
              </a>
            </div>
          </div>
        </section>

        {/* How it works */}
        <section id="how" className="border-y border-border bg-surface py-16 sm:py-20">
          <div className="mx-auto max-w-6xl px-4 sm:px-6">
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">How it works</h2>
            <p className="mt-2 max-w-2xl text-muted">AI where it&apos;s needed, and only once per layout.</p>
            <ol className="mt-10 grid gap-6 md:grid-cols-3">
              {steps.map((s, i) => (
                <li key={s.n} className="relative rounded-xl border border-border bg-bg p-5">
                  <StepVisual kind={s.visual} />
                  <div className="mt-4 flex items-center gap-3">
                    <span className="flex size-7 items-center justify-center rounded-full bg-accent text-sm font-semibold text-accent-fg">
                      {s.n}
                    </span>
                    <h3 className="font-semibold">{s.title}</h3>
                  </div>
                  <p className="mt-2 text-sm text-muted">{s.body}</p>
                  {i < steps.length - 1 && (
                    <span
                      className="absolute -right-5 top-1/2 hidden -translate-y-1/2 text-2xl text-border-strong md:block"
                      aria-hidden
                    >
                      →
                    </span>
                  )}
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* Features */}
        <section id="features" className="py-16 sm:py-20">
          <div className="mx-auto max-w-6xl px-4 sm:px-6">
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Everything you need to keep data fresh</h2>
            <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {features.map((f) => (
                <div key={f.title} className="rounded-xl border border-border bg-surface p-5">
                  <h3 className="font-semibold">{f.title}</h3>
                  <p className="mt-2 text-sm text-muted">{f.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* API */}
        <section id="api" className="border-y border-border bg-surface py-16 sm:py-20">
          <div className="mx-auto grid max-w-6xl gap-10 px-4 sm:px-6 lg:grid-cols-[1fr_1.4fr] lg:items-center">
            <div>
              <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">One API for every source</h2>
              <p className="mt-3 text-muted">
                Every crawl, upload and connector call is a job. Create one with an API key, stream its progress, then
                read the results — or subscribe to the change feed and only ever see what&apos;s new.
              </p>
              <ul className="mt-5 space-y-2 text-sm text-muted">
                <li>
                  <code className="font-mono text-fg">POST /v1/jobs</code> — crawl URLs or run a connector
                </li>
                <li>
                  <code className="font-mono text-fg">POST /v1/jobs/ingest</code> — upload files or text
                </li>
                <li>
                  <code className="font-mono text-fg">GET /v1/items/changes</code> — new and changed items
                </li>
              </ul>
            </div>
            <CodeBlock code={curl} label="Example: create a crawl job with curl" />
          </div>
        </section>

        {/* Access */}
        <section id="access" className="py-16 sm:py-20">
          <div className="mx-auto grid max-w-6xl gap-10 px-4 sm:px-6 lg:grid-cols-2">
            <div>
              <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Request access</h2>
              <p className="mt-3 text-muted">
                scraper0 is invite-only while we onboard early teams. Tell us a little about what you want to extract and
                we&apos;ll set up a workspace for you.
              </p>
              <p className="mt-4 text-sm text-muted">
                Already have an invite?{" "}
                <Link href="/login" className="font-medium text-accent hover:underline">
                  Sign in
                </Link>
                .
              </p>
            </div>
            <AccessRequestForm />
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-6 text-sm text-subtle sm:px-6">
          <Logo className="text-fg" />
          <p>Structured data from the messy web.</p>
        </div>
      </footer>
    </div>
  );
}
