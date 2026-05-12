from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pygad
except ImportError:  # pragma: no cover - optional at import time
    pygad = None


@dataclass(frozen=True)
class Job:
    id: int
    cpu: float
    ram: float
    duration: float


@dataclass(frozen=True)
class VM:
    id: int
    cpu_cap: float
    ram_cap: float
    cost_rate: float


@dataclass
class SimulationResult:
    finish_times: Dict[int, float]
    waiting_times: Dict[int, float]
    active_duration: float
    feasible: bool


class CloudScheduler:
    """Schedule cloud video jobs to VMs with GA and LPT baseline."""

    def __init__(
        self,
        jobs: Sequence[Dict[str, Any]],
        vms: Sequence[Dict[str, Any]],
        w1: float = 0.5,
        w2: float = 0.3,
        w3: float = 0.2,
        scheduling_policy: str = "concurrent",
    ) -> None:
        if scheduling_policy not in {"concurrent", "fcfs"}:
            raise ValueError("scheduling_policy must be 'concurrent' or 'fcfs'.")

        self.jobs = [self._parse_job(job, idx) for idx, job in enumerate(jobs)]
        self.vms = [self._parse_vm(vm, idx) for idx, vm in enumerate(vms)]

        if not self.jobs:
            raise ValueError("At least one job is required.")
        if not self.vms:
            raise ValueError("At least one VM is required.")

        self.w1 = w1
        self.w2 = w2
        self.w3 = w3
        self.scheduling_policy = scheduling_policy
        self.num_jobs = len(self.jobs)
        self.num_vms = len(self.vms)

    @staticmethod
    def _parse_job(raw: Dict[str, Any], fallback_id: int) -> Job:
        job_id = int(raw.get("id", fallback_id))
        cpu = float(raw.get("cpu", raw.get("required_cpu")))
        ram = float(raw.get("ram", raw.get("required_ram")))
        duration = float(raw.get("duration", raw.get("estimated_duration")))
        return Job(id=job_id, cpu=cpu, ram=ram, duration=duration)

    @staticmethod
    def _parse_vm(raw: Dict[str, Any], fallback_id: int) -> VM:
        vm_id = int(raw.get("id", fallback_id))
        cpu_cap = float(raw.get("cpu_cap", raw.get("cpu_capacity")))
        ram_cap = float(raw.get("ram_cap", raw.get("ram_capacity")))
        cost_rate = float(raw.get("cost_rate", raw.get("cost_rate_per_second")))
        return VM(id=vm_id, cpu_cap=cpu_cap, ram_cap=ram_cap, cost_rate=cost_rate)

    def simulate_execution(self, vm: VM, assigned_jobs: Sequence[Job]) -> SimulationResult:
        """Simulate execution on one VM and return finish/waiting metrics."""
        if not assigned_jobs:
            return SimulationResult(finish_times={}, waiting_times={}, active_duration=0.0, feasible=True)

        if self.scheduling_policy == "fcfs":
            return self._simulate_fcfs(vm, assigned_jobs)

        return self._simulate_concurrent(vm, assigned_jobs)

    @staticmethod
    def _simulate_fcfs(vm: VM, assigned_jobs: Sequence[Job]) -> SimulationResult:
        current_time = 0.0
        finish_times: Dict[int, float] = {}
        waiting_times: Dict[int, float] = {}

        for job in assigned_jobs:
            if job.cpu > vm.cpu_cap or job.ram > vm.ram_cap:
                return SimulationResult(
                    finish_times=finish_times,
                    waiting_times=waiting_times,
                    active_duration=current_time,
                    feasible=False,
                )

            start_time = current_time
            waiting_times[job.id] = start_time
            current_time += job.duration
            finish_times[job.id] = current_time

        return SimulationResult(
            finish_times=finish_times,
            waiting_times=waiting_times,
            active_duration=current_time,
            feasible=True,
        )

    @staticmethod
    def _simulate_concurrent(vm: VM, assigned_jobs: Sequence[Job]) -> SimulationResult:
        pending = deque(assigned_jobs)
        running: List[Tuple[float, Job]] = []

        available_cpu = vm.cpu_cap
        available_ram = vm.ram_cap
        current_time = 0.0

        finish_times: Dict[int, float] = {}
        waiting_times: Dict[int, float] = {}

        first_start: Optional[float] = None
        last_finish = 0.0

        while pending or running:
            while pending:
                job = pending[0]

                if job.cpu > vm.cpu_cap or job.ram > vm.ram_cap:
                    return SimulationResult(
                        finish_times=finish_times,
                        waiting_times=waiting_times,
                        active_duration=max(0.0, last_finish - (first_start or 0.0)),
                        feasible=False,
                    )

                if job.cpu <= available_cpu and job.ram <= available_ram:
                    pending.popleft()
                    waiting_times[job.id] = current_time
                    finish_time = current_time + job.duration
                    running.append((finish_time, job))
                    available_cpu -= job.cpu
                    available_ram -= job.ram
                    if first_start is None:
                        first_start = current_time
                else:
                    # FCFS discipline inside concurrent mode: do not skip queue head.
                    break

            if not running and pending:
                # This state should not happen with valid capacities, but kept for safety.
                return SimulationResult(
                    finish_times=finish_times,
                    waiting_times=waiting_times,
                    active_duration=max(0.0, last_finish - (first_start or 0.0)),
                    feasible=False,
                )

            if running:
                next_finish_time = min(item[0] for item in running)
                current_time = next_finish_time

                still_running: List[Tuple[float, Job]] = []
                for finish_time, job in running:
                    if abs(finish_time - next_finish_time) < 1e-12:
                        finish_times[job.id] = finish_time
                        available_cpu += job.cpu
                        available_ram += job.ram
                        last_finish = max(last_finish, finish_time)
                    else:
                        still_running.append((finish_time, job))
                running = still_running

        active_duration = 0.0 if first_start is None else max(0.0, last_finish - first_start)
        return SimulationResult(
            finish_times=finish_times,
            waiting_times=waiting_times,
            active_duration=active_duration,
            feasible=True,
        )

    def decode_chromosome(self, solution: Sequence[Any]) -> Dict[int, List[Job]]:
        if len(solution) != self.num_jobs:
            raise ValueError(f"Expected chromosome length {self.num_jobs}, got {len(solution)}.")

        assignments: Dict[int, List[Job]] = {vm_idx: [] for vm_idx in range(self.num_vms)}
        for job_idx, gene in enumerate(solution):
            vm_idx = int(round(float(gene)))
            if vm_idx < 0 or vm_idx >= self.num_vms:
                raise ValueError(f"Invalid VM index {vm_idx} at gene {job_idx}.")
            assignments[vm_idx].append(self.jobs[job_idx])

        return assignments

    def evaluate_solution(self, solution: Sequence[Any]) -> Dict[str, Any]:
        """Compute cost, makespan, waiting, and fitness from a chromosome."""
        try:
            assignments = self.decode_chromosome(solution)
        except ValueError:
            return {
                "feasible": False,
                "total_cost": float("inf"),
                "makespan": float("inf"),
                "avg_waiting_time": float("inf"),
                "fitness": 0.0,
                "vm_results": {},
            }

        vm_results: Dict[int, SimulationResult] = {}
        total_cost = 0.0
        makespan = 0.0
        waiting_values: List[float] = []

        for vm_idx, jobs_on_vm in assignments.items():
            vm = self.vms[vm_idx]
            sim = self.simulate_execution(vm, jobs_on_vm)
            if not sim.feasible:
                return {
                    "feasible": False,
                    "total_cost": float("inf"),
                    "makespan": float("inf"),
                    "avg_waiting_time": float("inf"),
                    "fitness": 0.0,
                    "vm_results": vm_results,
                }

            vm_results[vm_idx] = sim
            total_cost += sim.active_duration * vm.cost_rate
            if sim.finish_times:
                makespan = max(makespan, max(sim.finish_times.values()))
            waiting_values.extend(sim.waiting_times.values())

        avg_waiting = sum(waiting_values) / len(waiting_values) if waiting_values else 0.0
        denominator = (self.w1 * total_cost) + (self.w2 * makespan) + (self.w3 * avg_waiting)
        fitness = (1.0 / denominator) if denominator > 0 else 0.0

        return {
            "feasible": True,
            "total_cost": total_cost,
            "makespan": makespan,
            "avg_waiting_time": avg_waiting,
            "fitness": fitness,
            "vm_results": vm_results,
        }

    def fitness_func(self, ga_instance: Any, solution: Sequence[Any], solution_idx: int) -> float:
        _ = ga_instance, solution_idx
        return float(self.evaluate_solution(solution)["fitness"])

    def build_ga(
        self,
        num_generations: int = 100,
        sol_per_pop: int = 50,
        num_parents_mating: int = 20,
        crossover_type: str = "single_point",
        mutation_type: str = "random",
        mutation_percent_genes: int = 10,
        parent_selection_type: str = "sss",
        keep_elitism: int = 2,
        random_seed: Optional[int] = None,
    ) -> Any:
        if pygad is None:
            raise ImportError("PyGAD is not installed. Install with: pip install pygad")

        return pygad.GA(
            num_generations=num_generations,
            sol_per_pop=sol_per_pop,
            num_parents_mating=min(num_parents_mating, sol_per_pop),
            num_genes=self.num_jobs,
            gene_type=int,
            gene_space=range(self.num_vms),
            fitness_func=self.fitness_func,
            parent_selection_type=parent_selection_type,
            crossover_type=crossover_type,
            mutation_type=mutation_type,
            mutation_percent_genes=mutation_percent_genes,
            keep_elitism=keep_elitism,
            random_seed=random_seed,
            suppress_warnings=True,
        )

    def run_ga(self, **ga_kwargs: Any) -> Dict[str, Any]:
        ga = self.build_ga(**ga_kwargs)
        ga.run()

        best_solution, best_fitness, _ = ga.best_solution()
        metrics = self.evaluate_solution(best_solution)
        return {
            "solution": [int(round(float(g))) for g in best_solution],
            "fitness": float(best_fitness),
            "metrics": metrics,
            "ga_instance": ga,
        }

    def baseline_lpt(self) -> Dict[str, Any]:
        """Greedy Longest Processing Time assignment baseline."""
        chromosome = [-1] * self.num_jobs
        assignments: Dict[int, List[Job]] = {vm_idx: [] for vm_idx in range(self.num_vms)}

        jobs_by_lpt = sorted(
            enumerate(self.jobs),
            key=lambda item: item[1].duration,
            reverse=True,
        )

        for job_idx, job in jobs_by_lpt:
            best_vm_idx: Optional[int] = None
            best_projected_finish = float("inf")
            best_projected_cost = float("inf")

            for vm_idx, vm in enumerate(self.vms):
                if job.cpu > vm.cpu_cap or job.ram > vm.ram_cap:
                    continue

                candidate_jobs = assignments[vm_idx] + [job]
                sim = self.simulate_execution(vm, candidate_jobs)
                if not sim.feasible:
                    continue

                projected_finish = max(sim.finish_times.values(), default=0.0)
                projected_cost = sim.active_duration * vm.cost_rate

                if projected_finish < best_projected_finish or (
                    projected_finish == best_projected_finish and projected_cost < best_projected_cost
                ):
                    best_vm_idx = vm_idx
                    best_projected_finish = projected_finish
                    best_projected_cost = projected_cost

            if best_vm_idx is None:
                raise ValueError(
                    f"Job {job.id} cannot be assigned: exceeds all VM capacities."
                )

            assignments[best_vm_idx].append(job)
            chromosome[job_idx] = best_vm_idx

        metrics = self.evaluate_solution(chromosome)
        return {
            "solution": chromosome,
            "fitness": float(metrics["fitness"]),
            "metrics": metrics,
        }


if __name__ == "__main__":
    jobs = [
        {"id": 1, "cpu": 2, "ram": 4, "duration": 120},
        {"id": 2, "cpu": 1, "ram": 2, "duration": 60},
        {"id": 3, "cpu": 2, "ram": 1, "duration": 90},
        {"id": 4, "cpu": 1, "ram": 1, "duration": 30},
    ]
    vms = [
        {"id": 0, "cpu_cap": 4, "ram_cap": 8, "cost_rate": 0.05},
        {"id": 1, "cpu_cap": 2, "ram_cap": 4, "cost_rate": 0.02},
    ]

    scheduler = CloudScheduler(jobs, vms, w1=0.5, w2=0.3, w3=0.2, scheduling_policy="concurrent")

    lpt_result = scheduler.baseline_lpt()
    print("LPT solution:", lpt_result["solution"])
    print("LPT metrics:", lpt_result["metrics"])

    if pygad is not None:
        ga_result = scheduler.run_ga(num_generations=100, sol_per_pop=50)
        print("GA solution:", ga_result["solution"])
        print("GA metrics:", ga_result["metrics"])
    else:
        print("PyGAD not installed; GA run skipped.")
