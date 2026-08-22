import json
from Simulator import Simulator

with open("level1.json") as f:
    level = json.load(f)
with open("constants.json") as f:
    constants = json.load(f)

# Sample action sequence: gather wheat at N1, sell at Piltover (doesn't
# produce wheat -> pays full price), try an invalid action, try selling
# more than we have, etc. Exercises travel/gather/buy/sell + invalid handling.
actions = [
    {"type": "travel", "destination": "N1"},
    {"type": "gather"},
    {"type": "gather"},
    {"type": "travel", "destination": "Demacia"},
    {"type": "sell", "item": "wheat", "quantity": 12},
    {"type": "buy", "item": "sheep", "quantity": 3},
    {"type": "travel", "destination": "Piltover"},
    {"type": "sell", "item": "sheep", "quantity": 3},
    {"type": "travel", "destination": "Nowhere"},   # invalid: no such route
    {"type": "craft", "item": "bread", "quantity": 1},  # invalid: not unlocked at L1
    {"type": "sell", "item": "fish", "quantity": 999},  # invalid: not enough
]

sim = Simulator(level, constants, level_number=1)
log = sim.run(actions)

print(f"{'#':<3}{'type':<10}{'valid':<7}{'ticks':<7}{'tick':<6}{'enteloot':<10}detail")
for e in log:
    print(f"{e.index:<3}{e.action.get('type',''):<10}{str(e.valid):<7}"
          f"{e.ticks_used:<7}{e.tick_after:<6}{e.enteloot_after:<10.1f}{e.detail}")

print()
print("=== SUMMARY ===")
for k, v in sim.summary().items():
    print(f"{k}: {v}")