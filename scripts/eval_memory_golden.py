# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Golden-question retrieval evaluation for the memory RAG.

Measures hit@1, hit@k and mean reciprocal rank of `RagPipeline.search()` (the
same retrieval core the `memory_search` tool uses, including the hybrid lane)
against a set of golden questions with expected memories. Run it BEFORE and
AFTER any retrieval-affecting change (embedding model, chunking, thresholds)
so the change is measured instead of guessed.

This is a developer measuring tool, not a test: it needs a populated live
memory DB and is deliberately not collected by pytest/CI.

The questions file is looked up at ~/.vaf/eval_memory_golden.json by default,
so a set built from a real, personal memory store never enters the repository.
The committed scripts/eval_memory_golden.sample.json documents the format with
synthetic entries only.

Question entry format:
    {
      "q": "the question, in the language a user would ask it",
      "lang": "de",
      "expect_ids": ["ab12cd34"],   # memory-id prefixes; hit if any result matches
      "expect_tags": ["some-tag"]   # OR: hit if a result's tags intersect
    }

Read-only: no writes, no LLM calls (answer generation is out of scope; the
query-refinement step of the tool path is also skipped on purpose so the
retrieval core is measured in isolation).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_QUESTIONS = Path.home() / ".vaf" / "eval_memory_golden.json"
SAMPLE_QUESTIONS = Path(__file__).parent / "eval_memory_golden.sample.json"


def _load_questions(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SystemExit(f"Questions file {path} must be a non-empty JSON list")
    return data


def _is_hit(source, expect_ids, expect_tags) -> bool:
    mid = str(source.memory_id)
    if any(mid.startswith(p) for p in expect_ids):
        return True
    if expect_tags:
        tags = set(t.lower() for t in (source.metadata or {}).get("tags", []) if t)
        if tags.intersection(t.lower() for t in expect_tags):
            return True
    return False


async def _run(questions: list, k: int, threshold: float) -> int:
    from vaf.core.config import Config, get_local_admin_scope_id
    from vaf.memory.database import get_db
    from vaf.memory.rag import RagPipeline

    scope = get_local_admin_scope_id()
    if not scope:
        raise SystemExit("No local admin scope resolved; is this install bootstrapped?")

    model = Config.get("memory_embedding_model", "all-MiniLM-L6-v2")
    print(f"model={model} k={k} threshold={threshold} questions={len(questions)}")
    print("-" * 72)

    per_lang: dict = {}
    total_hit1 = total_hitk = 0
    mrr_sum = 0.0

    async with get_db(str(scope)) as db:
        pipeline = RagPipeline(db)
        for entry in questions:
            q = entry["q"]
            lang = entry.get("lang", "?")
            expect_ids = entry.get("expect_ids", [])
            expect_tags = entry.get("expect_tags", [])
            sources = await pipeline.search(
                q, k=k, threshold=threshold, user_scope_id=scope
            )
            rank = 0
            for i, src in enumerate(sources, start=1):
                if _is_hit(src, expect_ids, expect_tags):
                    rank = i
                    break
            hit1 = rank == 1
            hitk = rank > 0
            total_hit1 += hit1
            total_hitk += hitk
            mrr_sum += (1.0 / rank) if rank else 0.0
            stats = per_lang.setdefault(lang, [0, 0, 0])
            stats[0] += hit1
            stats[1] += hitk
            stats[2] += 1
            mark = f"rank={rank}" if rank else "MISS"
            print(f"[{lang}] {mark:8s} {q[:64]}")

    n = len(questions)
    print("-" * 72)
    print(f"hit@1: {total_hit1}/{n} ({total_hit1 / n:.0%})   "
          f"hit@{k}: {total_hitk}/{n} ({total_hitk / n:.0%})   "
          f"MRR: {mrr_sum / n:.3f}")
    for lang, (h1, hk, cnt) in sorted(per_lang.items()):
        print(f"  {lang}: hit@1 {h1}/{cnt}, hit@{k} {hk}/{cnt}")
    return 0 if total_hitk else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, default=None,
                        help=f"questions JSON (default: {DEFAULT_QUESTIONS}, "
                             "falling back to the committed sample)")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.3,
                        help="similarity threshold, mirrors memory_rag_threshold")
    args = parser.parse_args()

    path = args.questions
    if path is None:
        path = DEFAULT_QUESTIONS if DEFAULT_QUESTIONS.exists() else SAMPLE_QUESTIONS
    questions = _load_questions(path)
    return asyncio.run(_run(questions, args.k, args.threshold))


if __name__ == "__main__":
    sys.exit(main())
