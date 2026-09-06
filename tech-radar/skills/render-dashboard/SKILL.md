---
name: render-dashboard
description: Render a Tech Radar HTML dashboard from a collect-news JSON data document by running the bundled render.py script. Use when the user asks to build, generate, or refresh the dashboard.
---

# Render Dashboard

Turn a `collect-news` JSON data document into a self-contained HTML dashboard by
running the bundled **`render.py`** script. This skill is **presentation only** —
it never searches or scores. The HTML is produced by the script, NOT written
token-by-token by the model.

`render.py` is OS-agnostic and uses the Python **standard library only** (no
`pip install`, no PyYAML) — it reads the canonical JSON, not YAML. It does NOT
build cards in Python: it embeds the JSON inline into a static **Alpine.js +
Tailwind** template (both via CDN) and Alpine renders/filters everything
declaratively in the browser. To change the dashboard, edit the `TEMPLATE`/`CARD`
strings in `render.py`.

## Input

Optional argument: a path to a `reports/radar-<stamp>.json`. Default:
`reports/radar-latest.json` (the newest run).

## Prerequisites

- `python3` available (any OS).
- At least one `reports/radar-*.json` exists. If none:
  "No radar data found. Run `/tech-radar:collect-news` first."

## Flow

Run the script from the project root (one command — do not hand-write HTML):

```
python3 plugin/skills/render-dashboard/render.py [reports/radar-<stamp>.json]
```

It derives `{stamp}` from the data's `generated_at`, writes
`reports/dashboard-{stamp}.html` (never overwriting prior runs), refreshes
`reports/dashboard-latest.html`, and prints the path. Relay that path to the user.

The sections below document **what `render.py` emits** — to change the dashboard,
edit `render.py`, not this prose.

## Layout

Order: header (stats) → **🔥 Flash** (global, all topics) → **Topic selector** →
**Radar Hits** (per-topic summary + cards) → Hot News → **Theory & Research**
(if `research[]` non-empty) → footer.

**Topic selector (multi-topic).** Render a tab bar `[ All ] [ Topic A ] [ Topic B ] …`
from `topics[]` (dropdown instead of tabs if more than ~4 topics). Under the active
tab show that topic's `description`. Selecting a topic filters Radar Hits, Hot News
and Theory & Research to that topic; `All` shows everything. **Flash stays global —
never filtered** (the whole point is not missing a cross-topic surge).

Alpine drives this declaratively: `topic` state + `x-show`/`x-for` over the
filtered lists (`hits()`, `hotFor(slug)`, `researchItems()`); the tab bar is
`x-for` over `topics`. `radar_summary` is a map — the active topic's summary
shows (all summaries under `All`). With a single topic the bar still renders
(one tab + its description) and is effectively a no-op.

**🔥 Flash section.** Placed first so a surge is never missed. Take all items
with `flash: true` (temperature ≥ 4), **group them by theme**, and show one
representative per surging theme (highest `match`) as a compact, prominent card
with the flame and a one-line "what's surging". Omit the whole section if no item
is `flash: true`.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tech Radar Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 text-gray-900">
  <header><!-- Title, generated_at, stats: topics / scanned / stored / radar_hits --></header>
  <main>
    <!-- 🔥 Flash: surging themes (flash:true / temperature >= 4), grouped by theme. Omit if none. -->
    <section id="flash">
      <h2>🔥 Flash — surging now</h2>
      <!-- One compact card per surging theme: flame, theme name, one-line why, representative link -->
    </section>
    <!-- Topic selector: tabs from topics[]; active tab shows topic.description. Flash above is NOT filtered. -->
    <nav id="topic-tabs"><!-- [All] [Topic A] [Topic B] … --></nav>
    <p id="topic-desc"><!-- description of the active topic --></p>
    <!-- Every filterable card/section below carries data-topic="{slug}" -->
    <section id="radar-hits" data-topic="{slug-per-card}">
      <!-- FIRST: the single synthesized summary of ALL high hits (radar_summary) -->
      <div id="radar-summary" class="bg-white rounded-lg shadow p-5 border-l-4 border-red-500 mb-6">
        <h2 class="text-xl font-bold mb-2">📡 Radar Hits — Summary</h2>
        <p class="text-gray-700">{radar_summary}</p>
      </div>
      <!-- THEN: cards for each item in radar_hits -->
    </section>
    <!-- For each topic in hot_news: -->
    <section id="topic-{slug}">
      <h2><!-- topic name --></h2>
      <!-- Cards for that topic's medium-relevance items (NO summary block) -->
    </section>
    <!-- Adjacent theoretical work, if research[] is non-empty: -->
    <section id="research">
      <h2>🔬 Theory &amp; Research</h2>
      <!-- Cards for research[] items; show the `field` chip; NO summary block -->
    </section>
  </main>
  <footer><!-- topics[] snapshot, last collection time --></footer>
