from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from src.cloud_scheduler import CloudScheduler
except ImportError:  # pragma: no cover
    from cloud_scheduler import CloudScheduler


logger = logging.getLogger("cloud_scheduler_api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Cloud Resource Scheduler API",
    version="2.0.0",
    description="Schedules cloud processes to VMs using Genetic Algorithm (GA) and LPT baseline.",
)

# ---------------------------------------------------------------------------
# In-memory session state
# ---------------------------------------------------------------------------
_SESSION_STATUS: Dict[str, str] = {}       # session_uuid -> "queued"|"running"|"completed"|"failed"
_SESSION_RESULTS: Dict[str, Dict] = {}     # session_uuid -> full result payload
_STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pydantic models for the new API contract
# ---------------------------------------------------------------------------

class ProcessInput(BaseModel):
    uuid: str
    duration: int = Field(..., gt=0, description="Duration in seconds")
    cores: int = Field(..., gt=0)
    memory: int = Field(..., gt=0)


class VMInput(BaseModel):
    uuid: str
    cores: int = Field(..., gt=0)
    memory: int = Field(..., gt=0)
    cost: int = Field(..., gt=0, description="Cost per second (any currency unit)")


class StartRequest(BaseModel):
    session_uuid: str = Field(..., min_length=1)
    processes: List[ProcessInput]
    vms: List[VMInput]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _set_status(session_uuid: str, status: str) -> None:
    with _STATE_LOCK:
        _SESSION_STATUS[session_uuid] = status


def _set_result(session_uuid: str, result: Dict) -> None:
    with _STATE_LOCK:
        _SESSION_RESULTS[session_uuid] = result


def _translate_processes_to_jobs(processes: List[ProcessInput]) -> List[Dict[str, Any]]:
    """Convert the new process schema to the CloudScheduler job schema."""
    return [
        {
            "id": p.uuid,
            "cpu": float(p.cores),
            "ram": float(p.memory),
            "duration": float(p.duration),
        }
        for p in processes
    ]


def _translate_vms(vms: List[VMInput]) -> List[Dict[str, Any]]:
    """Convert the new VM schema to the CloudScheduler VM schema."""
    return [
        {
            "id": v.uuid,
            "cpu_cap": float(v.cores),
            "ram_cap": float(v.memory),
            "cost_rate": float(v.cost),
        }
        for v in vms
    ]


def _build_vm_detail(
    scheduler: CloudScheduler,
    solution: List[int],
    vm_inputs: List[VMInput],
    process_inputs: List[ProcessInput],
) -> List[Dict[str, Any]]:
    """
    For each VM in the solution, return a detailed breakdown:
    - which processes were assigned
    - total runtime on that VM (seconds)
    - total cost for that VM
    """
    assignments = scheduler.decode_chromosome(solution)
    process_map = {p.uuid: p for p in process_inputs}
    vm_map = {v.uuid: v for v in vm_inputs}

    vm_details = []
    for vm_idx, vm in enumerate(scheduler.vms):
        assigned_jobs = assignments.get(vm_idx, [])
        sim = scheduler.simulate_execution(vm, assigned_jobs)

        assigned_process_uuids = [job.id for job in assigned_jobs]
        runtime_seconds = sim.active_duration
        vm_obj = vm_map.get(str(vm.id))
        cost_per_second = vm_obj.cost if vm_obj else 0
        total_cost = runtime_seconds * cost_per_second

        # Per-process timing
        process_details = []
        for job in assigned_jobs:
            pinfo = process_map.get(str(job.id))
            process_details.append({
                "process_uuid": str(job.id),
                "duration_seconds": job.duration,
                "cores_required": int(job.cpu),
                "memory_required": int(job.ram),
                "start_time_seconds": _safe_float(sim.waiting_times.get(job.id, 0)),
                "finish_time_seconds": _safe_float(sim.finish_times.get(job.id, 0)),
            })

        vm_details.append({
            "vm_uuid": str(vm.id),
            "cores": vm_obj.cores if vm_obj else int(vm.cpu_cap),
            "memory": vm_obj.memory if vm_obj else int(vm.ram_cap),
            "cost_per_second": cost_per_second,
            "assigned_processes": assigned_process_uuids,
            "process_details": process_details,
            "runtime_seconds": round(runtime_seconds, 4),
            "total_cost": round(total_cost, 4),
        })

    return vm_details


