"""Example training loop: proves the synthetic data is useful.

Pipeline:

1. Build an implicit-feedback user/item matrix from ``click`` / ``play`` /
   ``complete`` events. Strengths accumulate (a completed watch counts more
   than a click); confidence = 1 + alpha * strength (Hu–Koren–Volinsky).
2. Fit a from-scratch numpy ALS model (no heavyweight deps).
3. Evaluate on **held-out personas** — users whose events never entered
   training. For each, fold in 70% of their history, measure recall@K on
   the remaining 30%, and compare against the random baseline (K / n_items).
   Also report catalog coverage and intra-list genre diversity.

If recall@K is clearly above random, the farm generated signal a real
recommender can learn from. If it isn't, either the personas are too
homogeneous or the click model is too noisy — both are tunable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .catalog import SyntheticCatalog
from .config import Config
from .personas import Persona

#: How much each event type contributes to implicit-feedback strength.
EVENT_WEIGHTS = {"click": 1.0, "play": 2.0, "complete": 5.0}


@dataclass
class InteractionData:
    """Sparse implicit-feedback matrix in per-row CSR-ish form."""

    user_ids: list[str]
    item_ids: list[str]
    user_index: dict[str, int]
    item_index: dict[str, int]
    # user_idx -> (item_idx array, confidence array), first-seen order
    positives: dict[int, tuple[np.ndarray, np.ndarray]]
    n_interactions: int = 0


def build_interaction_data(
    events: list[dict],
    user_ids: list[str],
    item_ids: list[str],
    alpha: float,
) -> InteractionData:
    """Accumulate strengths per (user, item) from weighted events."""
    user_index = {u: i for i, u in enumerate(user_ids)}
    item_index = {m: j for j, m in enumerate(item_ids)}
    strengths: dict[int, dict[int, float]] = {}
    first_seen: dict[int, list[int]] = {}

    for e in events:
        w = EVENT_WEIGHTS.get(e["type"])
        if w is None:
            continue
        u = user_index.get(e["persona_id"])
        j = item_index.get(e.get("item_id", ""))
        if u is None or j is None:
            continue
        row = strengths.setdefault(u, {})
        row[j] = row.get(j, 0.0) + w
        if j not in first_seen.setdefault(u, []):
            first_seen[u].append(j)

    positives: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    n = 0
    for u, order in first_seen.items():
        items = np.array(order, dtype=np.int64)
        conf = np.array(
            [1.0 + alpha * strengths[u][j] for j in order], dtype=np.float64
        )
        positives[u] = (items, conf)
        n += len(order)

    return InteractionData(
        user_ids=user_ids,
        item_ids=item_ids,
        user_index=user_index,
        item_index=item_index,
        positives=positives,
        n_interactions=n,
    )


def als_fit(
    data: InteractionData,
    n_factors: int,
    n_iter: int,
    reg: float,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, list[float]]:
    """Alternating least squares for implicit feedback (Hu et al.).

    Solves, per user u:  x_u = (Y^T C^u Y + λI)^{-1} Y^T C^u p_u
    using the Y^T Y + Y^T (C^u − I) Y trick so each solve only touches
    the user's interacted items. Returns (X, Y, losses).
    """
    n_users, n_items = len(data.user_ids), len(data.item_ids)
    Y = rng.normal(0.0, 1.0 / n_factors, size=(n_items, n_factors))
    X = np.zeros((n_users, n_factors))
    eye = np.eye(n_factors)
    losses: list[float] = []

    # Transposed view for the item pass: item -> (user idx, confidence).
    item_users: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    acc: dict[int, list[tuple[int, float]]] = {}
    for u, (items, conf) in data.positives.items():
        for j, c in zip(items.tolist(), conf.tolist()):
            acc.setdefault(j, []).append((u, c))
    for j, pairs in acc.items():
        us = np.array([p[0] for p in pairs], dtype=np.int64)
        cs = np.array([p[1] for p in pairs], dtype=np.float64)
        item_users[j] = (us, cs)

    for _ in range(n_iter):
        # -- user pass ------------------------------------------------------
        YtY = Y.T @ Y
        for u, (items, conf) in data.positives.items():
            Yu = Y[items]
            w = (conf - 1.0)[:, None]
            A = YtY + (Yu * w).T @ Yu + reg * eye
            b = Yu.T @ conf  # p_u = 1 for interacted items
            X[u] = np.linalg.solve(A, b)
        # -- item pass ------------------------------------------------------
        XtX = X.T @ X
        for j, (us, conf) in item_users.items():
            Xu = X[us]
            w = (conf - 1.0)[:, None]
            A = XtX + (Xu * w).T @ Xu + reg * eye
            b = Xu.T @ conf
            Y[j] = np.linalg.solve(A, b)
        losses.append(_observed_mse(X, Y, data))

    return X, Y, losses


def _observed_mse(
    X: np.ndarray, Y: np.ndarray, data: InteractionData
) -> float:
    """Mean (1 − x_u·y_i)² over observed positives. Should decrease."""
    tot, n = 0.0, 0
    for u, (items, _) in data.positives.items():
        pred = X[u] @ Y[items].T
        tot += float(np.sum((1.0 - pred) ** 2))
        n += len(items)
    return tot / max(n, 1)


def fold_in(
    items: np.ndarray, conf: np.ndarray, Y: np.ndarray, reg: float
) -> np.ndarray:
    """Compute a user factor from interacted items (for held-out users)."""
    f = Y.shape[1]
    Yh = Y[items]
    w = (conf - 1.0)[:, None]
    A = Y.T @ Y + (Yh * w).T @ Yh + reg * np.eye(f)
    b = Yh.T @ conf
    return np.linalg.solve(A, b)


@dataclass
class EvalMetrics:
    """Held-out evaluation results."""

    n_eval_users: int
    mean_recall_at_k: float
    random_baseline: float
    lift_vs_random: float
    catalog_coverage: float  # |union of all top-K| / n_items
    mean_distinct_genres: float  # avg distinct primary genres per top-K
    k: int

    def report(self) -> str:
        return (
            "Held-out evaluation "
            f"(K={self.k}, {self.n_eval_users} eval personas):\n"
            f"  recall@{self.k}        : {self.mean_recall_at_k:.3f}\n"
            f"  random baseline     : {self.random_baseline:.3f}\n"
            f"  lift vs random      : {self.lift_vs_random:.1f}x\n"
            f"  catalog coverage    : {self.catalog_coverage:.1%}\n"
            f"  distinct genres/topK: {self.mean_distinct_genres:.1f}"
        )


def evaluate_held_out(
    Y: np.ndarray,
    eval_events: list[dict],
    eval_user_ids: list[str],
    item_ids: list[str],
    catalog: SyntheticCatalog,
    config: Config,
) -> EvalMetrics:
    """Recall@K + diversity for personas excluded from training."""
    item_index = {m: j for j, m in enumerate(item_ids)}
    # Per-user interacted items in first-seen order.
    user_items: dict[str, list[str]] = {}
    for e in eval_events:
        if e["type"] not in EVENT_WEIGHTS or "item_id" not in e:
            continue
        lst = user_items.setdefault(e["persona_id"], [])
        if e["item_id"] not in lst:
            lst.append(e["item_id"])

    k = config.eval_top_k
    n_items = len(item_ids)
    recalls: list[float] = []
    all_topk: set[int] = set()
    genre_counts: list[int] = []

    for uid in eval_user_ids:
        items = [m for m in user_items.get(uid, []) if m in item_index]
        if len(items) < 3:
            continue  # need at least 2 for history + 1 held out
        cut = max(1, int(0.7 * len(items)))
        hist = np.array([item_index[m] for m in items[:cut]], dtype=np.int64)
        held = {item_index[m] for m in items[cut:]}
        conf = np.full(len(hist), 1.0 + config.als_confidence_alpha)
        x_new = fold_in(hist, conf, Y, config.als_regularization)
        scores = Y @ x_new
        scores[hist] = -np.inf  # don't recommend what they already saw
        topk = np.argpartition(scores, -k)[-k:]
        topk = topk[np.argsort(scores[topk])[::-1]]
        recalls.append(len(set(topk.tolist()) & held) / len(held))
        all_topk.update(topk.tolist())
        genres = {catalog.by_id[item_ids[j]].primary_genre for j in topk}
        genre_counts.append(len(genres))

    mean_recall = float(np.mean(recalls)) if recalls else 0.0
    random_base = k / n_items
    return EvalMetrics(
        n_eval_users=len(recalls),
        mean_recall_at_k=mean_recall,
        random_baseline=random_base,
        lift_vs_random=mean_recall / random_base if random_base else 0.0,
        catalog_coverage=len(all_topk) / n_items if n_items else 0.0,
        mean_distinct_genres=(
            float(np.mean(genre_counts)) if genre_counts else 0.0
        ),
        k=k,
    )


@dataclass
class TrainingReport:
    als_losses: list[float] = field(default_factory=list)
    metrics: EvalMetrics | None = None
    samples: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = ["ALS training:"]
        if self.als_losses:
            lines.append(
                f"  observed MSE {self.als_losses[0]:.4f} -> "
                f"{self.als_losses[-1]:.4f} "
                f"over {len(self.als_losses)} iterations"
            )
        if self.metrics:
            lines.append(self.metrics.report())
        if self.samples:
            lines.append("Sample recommendations vs persona taste:")
            lines.extend(f"  {s}" for s in self.samples)
        return "\n".join(lines)


def train_and_evaluate(
    events: list[dict],
    personas: list[Persona],
    catalog: SyntheticCatalog,
    config: Config,
    seed: int | None = None,
) -> TrainingReport:
    """Full loop: split personas, fit ALS on train, evaluate on held-out."""
    rng = np.random.default_rng(config.seed if seed is None else seed)
    order = rng.permutation(len(personas))
    n_train = int(len(personas) * config.train_fraction)
    train_personas = [personas[i] for i in order[:n_train]]
    eval_personas = [personas[i] for i in order[n_train:]]
    train_ids = {p.persona_id for p in train_personas}
    eval_ids = [p.persona_id for p in eval_personas]

    train_events = [e for e in events if e["persona_id"] in train_ids]
    eval_events = [e for e in events if e["persona_id"] in eval_ids]
    item_ids = [it.item_id for it in catalog.items]

    data = build_interaction_data(
        train_events,
        [p.persona_id for p in train_personas],
        item_ids,
        config.als_confidence_alpha,
    )
    _, Y, losses = als_fit(
        data,
        config.als_factors,
        config.als_iterations,
        config.als_regularization,
        rng,
    )
    metrics = evaluate_held_out(
        Y, eval_events, eval_ids, item_ids, catalog, config
    )
    samples = _sample_recommendations(
        eval_personas, eval_events, Y, item_ids, catalog, config, rng
    )
    return TrainingReport(als_losses=losses, metrics=metrics, samples=samples)


def _sample_recommendations(
    eval_personas: list[Persona],
    eval_events: list[dict],
    Y: np.ndarray,
    item_ids: list[str],
    catalog: SyntheticCatalog,
    config: Config,
    rng: np.random.Generator,
    n_samples: int = 3,
    n_recs: int = 5,
) -> list[str]:
    """Top-N recommendations for a few eval personas, next to their taste."""
    item_index = {m: j for j, m in enumerate(item_ids)}
    user_items: dict[str, list[str]] = {}
    for e in eval_events:
        if e["type"] in EVENT_WEIGHTS and "item_id" in e:
            lst = user_items.setdefault(e["persona_id"], [])
            if e["item_id"] not in lst:
                lst.append(e["item_id"])

    # Prefer personas with the richest histories (cleanest fold-in signal),
    # from different archetypes for an interesting sample.
    by_history = sorted(
        eval_personas,
        key=lambda p: len(user_items.get(p.persona_id, [])),
        reverse=True,
    )
    seen_arch: set[str] = set()
    chosen: list[Persona] = []
    for p in by_history:
        if p.archetype not in seen_arch and len(user_items.get(p.persona_id, [])) >= 3:
            seen_arch.add(p.archetype)
            chosen.append(p)
        if len(chosen) == n_samples:
            break

    lines: list[str] = []
    for p in chosen:
        hist = np.array(
            [item_index[m] for m in user_items[p.persona_id] if m in item_index],
            dtype=np.int64,
        )
        conf = np.full(len(hist), 1.0 + config.als_confidence_alpha)
        x_new = fold_in(hist, conf, Y, config.als_regularization)
        scores = Y @ x_new
        scores[hist] = -np.inf
        top = np.argpartition(scores, -n_recs)[-n_recs:]
        top = top[np.argsort(scores[top])[::-1]]
        recs = ", ".join(
            f"{catalog.by_id[item_ids[j]].title} "
            f"[{catalog.by_id[item_ids[j]].primary_genre}]"
            for j in top
        )
        taste = ", ".join(f"{g} {w:.0%}" for g, w in p.top_genres(2))
        lines.append(f"{p.persona_id} [{p.archetype}] loves {taste}")
        lines.append(f"  -> recommends: {recs}")
    return lines