</body>
</html>
```

## Card

```html
<div class="bg-white rounded-lg shadow p-4 border-l-4 border-{color}" data-topic="{slug}" data-stance="{stance}">
  <a href="{url}" target="_blank" class="text-lg font-semibold text-blue-700 hover:underline">{title}</a>
  <p class="text-gray-600 mt-2">{summary}</p>
  <div class="flex flex-wrap items-center gap-2 mt-3 text-sm text-gray-500">
    <span>{source}</span><span>·</span><span>{date}</span>
    <span class="px-2 py-0.5 rounded text-xs font-medium bg-{stance-color}">{stance}</span>
    <span class="px-2 py-0.5 rounded text-xs font-medium bg-{kind-color}">{kind}</span>
    <span class="px-2 py-0.5 rounded text-xs font-medium bg-{badge-color}">{relevance} · {match}%</span>
    <span title="theme temperature">{temperature_flames}</span>
    <!-- language badge if language != en -->
  </div>
</div>
```

### Visual mapping

Relevance border/badge:
- `high`: `border-red-500`, badge `bg-red-100 text-red-800`
- `medium`: `border-amber-400`, badge `bg-amber-100 text-amber-800`

`kind` badge (`bg-…-100 text-…-800`): announcement→blue, standard→indigo,
research→purple, implementation→teal, proposal→pink, analysis→slate,
guide→green, tool→cyan.

`stance` badge: `competitor` → red (`bg-red-100 text-red-800`, prefix ⚔️),
`adoptable` → emerald, `narrow` → gray, `complementary` → sky, `context` → zinc.
**Competitive emphasis:** a `competitor` card also gets a red ring
(`ring-1 ring-red-300`) so rivals pop; show `stats.competitors` in the header
(e.g. "⚔️ 1 competitive"); a competitor with `temperature` ≥ 3 should also appear
in the Flash band. Optionally add stance filter chips
(`[ all stances ] [ ⚔️ competitor ] [ adoptable ] …`) that toggle `data-stance`
visibility with the same JS used for the topic tabs.

`match`: show the number next to the relevance bucket (e.g. `high · 78%`).

`temperature` → flames: 1 `❄️`, 2 `🌡`, 3 `🔥`, 4 `🔥🔥`, 5 `🔥🔥🔥`.

Research cards (`#research`): use the `kind:research` purple badge, add a neutral
gray chip for `field` (e.g. `zero-knowledge-proofs`), show `match`%, and omit the
temperature flame (papers aren't a recurring-news theme).

**Tag filtering (click-to-filter).** Every badge on a card — `kind`, `stance`,
`field`, `relevance`, `source`, `language` — is clickable and toggles a facet
filter. Cards match the union within a facet and the intersection across facets
(e.g. kind=guide OR analysis, AND source=habr.com). Active filters show as
removable chips with a "clear all" button; the topic tabs combine with them.
Flash is global and never filtered. Implemented in Alpine: clicking a badge calls
`addFilter(facet, value)` (state in `f`), and the lists filter via `match(i)`;
active filters render from `chips`. No build step — works by opening the file.

**Keyword highlight.** Titles and summaries may contain `==term==` marks placed by
collect-news. After HTML-escaping the text, convert each `==term==` to a subtle
marker: `<mark class="bg-yellow-100 text-inherit rounded px-0.5">term</mark>`.
Render only — never add or guess marks here; the curation happens at collect time.

Grid for cards: `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4`. Light theme.
