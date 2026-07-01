"""Evaluate AI helpdesk triage predictions against the golden dataset.

The script accepts an optional JSONL predictions file with one row per ticket:
{"id": "T001", "category": "technical", "priority": "3", "team": "Technical Support", "confidence": 0.91}

Without predictions, it runs a deterministic keyword baseline so CI and portfolio
reviewers can exercise the harness without live Anthropic credentials.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLDEN = ROOT / "data" / "eval" / "golden.jsonl"
DEFAULT_OUTPUT = ROOT / "docs" / "eval"
CATEGORIES = ["technical", "billing", "general", "feature_request"]


def load_jsonl(path: Path) -> list[dict]:
    """Load non-empty JSONL rows from path."""
    rows = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"Invalid JSONL at {path}:{line_number}: {exc}",
                ) from exc
    return rows


def keyword_baseline(ticket: dict) -> dict:
    """Predict labels with transparent rules for offline harness execution."""
    text = f"{ticket['subject']} {ticket['description']}".lower()
    billing_terms = {
        "invoice",
        "billing",
        "billed",
        "refund",
        "payment",
        "card",
        "vat",
        "tax",
        "quote",
        "subscription",
        "currency",
        "purchase order",
        "po number",
        "seat",
        "ach",
        "discount",
    }
    technical_terms = {
        "500",
        "api",
        "timeout",
        "error",
        "fail",
        "fails",
        "invalid",
        "stuck",
        "crash",
        "webhook",
        "migration",
        "upload",
        "index",
        "sso",
        "oauth",
        "token",
        "two-factor",
        "emails not delivered",
        "slowly",
        "incomplete",
        "401",
    }
    feature_terms = {
        "feature request",
        "request dark mode",
        "webhook retry",
        "weekly summary",
        "custom roles",
        "microsoft teams alerts",
        "audit log export api",
        "approval workflow",
        "notification schedules",
        "bulk archive",
        "public status page",
        "custom dashboard",
        "jira integration",
        "multilingual templates",
        "per-user timezone",
        "scheduled exports",
        "status page",
        "quiet hours",
    }

    if any(term in text for term in billing_terms):
        category = "billing"
        team = "Billing Support"
        confidence = 0.86
    elif any(term in text for term in feature_terms):
        category = "feature_request"
        team = "Customer Success"
        confidence = 0.78
    elif any(term in text for term in technical_terms):
        category = "technical"
        team = "Technical Support"
        confidence = 0.84
    else:
        category = "general"
        team = "Customer Success"
        confidence = 0.72

    urgent_terms = {
        "all users",
        "production",
        "blocked",
        "outage",
        "cannot",
        "immediately",
    }
    high_terms = {"duplicate", "declined", "wrong", "delayed", "failed", "missing"}
    if any(term in text for term in urgent_terms):
        priority = "3"
        confidence = min(0.95, confidence + 0.05)
    elif any(term in text for term in high_terms):
        priority = "2"
    elif category in {"feature_request", "general"} and "asks" in text:
        priority = "0"
    else:
        priority = "1"

    return {
        "id": ticket["id"],
        "category": category,
        "priority": priority,
        "team": team,
        "confidence": confidence,
    }


def load_predictions(golden: list[dict], predictions_path: Path | None) -> list[dict]:
    """Return predictions aligned to the golden row order."""
    if not predictions_path:
        return [keyword_baseline(row) for row in golden]
    predictions_by_id = {row["id"]: row for row in load_jsonl(predictions_path)}
    missing = [row["id"] for row in golden if row["id"] not in predictions_by_id]
    if missing:
        raise SystemExit(
            f"Predictions missing {len(missing)} ids: {', '.join(missing[:10])}",
        )
    return [predictions_by_id[row["id"]] for row in golden]


def safe_divide(numerator: float, denominator: float) -> float:
    """Divide while returning zero for empty denominators."""
    return numerator / denominator if denominator else 0.0


def compute_metrics(golden: list[dict], predictions: list[dict]) -> dict:
    """Compute accuracy, per-category precision/recall, and calibration bins."""
    total = len(golden)
    category_correct = 0
    route_correct = 0
    priority_correct = 0
    labels = {category: {"tp": 0, "fp": 0, "fn": 0} for category in CATEGORIES}
    calibration = defaultdict(lambda: {"count": 0, "confidence": 0.0, "correct": 0})

    for expected, predicted in zip(golden, predictions, strict=True):
        category_match = expected["category"] == predicted.get("category")
        route_match = expected["team"] == predicted.get("team")
        priority_match = expected["priority"] == str(predicted.get("priority"))
        category_correct += int(category_match)
        route_correct += int(route_match)
        priority_correct += int(priority_match)

        for category in CATEGORIES:
            expected_category = expected["category"] == category
            predicted_category = predicted.get("category") == category
            labels[category]["tp"] += int(expected_category and predicted_category)
            labels[category]["fp"] += int(not expected_category and predicted_category)
            labels[category]["fn"] += int(expected_category and not predicted_category)

        confidence = float(predicted.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        bucket = min(9, math.floor(confidence * 10))
        bucket_label = f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}"
        calibration[bucket_label]["count"] += 1
        calibration[bucket_label]["confidence"] += confidence
        calibration[bucket_label]["correct"] += int(category_match)

    per_category = {}
    for category, counts in labels.items():
        precision = safe_divide(counts["tp"], counts["tp"] + counts["fp"])
        recall = safe_divide(counts["tp"], counts["tp"] + counts["fn"])
        per_category[category] = {
            "precision": precision,
            "recall": recall,
            "support": counts["tp"] + counts["fn"],
        }

    calibration_rows = []
    for bucket, values in sorted(calibration.items()):
        count = values["count"]
        calibration_rows.append(
            {
                "bucket": bucket,
                "count": count,
                "avg_confidence": safe_divide(values["confidence"], count),
                "accuracy": safe_divide(values["correct"], count),
            },
        )

    return {
        "total": total,
        "classification_accuracy": safe_divide(category_correct, total),
        "routing_accuracy": safe_divide(route_correct, total),
        "priority_accuracy": safe_divide(priority_correct, total),
        "per_category": per_category,
        "calibration": calibration_rows,
    }


def print_metrics(metrics: dict) -> None:
    """Write compact tables for terminal use."""
    lines = [
        "",
        "Overall Metrics",
        "----------------",
        f"Tickets                 {metrics['total']:>8}",
        f"Classification accuracy {metrics['classification_accuracy']:>8.2%}",
        f"Routing accuracy        {metrics['routing_accuracy']:>8.2%}",
        f"Priority accuracy       {metrics['priority_accuracy']:>8.2%}",
        "",
        "Per-Category Precision/Recall",
        "-----------------------------",
        f"{'Category':<18} {'Precision':>10} {'Recall':>10} {'Support':>8}",
    ]
    for category, values in metrics["per_category"].items():
        lines.append(
            f"{category:<18} {values['precision']:>10.2%} "
            f"{values['recall']:>10.2%} {values['support']:>8}",
        )

    lines.extend(
        [
            "",
            "Confidence Calibration",
            "----------------------",
            f"{'Bucket':<10} {'Count':>6} {'Avg Conf':>10} {'Accuracy':>10}",
        ],
    )
    for row in metrics["calibration"]:
        lines.append(
            f"{row['bucket']:<10} {row['count']:>6} "
            f"{row['avg_confidence']:>10.2%} {row['accuracy']:>10.2%}",
        )
    sys.stdout.write("\n".join(lines) + "\n")


def save_outputs(metrics: dict, output_dir: Path) -> None:
    """Save metrics JSON and a confidence calibration chart."""
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    rows = metrics["calibration"]
    labels = [row["bucket"] for row in rows]
    avg_confidence = [row["avg_confidence"] for row in rows]
    accuracy = [row["accuracy"] for row in rows]
    x_positions = range(len(labels))

    plt.figure(figsize=(9, 5))
    plt.plot(x_positions, avg_confidence, marker="o", label="Average confidence")
    plt.plot(x_positions, accuracy, marker="o", label="Observed accuracy")
    plt.xticks(list(x_positions), labels, rotation=30, ha="right")
    plt.ylim(0, 1)
    plt.ylabel("Rate")
    plt.title("AI Helpdesk Confidence Calibration")
    plt.grid(True, axis="y", alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "confidence_calibration.png", dpi=160)
    plt.close()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Evaluate AI helpdesk triage labels.")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    """Run the evaluation harness."""
    args = parse_args()
    golden = load_jsonl(args.golden)
    predictions = load_predictions(golden, args.predictions)
    predicted_ids = Counter(row["id"] for row in predictions)
    duplicates = [ticket_id for ticket_id, count in predicted_ids.items() if count > 1]
    if duplicates:
        raise SystemExit(f"Duplicate prediction ids: {', '.join(duplicates)}")
    metrics = compute_metrics(golden, predictions)
    print_metrics(metrics)
    save_outputs(metrics, args.output_dir)
    sys.stdout.write(f"\nSaved metrics to {args.output_dir / 'metrics.json'}\n")
    sys.stdout.write(
        f"Saved chart to {args.output_dir / 'confidence_calibration.png'}\n",
    )


if __name__ == "__main__":
    main()
