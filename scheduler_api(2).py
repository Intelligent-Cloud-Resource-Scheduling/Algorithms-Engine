from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Literal, Sequence
from urllib.parse import urlparse

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from src.cloud_scheduler import CloudScheduler
except ImportError:  # pragma: no cover
    from cloud_scheduler import CloudScheduler


logger = logging.getLogger("cloud_scheduler_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Cloud Resource Scheduler API", version="1.0.0")

_TASK_STATUS: Dict[str, str] = {}
_TASK_LOCK = threading.Lock()


class ScheduleRequest(BaseModel):
    request_id: str = Field(..., min_length=1)
    callback_url: str = Field(..., min_length=1)
    jobs: List[Dict[str, Any]]
    vms: List[Dict[str, Any]]
    scheduling_policy: Literal["concurrent", "fcfs"] = "concurrent"


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _set_status(request_id: str, status: str) -> None:
    with _TASK_LOCK:
        _TASK_STATUS[request_id] = status


def _solution_to_batches(scheduler: CloudScheduler, solution: Sequence[int]) -> List[Dict[str, Any]]:
    assignments = scheduler.decode_chromosome(solution)
    batches: List[Dict[str, Any]] = []

    for vm_idx, vm in enumerate(scheduler.vms):
        jobs = assignments.get(vm_idx, [])
        batches.append(
            {
                "vm_id": vm.id,
                "processes": [job.id for job in jobs],
            }
        )
    return batches


def _build_comparison_metrics(ga_metrics: Dict[str, Any], baseline_metrics: Dict[str, Any]) -> Dict[str, Any]:
    ga_total_cost = _safe_float(ga_metrics.get("total_cost"))
    ga_makespan = _safe_float(ga_metrics.get("makespan"))
    baseline_total_cost = _safe_float(baseline_metrics.get("total_cost"))
    baseline_makespan = _safe_float(baseline_metrics.get("makespan"))

    def _better(lhs: float | None, rhs: float | None) -> str:
        if lhs is None or rhs is None:
            return "unknown"
        if lhs < rhs:
            return "ga"
        if lhs > rhs:
            return "baseline"
        return "tie"

    cost_delta = None
    if ga_total_cost is not None and baseline_total_cost is not None:
        cost_delta = ga_total_cost - baseline_total_cost

    makespan_delta = None
    if ga_makespan is not None and baseline_makespan is not None:
        makespan_delta = ga_makespan - baseline_makespan

    return {
        "ga_total_cost": ga_total_cost,
        "baseline_total_cost": baseline_total_cost,
        "ga_makespan": ga_makespan,
        "baseline_makespan": baseline_makespan,
        "cost_delta_ga_minus_baseline": cost_delta,
        "makespan_delta_ga_minus_baseline": makespan_delta,
        "better_total_cost": _better(ga_total_cost, baseline_total_cost),
        "better_makespan": _better(ga_makespan, baseline_makespan),
    }


def _post_callback(callback_url: str, payload: Dict[str, Any], retries: int = 3, timeout: int = 30) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(callback_url, json=payload, timeout=timeout)
            response.raise_for_status()
            return
        except requests.RequestException as exc:  # pragma: no cover - network path
            last_error = exc
            logger.warning(
                "Callback attempt %s/%s failed for %s: %s",
                attempt,
                retries,
                callback_url,
                exc,
            )
            time.sleep(min(2 ** attempt, 5))

    if last_error is not None:
        raise last_error


def _run_optimization_and_callback(payload: ScheduleRequest) -> None:
    request_id = payload.request_id
    _set_status(request_id, "running")

    try:
        scheduler = CloudScheduler(
            jobs=payload.jobs,
            vms=payload.vms,
            w1=0.5,
            w2=0.3,
            w3=0.2,
            scheduling_policy=payload.scheduling_policy,
        )

        ga_result = scheduler.run_ga(
            num_generations=100,
            sol_per_pop=50,
            crossover_type="single_point",
            mutation_type="random",
        )
        baseline_result = scheduler.baseline_lpt()

        ga_batches = _solution_to_batches(scheduler, ga_result["solution"])
        baseline_batches = _solution_to_batches(scheduler, baseline_result["solution"])

        callback_payload = {
            "request_id": request_id,
            "GA_results": ga_batches,
            "Baseline_results": baseline_batches,
            "comparison_metrics": _build_comparison_metrics(
                ga_result["metrics"],
                baseline_result["metrics"],
            ),
        }

        _post_callback(payload.callback_url, callback_payload)
        _set_status(request_id, "completed")
        logger.info("Optimization completed and callback sent for request_id=%s", request_id)
    except Exception as exc:
        logger.exception("Optimization failed for request_id=%s", request_id)
        _set_status(request_id, "failed")
        error_payload = {
            "request_id": request_id,
            "error": str(exc),
        }
        try:
            _post_callback(payload.callback_url, error_payload)
        except Exception:
            logger.exception("Failed to deliver error callback for request_id=%s", request_id)


@app.post("/schedule")
def schedule(request: ScheduleRequest, background_tasks: BackgroundTasks) -> Dict[str, str]:
    if not _is_http_url(request.callback_url):
        raise HTTPException(status_code=422, detail="callback_url must be a valid http/https URL.")

    if not request.jobs:
        raise HTTPException(status_code=422, detail="jobs must not be empty.")
    if not request.vms:
        raise HTTPException(status_code=422, detail="vms must not be empty.")

    with _TASK_LOCK:
        if request.request_id in _TASK_STATUS and _TASK_STATUS[request.request_id] in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="request_id is already being processed.")
        _TASK_STATUS[request.request_id] = "queued"

    background_tasks.add_task(_run_optimization_and_callback, request)
    return {
        "status": "Task Received",
        "request_id": request.request_id,
    }


@app.get("/schedule/{request_id}/status")
def get_status(request_id: str) -> Dict[str, str]:
    with _TASK_LOCK:
        status = _TASK_STATUS.get(request_id, "not_found")
    return {"request_id": request_id, "status": status}
