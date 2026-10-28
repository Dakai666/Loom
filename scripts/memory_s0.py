"""#650 S0: synthetic recall evaluation and measurements on disposable copies.

No runtime implementation is replaced. Snapshot sources are opened mode=ro;
benchmarking requires our snapshot marker and copies it again before mutation.
Only aggregate statistics leave a private snapshot. Synthetic results may be
committed; never commit snapshots or their source paths.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ROOT / "tests/fixtures/memory_s0_cases.json"
STAMP = "2026-01-15T12:00:00+00:00"


def dump(path: Path, value: object) -> None:
    resolved = path.expanduser().resolve()
    if resolved.suffix != ".json" or (Path.home() / ".loom").resolve() in resolved.parents:
        raise ValueError("JSON reports must be outside live state directories")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def distribution(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(values), "p50": statistics.median(values),
            "p95": ordered[math.ceil(.95 * len(ordered)) - 1],
            "p99": ordered[math.ceil(.99 * len(ordered)) - 1], "max": ordered[-1]}


def snapshot(source: Path, destination: Path) -> None:
    source, destination = source.expanduser().resolve(), destination.resolve()
    if not source.is_file() or destination.exists() or source == destination:
        raise ValueError("snapshot needs an existing source and a NEW destination")
    if (Path.home() / ".loom").resolve() in destination.parents:
        raise ValueError("snapshot destination must be outside the live state directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL also protects against destination aliases/races. Never open a
    # source with SQLiteStore.initialize(), which performs runtime migrations.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        src.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
        assert src.total_changes == 0
    dump(destination.with_suffix(destination.suffix + ".s0.json"),
         {"snapshot": True, "bytes": destination.stat().st_size})


def checked_snapshot(path: Path) -> Path:
    path = path.resolve()
    if (Path.home() / ".loom").resolve() in path.parents:
        raise ValueError("benchmark refuses live state directories")
    marker = path.with_suffix(path.suffix + ".s0.json")
    if not marker.is_file() or json.loads(marker.read_text()) != {
        "snapshot": True, "bytes": path.stat().st_size,
    }:
        raise ValueError("benchmark needs a snapshot created by the snapshot command")
    return path


def create_log_probe(db: sqlite3.Connection, tool_prefix: int) -> None:
    """Experimental DDL ONLY on fixtures/copies; not a proposed migration.

    The external-content view and trigger payloads must use the SAME prefix.
    A direct external-content table + truncated trigger payload breaks
    rebuild/delete consistency. S1 must choose its actual migration in review.
    """
    if not 1 <= tool_prefix <= 1_000_000:
        raise ValueError("invalid prefix")
    expression = f"CASE WHEN role='tool' THEN substr(content,1,{tool_prefix}) ELSE content END"
    db.execute(f"CREATE VIEW s0_log_content AS SELECT id, {expression} AS content "
               "FROM session_log WHERE role IN ('user','assistant','tool')")
    db.execute("CREATE VIRTUAL TABLE s0_log_fts USING fts5(content, "
               "content='s0_log_content', content_rowid='id', tokenize='trigram')")
    for suffix, action, old, new in [
        ("ai", "INSERT", False, True), ("ad", "DELETE", True, False),
        ("au", "UPDATE", True, True),
    ]:
        statements = []
        for alias, deleting in [("old", old), ("new", new)]:
            if not deleting:
                continue
            expr = (f"CASE WHEN {alias}.role='tool' THEN substr({alias}.content,1,{tool_prefix}) "
                    f"ELSE {alias}.content END")
            if alias == "old":
                sql = ("INSERT INTO s0_log_fts(s0_log_fts,rowid,content) "
                       f"SELECT 'delete',{alias}.id,{expr}")
            else:
                sql = f"INSERT INTO s0_log_fts(rowid,content) SELECT {alias}.id,{expr}"
            statements.append(sql + f" WHERE {alias}.role IN ('user','assistant','tool');")
        db.executescript(f"CREATE TRIGGER s0_log_{suffix} AFTER {action} ON session_log "
                         "BEGIN " + " ".join(statements) + " END;")


def evaluate(case: dict, results: list[dict]) -> dict:
    """Score SOURCE+visible evidence, not topical similarity or familiarity."""
    expected = case["expected"]
    hits = {gold["ref"] for gold in expected for result in results[:5]
            if gold["ref"] == result["ref"]
            and all(fragment in result["excerpt"] for fragment in gold["contains"])}
    return {"hit_at_5": bool(hits) if expected else None,
            "all_sources_located": bool(expected) and len(hits) == len(expected),
            "empty": not results,
            "false_positive": not expected and bool(results)}


def summarize(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["evaluation"]["hit_at_5"] is not None]
    negatives = [r for r in rows if r["evaluation"]["hit_at_5"] is None]
    return {"positive_cases": len(positives), "negative_cases": len(negatives),
            "hit_at_5": sum(r["evaluation"]["hit_at_5"] for r in positives) / max(1, len(positives)),
            "all_sources_located_rate": sum(r["evaluation"]["all_sources_located"] for r in positives) / max(1, len(positives)),
            "false_positive_rate": sum(r["evaluation"]["false_positive"] for r in negatives) / max(1, len(negatives))}


class ReplayEmbeddings:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors[text] for text in texts]


async def build_fixture(path: Path, fixture: dict, provider=None):
    from loom.core.memory.store import SQLiteStore
    from loom.core.memory.semantic import SemanticMemory, SemanticEntry
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    store = SQLiteStore(str(path))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db, embedding_provider=provider)
        procedural = ProceduralMemory(db)
        for row in fixture["semantic"]:
            await semantic.upsert(SemanticEntry(**row))
        for row in fixture["skills"]:
            await procedural.upsert(SkillGenome(**row))
        for row in fixture["messages"]:
            await db.execute("INSERT INTO session_log "
                             "(session_id,turn_index,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)",
                             (row["session_id"], row["turn_index"], row["role"], row["content"],
                              json.dumps(row.get("metadata", {})), row.get("created_at", STAMP)))
        # Stable dates and recency ordering, independent of the machine clock.
        await db.execute("UPDATE semantic_entries SET created_at=?,updated_at=?", (STAMP, STAMP))
        await db.execute("UPDATE skill_genomes SET created_at=?,updated_at=?", (STAMP, STAMP))
        await db.commit()
    return store


async def synthetic(output: Path, base_url: str | None, model: str, replay: Path | None = None) -> dict:
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.procedural import ProceduralMemory
    fixture = json.loads(CASES.read_text())
    provider = None
    cache = {}
    texts = list(dict.fromkeys([f'{r["key"]} {r["value"]}' for r in fixture["semantic"]]
                              + [c["query"] for c in fixture["cases"]]))
    if replay:
        with gzip.open(replay, "rt") as stream:
            frozen = json.load(stream)
        if frozen["cases_sha256"] != hashlib.sha256(CASES.read_bytes()).hexdigest() or set(frozen["vectors"]) != set(texts):
            raise ValueError("replay vectors must match the entire synthetic fixture")
        cache, model = frozen["vectors"], frozen["model"]
        provider = ReplayEmbeddings(cache)
    if base_url:
        from loom.core.memory.embeddings import OllamaEmbeddingProvider
        actual = OllamaEmbeddingProvider(base_url=base_url, model=model)
        try:
            # Single-query shape matches production, with no repeated calls
            # during metric calculation. Latency here is NOT end-to-end.
            for text in texts:
                cache[text] = (await actual.embed([text]))[0]
        finally:
            await actual.close()
        provider = ReplayEmbeddings(cache)
    with tempfile.TemporaryDirectory(prefix="memory-s0-fixture-") as directory:
        store = await build_fixture(Path(directory) / "fixture.db", fixture, provider)
        rows = []
        async with store.connect() as db:
            search = MemorySearch(SemanticMemory(db, embedding_provider=provider), ProceduralMemory(db))
            for case in fixture["cases"]:
                kwargs = dict(case.get("args", {}))
                for name in ("since", "until"):
                    if name in kwargs:
                        kwargs[name] = datetime.fromisoformat(kwargs[name])
                found = await search.recall(case["query"], limit=5, **kwargs)
                normalized = [{"ref": ("skill:" if r.type == "skill" else "mem:") + r.key,
                               "excerpt": r.value[:300], "score": round(r.score, 7),
                               "fallback": bool(r.metadata.get("fallback")),
                               "method": r.metadata.get("method", "bm25" if r.score else "recency")}
                              for r in found]
                rows.append({"id": case["id"], "split": case["split"], "stage": case["stage"],
                             "query": case["query"], "args": case.get("args", {}),
                             "results": normalized, "evaluation": evaluate(case, normalized)})
    result = {"profile": "ollama_replay" if cache else "lexical_no_provider",
              "model": model if cache else None,
              "cases_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
              "python_sqlite": sqlite3.sqlite_version,
              "summary": summarize(rows),
              "by_stage": {stage: summarize([r for r in rows if r["stage"] == stage]) for stage in ("S1", "S2")},
              "by_split": {split: summarize([r for r in rows if r["split"] == split]) for split in ("development", "holdout")},
              "cases": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    dump(output, result)
    if cache and not replay:
        dump(output.with_suffix(".vectors.json"), {"model": model,
             "cases_sha256": result["cases_sha256"], "vectors": cache})
    return result


def index_size(db: sqlite3.Connection, table: str) -> int:
    # table is a local constant, not user input. dbstat counts real pages,
    # unlike whole-file growth which is affected by free-list reuse.
    return db.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name GLOB ?",
                      (table + "_*",)).fetchone()[0]


async def literal_probe(output: Path) -> dict:
    """Feasibility measurement of literal trigram MATCH, NOT an S1 retriever.

    Development cases only: do not tune thresholds/rewrites on holdout.
    This exposes semantic paraphrase gaps in a literal-only proposal.
    """
    from loom.core.memory.search import _sanitize_fts
    fixture = json.loads(CASES.read_text())
    rows = []
    with tempfile.TemporaryDirectory(prefix="memory-s0-literal-") as directory:
        path = Path(directory) / "fixture.db"
        await build_fixture(path, fixture)
        with sqlite3.connect(path) as db:
            create_log_probe(db, 512)
            db.execute("INSERT INTO s0_log_fts(s0_log_fts) VALUES('rebuild')")
            for case in fixture["cases"]:
                if (case["split"] != "development" or case["stage"] != "S1"
                        or not any(g["ref"].startswith("log:") for g in case["expected"])):
                    continue
                query = case["query"]
                filters=[]; time_params=[]
                for axis, operator in (("since",">="),("until","<")):
                    if axis in case.get("args",{}):
                        filters.append(f"created_at {operator} ?")
                        time_params.append(case["args"][axis])
                if len(query) < 3:
                    scope="".join(" AND "+f for f in filters)
                    found = db.execute("SELECT session_id,turn_index,content FROM session_log "
                                       f"WHERE content LIKE ?{scope} ORDER BY id DESC LIMIT 100", (f"%{query}%",*time_params)).fetchall()
                else:
                    scope="".join(" AND l."+f for f in filters)
                    found = db.execute("SELECT l.session_id,l.turn_index,l.content FROM s0_log_fts "
                                       "JOIN session_log l ON l.id=s0_log_fts.rowid "
                                       f"WHERE s0_log_fts MATCH ?{scope} ORDER BY rank LIMIT 100", (_sanitize_fts(query),*time_params)).fetchall()
                seen = set()
                results = []
                for sid, turn, text in found:
                    ref = f"log:{sid}:{turn}"
                    if ref not in seen:
                        results.append({"ref": ref, "excerpt": text[:300]})
                        seen.add(ref)
                rows.append({"id": case["id"], "query": query,
                             "has_log_gold": any(g["ref"].startswith("log:") for g in case["expected"]),
                             "results": results[:5], "evaluation": evaluate(case,results)})
    value={"scope":"development S1 cases; literal log MATCH/LIKE only, no semantic or skill routes, no query rewrite", "cases": rows}
    output.parent.mkdir(parents=True,exist_ok=True)
    dump(output,value)
    return value


async def benchmark(path: Path, output: Path, base_url: str | None, model: str, repeats: int) -> dict:
    from loom.core.memory.store import SQLiteStore
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.embeddings import OllamaEmbeddingProvider
    checked_snapshot(path)
    with tempfile.TemporaryDirectory(prefix="memory-s0-bench-") as directory:
        copy = Path(directory) / "copy.db"
        snapshot(path, copy)
        db = sqlite3.connect(copy)
        metadata_keys = db.execute("SELECT j.key,COUNT(*) FROM semantic_entries e,json_each(e.metadata) j "
                                   "GROUP BY j.key ORDER BY j.key").fetchall()
        total = db.execute("SELECT COUNT(*) FROM semantic_entries").fetchone()[0]
        live_where = "json_extract(metadata,'$.redirected_to') IS NULL"
        live = db.execute("SELECT COUNT(*) FROM semantic_entries WHERE " + live_where).fetchone()[0]
        # Report actual schema/metadata, rather than inventing a superseded flag.
        supersede_fields = [k for k, _ in metadata_keys if "supersed" in k.lower()]
        lengths = [r[0] for r in db.execute("SELECT length(content) FROM session_log WHERE role='tool'")]
        result = {"python_sqlite": sqlite3.sqlite_version, "python": platform.python_version(),
                  "semantic_rows": {"total": total, "non_redirect": live,
                    "redirect_stubs": total-live, "supersede_metadata_fields": supersede_fields,
                    "sources_total": db.execute("SELECT CASE WHEN source LIKE 'session:%' THEN 'session' "
                      "WHEN source IN ('dreaming','memorize','manual','user','agent','self_reflection') THEN source "
                      "WHEN source LIKE 'counter_factual:%' THEN 'counter_factual' "
                      "WHEN source LIKE 'task_reflector:%' THEN 'task_reflector' ELSE 'other' END,COUNT(*) FROM semantic_entries GROUP BY 1").fetchall(),
                    "sources_non_redirect": db.execute("SELECT CASE WHEN source LIKE 'session:%' THEN 'session' "
                      "WHEN source IN ('dreaming','memorize','manual','user','agent','self_reflection') THEN source "
                      "WHEN source LIKE 'counter_factual:%' THEN 'counter_factual' "
                      "WHEN source LIKE 'task_reflector:%' THEN 'task_reflector' ELSE 'other' END,COUNT(*) FROM semantic_entries WHERE " + live_where + " GROUP BY 1").fetchall()},
                  "session_log": {"roles": db.execute("SELECT role,COUNT(*),SUM(length(content)),SUM(length(CAST(content AS BLOB))) FROM session_log GROUP BY role").fetchall(),
                                  "tool_chars": distribution(lengths)}, "indexes": {}}
        result["semantic_rows"]["history_rows"] = db.execute(
            "SELECT COUNT(*) FROM semantic_entries WHERE json_array_length(metadata,'$.history')>0").fetchone()[0]
        result["session_log"]["tool_raw_json_rows"] = db.execute(
            "SELECT COUNT(*) FROM session_log WHERE role='tool' AND raw_json IS NOT NULL").fetchone()[0]
        result["session_log"]["tool_action_join"] = list(db.execute(
            "SELECT COUNT(*),SUM(EXISTS(SELECT 1 FROM action_records a "
            "WHERE a.call_id=json_extract(l.metadata,'$.tool_call_id') AND a.session_id=l.session_id)) "
            "FROM session_log l WHERE l.role='tool'").fetchone())
        result["session_log"]["rows_per_turn"] = distribution([r[0] for r in db.execute(
            "SELECT COUNT(*) FROM session_log GROUP BY session_id,turn_index")])
        result["session_log"]["recall_calls"] = db.execute(
            "SELECT json_extract(metadata,'$.tool_name'),COUNT(*),"
            "COUNT(DISTINCT json_extract(metadata,'$.tool_call_id')) FROM session_log WHERE role='tool' "
            "AND json_extract(metadata,'$.tool_name') IN ('recall','recall_period','ledger_recall') GROUP BY 1").fetchall()
        outputs = [r[0] for r in db.execute("SELECT output_len FROM action_records WHERE output_len IS NOT NULL")]
        result["action_capture_proxy"] = {"output_chars":distribution(outputs),
            "full_row_fraction_at_prefix": {str(n):sum(size<=n for size in outputs)/max(1,len(outputs))
                                            for n in (512,1024,4096,8192,16384)},
            "note":"recent action output_len subset only; not full persisted session tool content"}
        # A role-safe computed external-content view ensures prefix consistency.
        for prefix in (512, 4096, 16384):
            start = time.perf_counter()
            create_log_probe(db, prefix)
            db.execute("INSERT INTO s0_log_fts(s0_log_fts) VALUES('rebuild')")
            db.commit()
            result["indexes"][str(prefix)] = {"build_ms": (time.perf_counter()-start)*1000,
                "bytes": index_size(db, "s0_log_fts"),
                "tool_chars_retained_fraction": sum(min(n,prefix) for n in lengths)/max(1,sum(lengths)),
                "tools_with_full_content_fraction": sum(n<=prefix for n in lengths)/max(1,len(lengths))}
            for suffix in ("ai", "au", "ad"):
                db.execute(f"DROP TRIGGER s0_log_{suffix}")
            db.execute("DROP TABLE s0_log_fts")
            db.execute("DROP VIEW s0_log_content")
        start = time.perf_counter()
        db.execute("CREATE VIRTUAL TABLE s0_semantic_fts USING fts5(key,value, content='semantic_entries',content_rowid='rowid',tokenize='trigram')")
        db.execute("INSERT INTO s0_semantic_fts(s0_semantic_fts) VALUES('rebuild')")
        db.commit()
        result["semantic_trigram"] = {"build_ms": (time.perf_counter()-start)*1000,
            "bytes": index_size(db,"s0_semantic_fts"), "existing_unicode61_bytes": index_size(db,"semantic_fts")}
        # Explicit leading wildcard: measure actual full scans, including misses.
        scans = {}
        for word in ("放下", "記憶", "測試", "霧鉛"):
            timings=[]
            plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM session_log WHERE role IN ('user','assistant','tool') AND content LIKE ? ORDER BY id DESC LIMIT 40", (f"%{word}%",)).fetchall()
            for _ in range(repeats):
                start=time.perf_counter()
                count=len(db.execute("SELECT id FROM session_log WHERE role IN ('user','assistant','tool') AND content LIKE ? ORDER BY id DESC LIMIT 40", (f"%{word}%",)).fetchall())
                timings.append((time.perf_counter()-start)*1000)
            scans[word]={"ms":distribution(timings),"result_count":count,"plan":[r[3] for r in plan]}
        result["two_char_like"]=scans
        db.close()
        provider = OllamaEmbeddingProvider(base_url=base_url,model=model) if base_url else None
        timings=[]; output_lengths=[]
        # Public synthetic queries only. Do not emit private keys/excerpts.
        queries=["香蕉料理食譜與烘焙溫度換算", "drain-supersede", "記憶", "放下"]
        try:
            async with SQLiteStore(str(copy)).connect() as connection:
                search=MemorySearch(SemanticMemory(connection,embedding_provider=provider), ProceduralMemory(connection))
                for query in queries:
                    await search.recall(query,limit=5)  # warm-up, excluded
                    for _ in range(max(3,repeats//4)):
                        start=time.perf_counter()
                        found=await search.recall(query,limit=5)
                        text="\n\n".join(r.format() for r in found)
                        timings.append((time.perf_counter()-start)*1000)
                        output_lengths.append(len(text))
        finally:
            if provider:
                await provider.close()
        result["current_recall"]={"profile":"ollama_end_to_end" if provider else "lexical_no_provider",
            "warm":True,"queries":len(queries),"limit":5,"latency_ms":distribution(timings),
            "output_chars":distribution(output_lengths), "scope":"MemorySearch.recall plus result formatting; no agent turn/ledger emit"}
    output.parent.mkdir(parents=True,exist_ok=True)
    dump(output,result)
    return result


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    snap=sub.add_parser("snapshot")
    snap.add_argument("--source",type=Path,required=True)
    snap.add_argument("--destination",type=Path,required=True)
    probe=sub.add_parser("probe")
    probe.add_argument("--output",type=Path,required=True)
    for name in ("synthetic","benchmark"):
        child=sub.add_parser(name)
        child.add_argument("--output",type=Path,required=True)
        profile=child.add_mutually_exclusive_group()
        profile.add_argument("--ollama-url")
        child.add_argument("--model",default="qwen3-embedding:0.6b")
        if name=="benchmark":
            child.add_argument("--snapshot",type=Path,required=True)
            child.add_argument("--repeats",type=int,default=31)
        else:
            profile.add_argument("--replay",type=Path)
    args=parser.parse_args()
    if args.command=="snapshot":
        snapshot(args.source,args.destination)
        print("Read-only backup complete; no source content printed.")
    elif args.command=="probe":
        value=asyncio.run(literal_probe(args.output))
        print(json.dumps({"cases":len(value["cases"]),"scope":value["scope"]}))
    elif args.command=="synthetic":
        value=asyncio.run(synthetic(args.output,args.ollama_url,args.model,args.replay))
        print(json.dumps(value["summary"]))
    else:
        if args.repeats<3:
            parser.error("--repeats must be at least 3")
        value=asyncio.run(benchmark(args.snapshot,args.output,args.ollama_url,args.model,args.repeats))
        print(json.dumps(value["current_recall"]))


if __name__ == "__main__":
    main()
