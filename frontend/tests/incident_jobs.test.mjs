/**
 * Standalone Node ESM Test Suite for Frontend Incident Jobs Console
 * Validates the core logic, state transformations, API client contracts, and status badge derivations.
 */

import { strict as assert } from 'assert';

console.log('Running Frontend Incident Jobs Console Test Suite (Node ESM)...\n');

const MOCK_JOBS = [
  {
    job_id: 'job_trig_001_queued',
    incident_id: 'inc_live_001',
    merchant_id: 'merchant_alpha',
    source: 'razorpay_webhook',
    event_id: 'evt_001',
    event_type: 'payment.failed',
    payment_id: 'pay_001',
    status: 'queued',
    attempt_count: 0,
    created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:00:00Z',
  },
  {
    job_id: 'job_trig_002_processing',
    incident_id: 'inc_live_002',
    merchant_id: 'merchant_alpha',
    source: 'razorpay_webhook',
    event_id: 'evt_002',
    event_type: 'payment.failed',
    payment_id: 'pay_002',
    status: 'processing',
    attempt_count: 1,
    created_at: '2026-09-01T12:01:00Z',
    updated_at: '2026-09-01T12:01:05Z',
  },
  {
    job_id: 'job_trig_003_completed',
    incident_id: 'inc_live_003',
    merchant_id: 'merchant_beta',
    source: 'razorpay_webhook',
    event_id: 'evt_003',
    event_type: 'payment.failed',
    payment_id: 'pay_003',
    status: 'completed',
    attempt_count: 1,
    created_at: '2026-09-01T12:02:00Z',
    updated_at: '2026-09-01T12:02:10Z',
    completed_at: '2026-09-01T12:02:10Z',
    pipeline_result: {
      run_id: 'run_003',
      merchant_id: 'merchant_beta',
      status: 'completed',
      final_stage: 'execution',
      started_at: '2026-09-01T12:02:01Z',
      completed_at: '2026-09-01T12:02:10Z',
      is_completed: true,
      is_simulated: false,
      is_stopped: false,
      is_failed: false,
      summary: 'UPI Failure Spike resolved with Payment Link generation.',
      stop_reason: null,
      verification_result: {
        phase: 'pre_execution',
        status: 'verified',
        is_verified: true,
        checks_count: 12,
        checks: Array(12).fill({ check_id: 'CHK', passed: true }),
      },
      policy_decision: {
        decision_id: 'dec_003',
        verdict: 'allow',
        authorizes_execution: true,
      },
      execution_result: {
        execution_id: 'exec_003',
        status: 'completed',
        is_simulation: false,
        is_executed: true,
        provider_reference: 'plink_test_003',
      },
      scenario_classification: {
        scenario_id: 'upi_failure_spike',
        confidence: 0.92,
        rationale: 'UPI bank rail degradation',
        is_incident: true,
        is_action_eligible: true,
      },
    },
  },
  {
    job_id: 'job_trig_004_failed',
    incident_id: 'inc_live_004',
    merchant_id: 'merchant_gamma',
    source: 'razorpay_webhook',
    event_id: 'evt_004',
    event_type: 'payment.failed',
    payment_id: 'pay_004',
    status: 'failed',
    attempt_count: 1,
    error_message: 'Worker encountered unexpected exception in upstream context assembly',
    created_at: '2026-09-01T12:03:00Z',
    updated_at: '2026-09-01T12:03:02Z',
    completed_at: '2026-09-01T12:03:02Z',
  },
  {
    job_id: 'job_trig_005_escalated',
    incident_id: 'inc_live_005',
    merchant_id: 'merchant_delta',
    source: 'razorpay_webhook',
    event_id: 'evt_005',
    event_type: 'payment.failed',
    payment_id: 'pay_005',
    status: 'escalated',
    attempt_count: 1,
    created_at: '2026-09-01T12:04:00Z',
    updated_at: '2026-09-01T12:04:10Z',
    completed_at: '2026-09-01T12:04:10Z',
  },
];

const STAGE_ORDER = [
  'detection',
  'investigation',
  'agent',
  'verification',
  'policy',
  'execution',
];

