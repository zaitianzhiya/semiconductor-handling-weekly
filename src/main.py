"""Orchestrator: collect -> filter -> score -> AI -> render pipeline."""

import argparse
import re
import json
import time
import os
import sys
import yaml
from datetime import datetime
from pathlib import Path

from src.collectors.base import EventRecord
from src.collectors.real_search import RealSearchCollector
from src.filters.dedup import Deduplicator
from src.filters.quality import QualityFilter
from src.filters.scorer import Scorer
from src.render.markdown_weekly import MarkdownRenderer

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """Load all YAML config files and merge into one dict."""
    config: dict = {}
    for filename in ["sources.yml", "keywords.yml", "quality.yml"]:
        path = ROOT / "config" / filename
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            config.update(data)
    return config


# ── Wafer handling domain keyword -> category mapping ──
SEMI_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "#amhs": [
        "AMHS", "OHT", "Overhead Hoist", "天车", "Stocker", "stocker",
        "AGV", "AMR", "自动物料搬运", "automatic material handling",
        "automated material handling", "Conveyor", "输送线",
        "OHB", "NTB", "MCS", "material control system",
        "Daifuku", "大福", "Murata", "村田",
        "SS5000", "天帆", "弥费", "华芯智能", "成川",
        "whole fab", "fab automation transport",
    ],
    "#efem": [
        "EFEM", "equipment front end module", "设备前端模块",
        "mini-environment", "微环境", "Class 1", "FFU",
        "front end module", "大气传输", "前端传输",
        "RORZE", "乐孜", "Brooks", "Hirata", "平田",
        "Kensington", "Nidec Genmark",
        "果纳 EFEM", "Guona EFEM", "SRT", "微法尔",
        "广川 EFEM", "AEW6000",
    ],
    "#wafer_sorter": [
        "sorter", "晶圆分选", "wafer sorting", "晶圆分类",
        "wafer identification", "ID读取", "grade sorting",
        "slot mapping", "槽位", "OCR", "barcode",
        "分选机", "ASW6000", "multi-sorter",
        "RORZE sort", "Brooks sort",
    ],
    "#load_port": [
        "load port", "装载端口", "负载端口", "晶圆载入",
        "FOUP load", "FOSB load", "door opening", "开门机构",
        "mapping sensor", "purge", "吹扫", "N2 purge",
        "SINFONIA", "信浓", "TDK", "Cymechs",
        "广川 load port", "LP160", "RR700",
    ],
    "#wafer_robot": [
        "wafer robot", "晶圆机械手", "晶圆机器人", "wafer transfer robot",
        "atmospheric robot", "大气机械手", "vacuum robot", "真空机械手",
        "transfer robot", "传输机器人", "dual arm", "双臂",
        "单臂", "end effector", "末端执行器", "edge grip", "边缘握持",
        "JEL", "Kawasaki robot", "DAIHEN", "Robostar",
        "RB100", "SIASUN", "新松", "Yaskawa", "安川",
        "MagnaTran", "±0.02mm",
    ],
    "#foup_fosb": [
        "FOUP", "front opening unified box", "FOSB",
        "SMIF pod", "晶圆载具", "晶圆盒", "晶圆包装",
        "wafer carrier", "wafer container", "晶圆容器",
        "Entegris", "Shin-Etsu Polymer", "Miraial",
        "家登", "Gudeng", "中勤", "Chuang King",
        "purge FOUP", "自净化", "low outgassing", "低放气",
        "anti-static", "防静电",
    ],
    "#mcs_software": [
        "MCS", "material control", "物料控制软件",
        "fab scheduling", "晶圆厂调度", "AMHS software",
        "route optimization", "路径优化", "real-time tracking",
        "实时追踪", "digital twin", "数字孪生",
        "AI scheduling", "智能调度", "reinforcement learning",
        "MES interface", "仿真 schedule",
    ],
    "#china_handling": [
        "国产传输", "传输设备国产", "国产替代",
        "中国晶圆传输", "China wafer transport",
        "果纳", "Guona semiconductor", "果纳半导体",
        "弥费", "mifei", "Mifei",
        "微法尔", "WFR", "SRT",
        "华芯智能", "广川科技", "成川科技",
        "海晨股份", "和崎精密", "大族富创得",
        "新松", "SIASUN",
        "国产 AMHS", "国产 EFEM", "自主可控",
    ],
    "#fab_automation": [
        "fab automation", "晶圆厂自动化", "整厂自动化",
        "OHT+EFEM", "立体化对接", "clean transport",
        "fab logistic", "旧厂改造", "upgrade automation",
        "先进封装 传输", "HBM transfer",
        "300mm fab", "200mm fab",
        "SEMI standard", "SEMI E15.1", "SEMI E63", "SEMI E47.1",
    ],
}


