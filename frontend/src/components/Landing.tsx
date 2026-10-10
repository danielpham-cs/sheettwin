"use client";

import {
  ArrowRight, ChartPie, Check, FlaskConical, FolderOutput, GitBranch, Languages, Layers, Lock, MessageSquareText,
  Network, ShieldCheck, Sparkles, TriangleAlert, Upload, Wand2,
} from "lucide-react";
import { motion } from "motion/react";
import { I18nProvider, useI18n } from "@/lib/i18n";
import { LANDING } from "@/lib/landing-i18n";

const APP = "/app/";
const SEV = { high: "#d03b3b", medium: "#fab219", low: "#898781" } as const;
const SEV_LABEL = { zh: { high: "高", medium: "中", low: "低" }, en: { high: "High", medium: "Medium", low: "Low" } };

const FEATURES = [
  { key: "chat", icon: MessageSquareText, tint: "from-violet-500/30 text-violet-200" },
  { key: "graph", icon: Network, tint: "from-cyan-500/30 text-cyan-200" },
  { key: "charts", icon: ChartPie, tint: "from-fuchsia-500/30 text-fuchsia-200" },
  { key: "exports", icon: FolderOutput, tint: "from-emerald-500/30 text-emerald-200" },
] as const;
const WHY_ICONS = [GitBranch, Wand2, Lock, Layers];
const STEP_ICONS = [Upload, Layers, ShieldCheck, Sparkles];

