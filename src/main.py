"""Orchestrator: collect -> filter -> score -> AI -> render pipeline."""

import argparse
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
    """Wafer handling domain keyword classification."""
    text = f"{record.title} {record.description}".lower()
    matched: list[str] = []
    for cat_id, keywords in SEMI_CATEGORY_KEYWORDS.items():
        if any(kw.lower() in text for kw in keywords):
            matched.append(cat_id)
    return matched[:3]


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
    Falls back to keyword pre-processing only if no LLM key is available.
    """

    import re

    # Preprocessing: longest-match-first keyword substitution
    _PREPROCESS: list[tuple[str, str]] = sorted([
        ("RORZE", "乐孜芯创"), ("Brooks Automation", "Brooks自动化"),
        ("Daifuku", "大福"), ("Murata Machinery", "村田机械"),
        ("Hirata", "平田"), ("Genmark Automation", "Genmark"),
        ("Nidec", "日本电产"),
        ("SINFONIA Technology", "昕芙旎雅"),
        ("Kensington", "Kensington"), ("JEL", "JEL"),
        ("Cymechs", "Cymechs"),
        ("Guona Semiconductor", "果纳半导体"),
        ("果纳半导体", "果纳半导体"),
        ("Mifei Technology", "弥费科技"),
        ("弥费科技", "弥费科技"),
        ("SIASUN", "新松机器人"),
        ("新松机器人", "新松机器人"),
        ("U.S.", "美国"), ("China", "中国"), ("Chinese", "中国"),
        ("Japan", "日本"), ("Japanese", "日本"), ("Korea", "韩国"),
        ("Europe", "欧洲"), ("Taiwan", "台湾"),
        ("semiconductor", "半导体"), ("Semiconductor", "半导体"),
        ("AMHS", "自动物料搬运系统"),
        ("OHT", "天车"), ("overhead hoist", "天车"),
        ("AGV", "AGV"), ("AMR", "自主移动机器人"),
        ("stocker", "自动仓储"), ("Stocker", "自动仓储"),
        ("EFEM", "设备前端模块"),
        ("wafer sorter", "晶圆分选机"), ("Sorter", "分选机"),
        ("load port", "装载端口"), ("Load Port", "装载端口"),
        ("FOUP", "FOUP晶圆盒"),
        ("FOSB", "FOSB晶圆盒"),
        ("SMIF", "SMIF"),
        ("wafer handling", "晶圆传输"),
        ("wafer transfer", "晶圆传送"),
        ("wafer transport", "晶圆搬运"),
        ("wafer robot", "晶圆机器人"),
        ("fab automation", "晶圆厂自动化"),
        ("material handling", "物料搬运"),
        ("material control", "物料控制"),
        ("MCS", "物料控制系统"),
        ("MES", "制造执行系统"),
        ("clean stocker", "洁净仓储"),
        ("purge", "净化"), ("purge system", "净化系统"),
        ("new", "新"), ("New", "新"),
        ("first", "首个"), ("First", "首个"),
        ("largest", "最大"), ("Largest", "最大"),
        ("record", "创纪录"), ("Record", "创纪录"),
        ("breakthrough", "突破"), ("Breakthrough", "突破"),
        ("milestone", "里程碑"), ("Milestone", "里程碑"),
        ("delivery", "交付"), ("Delivery", "交付"),
        ("launch", "推出"), ("Launch", "推出"),
        ("announce", "宣布"), ("Announce", "宣布"),
        ("Announces", "宣布"), ("announces", "宣布"),
        ("Unveils", "发布"), ("unveils", "发布"),
        ("Soars", "飙升"), ("Surges", "暴涨"),
        ("Drops", "下跌"), ("Falls", "下跌"),
        ("Rises", "上涨"), ("Grows", "增长"),
        ("global", "全球"), ("Global", "全球"),
        ("world", "全球"), ("World", "全球"),
        ("market", "市场"), ("Market", "市场"),
        ("revenue", "营收"), ("Revenue", "营收"),
        ("shipment", "出货"), ("Shipment", "出货"),
        ("capacity", "产能"), ("Capacity", "产能"),
        ("manufacturing", "制造"), ("Manufacturing", "制造"),
        ("AI", "AI"), ("artificial intelligence", "AI"),
        ("data center", "数据中心"),
        ("Kospi", "韩国KOSPI指数"),
    ], key=lambda x: -len(x[0]))

    for r in records:
        en = r.title.strip()
        cn = en
        for term, cn_term in _PREPROCESS:
            idx = 0
            while True:
                idx = cn.find(term, idx)
                if idx == -1:
                    break
                before_ok = idx == 0 or not cn[idx - 1].isalnum() and cn[idx - 1] != "'"
                after_ok = (idx + len(term) == len(cn)
                            or not cn[idx + len(term)].isalnum() and cn[idx + len(term)] != "'")
                if before_ok and after_ok:
                    cn = cn[:idx] + cn_term + cn[idx + len(term):]
                    idx += len(cn_term)
                else:
                    idx += 1
        cn = re.sub(r'\s{2,}', ' ', cn).strip()
        r.title_cn = cn if cn != en else ""

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

    dedup = Deduplicator(str(ROOT / "data" / "state.json"))
    new_records, seen = dedup.deduplicate(merged)
    print(f"[Weekly] Dedup: {len(new_records)} new / {seen} already seen")

    if not new_records:
        print("[Weekly] All events already seen this cycle.")
        return

    # Filter + score
    qf = QualityFilter(config)
    scorer = Scorer(config)

    new_records = qf.filter(new_records)
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
    renderer = MarkdownRenderer(str(ROOT / "output"))
    stats = {
        "本周采集": len(records),
        "去重后": len(new_records),
        "新事件": len(new_records),
        "可信度分布": grade_str,
        "独立生态覆盖": _eco_coverage(new_records),
    }
    renderer.render_weekly_report(new_records, deep_analysis=deep_analysis, stats=stats)

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
