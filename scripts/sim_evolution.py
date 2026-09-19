#!/usr/bin/env python3
"""30-day cold-start evolution: 240 agents start with near-identical taste
(day 1 = cold start, one crowd). Each day they watch/search; tastes update
toward what they consume; crowds split into streams. K-means on final tastes
gives emergent clusters; every day labeled by nearest final centroid so the
page can show migration 'into clusters for real'."""
import json, os, sys
import numpy as np
sys.path.insert(0, "/home/hatch/workspace/synth-farm")

from synth_farm.config import Config
from synth_farm.personas import generate_personas, ARCHETYPES
from synth_farm.real_catalog import load_real_catalog

SEED, N, DAYS, K = 7, 240, 30, 6
rng = np.random.default_rng(SEED)

config = Config.from_env()
genres = list(config.genres)
gidx = {g: i for i, g in enumerate(genres)}
personas = generate_personas(N, genres, seed=SEED)

catalog = load_real_catalog()
by_genre = {}
for it in catalog.items:
    by_genre.setdefault(it.primary_genre, []).append(it)

# Cold start: near-uniform tastes. Behavior params (archetype) stay distinct.
tastes = rng.dirichlet(np.full(len(genres), 5.0), size=N)

cluster_trend = {a.name: np.ones(len(genres)) / len(genres) for a in ARCHETYPES}
global_trend = np.ones(len(genres)) / len(genres)
paths = [tastes.copy()]

for day in range(1, DAYS + 1):
    # current behavioral cluster per agent (nearest archetype centroid of *behavioral* taste)
    day_counts = {a.name: np.zeros(len(genres)) for a in ARCHETYPES}
    day_total = np.zeros(len(genres))
    for pi, p in enumerate(personas):
        n_watch = int(rng.poisson(4.0) + 1)
        for _ in range(n_watch):
            mix = (0.6 * tastes[pi] + 0.25 * cluster_trend[p.archetype]
                   + 0.15 * global_trend)
            mix = mix / mix.sum()
            if rng.random() < p.search_propensity:
                g = genres[int(rng.integers(len(genres)))]
                alpha = 0.05
            else:
                g = genres[int(rng.choice(len(genres), p=mix))]
                alpha = 0.12
            tastes[pi] = (1 - alpha) * tastes[pi] + alpha * np.eye(len(genres))[gidx[g]]
            day_counts[p.archetype][gidx[g]] += 1
            day_total[gidx[g]] += 1
    for a in ARCHETYPES:
        tot = day_counts[a.name].sum()
        if tot > 0:
            cluster_trend[a.name] = (0.5 * cluster_trend[a.name]
                                    + 0.5 * day_counts[a.name] / tot)
    tot = day_total.sum()
    if tot > 0:
        global_trend = 0.5 * global_trend + 0.5 * day_total / tot
    paths.append(tastes.copy())
    if day % 10 == 0:
        print(f"day {day} done", file=sys.stderr)

# Emergent clusters: k-means on final-day tastes
X = paths[-1]
ci = rng.choice(N, K, replace=False)
cent = X[ci]
for _ in range(100):
    d2 = ((X[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
    lab = d2.argmin(1)
    new = np.array([X[lab == k].mean(0) if (lab == k).any() else cent[k]
                    for k in range(K)])
    if np.allclose(new, cent):
        break
    cent = new
d2 = ((X[:, None, :] - cent[None, :, :]) ** 2).sum(-1)
final_lab = d2.argmin(1)

def label_day(T):
    return ((T[:, None, :] - cent[None, :, :]) ** 2).sum(-1).argmin(1)

cluster_names, cluster_top_genre = [], []
for k in range(K):
    top = genres[int(cent[k].argmax())]
    cluster_names.append(f"{top.title()} cluster")
    cluster_top_genre.append(top)

# One stable PCA projection for all days
all_t = np.stack(paths)
flat = all_t.reshape(-1, len(genres))
mean = flat.mean(axis=0)
U, S, Vt = np.linalg.svd(flat - mean, full_matrices=False)
proj = ((flat - mean) @ Vt[:2].T).reshape(DAYS + 1, N, 2)
mn, mx = proj.min(axis=(0, 1)), proj.max(axis=(0, 1))
proj = (proj - mn) / (mx - mn)

agents = []
for i, p in enumerate(personas):
    labs = [int(l) for l in [label_day(paths[d])[i] for d in range(DAYS + 1)]]
    agents.append({
        "id": p.persona_id,
        "archetype": p.archetype,
        "path": [[round(float(x), 4), round(float(y), 4)] for x, y in proj[:, i, :]],
        "cluster_by_day": labs,
    })

cent2d = []
for k in range(K):
    xy = (cent[k] - mean) @ Vt[:2].T
    xy = (xy - mn) / (mx - mn)
    cent2d.append([round(float(xy[0]), 4), round(float(xy[1]), 4)])

counts_by_day = []
for d in range(DAYS + 1):
    labs = [a["cluster_by_day"][d] for a in agents]
    counts_by_day.append([labs.count(k) for k in range(K)])

out = {
    "days": DAYS, "n_agents": N, "genres": genres,
    "clusters": [{"id": k, "name": cluster_names[k],
                  "top_genre": cluster_top_genre[k],
                  "centroid_2d": cent2d[k],
                  "final_size": int((final_lab == k).sum())} for k in range(K)],
    "agents": agents,
    "cluster_counts_by_day": counts_by_day,
}
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo-page", "evolution.json"), "w") as f:
    json.dump(out, f)

p0, p30 = proj[0], proj[-1]
s0 = float(np.linalg.norm(p0 - p0.mean(0), axis=1).mean())
s30 = float(np.linalg.norm(p30 - p30.mean(0), axis=1).mean())
disp = float(np.linalg.norm(p30 - p0, axis=1).mean())
print(f"spread day0={s0:.3f} day30={s30:.3f} mean_disp={disp:.3f}", file=sys.stderr)
print("final clusters:", [(cluster_names[k], int((final_lab == k).sum())) for k in range(K)], file=sys.stderr)
print("wrote evolution.json", file=sys.stderr)
