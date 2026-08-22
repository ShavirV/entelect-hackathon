import json
from Simulator import Simulator

print("=== AGE OF ENTELAND - LEVEL 2 ===\n")

with open("level2.json") as f:
    level = json.load(f)
with open("constants.json") as f:
    constants = json.load(f)

# ============================================================
# LEVEL 2 OPTIMIZED ACTION SEQUENCE
# ============================================================

actions = []

# ============================================================
# PHASE 1: Initial Crafting (Ticks 0-800)
# ============================================================

# Travel to N1 (wheat node, 2 ticks from Demacia)
actions.append({"type": "travel", "destination": "N1"})

# Gather wheat: 60 gathers * 6 wheat = 360 wheat = 120 bread
for _ in range(60):
    actions.append({"type": "gather"})

# Travel back to Demacia (crafting affinity)
actions.append({"type": "travel", "destination": "Demacia"})

# Craft bread at Demacia (1 tick each with affinity)
actions.append({"type": "craft", "item": "bread", "quantity": 120})

# Travel to Freljord (pays 40 for bread - highest)
actions.append({"type": "travel", "destination": "Freljord"})

# Sell bread
actions.append({"type": "sell", "item": "bread", "quantity": 120})

# ============================================================
# PHASE 2: More Crafting (Ticks 800-1600)
# ============================================================

actions.append({"type": "travel", "destination": "N1"})

# Gather more wheat
for _ in range(100):
    actions.append({"type": "gather"})

actions.append({"type": "travel", "destination": "Demacia"})

# Craft more bread
actions.append({"type": "craft", "item": "bread", "quantity": 200})

actions.append({"type": "travel", "destination": "Freljord"})

# Sell bread
actions.append({"type": "sell", "item": "bread", "quantity": 200})

# ============================================================
# PHASE 3: Build Fertilised-fields (Ticks 1600-2000)
# ============================================================

# Need: 2 fencing + 2 thatch
# Thatch: 2 wheat each = 4 wheat
actions.append({"type": "travel", "destination": "N1"})
actions.append({"type": "gather"})  # 6 wheat (enough for 3 thatch)
actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "thatch", "quantity": 2})

# Rope: 2 sheep each, need 2 rope = 4 sheep
actions.append({"type": "travel", "destination": "N6"})
actions.append({"type": "gather"})  # 3 sheep
actions.append({"type": "gather"})  # 6 sheep (enough for 3 rope)
actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "rope", "quantity": 2})

# Wood for fencing: 4 wood (2 per fencing, need 2 fencing)
actions.append({"type": "travel", "destination": "Noxus"})
actions.append({"type": "travel", "destination": "N2"})
actions.append({"type": "gather"})  # 5 wood
actions.append({"type": "travel", "destination": "Noxus"})
actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "fencing", "quantity": 2})

# Build Fertilised-fields at Demacia (doubles wheat)
actions.append({"type": "build", "upgrade": "fertilised-fields"})

# ============================================================
# PHASE 4: Crafting with Boosted Production (Ticks 2000-3000)
# ============================================================

actions.append({"type": "travel", "destination": "N1"})
for _ in range(120):
    actions.append({"type": "gather"})

actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "bread", "quantity": 240})

actions.append({"type": "travel", "destination": "Freljord"})
actions.append({"type": "sell", "item": "bread", "quantity": 240})

# ============================================================
# PHASE 5: Build Farmhouse (Ticks 3000-3500)
# ============================================================

# Need: 3 planks + 2 thatch
# Planks: 2 wood each = 6 wood
actions.append({"type": "travel", "destination": "Noxus"})
actions.append({"type": "travel", "destination": "N2"})
actions.append({"type": "gather"})  # 5 wood
actions.append({"type": "gather"})  # 10 wood
actions.append({"type": "travel", "destination": "Noxus"})
actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "planks", "quantity": 3})

# Thatch: 2 wheat each = 4 wheat
actions.append({"type": "travel", "destination": "N1"})
actions.append({"type": "gather"})  # 6 wheat
actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "thatch", "quantity": 2})

# Build Farmhouse at Demacia (doubles sheep)
actions.append({"type": "build", "upgrade": "farmhouse"})

# ============================================================
# PHASE 6: Final Crafting Push (Ticks 3500-5000)
# ============================================================

actions.append({"type": "travel", "destination": "N1"})
for _ in range(150):
    actions.append({"type": "gather"})

actions.append({"type": "travel", "destination": "Demacia"})
actions.append({"type": "craft", "item": "bread", "quantity": 300})

actions.append({"type": "travel", "destination": "Freljord"})
actions.append({"type": "sell", "item": "bread", "quantity": 300})

# ============================================================
# RUN SIMULATION
# ============================================================

print(f"Generated {len(actions)} actions")
print()

sim = Simulator(level, constants, level_number=2)
log = sim.run(actions)

# Print log
print(f"{'#':<4}{'type':<12}{'valid':<7}{'ticks':<7}{'tick':<6}{'enteloot':<12}detail")
for e in log:
    action_type = e.action.get('type', '') if isinstance(e.action, dict) else str(e.action)
    print(f"{e.index:<4}{action_type:<12}{str(e.valid):<7}"
          f"{e.ticks_used:<7}{e.tick_after:<6}{e.enteloot_after:<12.1f}{e.detail[:60]}")

print()
print("=" * 60)
print("=== LEVEL 2 SUMMARY ===")
print("=" * 60)
for k, v in sim.summary().items():
    print(f"{k}: {v}")

# Write to output.txt for submission
output = {"actions": actions}
with open("output.txt", "w") as f:
    json.dump(output, f, indent=2)
print(f"\nWritten {len(actions)} actions to output.txt")