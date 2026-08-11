"""Order-aware cyclic trajectory comparison for register-inference results.

This module deliberately consumes the published ``candidates.json`` schema only.
It does not participate in candidate inference or mutate its input.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULTS = {
    "gamma": 0.1, "position_weight": 1 / 3, "direction_weight": 2 / 3,
    "silhouette_threshold": 0.5, "merge_gap_threshold": 1.5, "max_clusters": 6,
    "signal_slice_id": "isInitMsg", "period_length": 7, "completed_cycles": 2,
    "completion_policy": "strict_same_phase", "low_discriminability_policy": "exclude",
}


def validate_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("trajectory clustering configuration must be a mapping")
    cfg = {**DEFAULTS, **(settings or {})}
    numeric = ("gamma", "position_weight", "direction_weight", "silhouette_threshold", "merge_gap_threshold")
    for name in numeric:
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    if cfg["gamma"] <= 0: raise ValueError("gamma must be > 0")
    if cfg["position_weight"] < 0 or cfg["direction_weight"] < 0 or cfg["position_weight"] + cfg["direction_weight"] <= 0:
        raise ValueError("position_weight and direction_weight must be non-negative with a positive sum")
    if not 0 <= cfg["silhouette_threshold"] <= 1: raise ValueError("silhouette_threshold must be in [0, 1]")
    if cfg["merge_gap_threshold"] < 1: raise ValueError("merge_gap_threshold must be >= 1")
    if isinstance(cfg.get("max_clusters"), bool) or not isinstance(cfg.get("max_clusters"), int) or cfg["max_clusters"] < 2:
        raise ValueError("max_clusters must be an integer >= 2")
    if cfg["period_length"] != 7 or cfg["completed_cycles"] != 2:
        raise ValueError("period_length=7 and completed_cycles=2 are required")
    if cfg["completion_policy"] != "strict_same_phase" or cfg["low_discriminability_policy"] != "exclude":
        raise ValueError("only strict_same_phase completion and exclude low_discriminability are supported")
    if not isinstance(cfg["signal_slice_id"], str) or not cfg["signal_slice_id"]:
        raise ValueError("signal_slice_id must be a non-empty string")
    return cfg


@dataclass(frozen=True)
class Point:
    before: int
    after: int
    input_value: int | None
    signals: tuple[tuple[str, int], ...]


@dataclass
class Trajectory:
    key: str
    origin: str
    edge: dict[str, Any]
    cycle_id: str
    sequence_line: int
    migration_status: str | None
    matched_group: int | None
    samples: list[dict[str, Any]]
    points: list[Point]
    analysis_points: list[dict[str, Any]] | None = None
    signal_slice: int | str = "not_applicable"
    clustering_status: str = "eligible"

    @property
    def io_key(self) -> tuple[str, str]:
        return (str(self.edge["logical_input"]), str(self.edge["logical_output"]))

    @property
    def slice_key(self) -> tuple[str, str, int | str]:
        return (*self.io_key, self.signal_slice)


def _value(item: Any) -> int | None:
    try:
        value = item.get("value") if isinstance(item, dict) else item
        return None if isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def point_from_region(region: dict[str, Any]) -> Point:
    values = region.get("input_register_values", {})
    input_value = None
    if isinstance(values, dict) and values:
        input_value = _value(next(iter(values.values())))
    if input_value is None:
        inputs = region.get("inputs", [])
        if inputs:
            input_value = _value(inputs[-1])
    signals = tuple(sorted(
        (str(item.get("signal_id", item.get("id", "signal"))), int(item["value"]))
        for item in region.get("signals", []) if _value(item) is not None
    ))
    before, after = _value(region.get("previous_output")), _value(region.get("terminal_output"))
    if before is None or after is None:
        raise ValueError("direct_region is missing previous or terminal KSI value")
    return Point(before, after, input_value, signals)


def _group_regions(edge: dict[str, Any]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for region in edge.get("direct_regions", []):
        if isinstance(region, dict) and isinstance(region.get("cycle_id"), str) and isinstance(region.get("sequence_line"), int):
            grouped[(region["cycle_id"], region["sequence_line"])].append(region)
    for regions in grouped.values():
        regions.sort(key=lambda item: int(item.get("repetition", 0)))
    return grouped


def _migration_lookup(edge: dict[str, Any]) -> dict[str, dict[str, Any]]:
    migration = edge.get("relatively_stable_inference_migration") or {}
    return {str(item.get("cycle_id")): item for item in migration.get("cycle_results", []) if isinstance(item, dict)}


def _signal_slice(samples: list[dict[str, Any]], signal_id: str) -> int | str:
    values = {value for sample in samples for item in sample.get("signals", [])
              if str(item.get("signal_id", item.get("id", ""))) == signal_id
              for value in [_value(item)] if value is not None}
    if not values:
        return "not_applicable"
    return next(iter(values)) if len(values) == 1 else "mixed"


def _completed_points(samples: list[dict[str, Any]]) -> tuple[list[Point], list[dict[str, Any]], str]:
    """Make two seven-position cycles without altering the eight observed samples.

    R3/R10 are the same phase.  Only a missing R3 numeric input may be filled
    from R10; all other differences fail completion rather than being extrapolated.
    """
    by_rep = {int(sample.get("repetition", -1)): sample for sample in samples}
    required = list(range(3, 11))
    if any(rep not in by_rep for rep in required):
        return [], [], "period_completion_failed"
    r3, r10 = by_rep[3], by_rep[10]
    p3, p10 = point_from_region(r3), point_from_region(r10)
    p3_input = p3.input_value
    same_phase_imputed = False
    if p3_input is None and p10.input_value is not None:
        p3 = Point(p3.before, p3.after, p10.input_value, p3.signals)
        same_phase_imputed = True
    # R3/R10 may only differ by the explicitly documented absent R3 input.
    if (p3.before, p3.after, p3.signals) != (p10.before, p10.after, p10.signals) or p3.input_value != p10.input_value:
        return [], [], "period_completion_failed"
    base = [point_from_region(by_rep[rep]) for rep in range(3, 10)]
    base[0] = p3
    metadata: list[dict[str, Any]] = []
    for index, point in enumerate(base):
        source = by_rep[index + 3]
        metadata.append({"cycle_position": index + 1, "cycle": 1, "source_repetition": index + 3,
                         "source": "same_phase_imputed" if index == 0 and same_phase_imputed else "observed",
                         "same_phase_imputed": index == 0 and same_phase_imputed, "pattern_completed": False,
                         "r_before": point.before, "r_after": point.after, "i": point.input_value,
                         "signals": [{"signal_id": key, "value": value} for key, value in point.signals]})
    # The observed R10 is position 1 of cycle two; positions 2..7 are copied pattern values.
    for index, point in enumerate(base):
        metadata.append({"cycle_position": index + 1, "cycle": 2, "source_repetition": 10 if index == 0 else index + 3,
                         "source": "observed" if index == 0 else "pattern_completed",
                         "same_phase_imputed": False, "pattern_completed": index != 0,
                         "r_before": point.before, "r_after": point.after, "i": point.input_value,
                         "signals": [{"signal_id": key, "value": value} for key, value in point.signals]})
    return base + base, metadata, "eligible"


def extract_trajectories(payload: dict[str, Any], settings: dict[str, Any] | None = None) -> tuple[list[Trajectory], list[dict[str, Any]], dict[tuple[str, str], int]]:
    """Extract stable and eligible hypothetical direct-region trajectories.

    Exclusions are structural: minimal predecessor defaults, all backward inference,
    migration failures, and hypothetical I/O pairs without a stable group.
    """
    results = {str(item.get("edge", {}).get("edge_id")): item for item in payload.get("results", [])}
    stable_groups: dict[tuple[str, str], int] = {}
    stable_ids: set[str] = set()
    for group in payload.get("relatively_stable_inference", {}).get("groups", []):
        key = (str(group.get("logical_input")), str(group.get("logical_output")))
        stable_groups[key] = int(group.get("group_index"))
        stable_ids.update(map(str, group.get("source_edge_ids", [])))
    kept: list[Trajectory] = []
    excluded: list[dict[str, Any]] = []
    for eid, item in results.items():
        edge = item.get("edge", {})
        grouped = _group_regions(item)
        if not grouped:
            continue
        grade = item.get("candidate_grade")
        assumptions = {str(value) for candidate in item.get("candidates", []) for value in candidate.get("assumptions", [])}
        origin = "stable" if eid in stable_ids and grade == "relatively_stable_candidate" else "hypothetical"
        migration = _migration_lookup(item)
        for (cycle_id, line), samples in grouped.items():
            status_info = migration.get(cycle_id, {})
            status = status_info.get("status")
            reason = None
            if origin == "hypothetical":
                if "minimal_predecessor_default" in assumptions:
                    reason = "minimal_predecessor_default"
                elif "backward_inference" in item:
                    reason = "backward_inference"
                elif status == "migration_failed":
                    reason = "migration_failed"
                elif any(int(sample.get("region_edge_count", 0)) <= 1 for sample in samples):
                    reason = "not_multi_edge_region"
                elif (str(edge.get("logical_input")), str(edge.get("logical_output"))) not in stable_groups:
                    reason = "unmatched_io"
            if reason:
                excluded.append({"edge_id": eid, "cycle_id": cycle_id, "sequence_line": line, "reason": reason})
                continue
            if origin == "hypothetical" and grade != "hypothetical_candidate":
                excluded.append({"edge_id": eid, "cycle_id": cycle_id, "sequence_line": line, "reason": "not_hypothetical_candidate"})
                continue
            key = f"{eid}:{cycle_id}:L{line}"
            points, analysis_points, completion = _completed_points(samples)
            slice_value = _signal_slice(samples, (settings or DEFAULTS)["signal_slice_id"])
            clustering_status = completion
            if clustering_status == "eligible" and slice_value == "mixed": clustering_status = "mixed_signal_slice"
            if clustering_status == "eligible" and len({(point.before, point.after, point.input_value) for point in points}) == 1:
                clustering_status = "low_discriminability"
            kept.append(Trajectory(key, origin, edge, cycle_id, line, status,
                stable_groups.get((str(edge.get("logical_input")), str(edge.get("logical_output")))), samples, points,
                analysis_points, slice_value, clustering_status))
    return kept, excluded, stable_groups


def ksi_distance(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return 0.0 if a is b else 1.0
    if a == b:
        return 0.0
    if a == 7 or b == 7:
        return 1.0
    return min(abs(a - b), 7 - abs(a - b)) / 3.0


def point_distance(a: Point, b: Point) -> float:
    fields = [ksi_distance(a.before, b.before), ksi_distance(a.after, b.after), ksi_distance(a.input_value, b.input_value)]
    return sum(fields) / len(fields)


def direction(a: Point, b: Point) -> Point:
    def delta(x: int | None, y: int | None) -> int | None:
        return None if x is None or y is None else (y - x) % 7 if x != 7 and y != 7 else (0 if x == y else 7)
    return Point(delta(a.before, b.before) or 0, delta(a.after, b.after) or 0, delta(a.input_value, b.input_value), ())


def _softmin(values: Iterable[float], gamma: float) -> float:
    values = tuple(values)
    base = min(values)
    return base - gamma * math.log(sum(math.exp(-(value - base) / gamma) for value in values))


def _soft_dtw(left: list[Point], right: list[Point], gamma: float, position_weight: float, direction_weight: float) -> float:
    n, m = len(left), len(right)
    if not n or not m:
        return float("inf")
    ldir = [direction(left[i], left[(i + 1) % n]) for i in range(n)]
    rdir = [direction(right[i], right[(i + 1) % m]) for i in range(m)]
    table = [[float("inf")] * (m + 1) for _ in range(n + 1)]
    table[0][0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = position_weight * point_distance(left[i - 1], right[j - 1]) + direction_weight * point_distance(ldir[i - 1], rdir[j - 1])
            table[i][j] = cost + _softmin((table[i - 1][j], table[i][j - 1], table[i - 1][j - 1]), gamma)
    return table[n][m]


def _cyclic_score(left: list[Point], right: list[Point], cfg: dict[str, float]) -> float:
    def score(a: list[Point], b: list[Point]) -> float:
        return _soft_dtw(a, b, cfg["gamma"], cfg["position_weight"], cfg["direction_weight"])
    # Two completed cycles contain duplicate rotations.  Keep only unique cyclic
    # point sequences, so doubling a period never gives it extra weight or cost.
    def rotations(points: list[Point]) -> list[list[Point]]:
        seen: set[tuple[Point, ...]] = set(); result = []
        for index in range(len(points)):
            rotated = tuple(points[index:] + points[:index])
            if rotated not in seen:
                seen.add(rotated); result.append(list(rotated))
        return result
    return min(score(a, b) for a in rotations(left) for b in rotations(right))


def cyclic_soft_dtw_distance(left: list[Point], right: list[Point], **settings: float) -> float:
    cfg = {**DEFAULTS, **settings}
    # The same orbit minimization is used for both cross and self scores, making
    # the divergence invariant to either cycle's chosen start and explicitly symmetric.
    cross = _cyclic_score(left, right, cfg)
    self_score = (_cyclic_score(left, left, cfg) + _cyclic_score(right, right, cfg)) / 2
    return max(0.0, cross - self_score)


def distance_matrix(items: list[Trajectory], settings: dict[str, float], cache: dict[tuple[str, str], float] | None = None, self_cache: dict[str, float] | None = None) -> list[list[float]]:
    matrix = [[0.0] * len(items) for _ in items]
    cache = cache if cache is not None else {}; self_cache = self_cache if self_cache is not None else {}
    cfg = {**DEFAULTS, **settings}
    for i in range(len(items)):
        for j in range(i):
            pair = tuple(sorted((items[i].key, items[j].key)))
            if pair not in cache:
                left_key, right_key = items[i].key, items[j].key
                if left_key not in self_cache: self_cache[left_key] = _cyclic_score(items[i].points, items[i].points, cfg)
                if right_key not in self_cache: self_cache[right_key] = _cyclic_score(items[j].points, items[j].points, cfg)
                cache[pair] = max(0.0, _cyclic_score(items[i].points, items[j].points, cfg) - (self_cache[left_key] + self_cache[right_key]) / 2)
            matrix[i][j] = matrix[j][i] = cache[pair]
    return matrix


def average_linkage(matrix: list[list[float]]) -> list[dict[str, Any]]:
    clusters = {index: {index} for index in range(len(matrix))}; next_id = len(matrix); merges = []
    while len(clusters) > 1:
        pairs = []
        ids = sorted(clusters)
        for pos, a in enumerate(ids):
            for b in ids[pos + 1:]:
                members = [(i, j) for i in clusters[a] for j in clusters[b]]
                pairs.append((sum(matrix[i][j] for i, j in members) / len(members), a, b))
        height, a, b = min(pairs)
        merged = clusters.pop(a) | clusters.pop(b)
        clusters[next_id] = merged
        merges.append({"left": a, "right": b, "cluster": next_id, "height": height, "size": len(merged)})
        next_id += 1
    return merges


def _cut(merges: list[dict[str, Any]], count: int, n: int) -> list[list[int]]:
    clusters = {i: {i} for i in range(n)}
    for merge in merges[: max(0, n - count)]:
        clusters[merge["cluster"]] = clusters.pop(merge["left"]) | clusters.pop(merge["right"])
    return [sorted(value) for _, value in sorted(clusters.items())]


def silhouette(matrix: list[list[float]], clusters: list[list[int]]) -> float:
    if len(clusters) < 2 or any(len(cluster) == len(matrix) for cluster in clusters): return 0.0
    membership = {item: index for index, cluster in enumerate(clusters) for item in cluster}; scores = []
    for i in range(len(matrix)):
        own = clusters[membership[i]]
        if len(own) == 1:
            scores.append(0.0)
            continue
        a = sum(matrix[i][j] for j in own if j != i) / max(1, len(own) - 1)
        b = min(sum(matrix[i][j] for j in cluster) / len(cluster) for index, cluster in enumerate(clusters) if index != membership[i])
        scores.append((b - a) / max(a, b, 1e-12))
    return sum(scores) / len(scores)


def cluster(matrix: list[list[float]], settings: dict[str, float]) -> dict[str, Any]:
    n = len(matrix)
    if n <= 1: return {"merges": [], "candidates": [], "selected_k": n, "clusters": [[0]] if n else []}
    merges = average_linkage(matrix); candidates = []
    for k in range(2, min(int(settings.get("max_clusters", 6)), n - 1) + 1):
        groups = _cut(merges, k, n); height = merges[n - k]["height"]
        prior = merges[n - k - 1]["height"] if n - k - 1 >= 0 else height
        gap = height / max(prior, 1e-12)
        candidates.append({"k": k, "silhouette": silhouette(matrix, groups), "merge_gap": gap, "clusters": groups})
    valid = [item for item in candidates if item["silhouette"] >= settings.get("silhouette_threshold", .5) and item["merge_gap"] >= settings.get("merge_gap_threshold", 1.5)]
    selected = max(valid, key=lambda item: (item["silhouette"], -item["k"])) if valid else None
    return {"merges": merges, "candidates": candidates, "selected_k": selected["k"] if selected else 1, "clusters": selected["clusters"] if selected else [list(range(n))]}


def _serialize_trajectory(item: Trajectory) -> dict[str, Any]:
    return {"id": item.key, "origin": item.origin, "edge": item.edge, "cycle_id": item.cycle_id,
            "sequence_line": item.sequence_line, "migration_status": item.migration_status,
            "matched_group": item.matched_group, "samples": item.samples,
            "analysis_points": item.analysis_points or [], "signal_slice": item.signal_slice,
            "clustering_status": item.clustering_status}


def _analysis(name: str, items: list[Trajectory], settings: dict[str, Any], cache: dict, self_cache: dict) -> dict[str, Any]:
    output = {"name": name, "trajectory_ids": [item.key for item in items]}
    matrix = distance_matrix(items, settings, cache, self_cache)
    result = cluster(matrix, settings)
    result["distance_matrix"] = matrix
    result["clusters"] = [[items[index].key for index in group] for group in result["clusters"]]
    for candidate in result["candidates"]:
        candidate["clusters"] = [[items[index].key for index in group] for group in candidate["clusters"]]
    output.update(result)
    return output


def analyze(payload: dict[str, Any], settings: dict[str, float] | None = None) -> dict[str, Any]:
    cfg = validate_settings(settings); trajectories, excluded, stable_groups = extract_trajectories(payload, cfg)
    stable = [item for item in trajectories if item.origin == "stable"]
    hypothetical = [item for item in trajectories if item.origin == "hypothetical"]
    clustered = [item for item in trajectories if item.clustering_status == "eligible"]
    stable_clustered = [item for item in clustered if item.origin == "stable"]
    hypothetical_clustered = [item for item in clustered if item.origin == "hypothetical"]
    stable_by_io: dict[tuple[str, str, int | str], list[Trajectory]] = defaultdict(list); hypo_by_io: dict[tuple[str, str, int | str], list[Trajectory]] = defaultdict(list)
    for item in stable_clustered: stable_by_io[item.slice_key].append(item)
    for item in hypothetical_clustered: hypo_by_io[item.slice_key].append(item)
    tiers: dict[str, list[dict[str, Any]]] = {"stable_internal": [], "hypothetical_internal": [], "joint": []}
    cache: dict[tuple[str, str], float] = {}; self_cache: dict[str, float] = {}
    def name(key: tuple[str, str, int | str]) -> str: return "/".join(map(str, key))
    for key, items in sorted(stable_by_io.items()): tiers["stable_internal"].append(_analysis(name(key), items, cfg, cache, self_cache))
    for key, items in sorted(hypo_by_io.items()): tiers["hypothetical_internal"].append(_analysis(name(key), items, cfg, cache, self_cache))
    for key in sorted(hypo_by_io): tiers["joint"].append(_analysis(name(key), stable_by_io[key] + hypo_by_io[key], cfg, cache, self_cache))
    consistency = []
    joint_by_name = {item["name"]: item for item in tiers["joint"]}
    stable_by_id = {item.key: item for item in stable_clustered}
    migration_total = [item for item in hypothetical if item.migration_status == "migration_succeeded"]
    migration_not_clustered = [item for item in migration_total if item.clustering_status != "eligible"]
    for item in hypothetical_clustered:
        if item.migration_status != "migration_succeeded": continue
        view = joint_by_name[name(item.slice_key)]
        containing = next(group for group in view["clusters"] if item.key in group)
        stable_in_cluster = [identifier for identifier in containing if identifier in stable_by_id and stable_by_id[identifier].matched_group == item.matched_group]
        consistency.append({"trajectory_id": item.key, "matched_group": item.matched_group,
                            "signal_slice": item.signal_slice, "consistent": bool(stable_in_cluster), "cluster": containing})
    low = [item for item in trajectories if item.clustering_status == "low_discriminability"]
    status_counts = {status: sum(item.clustering_status == status for item in trajectories)
                     for status in ("eligible", "low_discriminability", "period_completion_failed", "mixed_signal_slice")}
    return {"schema": "register-trajectory-clustering-v2", "settings": cfg,
            "counts": {"extracted_total": len(trajectories), "stable_total": len(stable),
                       "eligible_hypothetical_total": len(hypothetical), "low_discriminability": len(low),
                       "actually_clustered": len(clustered), "stable_clustered": len(stable_clustered),
                       "hypothetical_clustered": len(hypothetical_clustered),
                       "migration_succeeded_total": len(migration_total), "migration_succeeded_clustered": len(consistency),
                       "migration_succeeded_not_clustered": len(migration_not_clustered), **status_counts},
            "trajectories": [_serialize_trajectory(item) for item in trajectories],
            "stable_groups": [{"logical_input": key[0], "logical_output": key[1], "group_index": group} for key, group in sorted(stable_groups.items())],
            "excluded": excluded, "tiers": tiers, "migration_success_consistency": consistency}