const fadeUp = {
  initial: { opacity: 0, y: 24 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-80px" },
  transition: { duration: 0.6, ease: [0.2, 0.8, 0.2, 1] as const },
};

export default function Landing() {
  return (
    <I18nProvider>
      <Page />
    </I18nProvider>
  );
}

/** A screenshot in a browser window frame. */
function Shot({ src, alt, className = "" }: { src: string; alt: string; className?: string }) {
  return (
    <div className={`overflow-hidden rounded-2xl border border-white/10 bg-zinc-900 shadow-2xl shadow-black/60 ${className}`}>
      <div className="flex items-center gap-1.5 border-b border-white/5 bg-zinc-900/90 px-4 py-2.5">
        <span className="size-2.5 rounded-full bg-rose-400/70" />
        <span className="size-2.5 rounded-full bg-amber-400/70" />
        <span className="size-2.5 rounded-full bg-emerald-400/70" />
        <span className="ml-3 truncate rounded-md bg-white/5 px-3 py-0.5 font-mono text-[11px] text-zinc-500">127.0.0.1:8765/app</span>
      </div>
      {/* eslint-disable-next-line @next/next/no-img-element -- static export, no image optimizer */}
      <img src={src} alt={alt} className="block w-full" loading="lazy" />
    </div>
  );
}

function Kicker({ children }: { children: React.ReactNode }) {
  return <p className="text-sm font-medium uppercase tracking-[0.2em] text-violet-300">{children}</p>;
}

function Page() {
  const { lang, setLang } = useI18n();
  const c = LANDING[lang];
  const shot = (name: string) => `/screenshots/${lang}/${name}.jpg`;

  return (
    <div className="relative h-full overflow-y-auto overflow-x-hidden scroll-smooth">
      {/* Aurora backdrop */}
      <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
        <div className="absolute -left-40 -top-40 size-[720px] animate-aurora rounded-full bg-violet-600/25 blur-[150px]" />
        <div className="absolute -right-40 top-1/3 size-[620px] animate-aurora rounded-full bg-cyan-500/15 blur-[150px] [animation-delay:-6s]" />
        <div className="absolute bottom-0 left-1/3 size-[520px] animate-aurora rounded-full bg-fuchsia-600/10 blur-[130px] [animation-delay:-12s]" />
        <div className="absolute inset-0 bg-[radial-gradient(rgb(255_255_255/0.035)_1px,transparent_1px)] [background-size:22px_22px] [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]" />
      </div>

      {/* Nav */}
      <header className="sticky top-0 z-30 border-b border-white/5 bg-[#07080d]/70 backdrop-blur-xl">
        <nav className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3.5">
          <a href="#top" className="flex items-center gap-2 font-semibold">
            <span className="grid size-8 place-items-center rounded-xl bg-gradient-to-br from-violet-500 to-cyan-400 shadow-lg shadow-violet-500/30">
              <Sparkles className="size-4 text-white" />
            </span>
            <span className="text-gradient text-lg">SheetTwin</span>
          </a>
          <div className="ml-6 hidden gap-6 text-sm text-zinc-400 md:flex">
            <a href="#features" className="transition hover:text-white">{c.nav.features}</a>
            <a href="#how" className="transition hover:text-white">{c.nav.how}</a>
            <a href="#issues" className="transition hover:text-white">{c.nav.issues}</a>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setLang(lang === "zh" ? "en" : "zh")}
              className="flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-sm text-zinc-400 transition hover:bg-white/5 hover:text-white"
            >
              <Languages className="size-4" />{lang === "zh" ? "EN" : "中文"}
            </button>
            <a href={APP} className="rounded-xl bg-white/10 px-4 py-1.5 text-sm font-medium text-white ring-1 ring-white/15 transition hover:bg-white/15">
              {c.nav.open}
            </a>
          </div>
        </nav>
      </header>

      <main id="top">
        {/* Hero */}
        <section className="mx-auto max-w-6xl px-6 pb-20 pt-16 text-center sm:pt-24">
          <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
            <span className="inline-flex items-center gap-2 rounded-full border border-violet-400/20 bg-violet-500/10 px-3 py-1 text-xs text-violet-200">
              <span className="size-1.5 animate-pulse rounded-full bg-violet-300" />{c.hero.badge}
            </span>
            <h1 className="mx-auto mt-6 max-w-4xl text-[1.8rem] font-semibold leading-tight tracking-tight sm:text-6xl">
              {c.hero.title1}
              <br />
              <span className="text-gradient">{c.hero.title2}</span>
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-base leading-relaxed text-zinc-400 sm:text-lg">{c.hero.sub}</p>
            <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
              <a href={APP} className="group flex items-center gap-2 rounded-full bg-gradient-to-r from-violet-500 to-cyan-400 px-6 py-3 text-sm font-medium text-white shadow-lg shadow-violet-500/30 transition hover:scale-105">
                {c.hero.start}<ArrowRight className="size-4 transition group-hover:translate-x-0.5" />
              </a>
              <a href={`${APP}?sample=1`} className="flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-6 py-3 text-sm font-medium text-zinc-100 transition hover:bg-white/10">
                <FlaskConical className="size-4 text-cyan-300" />{c.hero.sample}
              </a>
            </div>
            <div className="mt-7 flex flex-wrap justify-center gap-x-5 gap-y-2 text-xs text-zinc-500">
              {c.hero.chips.map((chip) => (
                <span key={chip} className="flex items-center gap-1.5"><Check className="size-3.5 text-emerald-300/80" />{chip}</span>
              ))}
            </div>
          </motion.div>
          <motion.div
            initial={{ opacity: 0, y: 40, rotateX: 12 }}
            animate={{ opacity: 1, y: 0, rotateX: 0 }}
            transition={{ duration: 0.9, delay: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
            className="relative mx-auto mt-16 max-w-5xl [perspective:1600px]"
          >
            <div className="absolute -inset-x-10 -inset-y-6 -z-10 rounded-[3rem] bg-gradient-to-r from-violet-600/30 via-fuchsia-500/20 to-cyan-500/30 blur-3xl" />
            <Shot src={shot("chat")} alt={c.shotAlt.chat} />
          </motion.div>
        </section>

        {/* Why */}
        <section className="mx-auto max-w-6xl px-6 py-20">
          <motion.div {...fadeUp} className="max-w-2xl">
            <Kicker>{c.why.kicker}</Kicker>
            <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">{c.why.title}</h2>
          </motion.div>
          <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {c.why.items.map((item, i) => {
              const Icon = WHY_ICONS[i];
              return (
                <motion.div key={item.title} {...fadeUp} transition={{ ...fadeUp.transition, delay: i * 0.08 }}
                  className="glow-border rounded-3xl bg-white/[0.03] p-6">
                  <span className="grid size-10 place-items-center rounded-2xl bg-rose-500/10 text-rose-300 ring-1 ring-rose-400/20">
                    <Icon className="size-5" />
                  </span>
                  <h3 className="mt-5 font-medium text-white">{item.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-zinc-400">{item.body}</p>
                </motion.div>
              );
            })}
          </div>
        </section>

        {/* Features */}
        <section id="features" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-20">
          <motion.div {...fadeUp} className="mx-auto max-w-2xl text-center">
            <Kicker>{c.features.kicker}</Kicker>
            <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">{c.features.title}</h2>
          </motion.div>
          <div className="mt-16 space-y-24">
            {FEATURES.map(({ key, icon: Icon, tint }, i) => {
              const f = c.features.items[key];
              return (
                <div key={key} className="grid items-center gap-10 lg:grid-cols-5">
                  <motion.div {...fadeUp} className={`lg:col-span-2 ${i % 2 ? "lg:order-2" : ""}`}>
                    <span className={`grid size-12 place-items-center rounded-2xl bg-gradient-to-br to-transparent ring-1 ring-white/10 ${tint}`}>
                      <Icon className="size-6" />
                    </span>
                    <h3 className="mt-5 text-2xl font-semibold tracking-tight">{f.title}</h3>
                    <p className="mt-3 leading-relaxed text-zinc-400">{f.body}</p>
                    <ul className="mt-5 space-y-2 text-sm text-zinc-300">
                      {f.points.map((p) => (
                        <li key={p} className="flex items-start gap-2"><Check className="mt-0.5 size-4 shrink-0 text-emerald-300" />{p}</li>
                      ))}
                    </ul>
                  </motion.div>
                  <motion.div {...fadeUp} transition={{ ...fadeUp.transition, delay: 0.1 }} className={`lg:col-span-3 ${i % 2 ? "lg:order-1" : ""}`}>
                    <Shot src={shot(key)} alt={c.shotAlt[key]} />
                  </motion.div>
                </div>
              );
            })}
          </div>
        </section>

        {/* How it works */}
        <section id="how" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-20">
          <motion.div {...fadeUp} className="mx-auto max-w-2xl text-center">
            <Kicker>{c.how.kicker}</Kicker>
            <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">{c.how.title}</h2>
          </motion.div>
          <div className="relative mt-14 grid gap-6 md:grid-cols-4">
            <div aria-hidden className="absolute left-[12%] right-[12%] top-6 hidden h-px bg-gradient-to-r from-violet-400/0 via-violet-400/40 to-cyan-400/0 md:block" />
            {c.how.steps.map((s, i) => {
              const Icon = STEP_ICONS[i];
              return (
                <motion.div key={s.title} {...fadeUp} transition={{ ...fadeUp.transition, delay: i * 0.1 }} className="relative text-center">
                  <span className="relative mx-auto grid size-12 place-items-center rounded-2xl bg-zinc-900 text-violet-200 ring-1 ring-violet-400/30 shadow-lg shadow-violet-500/20">
                    <Icon className="size-5" />
                    <span className="absolute -right-2 -top-2 grid size-5 place-items-center rounded-full bg-gradient-to-br from-violet-500 to-cyan-400 text-[10px] font-semibold text-white">{i + 1}</span>
                  </span>
                  <h3 className="mt-4 font-medium text-white">{s.title}</h3>
                  <p className="mx-auto mt-1.5 max-w-[15rem] text-sm leading-relaxed text-zinc-400">{s.body}</p>
                </motion.div>
              );
            })}
          </div>

          <div className="mt-16 grid items-center gap-8 lg:grid-cols-5">
            <motion.div {...fadeUp} className="space-y-2.5 lg:col-span-3">
              {[...c.how.layers].reverse().map((l, i) => (
                <div key={l.id} className="flex items-center gap-4 rounded-2xl border border-white/[0.07] bg-white/[0.03] px-5 py-4"
                  style={{ marginLeft: `${i * 4}%`, marginRight: `${(3 - i) * 4}%` }}>
                  <span className="grid size-10 shrink-0 place-items-center rounded-xl font-mono text-sm font-semibold text-white"
                    style={{ background: `linear-gradient(135deg, ${["#8b5cf6", "#7c6cf0", "#4f8ff0", "#22d3ee"][3 - i]}, transparent 140%)` }}>
                    {l.id}
                  </span>
                  <span className="min-w-0">
                    <span className="block font-medium text-white">{l.name}</span>
                    <span className="block truncate text-sm text-zinc-400">{l.body}</span>
                  </span>
                </div>
              ))}
            </motion.div>
            <motion.div {...fadeUp} transition={{ ...fadeUp.transition, delay: 0.1 }}
              className="glow-border rounded-3xl bg-white/[0.03] p-6 lg:col-span-2">
              <span className="grid size-10 place-items-center rounded-2xl bg-emerald-500/10 text-emerald-300 ring-1 ring-emerald-400/20">
                <ShieldCheck className="size-5" />
              </span>
              <h3 className="mt-4 font-medium text-white">{c.how.steps[2].title}</h3>
              <ul className="mt-4 space-y-3 text-sm text-zinc-300">
                {c.how.checks.map((check) => (
                  <li key={check} className="flex items-start gap-2.5"><Check className="mt-0.5 size-4 shrink-0 text-emerald-300" />{check}</li>
                ))}
              </ul>
            </motion.div>
          </div>
        </section>

        {/* Issues */}
        <section id="issues" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-20">
          <motion.div {...fadeUp} className="mx-auto max-w-2xl text-center">
            <Kicker>{c.issues.kicker}</Kicker>
            <h2 className="mt-3 text-3xl font-semibold tracking-tight sm:text-4xl">{c.issues.title}</h2>
            <p className="mt-4 text-zinc-400">{c.issues.sub}</p>
          </motion.div>
          <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {c.issues.items.map((it, i) => {
              const sev = it.severity as keyof typeof SEV;
              return (
                <motion.div key={it.kind} {...fadeUp} transition={{ ...fadeUp.transition, delay: (i % 3) * 0.08 }}
                  className="flex flex-col rounded-3xl border border-white/[0.07] bg-[#141419]/80 p-6 backdrop-blur">
                  <div className="flex items-center gap-2">
                    <span className="flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium" style={{ background: `${SEV[sev]}22`, color: SEV[sev] }}>
                      <TriangleAlert className="size-3" />{SEV_LABEL[lang][sev]}
                    </span>
                    <span className="truncate font-mono text-[11px] text-zinc-500">{it.kind}</span>
                  </div>
                  <h3 className="mt-4 font-medium text-white">{it.title}</h3>
                  <p className="mt-2 flex-1 text-sm leading-relaxed text-zinc-400">{it.body}</p>
                  <code className="mt-4 self-start rounded-lg border border-violet-400/20 bg-violet-500/10 px-2 py-1 font-mono text-xs text-violet-200">{it.cell}</code>
                </motion.div>
              );
            })}
          </div>
          <p className="mt-8 text-center text-sm text-zinc-500">{c.issues.more}</p>
        </section>

        {/* CTA */}
        <section className="mx-auto max-w-6xl px-6 pb-24 pt-10">
          <motion.div {...fadeUp} className="glow-border relative overflow-hidden rounded-[2rem] bg-gradient-to-br from-violet-600/20 via-zinc-900/60 to-cyan-500/15 px-8 py-16 text-center">
            <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{c.cta.title}</h2>
            <p className="mx-auto mt-4 max-w-xl text-zinc-400">{c.cta.sub}</p>
            <div className="mt-8 flex flex-wrap justify-center gap-3">
              <a href={APP} className="group flex items-center gap-2 rounded-full bg-gradient-to-r from-violet-500 to-cyan-400 px-6 py-3 text-sm font-medium text-white shadow-lg shadow-violet-500/30 transition hover:scale-105">
                {c.hero.start}<ArrowRight className="size-4 transition group-hover:translate-x-0.5" />
              </a>
              <a href={`${APP}?sample=1`} className="flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-6 py-3 text-sm font-medium text-zinc-100 transition hover:bg-white/10">
                <FlaskConical className="size-4 text-cyan-300" />{c.hero.sample}
              </a>
            </div>
          </motion.div>
        </section>
      </main>

      <footer className="border-t border-white/5">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-3 px-6 py-8 text-sm text-zinc-500">
          <span>{c.footer.note}</span>
          <div className="ml-auto flex items-center gap-5">
            <a href={APP} className="transition hover:text-white">{c.nav.open}</a>
            <a href="/legacy" className="transition hover:text-white">{c.footer.legacy}</a>
            <button onClick={() => setLang(lang === "zh" ? "en" : "zh")} className="flex items-center gap-1.5 transition hover:text-white">
              <Languages className="size-4" />{lang === "zh" ? "English" : "中文"}
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
}
