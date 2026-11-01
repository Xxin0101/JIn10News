from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import RunStats


def setup_logger(log_dir: Path, run_id: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"jin10_report_{run_id}.log"
    logger = logging.getLogger("jin10_market_report")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger, log_path


def write_summary_json(
    log_dir: Path,
    run_id: str,
    stats: RunStats,
    output_docx: Path | None,
    extra: dict[str, Any] | None = None,
) -> Path:
    summary_path = log_dir / f"jin10_report_summary_{run_id}.json"
    payload = {
        "fetch_started_at": stats.fetch_started_at.isoformat(),
        "fetch_finished_at": stats.fetch_finished_at.isoformat() if stats.fetch_finished_at else None,
        "fetched_total": stats.fetched_total,
        "within_window_total": stats.within_window_total,
        "filtered_total": stats.filtered_total,
        "duplicate_merged_total": stats.duplicate_merged_total,
        "high_priority_total": stats.high_priority_total,
        "pages_fetched": stats.pages_fetched,
        "api_status": stats.api_status,
        "failures": stats.failures,
        "output_docx": str(output_docx) if output_docx else None,
        "written_at": datetime.now().isoformat(),
    }
    if extra:
        payload.update(extra)
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_path
