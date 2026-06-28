# MoodMeal

**The food agent that runs itself.** Tell it how you feel and what you'll spend — it finds the dish, checks the health honestly, and pays from a capped wallet. Set it on a schedule and it orders for you, on time, on budget, with no tap.

Built for the **Hermes Agent — Accelerated Business Hackathon (2026)**.

---

## What it does

You give MoodMeal a **mood** and a **budget** ("stressed, dinner soon, ~$20"). It runs the whole loop end to end:

```
mood  →  scout  →  health filter  →  verdict  →  spend gate  →  pay
```

1. **Mood** — the agent reads how you feel and what would actually help (light? comfort? warm?).
2. **Scout** — a tool call (`search_menu`) returns every dish *within budget* with real facts: calories, macros, ingredients. No judgment here — just options.
3. **Health filter** — the agent **delegates to a separate dietitian agent** (`moodmeal-dietitian`) so the nutrition verdict is reasoned coldly, on its own terms, not bent to justify a tasty pick. Integrity by separation.
4. **Verdict** — it offers exactly two dishes, one comfort and one light, as a table: price · calories · the dietitian's health read · why it fits your mood. You pick.
5. **Spend gate** — before *any* payment, `check_spend_limit` enforces hard per-order and total caps. If the wallet is too low it **stops and sends a Stripe top-up link** instead of overspending.
6. **Pay** — only if the gate passes, it charges the capped wallet, books a small fee as revenue, and returns a real receipt.

## The autonomy (what makes it an *agent*, not a recommender)

You can also schedule a standing order — *"lunch every workday at 12, keep it light, ~$25."* You approve **once** at setup; from then on it wakes up on a cron, runs the full pipeline, and **auto-pays within the caps with no tap.** The caps are the hard floor even on auto-pay, so it can never run away.

> A recommender suggests. An agent acts. That's the difference.

## Safety & money model

- **Capped wallet** — a prepaid balance with a per-order cap, a total cap, and a reserve. The spend gate is checked before every charge; the agent cannot overspend.
- **Stripe top-ups** — when the balance runs low, the agent generates a Stripe link; a human pays it with a card, then the agent verifies the payment and credits the wallet. The agent never moves money into its own wallet on its own.
- **Self-funding** — every order books a small fee (`order_fee_rate`) as revenue, so the business earns from doing its job.
- **Stripe test mode** — runs against `sk_test_` keys only; no real money moves.

## Project layout

```
moodmeal/              the main skill (host: mood → scout → delegate → pay → schedule)
  SKILL.md             the agent's instructions
  config.json          caps, fee rate, reserve
  data/menu.json       the menu (dish facts)
  scripts/             tools: search_menu, check_spend_limit, charge_wallet, top-ups, add_schedule
  console.html         live dashboard (wallet balance, ledger, standing orders)
  dash_server.py       serves the dashboard + reconciles fired one-shot orders out of the panel
dietitian/             the independent health-judge subagent (delegate target)
scout/, moodmeal-lite/ earlier / minimal variants
```

## Configuration

Secrets live in `~/.hermes/.env` (never committed). Set `STRIPE_API_KEY` to an `sk_test_...` key to use real Stripe test mode; without it, the wallet runs in offline/simulated mode. Caps and the fee rate are in `moodmeal/config.json`.

> Runtime state (wallet balance, ledger, schedules) lives in a gitignored `state/` folder and is not part of the repo.

## Stack

Hermes Agent runtime · native `cronjob` + `delegate_task` · Stripe (test mode) · Python tools via `execute_code`.

---

*Built to learn — feedback and critique very welcome.*
