---
name: moodmeal-lite
description: Fast MoodMeal — the same mood-to-meal food agent, but single-pass (no subagents) for quick casual orders. Reads mood, finds dishes in budget + scores their health in ONE step, offers two, runs the spend gate, and pays from the capped wallet. Use for normal/fast ordering; use the full moodmeal crew only when you want a detailed multi-agent run.
version: 0.1.0
metadata:
  hermes:
    tags: [food, mood, payments, stripe, agent-business, fast]
---

# MoodMeal · Lite (fast path)

Same business as the full `moodmeal` crew — read the mood, find a meal in budget, pay safely — but **you do it yourself in one pass**, no Scout/Dietitian subagents. This is the fast path for real lunch speed. The mood reading is yours; menu + health + payment are tools.

It reuses the **same** tools, menu, wallet, and config as `moodmeal` (shared `state/wallet.json`), so orders here show up everywhere.

## Tools (all in the shared moodmeal scripts)

```python
import sys, json
sys.path.append("/home/automationlearn/.hermes/skills/business/moodmeal/scripts")
from moodmeal_tools import search_menu, score_health, check_spend_limit, charge_wallet, request_topup_link
```

## Workflow

1. **Read the mood.** Infer emotional state + what would help (comfort vs balanced). Pull memory of past picks first. The mood judgment is yours.

2. **One pass: find + facts.** In a SINGLE `execute_code` block, get the in-budget dishes and their nutrition facts. Cache the result for follow-ups:

```python
import sys, json
sys.path.append("/home/automationlearn/.hermes/skills/business/moodmeal/scripts")
from moodmeal_tools import search_menu, score_health

BUDGET = 20
cands = search_menu(BUDGET)["results"]          # pure budget filter — facts only
health = {h["id"]: h for h in score_health(dish_ids=[c["id"] for c in cands])["items"]}
print(json.dumps({"candidates": cands, "health": health}, ensure_ascii=False))
```

   `search_menu` returns facts (calories, macros, ingredients) — no tags. *You* do the mood + health reasoning over those facts.

3. **Offer two.** From that one result, present exactly two — one leaning **comfort/satisfying**, one leaning **light/balanced** — as a table: **Dish · Price · Calories · Health read · Why it fits**. Calories come from `health[id].calories`; the **health read** is *your* judgment reasoned from calories + macros + ingredients (e.g. "780 cal, fried + white rice → heavy, sluggish"); the **why it fits** must reference their *actual mood/situation*, never a canned label. Use `clarify` to let them pick and approve paying.

4. **Follow-ups are free — reuse, don't re-run.** If they say *"something refreshing instead"*, *"anything lighter?"* — you already have the candidates + health from step 2. Just **re-pick from what you have** and offer two new ones immediately. Only re-run step 2 if the **budget changes**.

5. **Gate the spend.** After approval, `check_spend_limit(amount)`. If `allowed` is false: over cap → cheaper option; low balance → `request_topup_link` and share it. **Never skip the gate.**

6. **Pay.** Only if the gate passed, `charge_wallet(amount, dish_id, note)`. Show the returned `printed_receipt` **verbatim** (it's formatted in $ with the invoice number + payment id; includes a `receipt_url` if real Stripe is on) plus the new wallet balance. Always $/USD, never ¥.

7. **Remember.** Save the outcome to memory so next time is smarter.

## THE SAFETY RULE (non-negotiable)
**Never call `charge_wallet` before `check_spend_limit` has returned `allowed: true` for that exact amount.** The gate is the whole trust model.

## Economy
Every order books a small fee (`order_fee_rate` in config) as revenue. The wallet is capped per-order and total. The agent earns from doing its job and can fund its own costs — a self-funding business.
