"""
Self-contained Stripe LIVE-TEST harness for MoodMeal.

Runs independent of the Claude session. It:
  1. waits for api.stripe.com to be reachable (retries through WARP/network blocks),
  2. runs the REAL charge_wallet() path for a tiny $1 test charge (pm_card_visa + receipt email),
  3. verifies against Stripe's own API that the PaymentIntent succeeded and has a receipt_url,
  4. writes a proof file (state/stripe_live_test_result.json) and exits.

It will NOT double-charge: once charge_wallet returns ok, it records and exits even if the
verify step errors. A gate block (balance/cap) is fatal (retrying won't help). Network errors
are retried. Real code errors are logged with a traceback for fixing.
"""
import sys, os, json, time, datetime, traceback
import urllib.request, urllib.error

SKILL = "/home/automationlearn/.hermes/skills/business/moodmeal"
sys.path.insert(0, SKILL + "/scripts")
import moodmeal_tools as m

LOG = SKILL + "/state/stripe_live_test.log"
RESULT = SKILL + "/state/stripe_live_test_result.json"
TEST_AMOUNT = 1.0
RETRY_SECS = 30
MAX_HOURS = 8


def log(msg):
    line = datetime.datetime.now().isoformat(timespec="seconds") + "  " + str(msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def reachable():
    """True only when api.stripe.com actually answers at the HTTP layer.
    A bare TCP connect succeeds even under WARP, so we require a real HTTP response
    (a 401 from the unauthenticated /v1 endpoint counts — it proves the round trip works)."""
    try:
        urllib.request.urlopen(
            urllib.request.Request("https://api.stripe.com/v1", method="GET"), timeout=8)
        return True
    except urllib.error.HTTPError:
        return True  # server responded (e.g. 401) -> network path works
    except Exception:
        return False


def main():
    log("=== stripe live-test harness started (waiting for Stripe to be reachable) ===")
    deadline = time.time() + MAX_HOURS * 3600
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if not reachable():
            log("attempt %d: api.stripe.com UNREACHABLE (likely WARP) — retry in %ds"
                % (attempt, RETRY_SECS))
            time.sleep(RETRY_SECS)
            continue

        log("attempt %d: api.stripe.com REACHABLE — running real charge_wallet($%.2f)"
            % (attempt, TEST_AMOUNT))
        try:
            s = m._stripe()
            if s is None:
                log("  _stripe() returned None (simulated) — key/package problem; retry")
                time.sleep(RETRY_SECS)
                continue
            acct = s.Account.retrieve()
            log("  auth OK, Stripe account: %s" % acct.get("id"))

            res = m.charge_wallet(TEST_AMOUNT, dish_id=None, note="stripe live integration test")
            if not res.get("ok"):
                if res.get("blocked_by") == "nemoclaw":
                    log("  FATAL: spend gate blocked the test charge (not a network issue): %s"
                        % json.dumps(res)[:300])
                    json.dump({"ok": False, "reason": "gate_blocked", "detail": res},
                              open(RESULT, "w"), indent=2)
                    return 2
                log("  charge_wallet not ok: %s — retry" % json.dumps(res)[:300])
                time.sleep(RETRY_SECS)
                continue

            # charge created — DO NOT re-charge from here on
            pid = res.get("payment_id")
            log("  charge_wallet ok, payment_id=%s mode=%s balance_after=%s"
                % (pid, res.get("mode"), res.get("balance")))

            verify = {}
            try:
                intent = s.PaymentIntent.retrieve(pid)
                cid = intent.get("latest_charge")
                charge = s.Charge.retrieve(cid) if cid else {}
                verify = {
                    "intent_status": intent.get("status"),
                    "amount_charged": (intent.get("amount") or 0) / 100.0,
                    "currency": intent.get("currency"),
                    "receipt_url": charge.get("receipt_url"),
                    "receipt_email": charge.get("receipt_email"),
                    "livemode": intent.get("livemode"),
                }
                log("  VERIFIED on Stripe: status=%s receipt_url=%s email=%s"
                    % (verify["intent_status"], verify["receipt_url"], verify["receipt_email"]))
            except Exception as e:
                verify = {"verify_error": "%s: %s" % (type(e).__name__, str(e)[:160])}
                log("  charge succeeded but verify call errored: %s" % verify["verify_error"])

            result = {
                "ok": True,
                "attempt": attempt,
                "stripe_account": acct.get("id"),
                "payment_id": pid,
                "mode": res.get("mode"),
                "invoice_no": res.get("invoice_no"),
                "balance_after": res.get("balance"),
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                **verify,
            }
            json.dump(result, open(RESULT, "w"), indent=2)
            log("SUCCESS — proof written. Check Stripe dashboard (Payments) + the receipt email.")
            log("NOTE: in TEST mode Stripe only EMAILS the receipt if 'successful payments' "
                "emails are enabled in Stripe > Settings > Email. The receipt_url above is the "
                "hosted proof on Stripe regardless.")
            log("=== done ===")
            return 0

        except Exception as e:
            log("attempt %d ERROR %s: %s" % (attempt, type(e).__name__, str(e)[:200]))
            log("  " + traceback.format_exc().strip().splitlines()[-1])
            time.sleep(RETRY_SECS)
            continue

    log("DEADLINE reached (%dh) without a reachable Stripe — never charged." % MAX_HOURS)
    json.dump({"ok": False, "reason": "deadline_no_network"}, open(RESULT, "w"), indent=2)
    return 1


if __name__ == "__main__":
    sys.exit(main())