def _build_algorithm_result(
    label: str,
    scheduler: CloudScheduler,
    solution: List[int],
    metrics: Dict[str, Any],
    vm_inputs: List[VMInput],
    process_inputs: List[ProcessInput],
) -> Dict[str, Any]:
    vm_details = _build_vm_detail(scheduler, solution, vm_inputs, process_inputs)
    overall_cost = sum(v["total_cost"] for v in vm_details)
    overall_runtime = metrics.get("makespan", 0.0) or 0.0
    avg_waiting = metrics.get("avg_waiting_time", 0.0) or 0.0

    return {
        "algorithm": label,
        "vms": vm_details,
        "overall_cost": round(overall_cost, 4),
        "overall_runtime_seconds": round(float(overall_runtime), 4),
        "average_waiting_time_seconds": round(float(avg_waiting), 4),
        "feasible": metrics.get("feasible", False),
    }


def _compare(ga: Dict[str, Any], lpt: Dict[str, Any]) -> Dict[str, Any]:
    def _winner(ga_val, lpt_val, label: str) -> Dict[str, Any]:
        if ga_val is None or lpt_val is None:
            return {"winner": "unknown", "ga": ga_val, "lpt": lpt_val}
        if ga_val < lpt_val:
            winner = "GA"
        elif lpt_val < ga_val:
            winner = "LPT"
        else:
            winner = "tie"
        return {
            "winner": winner,
            "ga": round(ga_val, 4),
            "lpt": round(lpt_val, 4),
            "difference": round(ga_val - lpt_val, 4),
        }

    cost_cmp = _winner(ga["overall_cost"], lpt["overall_cost"], "cost")
    runtime_cmp = _winner(ga["overall_runtime_seconds"], lpt["overall_runtime_seconds"], "runtime")
    wait_cmp = _winner(ga["average_waiting_time_seconds"], lpt["average_waiting_time_seconds"], "waiting")

    winners = [cost_cmp["winner"], runtime_cmp["winner"], wait_cmp["winner"]]
    ga_wins = winners.count("GA")
    lpt_wins = winners.count("LPT")
    overall_winner = "GA" if ga_wins > lpt_wins else ("LPT" if lpt_wins > ga_wins else "tie")

    return {
        "overall_winner": overall_winner,
        "cost_comparison": cost_cmp,
        "runtime_comparison": runtime_cmp,
        "average_waiting_time_comparison": wait_cmp,
    }


