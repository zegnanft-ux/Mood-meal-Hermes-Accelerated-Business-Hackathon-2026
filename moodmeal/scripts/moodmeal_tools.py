"""
MoodMeal tools — the business layer the Hermes agent calls.

Five tools:
  search_menu(budget, keywords=None, tags=None)  -> candidate dishes within budget   [Scout]
  score_health(dish_ids=None, dishes=None)        -> objective nutrition facts per dish [Dietitian]
  check_spend_limit(amount)                       -> NemoClaw-style spend gate (call BEFORE any charge)
  charge_wallet(amount, dish_id=None, note="")    -> spend from the agent wallet via Stripe (gated)
  request_topup_link(amount)                       -> Stripe link to refill the wallet when low

Design notes:
  * No business numbers are hardcoded in logic — caps/fee/currency live in config.json.
  * search_menu does retrieval only; the *mood reading* is the LLM's job (it passes keywords/tags).
  * Runs OFFLINE by default (simulated Stripe). Set STRIPE_API_KEY (sk_test_...) to use real Stripe test mode.
  * check_spend_limit is the single safety gate — charge_wallet refuses unless it passes first.
"""

import json
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "menu.json"
CONFIG = ROOT / "config.json"
STATE = ROOT / "state" / "wallet.json"
SCHEDULE = ROOT / "state" / "schedule.json"


# ---------- config / state ----------

def _cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _menu() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _load_wallet() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    cfg = _cfg()
    wallet = {"balance": cfg["starting_balance"], "currency": cfg["currency"], "ledger": []}
    _save_wallet(wallet)
    return wallet


def _save_wallet(wallet: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(wallet, indent=2), encoding="utf-8")


