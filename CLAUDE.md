# CLAUDE.md — 半导体传输设备每周周报 (Semiconductor Wafer Handling Weekly Digest)

## Project overview

**Wafer Handling Weekly Digest** is an automated pipeline that collects semiconductor wafer handling & AMHS (Automated Material Handling System) industry news weekly, enriches with multi-source citations, generates AI-powered Chinese summaries (via Gemini), and publishes structured Markdown reports to a GitHub Pages site.

Key numbers: 22 information sources (15 Tier 1 DDG + 7 Tier 2 skeleton), 5-ecosystem diversity, 9-domain taxonomy, bilingual CN+EN report output.

## Architecture

```
Multi-source Collectors -> Dedup/Merge -> Quality Filter -> 5-ecosystem Scorer -> Gemini AI (deep analysis) -> Markdown Renderer -> GitHub Actions commit/push
```

**Entry point:** `python run.py --mode weekly`

## Key modules

| Module | Path | Purpose |
|--------|------|---------|
| Collectors | `src/collectors/` | RealSearchCollector: DDG news for Tier 1, keyword skeleton for Tier 2 |
| Filters | `src/filters/` | Dedup (JSON state), quality gates, eco-diversity scorer |
| AI | `src/ai/` | LLM client (multi-provider), deep analyzer, feedback loader |
| Render | `src/render/` | Bilingual Markdown weekly reports with link + poster columns |
| Config | `config/` | YAML: 22 sources, wafer keywords, quality thresholds + scoring |
| Prompts | `prompts/` | Domain-specific AI behavior (weekly-deep, taxonomy) |

## Domain focus — Wafer Handling & Transport Equipment

Covers 9 categories tracked weekly:
- `#amhs` — OHT/AGV/Stocker/MCS whole-fab material handling
- `#efem` — Equipment Front End Module (Class 1 mini-environment)
- `#wafer_sorter` — Wafer ID, grade sorting, slot mapping
- `#load_port` — FOUP/FOSB docking, purge, mapping sensors
- `#wafer_robot` — Atmospheric/Vacuum transfer robots, end effectors
- `#foup_fosb` — Wafer carriers, containers, packaging
- `#mcs_software` — Material control, scheduling, digital twin
- `#china_handling` — Chinese domestic substitution progress
- `#fab_automation` — Fab-level automation integration

## Key companies tracked

**Japan AMHS duopoly:** Daifuku, Murata Machinery
**Japan EFEM/Robot:** RORZE, Hirata, SINFONIA, JEL, Kawasaki, DAIHEN
**US EFEM/Robot:** Brooks Automation, Nidec Genmark, Kensington
**China local:** Guona (果纳), Mifei (弥费), SRT/WFR (微法尔), SIASUN (新松), 广川, 华芯智能
**Korea:** SEMES, Cymechs

## Configuration files

- `config/sources.yml` — 22 sources across 5 ecosystems
- `config/keywords.yml` — 35 positive keywords, 6 negative, 22 tracked companies
- `config/quality.yml` — Scoring weights, ecosystem weights, 5-dimension weights

## Important implementation details

- **Absolute imports**: All modules use `from src.collectors.base import ...` pattern
- **No LLM fallback**: Pipeline runs data-only if Gemini unavailable
- **git add -f output/**: Required because `.gitignore` excludes output
- **CN title generation**: Batch LLM translation with keyword preprocessing fallback
- **Concurrency**: `concurrency: group: weekly-wafer-handling-digest` with `cancel-in-progress: false`

## GitHub Workflows

| Workflow | Schedule (UTC) | Purpose |
|----------|---------------|---------|
| Weekly Digest | Mon 10:52 | Full pipeline: collect + AI + render + commit |
| Watchdog | Mon 00:00/01:30/05:00 | Dispatch main workflow if report missing |

Secrets required: `GH_TOKEN`, `GEMINI_API_KEY`

## 关键实现要求

- **双语标题**: 所有表格的事件列必须使用中英文双语格式。EN标题为主文本，CN翻译放在 `<br/><small>` 标签中。
- **实现路径**: `markdown_weekly.py` 中的 `_event_title()` 方法 + `main.py` 中的 `_generate_cn_titles()` LLM批量翻译全部事件
- **Fallback**: 所有项目必须有足够大的 `_PREPROCESS` 字典（30+对），保证无LLM时的基本可读性
- **LLM速率保护**: LLM翻译使用 `BATCH_SIZE=15` + `time.sleep(2)` + 3次重试，避免Gemini 429错误
- **审核**: 首次部署后检查 `grep '<br/><small>' output/` 确认双语渲染