def _auto_categorize(record: EventRecord, config: dict) -> list[str]:
    """Auto-classify based on title keyword matching (word-boundary for ASCII, substring for CJK)."""
    text = (record.title or "").lower()
    category_mapping = config.get("category_mapping", {})
    matched: list[str] = []
    for cat_id, keywords in category_mapping.items():
        for kw in keywords:
            if _kw_match((kw or "").lower(), text):
                cat_name = cat_id
                for cc in config.get("categories", []):
                    if cc.get("id") == cat_id:
                        cat_name = cc.get("name", cat_id)
                        break
                matched.append(cat_name)
                break
    return matched


def _kw_match(kw: str, text: str) -> bool:
    if not kw:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in kw):
        return kw in text
    return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text) is not None

def _merge_records(records: list[EventRecord]) -> list[EventRecord]:
    """Merge records with same event_id, combining citation chains."""
    merged: dict[str, EventRecord] = {}
    for r in records:
        if r.event_id in merged:
            existing = merged[r.event_id]
            existing_keys = {c.source_key for c in existing.citations}
            for c in r.citations:
                if c.source_key not in existing_keys:
                    existing.citations.append(c)
            if r.description and len(r.description) > len(existing.description or ""):
                existing.description = r.description
        else:
            merged[r.event_id] = r
    return list(merged.values())


def _generate_cn_titles(records: list[EventRecord]) -> None:
    """Generate Chinese titles for ALL event records via LLM batch translation.

    Strategy: LLM translates all events in batches (20 per call).
    Skipped entirely when no LLM key is configured (keyword substitution
    produced mixed-language garbage).
    """
    try:
        from src.ai.llm_client import LLMClient
        _llm_client = LLMClient()
    except Exception:
        _llm_client = None
    if _llm_client is None:
        # No LLM key configured — leave titles untranslated instead of
        # emitting mixed-language keyword substitutions.
        print("[CN translate] No LLM key — skipping Chinese title generation")
        return


    import re

    # LLM batch translation for ALL events
    try:
        from src.ai.llm_client import LLMClient
        client = LLMClient()
    except Exception:
        print("  [CN translate] No LLM key found -- using keyword-only fallback")
        return

    BATCH_SIZE = 20
    id_to_cn: dict[str, str] = {}
    all_records = [r for r in records if r.title.strip()]

    for batch_start in range(0, len(all_records), BATCH_SIZE):
        batch = all_records[batch_start:batch_start + BATCH_SIZE]
        lines = [f"{j+1}. {r.title}" for j, r in enumerate(batch)]
        prompt = (
            "Translate these semiconductor wafer handling news headlines into concise, fluent Chinese.\n"
            "Rules: keep technical acronyms (OHT/AMHS/EFEM/FOUP/FOSB/AGV/AMR/MCS) as-is.\n"
            "Return exactly one line per number, format: N. Chinese translation\n\n"
            + "\n".join(lines)
        )

        for attempt in range(3):
            try:
                import time
                if attempt > 0:
                    time.sleep(60)  # wait for rate-limit window to reset
                result = client.chat(
                    "You are a semiconductor equipment industry translator. Translate English news headlines "
                    "into fluent, concise Chinese. Preserve technical acronyms. Output format: "
                    "N. Chinese translation \u2014 one numbered line per headline, no extra text.",
                    prompt, temperature=0.1,
                )
                for line in result.strip().split("\n"):
                    line = line.strip()
                    parts = line.split(". ", 1)
                    if len(parts) == 2 and parts[0].isdigit():
                        idx = int(parts[0]) - 1
                        if 0 <= idx < len(batch):
                            id_to_cn[batch[idx].event_id] = parts[1].strip()
                break
            except Exception as e:
                print(f"  [CN translate] Batch {batch_start // BATCH_SIZE + 1} attempt {attempt + 1} failed: {str(e)[:80]}")
                if attempt == 2:
                    print(f"  [CN translate] Batch {batch_start // BATCH_SIZE + 1} exhausted retries, using keyword preprocess")
        import time
        time.sleep(2)  # rate limiting guard

    for r in records:
        if r.event_id in id_to_cn and id_to_cn[r.event_id]:
            r.title_cn = id_to_cn[r.event_id]

    print(f"  [CN translate] LLM translated {len(id_to_cn)}/{len(all_records)} titles")