def _env_key(name: str) -> str:
    """Read a secret from the process env, falling back to ~/.hermes/.env (or a skill-local .env).
    Hermes does NOT propagate .env vars into the execute_code sandbox, so os.environ is empty there;
    the tool reads the .env file directly as a fallback so real Stripe works when invoked by the agent."""
    val = os.environ.get(name, "")
    if val:
        return val
    for p in (Path.home() / ".hermes" / ".env", ROOT / ".env"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith(name + "="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def _stripe():
    """Return the stripe module configured for test mode, or None to simulate."""
    key = _env_key("STRIPE_API_KEY")
    if not key.startswith("sk_test_"):
        return None
    try:
        import stripe
    except ImportError:
        return None
    stripe.api_key = key
    return stripe


# ---------- TOOL 1: search_menu ----------

def search_menu(budget: float, limit: int = 30) -> dict:
    """Return every dish within budget. PURE budget filter — no mood or health judgment here.
    The mood fit (Host) and the health read (Dietitian) are reasoned by the LLM over the real
    facts each dish carries (calories, macros, ingredients). We do not pre-label dishes."""
    out = [d for d in _menu()["dishes"] if d["price"] <= budget]
    out.sort(key=lambda x: x["price"])
    return {"ok": True, "budget": budget, "currency": _cfg()["currency"],
            "count": len(out), "results": out[:limit]}


# ---------- TOOL 2: check_spend_limit (NemoClaw gate) ----------

def check_spend_limit(amount: float) -> dict:
    """Bounded-autonomy spend gate. Enforces (all from config, never hardcoded):
      - per_order_cap   : max single order
      - reserve         : balance floor the agent must never spend below
      - daily_spend_cap : max spent in a rolling 24h
    Also returns `auto_approve`: True when the amount is small enough to pay WITHOUT asking the
    user (autonomous), False when it's allowed but should be confirmed first."""
    cfg = _cfg()
    wallet = _load_wallet()
    cap = cfg["per_order_cap"]
    bal = wallet["balance"]
    reserve = cfg.get("reserve", 0)
    daily_cap = cfg.get("daily_spend_cap")
    auto_under = cfg.get("auto_approve_under", 0)

    now = int(time.time())
    spent_24h = sum(-e["amount"] for e in wallet["ledger"]
                    if e.get("type") == "food_spend" and now - e.get("ts", 0) < 86400)

    base = {"cap": cap, "balance": bal, "reserve": reserve,
            "daily_cap": daily_cap, "spent_24h": round(spent_24h, 2)}
    if amount > cap:
        return {"allowed": False, "reason": f"amount {amount} exceeds per-order cap {cap}", **base}
    if amount > bal - reserve:
        return {"allowed": False, "reason": f"would breach reserve {reserve} (balance {bal})",
                "suggest": "request_topup_link", **base}
    if daily_cap is not None and spent_24h + amount > daily_cap:
        return {"allowed": False, "reason": f"would exceed daily cap {daily_cap} (already spent {round(spent_24h, 2)} in 24h)", **base}
    auto = amount <= auto_under
    return {"allowed": True, "auto_approve": auto,
            "reason": f"auto-approved (≤ {auto_under})" if auto else "within caps — confirm with user first",
            **base}


# ---------- TOOL 3: charge_wallet (internal wallet spend) ----------

def charge_wallet(amount: float, dish_id: str = None, note: str = "", auto: bool = False) -> dict:
    """Spend from the prepaid wallet. Refuses unless check_spend_limit passes. Books a small fee as revenue.
    This is an INTERNAL balance deduction — it does NOT charge Stripe. Money enters the business ONCE,
    when the customer tops up (request_topup_link/confirm_topup); ordering just draws that balance down.
    That keeps the Stripe dashboard coherent (top-ups only, no per-order double-charge).
    Pass auto=True ONLY when a pre-approved scheduled (cron) order fires autonomously — it flags the
    ledger entry so the dashboard can show which orders the agent paid by itself."""
    gate = check_spend_limit(amount)
    if not gate["allowed"]:
        return {"ok": False, "blocked_by": "nemoclaw", **gate}

    cfg = _cfg()
    wallet = _load_wallet()
    merchant = _menu()["merchant_id"]
    fee = round(amount * cfg["order_fee_rate"], 2)          # MoodMeal's commission (revenue)
    merchant_payout = round(amount - fee, 2)                # what the restaurant receives
    cur = cfg["currency"]                       # e.g. CNY
    sym = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(cur, "")
    dish_name = {d["id"]: d["name"] for d in _menu()["dishes"]}.get(dish_id, dish_id or "order")
    invoice_no = f"MM-{len(wallet['ledger']) + 1:04d}"
    receipt_url = None

    # INTERNAL wallet deduction only — ordering does NOT charge Stripe.
    # The money already entered the business once, at top-up time
    # (request_topup_link/confirm_topup). Spending the balance is just bookkeeping,
    # so there is no second Stripe charge per meal.
    payment_id = f"wallet_{uuid.uuid4().hex[:12]}"
    mode = "wallet"

    wallet["balance"] = round(wallet["balance"] - amount, 2)
    invoice = {
        "invoice_no": invoice_no,
        "ts": int(time.time()),
        "merchant": merchant,
        "currency": cur,
        "line_items": [{"item": dish_name, "dish_id": dish_id, "amount": amount}],
        "subtotal": amount,
        "merchant_commission": fee,            # MoodMeal's cut (commission revenue)
        "merchant_payout": merchant_payout,    # restaurant's share (settled via Stripe Connect in prod)
        "total_paid": amount,
        "payment_id": payment_id,
        "receipt_url": receipt_url,
        "mode": mode,
    }
    entry = {"ts": invoice["ts"], "type": "food_spend", "amount": -amount, "fee_revenue": fee,
             "merchant_payout": merchant_payout,
             "dish_id": dish_id, "merchant": merchant, "payment_id": payment_id,
             "invoice_no": invoice_no, "mode": mode,
             "receipt_url": receipt_url, "auto": bool(auto)}
    wallet["ledger"].append(entry)
    _save_wallet(wallet)

    # A printable receipt the agent can show verbatim.
    printed = (f"INVOICE {invoice_no}  ·  {merchant}\n"
               f"  {dish_name} ............ {sym}{amount}\n"
               f"  → Restaurant payout ... {sym}{merchant_payout}\n"
               f"  → MoodMeal commission . {sym}{fee}\n"
               f"  Total paid ............ {sym}{amount}  ({mode})\n"
               f"  Payment ID: {payment_id}")

    return {"ok": True, "mode": mode, "payment_id": payment_id, "amount": amount,
            "currency": cur, "fee_revenue": fee, "merchant_payout": merchant_payout,
            "commission": fee, "balance": wallet["balance"],
            "invoice": invoice, "invoice_no": invoice_no, "receipt_url": receipt_url,
            "printed_receipt": printed}


# ---------- TOOL 5: score_health (Dietitian) ----------

def score_health(dish_ids=None, dishes=None) -> dict:
    """Return objective nutrition FACTS per dish for the Dietitian agent to judge.

    Pass dish_ids (ids from the menu) or dishes (full dish dicts from search_menu).
    Returns facts only: calories, share of a daily reference, macros (protein/carb/fat),
    and ingredients. It does NOT label anything light/heavy/healthy — the health judgment
    for *this mood* is the Dietitian agent's job. Numbers live in config.json, not in code.
    """
    cfg = _cfg()
    ref = cfg.get("daily_calorie_reference", 2000)
    menu = {d["id"]: d for d in _menu()["dishes"]}
    items = dishes if dishes else [menu[i] for i in (dish_ids or []) if i in menu]
    out = []
    for d in items:
        cal = d.get("calories", 0)
        out.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "calories": cal,
            "pct_daily": round(100 * cal / ref, 1) if ref else None,
            "protein_g": d.get("protein_g"),
            "carb_g": d.get("carb_g"),
            "fat_g": d.get("fat_g"),
            "ingredients": d.get("ingredients"),
        })
    return {"ok": True, "daily_calorie_reference": ref, "items": out}


# ---------- TOOL 4: request_topup_link (Stripe earn / refill) ----------

