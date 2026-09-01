#!/usr/bin/env python3
"""Live Baseline Seeder & E2E Webhook Test Script for Merchant FinPilot.

Seeds historical baseline payments (past 7 days, >=100 transactions) into
the deployed Render backend, then sends real-time signed Razorpay TEST
`payment.failed` webhooks to verify the complete 6-stage autonomous pipeline:

WEBHOOK -> INGESTION -> DETECT -> INVESTIGATE -> REASON -> VERIFY -> AUTHORIZE -> EXECUTE
"""

import argparse
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error


def send_http(url: str, method: str = "GET", data: bytes = None, headers: dict = None, timeout: int = 30) -> tuple:
    """Send an HTTP request using Python standard library (no extra dependencies)."""
    headers = headers or {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body_bytes = resp.read()
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            return status, body_json
    except urllib.error.HTTPError as err:
        body_bytes = err.read()
        try:
            body_json = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            body_json = {"error": err.reason, "raw": body_bytes.decode("utf-8", errors="replace")}
        return err.code, body_json
    except Exception as exc:
        return 0, {"error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed baseline and test live FinPilot webhooks on Render.")
    parser.add_argument("--base-url", required=True, help="Base URL of deployed Render service (e.g. https://merchant-finpilot-api.onrender.com)")
    parser.add_argument("--webhook-secret", required=True, help="Razorpay Webhook Secret configured on Render")
    parser.add_argument("--merchant-id", default="merchant_alpha", help="Target merchant ID")
    parser.add_argument("--cluster-size", type=int, default=3, help="Number of failure webhooks to send in incident cluster")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    secret = args.webhook_secret
    merchant_id = args.merchant_id

    print("=" * 70)
    print(" Merchant FinPilot - Live Baseline Seeder & Webhook E2E Tester")
    print("=" * 70)
    print(f"Target API:     {base_url}")
    print(f"Merchant ID:    {merchant_id}")
    print(f"Failure Count:  {args.cluster_size}")
    print("=" * 70)

    # 1. Health Check
    print("\n[Step 1/4] Checking deployment health...")
    status, health = send_http(f"{base_url}/api/v1/health")
    if status != 200:
        print(f"❌ Health check failed ({status}): {health}")
        return 1
    print(f"✓ Health Check OK: mode={health.get('execution_mode')}, service={health.get('service')}")

    # 2. Seed Baseline (7 Days Normal Historical Baseline)
    print(f"\n[Step 2/4] Seeding 7-day historical baseline for merchant '{merchant_id}'...")
    seed_payload = json.dumps({
        "merchant_id": merchant_id,
        "scenario_id": "normal",
        "context_notes": "Automated baseline generation for live test"
    }).encode("utf-8")
    status, seed_res = send_http(
        f"{base_url}/api/v1/incidents/process",
        method="POST",
        data=seed_payload,
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    if status not in (200, 201):
        print(f"❌ Baseline seeding failed ({status}): {seed_res}")
        return 1
    print(f"✓ 7-Day Baseline Established! (Historical payments saved to SQLite)")

    # 3. Send Real-Time Signed Razorpay TEST Webhooks
    print(f"\n[Step 3/4] Sending {args.cluster_size} real-time signed Razorpay TEST `payment.failed` webhooks...")
    job_ids = []
    for i in range(args.cluster_size):
        now_ts = int(time.time())
        event_id = f"evt_live_test_{now_ts}_{i + 1}"
        payment_id = f"pay_live_test_{now_ts}_{i + 1}"
        order_id = f"order_live_test_{now_ts}_{i + 1}"

        payload_dict = {
            "entity": "event",
            "id": event_id,
            "account_id": f"acc_{merchant_id}",
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "entity": "payment",
                        "amount": 45000,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": order_id,
                        "method": "upi",
                        "vpa": "customer@okhdfcbank",
                        "email": "customer@example.com",
                        "contact": "+919876543210",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "UPI bank server timeout during payment authorization",
                        "error_source": "bank",
                        "error_step": "payment_authorization",
                        "error_reason": "payment_failed",
                        "created_at": now_ts,
                    }
                }
            },
            "created_at": now_ts,
        }

        body_bytes = json.dumps(payload_dict).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

        webhook_url = f"{base_url}/api/v1/webhooks/razorpay?merchant_id={merchant_id}"
        wh_status, wh_res = send_http(
            webhook_url,
            method="POST",
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            }
        )

        if wh_status != 200:
            print(f"❌ Webhook {i + 1} rejected ({wh_status}): {wh_res}")
            return 1

        job_id = wh_res.get("job_id")
        job_status = wh_res.get("job_status") or wh_res.get("status")
        job_ids.append(job_id)
        print(f"  ✓ Webhook {i + 1}/{args.cluster_size} accepted: event={event_id} payment={payment_id} -> job={job_id} ({job_status})")
        time.sleep(1)

    # 4. Monitor Background Pipeline Execution
    print(f"\n[Step 4/4] Monitoring {len(job_ids)} background worker pipeline jobs...")
    completed_jobs = {}
    for attempt in range(40):
        time.sleep(2.5)
        all_done = True
        for jid in job_ids:
            if jid in completed_jobs:
                continue

            j_status, job_res = send_http(f"{base_url}/api/v1/incidents/jobs/{jid}")
            if j_status != 200:
                print(f"  [Attempt {attempt + 1}] Job {jid}: HTTP {j_status} -> {job_res}")
                all_done = False
                continue

            job_data = job_res.get("job") if isinstance(job_res.get("job"), dict) else job_res
            if not isinstance(job_data, dict):
                job_data = {}

            status_val = str(job_data.get("status") or "evaluating").lower()
            pipe_res = job_data.get("pipeline_result")
            if not pipe_res and job_data.get("payload_json"):
                try:
                    pipe_res = json.loads(job_data["payload_json"])
                except Exception:
                    pipe_res = {}
            if not isinstance(pipe_res, dict):
                pipe_res = {}

            scen_cls = pipe_res.get("scenario_classification") or {}
            scen_id = scen_cls.get("scenario_id") or "evaluating"

            print(f"  [Attempt {attempt + 1}] Job {jid}: Status={status_val.upper()} | Scenario={scen_id}")

            if status_val in ("completed", "failed", "escalated"):
                completed_jobs[jid] = {
                    "job_data": job_data,
                    "pipe_res": pipe_res,
                    "status": status_val,
                }
            else:
                all_done = False

        if all_done:
            break

    print("\n" + "=" * 70)
    print(" CLUSTER EXECUTION SUMMARY")
    print("=" * 70)
    for jid in job_ids:
        info = completed_jobs.get(jid, {})
        jdata = info.get("job_data", {})
        pres = info.get("pipe_res", {})
        s_val = info.get("status", "unknown").upper()
        s_cls = pres.get("scenario_classification") or {}

        print(f"\n▶ Job: {jid} [{s_val}]")
        print(f"  Incident ID:        {jdata.get('incident_id', 'N/A')}")
        print(f"  Payment ID:         {jdata.get('payment_id', 'N/A')}")
        print(f"  Scenario:           {s_cls.get('scenario_id', 'N/A')} (confidence: {s_cls.get('confidence', 'N/A')})")
        if s_cls.get("rationale"):
            print(f"  Rationale:          {s_cls.get('rationale')}")

        v_res = pres.get("verification_result") or {}
        if v_res:
            print(f"  VERIFY Gate:        {v_res.get('status', 'N/A')} ({v_res.get('summary', 'N/A')})")

        pol_res = pres.get("policy_decision") or {}
        if pol_res:
            print(f"  AUTHORIZE Gate:     {pol_res.get('verdict', 'N/A')} ({pol_res.get('rationale', 'N/A')})")

        exec_res = pres.get("execution_result") or {}
        if exec_res:
            print(f"  EXECUTE Stage:      Outcome: {exec_res.get('outcome', 'N/A')}")
            if exec_res.get("provider_reference"):
                print(f"  Razorpay Ref:       {exec_res.get('provider_reference')}")
            if exec_res.get("action_type"):
                print(f"  Action Executed:    {exec_res.get('action_type')}")

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
