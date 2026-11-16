"""Compare vector and hybrid retrieval on the same versioned demo corpus."""

import argparse
import json
import math
from contextlib import closing
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from chromadb.api.types import EmbeddingFunction

from backend.rag.rag_knowledge import RAGKnowledge
from backend.rag.review_rule_demo import DEMO_RULES, seed_review_rules


# Columns are query, expected chunk, and the closest chunk under fixed embeddings.
# The fixed vectors deliberately put similar clauses ahead of two exact versions.
QUESTIONS = (
    ("ctr-review-v1#low-ctr", 0, 1),
    ("ctr-review-v2#low-ctr", 1, 0),
    ("计划曝光不足时，点击率需要多少样本量才能比较？", 2, 2),
    ("比较两个窗口，怎样对齐天数并计算窗口 CTR？", 3, 3),
    ("触达频次升高，点击率持续下滑，怎样排查素材疲劳？", 4, 4),
    ("平均点击成本 CPC 突然上升，先检查什么？", 5, 5),
    ("日预算提前耗尽，怎么核对预算消耗速度？", 6, 6),
    ("转化骤降怎么检查转化事件上报和归因窗口？", 7, 7),
)


class FixedReviewEmbedding(EmbeddingFunction):
    def __init__(self):
        self._positions = {rule[-1]: i for i, rule in enumerate(DEMO_RULES)}
        self._positions.update({query: closest for query, _, closest in QUESTIONS})

    @staticmethod
    def name():
        return "fixed-review-embedding"

    @staticmethod
    def build_from_config(config):
        return FixedReviewEmbedding()

    def get_config(self):
        return {}

    def __call__(self, input):
        angles = [0.25 * self._positions.get(text, 1) for text in input]
        return [[math.cos(angle), math.sin(angle)] for angle in angles]


def compare(rag, repetitions):
    modes = {"vector": rag.search, "hybrid": rag.hybrid_search}
    latencies = {mode: [] for mode in modes}
    ranks = {mode: {} for mode in modes}
    for query, _, _ in QUESTIONS:
        for search in modes.values():
            search(query, top_k=5)
    for repetition in range(repetitions):
        mode_order = list(modes) if repetition % 2 == 0 else list(reversed(modes))
        for query, expected, _ in QUESTIONS:
            chunk_id = DEMO_RULES[expected][0]
            for mode in mode_order:
                start = perf_counter()
                hits = modes[mode](query, top_k=5)
                latencies[mode].append((perf_counter() - start) * 1000)
                ranks[mode][query] = next((i for i, hit in enumerate(hits, 1) if hit["chunk_id"] == chunk_id), None)
    return {
        mode: {
            **{f"Hit@{k}": sum(rank is not None and rank <= k for rank in ranks[mode].values()) / len(QUESTIONS)
               for k in (1, 3, 5)},
            "p50_ms": round(median(latencies[mode]), 3),
            "p95_ms": round(sorted(latencies[mode])[math.ceil(0.95 * len(latencies[mode])) - 1], 3),
            "ranks": ranks[mode],
        }
        for mode in modes
    }


def evaluate(embedding, repetitions):
    embedding_fn = FixedReviewEmbedding() if embedding == "fixed" else None
    with TemporaryDirectory() as path, closing(RAGKnowledge(db_path=path, embedding_fn=embedding_fn)) as rag:
        seed_review_rules(rag)
        results = compare(rag, repetitions)
    return {"embedding": embedding, "queries": len(QUESTIONS), "repetitions": repetitions, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding", choices=("fixed", "bge"), required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    print(json.dumps(evaluate(args.embedding, args.repetitions), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
