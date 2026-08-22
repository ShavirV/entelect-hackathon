"""
Age of Enteland - tail-income A/B harness.

Runs the shared build pipeline once, then appends three different tail
strategies (none / capped / uncapped) and reports the engine-visible
metrics for each side by side. Use this to find where the real payoff
curve bends for a given level file, instead of hand-editing solver files
and re-running them one at a time.

Usage:
    python3 compare_tail_variants.py <level_file> <level_number> \
        [--hub HUB] [--upkeep] [--caps 500,1000,3000,10000]

Notes:
- "score" (total_infrastructure_score_value) is expected to be IDENTICAL
  across all variants on a given level, since the tail never unlocks
  additional builds under the current pipeline (build_actions_for_pairs
  already self-funds any Enteloot shortfall for the chosen prefix; the
  tail only runs *after* that prefix is finalized). If you see score
  differ between variants, that's a signal something upstream is
  non-deterministic or budget-order-sensitive and worth investigating
  before trusting any of these numbers.
- What genuinely varies is final_enteloot (the thing whose scoring
  weight is unknown/opaque to this repo) and action_count / final_tick
  (the cost side of that trade-off: bigger submissions, more solve time,
  more idle-vs-used ticks).
"""

import argparse
import sys
import time

from engine import load_json
from common_solver import (
    candidate_upgrade_order,
    plan_feasible_prefix,
    plan_income_tail,
    replay,
)


def find_crafting_hub(level):
    start = level["run"]["starting_town"]
    if "crafting" in level["towns"][start].get("affinities", []):
        return start
    candidates = [n for n, t in level["towns"].items() if "crafting" in t.get("affinities", [])]
    return sorted(candidates)[0] if candidates else start


def mine_reachable(level, hub):
    from common_solver import build_adjacency, dijkstra
    adj = build_adjacency(level["routes"])
    dist, _ = dijkstra(adj, hub)
    for name, node in level["nodes"].items():
        if node["type"] == "mine" and name in dist:
            return True
    return False


def run_variant(constants, level, level_number, hub, base_actions, kept_pairs,
                 ordered_pairs, use_upkeep, label, max_tail_ticks):
    t0 = time.time()
    if max_tail_ticks == "none":
        actions = base_actions
    else:
        cap = None if max_tail_ticks == "uncapped" else int(max_tail_ticks)
        actions, _result = plan_income_tail(
            constants, level, level_number, hub, base_actions,
            use_upkeep=use_upkeep, max_tail_ticks=cap,
        )
    elapsed = time.time() - t0
    result, invalid = replay(constants, level, level_number, actions)
    return {
        "label": label,
        "action_count": len(actions),
        "invalid": len(invalid),
        "final_tick": result["final_tick"],
        "total_ticks": level["run"]["total_ticks"],
        "score": result["total_infrastructure_score_value"],
        "final_enteloot": result["final_enteloot"],
        "solve_seconds": round(elapsed, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("level_file")
    ap.add_argument("level_number", type=int)
    ap.add_argument("--hub", default=None, help="Override crafting hub town")
    ap.add_argument("--upkeep", action="store_true", help="Trigger upkeep during build tour (Level 4)")
    ap.add_argument("--caps", default="500,1000,3000,10000",
                     help="Comma-separated MAX_TAIL_TICKS values to test, plus 'none' and 'uncapped' always included")
    args = ap.parse_args()

    constants = load_json("resources.json")
    level = load_json(args.level_file)
    hub = args.hub or find_crafting_hub(level)

    print(f"Level file: {args.level_file}  |  level_number={args.level_number}  |  hub={hub}")
    print(f"Tick budget: {level['run']['total_ticks']}  |  starting Enteloot: {level['run']['starting_enteloot']}")

    have_mine = mine_reachable(level, hub) if args.level_number >= 3 else False
    extra_target_items = [("boots", 1), ("pickaxe", 1)] if have_mine else None

    ordered_pairs = candidate_upgrade_order(constants, list(level["towns"].keys()), args.level_number)
    base_actions, meta, kept_pairs = plan_feasible_prefix(
        constants, level, args.level_number, ordered_pairs, hub,
        extra_target_items=extra_target_items, use_upkeep=args.upkeep,
    )
    base_result, base_invalid = replay(constants, level, args.level_number, base_actions)
    print(f"Base build-only prefix: {len(kept_pairs)}/{len(ordered_pairs)} upgrades, "
          f"score={base_result['total_infrastructure_score_value']}, "
          f"final_tick={base_result['final_tick']}, invalid={len(base_invalid)}")
    if base_invalid:
        print("WARNING: base prefix has invalid actions - fix that before comparing tail variants.")
        return 1

    variant_specs = ["none"] + [c.strip() for c in args.caps.split(",") if c.strip()] + ["uncapped"]

    rows = []
    for spec in variant_specs:
        rows.append(run_variant(
            constants, level, args.level_number, hub, base_actions, kept_pairs,
            ordered_pairs, args.upkeep, label=spec, max_tail_ticks=spec,
        ))

    headers = ["variant", "actions", "invalid", "final_tick/total", "score", "final_enteloot", "solve_s"]
    widths = [12, 9, 8, 18, 10, 15, 9]
    print()
    print("".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-" * sum(widths))
    for r in rows:
        print("".join(str(v).ljust(w) for v, w in zip([
            r["label"], r["action_count"], r["invalid"],
            f"{r['final_tick']}/{r['total_ticks']}", r["score"],
            r["final_enteloot"], r["solve_seconds"],
        ], widths)))

    scores = {r["score"] for r in rows}
    if len(scores) > 1:
        print("\nNOTE: score differs across variants - investigate before trusting these numbers "
              "(expected to be identical; the tail shouldn't change infra score under the current pipeline).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())