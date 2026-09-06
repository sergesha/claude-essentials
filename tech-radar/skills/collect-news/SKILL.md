---
name: collect-news
description: Collect tech news for configured topics. Searches SearXNG, summarizes, classifies (kind), scores match % and theme temperature, stores in Redis, and writes a timestamped JSON data document. HTML/YAML views are produced separately by scripts (render-dashboard / export_yaml.py).
---

# Collect News

Collect news for Tech Radar topics and write a structured **JSON data document**
(the canonical, single source of truth). All representations are generated from it
by scripts, never hand-written: HTML via the `render-dashboard` skill's `render.py`,
and an optional human-readable YAML view via `export_yaml.py`.

## Input

Optional arguments:
- Topic filter: name or slug of a specific topic (default: all topics)
- Time range: `day`, `week`, `month` (default: `day`)

There is no HTML/`--full` mode here anymore. This skill always writes the JSON
document; in interactive use it also prints a short summary to chat.

## Prerequisites

- Docker stack must be running (SessionStart hook handles this)
- At least one topic config must exist in `topics/`
- searxng-mcp must be available
- redis-memory-mcp must be available (via plugin)

If no topics exist, tell the user:
"No topics configured. Run `/tech-radar:configure-topic` to create one."

## Flow

### 1. Load Topics

Read all `topics/*.yaml` files. If a topic filter was provided, select
only the matching topic.

### 2. Refine Queries

For each topic, take its `keywords` and `queries` and expand them:

- Add current year to time-sensitive queries (e.g., append "2026")
- Generate 2-3 synonym variations of key phrases
- For each non-English language in `languages`, translate the top 3
  queries into that language

Keep total queries per topic under 15 to avoid excessive API calls.