def _post_callback(url: str, payload: Dict[str, Any], retries: int = 3, timeout: int = 30) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            logger.info("Callback delivered to %s (attempt %s)", url, attempt)
            return
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Callback attempt %s/%s failed for %s: %s", attempt, retries, url, exc)
            time.sleep(min(2 ** attempt, 8))
    if last_error is not None:
        raise last_error


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_algorithms(request: StartRequest) -> None:
    session_uuid = request.session_uuid
    _set_status(session_uuid, "running")

    callback_url = os.environ.get("CALLBACK_SERVER_URL", "").strip()

    try:
        jobs_data = _translate_processes_to_jobs(request.processes)
        vms_data = _translate_vms(request.vms)

        scheduler = CloudScheduler(
            jobs=jobs_data,
            vms=vms_data,
            w1=0.5,
            w2=0.3,
            w3=0.2,
            scheduling_policy="concurrent",
        )

        # Run GA
        logger.info("[%s] Starting GA...", session_uuid)
        ga_raw = scheduler.run_ga(
            num_generations=100,
            sol_per_pop=50,
            crossover_type="single_point",
            mutation_type="random",
        )

        # Run LPT baseline
        logger.info("[%s] Starting LPT baseline...", session_uuid)
        lpt_raw = scheduler.baseline_lpt()

        ga_result = _build_algorithm_result(
            "Genetic Algorithm (GA)",
            scheduler,
            ga_raw["solution"],
            ga_raw["metrics"],
            request.vms,
            request.processes,
        )
        lpt_result = _build_algorithm_result(
            "Longest Processing Time (LPT)",
            scheduler,
            lpt_raw["solution"],
            lpt_raw["metrics"],
            request.vms,
            request.processes,
        )
        comparison = _compare(ga_result, lpt_result)

        payload: Dict[str, Any] = {
            "session_uuid": session_uuid,
            "status": "completed",
            "ga_result": ga_result,
            "lpt_result": lpt_result,
            "comparison": comparison,
        }

        _set_result(session_uuid, payload)
        _set_status(session_uuid, "completed")
        logger.info("[%s] Algorithms completed successfully.", session_uuid)

        if callback_url:
            try:
                _post_callback(callback_url, payload)
            except Exception as cb_exc:
                logger.exception("[%s] Failed to deliver callback: %s", session_uuid, cb_exc)
        else:
            logger.warning("[%s] CALLBACK_SERVER_URL not set; skipping callback.", session_uuid)

    except Exception as exc:
        logger.exception("[%s] Algorithm run failed: %s", session_uuid, exc)
        _set_status(session_uuid, "failed")

        error_payload: Dict[str, Any] = {
            "session_uuid": session_uuid,
            "status": "failed",
            "error": str(exc),
        }
        _set_result(session_uuid, error_payload)

        if callback_url:
            try:
                _post_callback(callback_url, error_payload)
            except Exception:
                logger.exception("[%s] Failed to deliver error callback.", session_uuid)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/start-new", summary="Start GA + LPT scheduling for a session")
def start_new(request: StartRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """
    Accepts session scheduling input (processes + VMs), fires off GA and LPT
    algorithms in the background, and immediately returns an acknowledgement.
    """
    if not request.processes:
        raise HTTPException(status_code=422, detail="processes must not be empty.")
    if not request.vms:
        raise HTTPException(status_code=422, detail="vms must not be empty.")

    with _STATE_LOCK:
        current = _SESSION_STATUS.get(request.session_uuid)
        if current in {"queued", "running"}:
            raise HTTPException(
                status_code=409,
                detail=f"Session '{request.session_uuid}' is already being processed (status: {current}).",
            )
        _SESSION_STATUS[request.session_uuid] = "queued"

    background_tasks.add_task(_run_algorithms, request)

    return {
        "session_uuid": request.session_uuid,
        "status": "queued",
        "message": "Scheduling algorithms (GA + LPT) have been started. "
                   "Results will be POSTed to CALLBACK_SERVER_URL when ready.",
        "num_processes": len(request.processes),
        "num_vms": len(request.vms),
    }


@app.get("/results/{session_uuid}", summary="Poll results for a session")
def get_results(session_uuid: str) -> Dict[str, Any]:
    """
    Returns the current status and — once completed — the full scheduling
    results for the given session UUID.
    """
    with _STATE_LOCK:
        status = _SESSION_STATUS.get(session_uuid)
        result = _SESSION_RESULTS.get(session_uuid)

    if status is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_uuid}' not found.")

    if status in {"queued", "running"}:
        return {"session_uuid": session_uuid, "status": status, "message": "Algorithms are still running."}

    # completed or failed
    return result or {"session_uuid": session_uuid, "status": status}


@app.get("/health", summary="Health check")
def health() -> Dict[str, str]:
    return {"status": "ok"}