def request_topup_link(amount: float) -> dict:
    """Create a Stripe link for the user to refill the agent wallet (caps the wallet total).
    Records the link as a pending top-up so confirm_topup() can credit the wallet once it's paid."""
    cfg = _cfg()
    wallet = _load_wallet()
    if wallet["balance"] + amount > cfg["wallet_total_cap"]:
        amount = max(0, cfg["wallet_total_cap"] - wallet["balance"])

    stripe = _stripe()
    if stripe is None:
        return {"ok": True, "mode": "simulated", "amount": amount,
                "url": f"https://checkout.stripe.com/test/topup/sim_{uuid.uuid4().hex[:8]}",
                "note": "simulated link — set STRIPE_API_KEY to generate a real test link"}

    link = stripe.PaymentLink.create(
        line_items=[{"price_data": {"currency": cfg["currency"].lower(),
                                    "product_data": {"name": "MoodMeal wallet top-up"},
                                    "unit_amount": int(amount * 100)}, "quantity": 1}],
    )
    wallet["pending_topup"] = {"link_id": link.id, "amount": amount, "ts": int(time.time())}
    _save_wallet(wallet)
    return {"ok": True, "mode": "stripe_test", "amount": amount, "url": link.url,
            "next": "after you pay this link with the test card, call confirm_topup() to credit the wallet"}


# ---------- TOOL 7: confirm_topup (credit wallet after the link is paid) ----------

def confirm_topup() -> dict:
    """Credit the wallet AFTER the user pays the most recent top-up link.
    Verifies WITH Stripe that the payment link was actually paid (test mode) before adding funds,
    so the balance can't be inflated without a real (test) payment. Idempotent: each Stripe
    checkout session is credited at most once."""
    cfg = _cfg()
    wallet = _load_wallet()
    pending = wallet.get("pending_topup")
    if not pending:
        return {"ok": False, "reason": "no pending top-up — call request_topup_link first"}

    stripe = _stripe()
    if stripe is None:
        return {"ok": False, "mode": "simulated",
                "reason": "Stripe not configured (simulated) — cannot verify a real payment"}
    try:
        sessions = stripe.checkout.Session.list(payment_link=pending["link_id"], limit=10)
    except Exception as e:
        return {"ok": False, "reason": f"could not reach Stripe to verify payment: {e}"}

    paid = next((s for s in sessions.data if s.get("payment_status") == "paid"), None)
    if paid is None:
        return {"ok": False, "pending": True,
                "reason": "no completed payment yet — pay the link with the test card, then run confirm_topup again"}

    if any(e.get("payment_id") == paid["id"] for e in wallet["ledger"]):
        return {"ok": True, "already_credited": True, "balance": wallet["balance"]}

    amount = pending["amount"]
    cap = cfg["wallet_total_cap"]
    if wallet["balance"] + amount > cap:
        amount = max(0, cap - wallet["balance"])
    wallet["balance"] = round(wallet["balance"] + amount, 2)
    wallet["ledger"].append({"ts": int(time.time()), "type": "topup", "amount": amount,
                             "payment_id": paid["id"], "mode": "stripe_test"})
    wallet.pop("pending_topup", None)
    _save_wallet(wallet)
    return {"ok": True, "credited": amount, "balance": wallet["balance"], "payment_id": paid["id"]}


# ---------- TOOL 6: add_schedule (records a standing order for the dashboard) ----------

def add_schedule(when: str, goal: str, budget: float, job_id: str = None,
                 recurrence: str = None) -> dict:
    """Record a standing order so the dashboard's 'Scheduled standing orders' panel shows it.
    Call this right AFTER you create the cronjob, with the same time/goal/budget.
      when       : human time/recurrence shown to the user ("tomorrow 1pm", "every workday 12:00")
      goal       : what to order for ("light lunch", "comfort dinner")
      budget     : per-run auto-pay budget the user pre-approved
      job_id     : the cronjob id you created (lets us remove it later)
      recurrence : optional cron expression if recurring
    This does NOT schedule anything itself — the native cronjob tool does that. It only logs the
    standing order to state/schedule.json for visibility."""
    items = []
    if SCHEDULE.exists():
        try:
            items = json.loads(SCHEDULE.read_text(encoding="utf-8"))
        except Exception:
            items = []
    items.append({"job_id": job_id, "when": when, "recurrence": recurrence,
                  "goal": goal, "budget": budget, "ts": int(time.time())})
    SCHEDULE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return {"ok": True, "count": len(items), "schedule": items}


# ---------- CLI: lets `execute_code` / terminal call any tool ----------

if __name__ == "__main__":
    import sys
    fns = {"search_menu": search_menu, "check_spend_limit": check_spend_limit,
           "charge_wallet": charge_wallet, "request_topup_link": request_topup_link,
           "confirm_topup": confirm_topup,
           "score_health": score_health, "add_schedule": add_schedule}
    if len(sys.argv) < 2 or sys.argv[1] not in fns:
        print(json.dumps({"error": "usage: moodmeal_tools.py <tool> '<json-args>'", "tools": list(fns)}))
        sys.exit(1)
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(fns[sys.argv[1]](**args), indent=2))