**Research / theory expansion.** If the topic has `research_keywords`, also build
science-oriented queries pairing the topic core with each adjacent theoretical
concept (e.g. "agent communication zero-knowledge proofs", "multi-agent secure
multi-party computation"). These surface theoretical work close to the topic
(arXiv, IEEE, IACR ePrint, Google Scholar). Budget ~1 query per research_keyword,
on top of the 15 above.

### 3. Search

Run each refined query across BOTH categories. Niche/IT and Russian-language
deep-dives (Habr, Medium, vendor blogs) live in general web search, not the
news engines — querying only `news` silently drops them.

```
searxng-mcp → search(query, category="news",    time_range="{time_range}", language="{lang}")
searxng-mcp → search(query, category="general", time_range="{wider}",      language="{lang}")
```

For the **research/theory** queries from step 2, use the `science` category
(English), which returns scholarly sources (arXiv, IEEE, IACR ePrint, Google
Scholar):

```
searxng-mcp → search(research_query, category="science", time_range="year", language="en")
```

- `news`: use the requested `time_range`.
- `general`: high-value write-ups are often older than a day/week — try
  `{time_range}` first and, if it returns little, widen to `month`, then `year`.
- `science`: use a `year` window (papers are dated by year).
- Do NOT rely on `category="it"` — it is unreliable on this SearXNG instance
  (frequently returns nothing).

**Preferred sources.** If `topic.sources` lists domains (e.g. `habr.com`,
`medium.com`), make sure the general-category queries cover them and flag any
listed source that returned nothing for the run. Habr's canonical domain is
`habr.com` (`habr.ru` redirects there).

### 4. Deduplicate

Deduplicate results by URL across all queries AND categories for a topic.
If two results share the same base URL (ignoring query params and
fragments), keep the one with the longer snippet.

### 5. Cross-run dedup (seen-URL kv index)

Drop candidates already ingested by ANY previous run before spending tokens
on them. The corpus-wide index lives in redis kv, one key per topic:

```
redis-memory-mcp → kv_get(key = "tech-radar:seen-urls:{topic-slug}")
```

The value is a JSON array of normalized URLs stored across all runs; treat a
missing key as `[]`. Normalize candidate URLs EXACTLY as in step 4 (base URL,
query params and fragments stripped). A candidate whose normalized URL is in
the array is skipped entirely — no summary, no `mem_save` — and counted in
`corpus_duplicates_skipped`. Unseen candidates continue to step 6.

After the topic's candidates are processed, append every newly stored
normalized URL to the array and write it back:

```
redis-memory-mcp → kv_set(
  key = "tech-radar:seen-urls:{topic-slug}",
  value = "{updated JSON array}",
  ttl_days = 0
)
```

### 6. Summarize and Classify

For each unique result:

**Generate summary** in the topic's `output_lang` (default: English):
- 2-3 sentences extracting key facts
- Focus on: what happened, who is involved, why it matters
- Do NOT reproduce full article text
- **Highlight inline:** wrap the 1–3 most decision-relevant terms in
  `==double equals==` (Markdown highlight) — distinctive protocols, mechanisms,
  named standards/orgs (e.g. `==A2A==`, `==mTLS==`, `==zero-knowledge proofs==`),
  NOT generic words like "agent"/"AI". Mark the actual words, so it works in any
  language (RU summaries mark RU words). Keep it sparse — over-marking kills it.

**Classify `kind`** — pick exactly ONE primary value from the
[Kind taxonomy](#kind-taxonomy) (optionally one secondary). This labels the
nature of the item (idea, proposal, implementation write-up, etc.).

**Assign `stance`** — the item's strategic relation to the topic's `positioning`
(what WE build). Pick ONE from the [Stance taxonomy](#stance-taxonomy):
`competitor` / `adoptable` / `narrow` / `complementary` / `context`. This is
orthogonal to `kind` (a research paper can be `adoptable` or a `competitor`'s
approach). If the topic has no `positioning`, skip stance.

### 7. Store in Redis

Store every result you intend to keep (relevance `medium`/`high` after scoring
in step 8 — but you may store first and prune `low` from the YAML). Saving first
is required because the `match` and `temperature` metrics are computed via
semantic search over stored memory.

```
redis-memory-mcp → mem_save(
  text = "{title}\n{summary}\nSource: {url}\nDate: {date}",
  label = "{title}",
  tags = [topic.redis_tags..., "{source_name}", "{language}", "{date}",
          "kind-{kind}", "stance-{stance}"],   # research items also add "field-{research_keyword}"
  ttl_days = 30
)
```

Note: do NOT pre-bake `relevance:` into tags — it is derived from `match` in
step 8. (Tags must avoid characters the TAG index dislikes; the server
sanitizes them, but keep them simple: letters, digits, `-`, `_`.)

### 8. Score `match` and `temperature`

**`match` (%) — precision of fit to the tracked topic.**
Compute cosine similarity of each article against `topic.description`:

```
redis-memory-mcp → mem_search(query = "{topic.description}", top_k = 30,
                              tags = "{topic primary redis_tag}")
```

Read the similarity % reported per result and map it onto the items by ID/URL.
That percentage is `match`. Derive the relevance bucket from it:

- `high`   — `match` ≥ 70
- `medium` — `match` 50–69
- `low`    — `match` < 50  → drop from the YAML (and optionally `mem_delete`)

**Research items** (`kind: research`, found via the research/theory expansion)
go into the YAML `research:` list — NOT radar_hits/hot_news. Keep them at a lower
floor (`match` ≥ 40), since adjacent theory scores lower against a product-oriented
`topic.description`. Each records which `research_keyword` it matched as `field`.

**`temperature` (1–5) — heat of the theme across our corpus.**
For each kept item, search memory for the same theme and count corroboration:

```
redis-memory-mcp → mem_search(query = "{item title / core theme}", top_k = 10)
```

Count **distinct sources** among results with similarity ≥ 60%, then weight by
recency (mentions within the last 7 days count full; older ones decay). Map:

| temperature | meaning | rule of thumb |
|---|---|---|
| 1 ❄️ | isolated | single source |
| 2 🌡 | warm | 2 distinct sources |
| 3 🔥 | hot | 3–4 distinct sources, recent |
| 4 🔥🔥 | very hot | 5+ distinct sources |
| 5 🔥🔥🔥 | blazing | 5+ sources AND a surge in this run |

Temperature is a property of the **theme**, so near-duplicate items about the
same story share a temperature.

**Flash flag.** Mark any item with `temperature >= 4` as `flash: true` — a theme
that is bursting across sources right now and must not be missed. `stats.flash`
counts the **distinct surging themes** (not items), since several flash items
usually belong to the same hot story.

### 9. Synthesize the Radar Hits summary

For EACH topic that has `high`-relevance hits, write ONE combined summary of that
topic's hits (3–5 sentences on cross-cutting themes and notable developments, in
the topic's `output_lang`; not one per article). Store as `radar_summary` keyed
by topic slug. Do NOT synthesize a summary for medium-relevance / hot-news items.

### 10. Write the JSON data document

Write `reports/radar-{YYYY-MM-DD-HHMM}.json` (timestamp = run start, UTC; set
`generated_at` to that instant as a **quoted ISO-8601 string**, e.g.
`"2026-06-14T08:10:00Z"`). **Never overwrite** an existing file — the timestamp
makes each run unique. Then refresh the pointer `reports/radar-latest.json` as a
**copy** of the file just written (the only file overwritten).

JSON is the canonical store. To produce views: run the `render-dashboard` skill
for HTML, or `python3 plugin/skills/collect-news/export_yaml.py reports/radar-latest.json`
for a human-readable YAML rendering. Do not hand-write either.

Also record the run metadata:

```
redis-memory-mcp → kv_set(
  key = "tech-radar:last-run:{topic-slug}",
  value = "{timestamp}|{articles_found}|{radar_hits}",
  ttl_days = 0
)
```

See [Data schema](#data-schema) below.

### 11. Chat output (interactive use)

Print a short summary to chat organized by topic:
- Radar Hits first (high relevance): title, `kind`, `match`%, `temperature`, link
- Then medium relevance (hot news)
- Footer: total scanned, stored, corpus duplicates skipped, time range, and the written YAML path
- Strip the `==...==` highlight marks for chat (plain text); they are only for
  the HTML renderer. Keep them intact in the YAML.

---

## Kind taxonomy

Pick ONE primary `kind` per item (optionally one secondary). Stored as the tag
`kind-{value}` — use a hyphen, not a colon: the RediSearch TAG index strips `:`
(so `kind:research` would be saved as `kindresearch`). Same for `field-{value}`.

| `kind` | use for |
|---|---|
| `announcement` | release, launch, version/feature update, partnership |
| `standard` | spec / protocol / formal draft (IETF, W3C, Linux Foundation) |
| `research` | paper, preprint, benchmark, experiment |
| `implementation` | how it's built — SDK, integration, technical deep-dive |
| `proposal` | idea, vision, opinion/position, RFC-style suggestion |
| `analysis` | trend/landscape, comparison, survey, commentary |
| `guide` | tutorial, practical how-to |
| `tool` | product, platform, library, service |

## Stance taxonomy

The item's strategic relation to the topic's `positioning` (what WE build). One
value per item, stored as `stance-{value}`. Orthogonal to `kind`.

| `stance` | meaning | example |
|---|---|---|
| `competitor` | targets the same broad space we do — a potential rival | a rival secure multi-agent platform |
| `adoptable` | a protocol/spec/standard we could implement or interoperate with | A2A, MCP, IETF AIMS draft |
| `narrow` | a subset of what we do — narrower scope | a messaging-only library |
| `complementary` | adjacent infra/tooling we'd use alongside, not compete with | IAM, AI gateway, TEE |
| `context` | background, trend, or risk reporting — no direct product relation | "agents caused incidents at 42% of orgs" |

A `competitor` that is also heating up (`temperature` ≥ 3) is the highest-priority
signal — surface it prominently (see the dashboard's competitive emphasis).

## Metrics summary

- **`match`** (0–100): cosine similarity to `topic.description`; objective. The
  `relevance` bucket (high/medium/low) is derived from it via the thresholds above.
- **`temperature`** (1–5): theme corroboration (distinct sources) + recency over
  the Redis corpus. Not citability (no citation data available) — heat of the story.

## Data schema

Canonical file is **JSON**; the structure is shown here in YAML form only for
readability (`generated_at` must be a quoted ISO string in the JSON).

```yaml
generated_at: "2026-06-14T00:30:00Z" # UTC, run start (quoted ISO string)
time_range: week
stats:
  topics: 1
  scanned: 25
  corpus_duplicates_skipped: 3         # dropped by the cross-run seen-URL index (step 5)
  stored: 16
  radar_hits: 11
  research: 4
  flash: 2                           # distinct surging themes (temperature >= 4)
  competitors: 1                     # items with stance: competitor
radar_summary:                       # map: topic-slug -> synthesized summary of that topic's high hits
  a2a-secure-messaging: |
    ...
radar_hits:                          # relevance: high, across all topics (each card carries its `topic`)
  - title: "..."
    url: "https://..."
    source: "securew2.com"
    date: "2026-06-12"
    topic: "a2a-secure-messaging"
    kind: "standard"                 # optional: kind_secondary
    stance: "adoptable"              # competitor | adoptable | narrow | complementary | context
    language: "en"
    match: 78                        # %
    relevance: high                  # derived from match
    temperature: 5                   # 1–5
    flash: true                      # present/true only when temperature >= 4
    summary: "The ==A2A== protocol ... signed ==Agent Cards== ..."  # ==term== marks 1–3 key spans
hot_news:                            # relevance: medium, grouped by topic; no summary block
  a2a-secure-messaging:
    - title: "..."
      url: "https://..."
      source: "nokia.com"
      date: "2026-06-10"
      kind: "announcement"
      language: "en"
      match: 58
      relevance: medium
      temperature: 2
      summary: "..."
research:                            # adjacent theoretical work (papers); kind: research
  - title: "..."
    url: "https://arxiv.org/abs/..."
    source: "arxiv.org"
    date: "2026"
    kind: "research"
    field: "zero-knowledge-proofs"   # which research_keyword it matched
    stance: "adoptable"              # competitor | adoptable | narrow | complementary | context
    topic: "a2a-secure-messaging"    # so research filters with the topic selector too
    language: "en"
    match: 52
    summary: "..."
topics:                              # snapshot of configs (for the dashboard selector + footer)
  - name: "Agent-to-Agent (A2A) Secure Messaging & Multi-Agent Protocols"
    slug: "a2a-secure-messaging"
    description: |                    # shown under the active tab in the selector
      Frameworks, protocols, and standards for secure, encrypted communication
      between autonomous AI agents ...
    languages: ["en", "ru"]
    output_lang: "en"
    schedule: "daily"
```

To turn this into an HTML dashboard, run the `render-dashboard` skill.