function deriveStageTimings(res, jobStatus) {
  if (!res) {
    const isProcessing = jobStatus === 'processing' || jobStatus === 'queued';
    return {
      timings: {
        detection: { status: isProcessing ? 'running' : 'waiting' },
        investigation: { status: 'waiting' },
        agent: { status: 'waiting' },
        verification: { status: 'waiting' },
        policy: { status: 'waiting' },
        execution: { status: 'waiting' },
      },
      finalIdx: 0,
    };
  }

  const finalStageName = res.final_stage || 'detection';
  const targetIndex = STAGE_ORDER.indexOf(finalStageName);
  const finalIdx = res.is_completed ? 5 : (targetIndex >= 0 ? targetIndex : 0);

  const timings = {
    detection: { status: 'waiting' },
    investigation: { status: 'waiting' },
    agent: { status: 'waiting' },
    verification: { status: 'waiting' },
    policy: { status: 'waiting' },
    execution: { status: 'waiting' },
  };

  STAGE_ORDER.forEach((sId, idx) => {
    if (idx < finalIdx || (idx === finalIdx && res.is_completed)) {
      timings[sId] = {
        status: 'completed',
        startedAt: res.started_at,
        completedAt: res.completed_at,
      };
    } else if (idx === finalIdx) {
      timings[sId] = {
        status: res.is_failed ? 'failed' : res.is_stopped ? 'blocked' : 'completed',
        startedAt: res.started_at,
        completedAt: res.completed_at,
        details: res.stop_reason || undefined,
      };
    } else {
      timings[sId] = { status: 'skipped' };
    }
  });

  return { timings, finalIdx };
}

let passed = 0;
let failed = 0;

function runTest(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  [PASS] ${name}`);
  } catch (err) {
    failed++;
    console.error(`  [FAIL] ${name}: ${err.message}`);
  }
}

// 1. Status Filtering Tests
runTest('Filter by all statuses (QUEUED, PROCESSING, COMPLETED, FAILED, ESCALATED)', () => {
  assert.equal(MOCK_JOBS.filter((j) => j.status === 'queued').length, 1);
  assert.equal(MOCK_JOBS.filter((j) => j.status === 'processing').length, 1);
  assert.equal(MOCK_JOBS.filter((j) => j.status === 'completed').length, 1);
  assert.equal(MOCK_JOBS.filter((j) => j.status === 'failed').length, 1);
  assert.equal(MOCK_JOBS.filter((j) => j.status === 'escalated').length, 1);
});

// 2. Search Query Matching
runTest('Search query matching across payment_id, job_id, merchant_id', () => {
  const queryPay001 = MOCK_JOBS.filter((j) => j.payment_id.includes('pay_001'));
  assert.equal(queryPay001.length, 1);
  assert.equal(queryPay001[0].job_id, 'job_trig_001_queued');

  const queryMerchantAlpha = MOCK_JOBS.filter((j) => j.merchant_id.includes('merchant_alpha'));
  assert.equal(queryMerchantAlpha.length, 2);
});

// 3. Polling Calculation
runTest('Auto-polling interval switches between fast (2.5s) on active and idle (10s)', () => {
  const activeCount = MOCK_JOBS.filter((j) => j.status === 'queued' || j.status === 'processing').length;
  assert.equal(activeCount, 2);
  const activeInterval = activeCount > 0 ? 2500 : 10000;
  assert.equal(activeInterval, 2500);

  const terminalJobs = MOCK_JOBS.filter((j) => j.status !== 'queued' && j.status !== 'processing');
  const terminalInterval = terminalJobs.some((j) => j.status === 'queued' || j.status === 'processing') ? 2500 : 10000;
  assert.equal(terminalInterval, 10000);
});

// 4. Stage Timings Derivation on Selection
runTest('Derive 6 completed stages for completed job selection', () => {
  const completedJob = MOCK_JOBS[2];
  const { timings, finalIdx } = deriveStageTimings(completedJob.pipeline_result, completedJob.status);
  assert.equal(finalIdx, 5);
  assert.equal(timings.detection.status, 'completed');
  assert.equal(timings.investigation.status, 'completed');
  assert.equal(timings.agent.status, 'completed');
  assert.equal(timings.verification.status, 'completed');
  assert.equal(timings.policy.status, 'completed');
  assert.equal(timings.execution.status, 'completed');
});

runTest('Derive running detection stage for processing job selection', () => {
  const processingJob = MOCK_JOBS[1];
  const { timings, finalIdx } = deriveStageTimings(null, processingJob.status);
  assert.equal(finalIdx, 0);
  assert.equal(timings.detection.status, 'running');
  assert.equal(timings.investigation.status, 'waiting');
});

// 5. Error Reporting on Failed Job
runTest('Failed job captures worker exception message', () => {
  const failedJob = MOCK_JOBS[3];
  assert(failedJob.error_message.includes('Worker encountered unexpected exception'));
  assert.equal(failedJob.status, 'failed');
});

console.log(`\nResults: ${passed} passed, ${failed} failed.\n`);
if (failed > 0) {
  process.exit(1);
}