def run_weekly(config: dict):
    """Full weekly pipeline: collect from all Tier 1 + Tier 2 sources."""
    print(f"[Weekly] Starting pipeline -- {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    records: list[EventRecord] = []

    sources_cfg = config.get("sources", {})
    enabled_sources = {k: v for k, v in sources_cfg.items() if v.get("enabled", True)}

    print(f"[Weekly] Collecting from {len(enabled_sources)} sources...")

    for source_key, source_cfg in enabled_sources.items():
        try:
            collector = RealSearchCollector(config, source_key)
            collector.gh_token = os.environ.get("GH_TOKEN", "")
            items = collector.collect()
            for item in items:
                item.categories = _auto_categorize(item, config)
            records.extend(items)
            if items:
                print(f"  [{source_key}] {len(items)} items -- {source_cfg.get('name', source_key)}")
        except Exception as e:
            print(f"  [{source_key}] FAILED: {e}")

    if not records:
        print("[Weekly] No records collected -- check source configuration.")
        return

    # Merge + dedup
    merged = _merge_records(records)
    print(f"[Weekly] Merged: {len(merged)} unique events (from {len(records)} raw)")

    qf = QualityFilter(config)
    filtered, qstats = qf.filter(merged)
    print(f"[Weekly] Quality filter: {qstats}")
    if not filtered:
        print("[Weekly] No records passed quality filter.")
        return

    dedup = Deduplicator(str(ROOT / "data" / "dedup_state.json"))
    new_records, seen = dedup.deduplicate(filtered)
    print(f"[Weekly] Dedup: {len(new_records)} new / {seen} already seen")

    if not new_records:
        print("[Weekly] All events already seen this cycle.")
        return

    scorer = Scorer(config)
    new_records = scorer.score(new_records)
    new_records.sort(key=lambda r: r.confidence_score, reverse=True)

    grade_counts = {}
    for r in new_records:
        g = r.confidence_grade
        grade_counts[g] = grade_counts.get(g, 0) + 1
    grade_str = ", ".join(f"{k}:{v}" for k, v in sorted(grade_counts.items()))
    print(f"[Weekly] Filtered+Scored: {len(new_records)} events -- {grade_str}")

    # Generate Chinese titles (LLM batch translation with rule-based fallback)
    _generate_cn_titles(new_records)
    cn_count = sum(1 for r in new_records if r.title_cn)
    print(f"[Weekly] CN titles generated: {cn_count}/{len(new_records)}")

    # AI deep analysis
    deep_analysis = ""
    try:
        from src.ai.llm_client import LLMClient
        from src.ai.deep_analyzer import DeepAnalyzer

        client = LLMClient()
        analyzer = DeepAnalyzer(client, ROOT / "prompts")
        top_n = min(len(new_records), 15)
        deep_analysis = analyzer.analyze(new_records, top_n=top_n)
        print(f"[Weekly] AI deep analysis generated ({len(deep_analysis)} chars)")
    except Exception as e:
        print(f"[Weekly] AI skipped (will render data-only report): {e}")

    # Render
    category_order = [c.get("name") for c in config.get("categories", [])]
    renderer = MarkdownRenderer(str(ROOT / "output"), category_order=category_order)
    stats = {
        "本周采集": len(records),
        "历史已见": seen,
        "质量过滤排除": sum(qstats.values()) - qstats["kept"] - qstats["fallback_excluded"],
        "占位骨架排除": qstats["fallback_excluded"],
        "新事件": len(new_records),
        "可信度分布": grade_str,
        "独立生态覆盖": _eco_coverage(new_records),
    }
    renderer.render_weekly_report(new_records, deep_analysis=deep_analysis, stats=stats)
    dedup.save()

    print(f"[Weekly] ✅ Done -- report written to output/")
    print(f"[Weekly] Top event: {new_records[0].title[:80] if new_records else 'N/A'}")


def _eco_coverage(records: list[EventRecord]) -> str:
    ecosystems: set[str] = set()
    for r in records:
        for c in r.citations:
            ecosystems.add(c.ecosystem)
    return f"{len(ecosystems)} ecosystems: {', '.join(sorted(ecosystems)[:8])}"


# ---- CLI entry ----

def main():
    parser = argparse.ArgumentParser(description="Semiconductor wafer handling weekly digest")
    parser.add_argument(
        "--mode", choices=["weekly", "daily"], default="weekly",
        help="Run mode: weekly (full pipeline) or daily (Tier 1 only)",
    )
    args = parser.parse_args()

    # Ensure root in path for absolute imports
    sys.path.insert(0, str(ROOT))

    config = load_config()
    print(f"[Main] Mode: {args.mode} | Sources: {len(config.get('sources', {}))}")

    if args.mode == "weekly":
        run_weekly(config)
    else:
        print("[Main] Daily mode not yet configured -- use weekly.")


if __name__ == "__main__":
    main()
