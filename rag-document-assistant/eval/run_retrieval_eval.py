import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from app.database import SessionLocal
from app.demo_seed import DEMO_TENANT_ID
from app.retrieval_service import search_similar_chunks


def load_eval_set(eval_path: str) -> list[dict[str, Any]]:
    path = Path(eval_path)

    if not path.exists():
        raise FileNotFoundError(f"Eval file not found: {eval_path}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("Eval file must contain a JSON list of examples.")

    return data


def parse_page_span(metadata: dict[str, Any]) -> list[int]:
    """
    Parse page_span from PostgreSQL retrieval metadata.
    """

    page_span = metadata.get("page_span")

    if isinstance(page_span, list):
        return [int(page) for page in page_span]

    if isinstance(page_span, str) and page_span.strip():
        return [int(page.strip()) for page in page_span.split(",") if page.strip()]

    start_page = metadata.get("start_page")
    end_page = metadata.get("end_page")

    if start_page is not None and end_page is not None:
        return list(range(int(start_page), int(end_page) + 1))

    return []


def pages_overlap(retrieved_pages: list[int], gold_pages: list[int]) -> bool:
    return bool(set(retrieved_pages) & set(gold_pages))


def document_matches(
    retrieved_metadata: dict[str, Any],
    gold_document_id: str | None,
    gold_document_name: str | None,
) -> bool:
    """
    Require a match if gold document identifiers are provided.
    If not provided, only page overlap is used.
    """
    if gold_document_id:
        return retrieved_metadata.get("document_id") == gold_document_id

    if gold_document_name:
        return retrieved_metadata.get("document_name") == gold_document_name

    return True


def is_relevant_chunk(
    retrieved_chunk: dict[str, Any],
    gold_pages: list[int],
    gold_document_id: str | None,
    gold_document_name: str | None,
) -> bool:
    metadata = retrieved_chunk["metadata"]
    retrieved_pages = parse_page_span(metadata)

    return (
        document_matches(
            metadata,
            gold_document_id,
            gold_document_name,
        )
        and pages_overlap(retrieved_pages, gold_pages)
    )


def evaluate_example(
    example: dict[str, Any],
    session,
    tenant_id: uuid.UUID,
    top_k: int,
    max_distance: float,
    document_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    
    question = example["question"]
    should_answer = bool(example.get("should_answer", True))
    gold_pages = example.get("gold_pages", [])
    gold_document_name = example.get("gold_document_name")
    gold_document_id = example.get("gold_document_id")

    retrieved_chunks = search_similar_chunks(
        session,
        query=question,
        tenant_id=tenant_id,
        top_k=top_k,
        document_id=document_id,
        max_distance=max_distance,
    )

    retrieved_page_spans = [
        parse_page_span(chunk["metadata"]) for chunk in retrieved_chunks
    ]

    if should_answer:
        relevant_chunks = [chunk
        for chunk in retrieved_chunks
        if is_relevant_chunk(chunk, gold_pages, gold_document_id, gold_document_name,)
        ]

        retrieval_hit = len(relevant_chunks) > 0

        source_precision = (
            len(relevant_chunks) / len(retrieved_chunks)
            if relevant_chunks
            else 0.0
        )

        refusal_retrieval_correct = None
    
    else:
        relevant_chunks = []
        retrieval_hit = None
        source_precision = None

        # If the question is unanswerable, the RAG should ideally return no retrieved chunk
        refusal_retrieval_correct = len(retrieved_chunks) == 0

    return {
        "question": question,
        "should_answer": should_answer,
        "gold_document_id": gold_document_id,
        "gold_document_name": gold_document_name,
        "gold_pages": gold_pages,
        "num_retrieved": len(retrieved_chunks),
        "retrieved_page_spans": retrieved_page_spans,
        "retrieval_hit": retrieval_hit,
        "source_precision": source_precision,
        "refusal_retrieval_correct": refusal_retrieval_correct,
        "retrieved_sources": [
            {
                "document_name": chunk["metadata"].get("document_name"),
                "document_id": chunk["metadata"].get("document_id"),
                "chunk_index": chunk["metadata"].get("chunk_index"),
                "page_span": parse_page_span(chunk["metadata"]),
                "distance": chunk["distance"],
                "text_preview": chunk["text"][:160],
            }
            for chunk in retrieved_chunks
        ],
    }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    answerable_results = [
        result
        for result in results
        if result["should_answer"]
    ]

    unanswerable_results = [
        result
        for result in results
        if not result["should_answer"]
    ]

    retrieval_hits = [
        result["retrieval_hit"]
        for result in answerable_results
        if result["retrieval_hit"] is not None
    ]

    source_precisions = [
        result["source_precision"]
        for result in answerable_results
        if result["source_precision"] is not None
    ]

    refusal_retrieval_corrects = [
        result["refusal_retrieval_correct"]
        for result in  unanswerable_results
        if result["refusal_retrieval_correct"] is not None
    ]

    retrieval_hit_rate = (
        sum(retrieval_hits) / len(retrieval_hits)
        if retrieval_hits
        else None
    )

    mean_source_precision = (
        sum(source_precisions) / len(source_precisions)
        if source_precisions
        else None
    )

    refusal_retrieval_accuracy = (
        sum(refusal_retrieval_corrects) / len(refusal_retrieval_corrects)
        if refusal_retrieval_corrects
        else None
    )

    return {
        "number_examples": len(results),
        "num_answerables": len(answerable_results),
        "num_unanserables": len(unanswerable_results),
        "retrieval_hit_rate": retrieval_hit_rate,
        "mean_source_precision": mean_source_precision,
        "refusal_retrieval_accuracy": refusal_retrieval_accuracy,
    }


def print_example_result(index: int, result: dict[str, Any]) -> None:
    print("=" * 80)
    print(f"Example {index}")
    print(f"Question: {result['question']}")
    print(f"Should answer: {result['should_answer']}")

    if result["should_answer"]:
        status = "HIT" if result["retrieval_hit"] else "MISS"
        print(f"Retrieval result: {status}")
        print(f"Gold pages: {result['gold_pages']}")
        print(f"retrieved_page_spans: {result['retrieved_page_spans']}")
        print(f"source_precision: {result['source_precision']:.3f}")

    else:
        status = (
            "CORRECT REFUSAL RETRIEVAL"
            if result["refusal_retrieval_correct"]
            else "POSSIBLE FALSE EVIDENCE"
        )
        print(f"Retrieval result: {status}")
        print(f"Number of retrieved: {result['num_retrieved']}")

    print("\nTop retrieved sources")

    for source in result["retrieved_sources"]:
        print(
            f"-{source['document_name']} | "
            f"chunk {source['chunk_index']} | "
            f"pages {source['page_span']} | "
            f"distance {source['distance']:.4f}"           
        )
        print(f"  Preview: {source['text_preview']!r}")


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "#" * 80)
    print("Aggregate retrieval evaluation")
    print("#" * 80)
    print(f"Number of total examples: {summary['number_examples']}")
    print(f"Answerable examples: {summary['num_answerables']}")
    print(f"Unsnaswerable examples: {summary['num_unanserables']}")

    if summary["retrieval_hit_rate"] is not None:
        print(f"retrieval hit rate: {summary['retrieval_hit_rate']:.3f}")
    else:
        print(f"retrieval hit rate: N/A")

    if summary["mean_source_precision"] is not None:
        print(f"Mean source precision: {summary['mean_source_precision']:.3f}")
    else:
        print(f"Mean source precision: N/A")

    
    if summary["refusal_retrieval_accuracy"] is not None:
        print(f"Refusal retrieval accuracy: {summary['refusal_retrieval_accuracy']:.3f}")
    else:
        print(f"Refusal retrieval accuracy: N/A")


def save_results(
    output_path: str,
    results: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    output = {
        "summary": summary,
        "results": results
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(output, file, indent=2, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run offline retrieval evaluation for the RAG system."
    )

    parser.add_argument(
        "--eval-file",
        default="eval/sample_eval_set.json",
        help="Path to eval JSON file.",
    )

    parser.add_argument(
        "--tenant-id",
        default=str(DEMO_TENANT_ID),
        help="Tenant UUID whose ingested documents should be searched.",
    )

    parser.add_argument(
        "--document-id",
        default=None,
        help="Optional document_id to restrict evaluation to one document.",
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of chunks to retrieve per question.",
    )

    parser.add_argument(
        "--max-distance",
        type=float,
        default=0.8,
        help="Maximum pgvector cosine distance allowed for retrieved chunks.",
    )

    parser.add_argument(
        "--output-file",
        default="eval/retrieval_eval_results.json",
        help="Where to save detailed eval results.",
    )

    args = parser.parse_args()

    eval_examples = load_eval_set(args.eval_file)

    tenant_id = uuid.UUID(args.tenant_id)
    document_id = uuid.UUID(args.document_id) if args.document_id else None
    with SessionLocal() as session:
        results = [
            evaluate_example(
                example=example,
                session=session,
                tenant_id=tenant_id,
                top_k=args.top_k,
                max_distance=args.max_distance,
                document_id=document_id,
            )
            for example in eval_examples
        ]

    for index, result in enumerate(results, start=1):
        print_example_result(index, result)

    summary = summarize_results(results)
    print_summary(summary)

    save_results(
        output_path=args.output_file,
        results=results,
        summary=summary,
    )

    print(f"\nSaved detailed results to: {args.output_file}")


if __name__ == "__main__":
    main()
