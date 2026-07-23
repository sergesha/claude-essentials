---
name: configure-topic
description: Interactively create or edit a tech radar topic configuration. Asks the user to describe a technology, proposes keywords/queries, runs probe searches to validate, and saves a YAML config.
---

# Configure Topic

Create or edit a topic configuration for the Tech Radar.

## Input

Optional argument: path to an existing `topics/*.yaml` file to edit.
If no argument, create a new topic.

## Flow

### 1. Gather Description

Ask the user to describe the technology or concept they want to track.
Prompt: "Describe the technology or concept you want to monitor.
Be specific — the description drives how well we detect relevant news."

### 2. Analyze and Propose

Based on the description, propose:

- **Name:** short human-readable name (used in dashboard headers)
- **Slug:** kebab-case for the filename (`topics/{slug}.yaml`)
- **Keywords:** 5-10 exact phrases for search
- **Queries:** 3-5 full search queries with different angles on the topic
- **Languages:** recommend `en` always; suggest `zh`, `ja`, `ko`, `ru` if
  the technology has known activity in those regions
- **Sources:** recommend specific SearXNG engines if the topic fits
  (e.g., `arxiv` for academic, `github` for open source)
- **Schedule:** recommend `daily` unless user specifies otherwise
- **Output language:** default `en`

Present proposals to the user. Ask if they want to adjust anything.

### 3. Probe Phase

For each proposed query, run a test search:

```
searxng-mcp → search(query, category="news", time_range="week", num_results=5)
```

Show results to the user in a summary table:
- Query used
- Number of results
- Top 3 titles + sources
- Relevance assessment (does this look on-topic?)

If results are poor:
- Suggest refined queries
- Try alternative phrasings or broader/narrower scope
- Ask user which direction to adjust

Iterate until the user is satisfied with result quality.

### 4. Save Config

Write the validated config to `topics/{slug}.yaml`:

```yaml
name: "{name}"

description: |
  {user's description, possibly refined during the session}

keywords:
  - "{keyword1}"
  - "{keyword2}"

queries:
  - "{validated query 1}"
  - "{validated query 2}"

languages:
  - en
  - {other recommended languages}

schedule: "daily"

output_lang: "en"

redis_tags:
  - "tech-radar"
  - "{slug}"

sources: []
```

Report: "Topic `{name}` saved to `topics/{slug}.yaml`.
Run `/tech-radar:collect-news` to start collecting, or
`/schedule` to set up automatic collection."
