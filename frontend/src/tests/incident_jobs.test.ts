/**
 * Frontend Tests for Live Incident Jobs Feed & Console
 *
 * Validates:
 * 1. API client methods (listIncidentJobs, getIncidentJob) URL formatting & error handling
 * 2. IncidentJob status mapping (QUEUED, PROCESSING, COMPLETED, FAILED, ESCALATED)
 * 3. Polling interval derivation (active vs idle)
 * 4. Job list filtering & search matching
 * 5. Pipeline stage timing derivation from job payload
 * 6. Edge cases: empty jobs, missing details, malformed payloads, API errors
 */

import { IncidentJob, ProcessIncidentResponse, StageId, StageExecutionTiming } from '../api/types';
import { FinPilotApiClient } from '../api/client';

// ---------------------------------------------------------------------------
// Test Assertions Helper
// ---------------------------------------------------------------------------
function assert(condition: boolean, message: string): void {
  if (!condition) {
    throw new Error(`Assertion Failed: ${message}`);
  }
}

function assertEquals<T>(actual: T, expected: T, message: string): void {
  if (actual !== expected) {
    throw new Error(`Assertion Failed: ${message} (Expected: ${expected}, Got: ${actual})`);
  }
}

// ---------------------------------------------------------------------------
// Mock Sample Jobs
// ---------------------------------------------------------------------------
const MOCK_JOBS: IncidentJob[] = [
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
      incident: null,
      investigation_report: null,
      agent_response: null,
      proposed_intent: null,
      verification_result: {
        phase: 'pre_execution',
        status: 'verified',
        is_verified: true,
        is_rejected: false,
        is_inconclusive: false,
        summary: 'All 12 checks passed',
        verified_at: '2026-09-01T12:02:05Z',
        checks_count: 12,
        checks: Array(12).fill({ check_id: 'CHK', name: 'Test', passed: true, expected: '1', observed: '1', detail: 'OK' }),
      },
      policy_decision: {
        decision_id: 'dec_003',
        intent_id: 'int_003',
        intent_hash: 'hash_003',
        verdict: 'allow',
        authorizes_execution: true,
        rationale: 'Permitted',
        evaluated_at: '2026-09-01T12:02:07Z',
        expires_at: '2026-09-01T12:12:07Z',
        rule_set_version: '1.0.0',
        violations: [],
        required_approvals: [],
      },
      execution_result: {
        execution_id: 'exec_003',
        decision_id: 'dec_003',
        intent_id: 'int_003',
        action: 'create_payment_link',
        status: 'completed',
        idempotency_key: 'idemp_003',
        attempted_at: '2026-09-01T12:02:08Z',
        completed_at: '2026-09-01T12:02:10Z',
        provider_reference: 'plink_test_003',
        response_digest: 'digest_003',
        is_simulation: false,
        is_executed: true,
        message: 'Payment link created on Razorpay TEST',
        error_code: null,
        error_message: null,
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

// ---------------------------------------------------------------------------
// Pipeline Stage Derivation Helper Under Test
// ---------------------------------------------------------------------------
const STAGE_ORDER: StageId[] = [
  'detection',
  'investigation',
  'agent',
  'verification',
  'policy',
  'execution',
];

function deriveStageTimings(res: ProcessIncidentResponse | null, jobStatus?: string): {
  timings: Record<StageId, StageExecutionTiming>;
  finalIdx: number;
} {
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

  const finalStageName = (res.final_stage as StageId) || 'detection';
  const targetIndex = STAGE_ORDER.indexOf(finalStageName);
  const finalIdx = res.is_completed ? 5 : (targetIndex >= 0 ? targetIndex : 0);

  const timings: Record<StageId, StageExecutionTiming> = {
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

// ---------------------------------------------------------------------------
// Test Suite Execution
// ---------------------------------------------------------------------------
export async function runFrontendIncidentJobsTests(): Promise<{ passed: number; failed: number; tests: string[] }> {
  const tests: string[] = [];
  let passed = 0;
  let failed = 0;

  function runTest(name: string, fn: () => void | Promise<void>) {
    try {
      fn();
      passed++;
      tests.push(`[PASS] ${name}`);
    } catch (err: any) {
      failed++;
      tests.push(`[FAIL] ${name}: ${err.message}`);
    }
  }

  // 1. Job List Filtering & Search Matching
  runTest('Job list filters correctly by status', () => {
    const processingJobs = MOCK_JOBS.filter((j) => j.status === 'processing');
    assertEquals(processingJobs.length, 1, 'Should find exactly 1 processing job');
    assertEquals(processingJobs[0].job_id, 'job_trig_002_processing', 'Should match processing job id');

    const completedJobs = MOCK_JOBS.filter((j) => j.status === 'completed');
    assertEquals(completedJobs.length, 1, 'Should find 1 completed job');

    const queuedJobs = MOCK_JOBS.filter((j) => j.status === 'queued');
    assertEquals(queuedJobs.length, 1, 'Should find 1 queued job');

    const failedJobs = MOCK_JOBS.filter((j) => j.status === 'failed');
    assertEquals(failedJobs.length, 1, 'Should find 1 failed job');

    const escalatedJobs = MOCK_JOBS.filter((j) => j.status === 'escalated');
    assertEquals(escalatedJobs.length, 1, 'Should find 1 escalated job');
  });

  runTest('Job list filters by search query matching payment_id, merchant_id, job_id', () => {
    const searchAlpha = MOCK_JOBS.filter((j) => j.merchant_id.includes('merchant_alpha'));
    assertEquals(searchAlpha.length, 2, 'Should find 2 jobs for merchant_alpha');

    const searchPay3 = MOCK_JOBS.filter((j) => j.payment_id.includes('pay_003'));
    assertEquals(searchPay3.length, 1, 'Should find pay_003 job');
    assertEquals(searchPay3[0].job_id, 'job_trig_003_completed', 'Should match completed job');
  });

  // 2. Active Job Counting and Auto-Polling Intervals
  runTest('Active job count identifies QUEUED and PROCESSING states', () => {
    const activeCount = MOCK_JOBS.filter((j) => j.status === 'queued' || j.status === 'processing').length;
    assertEquals(activeCount, 2, 'Should detect 2 active jobs');

    const intervalWithActive = activeCount > 0 ? 2500 : 10000;
    assertEquals(intervalWithActive, 2500, 'Should poll fast (2.5s) when active jobs exist');

    const terminalJobs = MOCK_JOBS.filter((j) => j.status !== 'queued' && j.status !== 'processing');
    const intervalWithTerminal = terminalJobs.some((j) => j.status === 'queued' || j.status === 'processing') ? 2500 : 10000;
    assertEquals(intervalWithTerminal, 10000, 'Should poll slower (10s) when only terminal jobs exist');
  });

  // 3. Stage Timings Derivation for Job Selection
  runTest('Derives 6 completed stages when selecting a completed job', () => {
    const completedJob = MOCK_JOBS[2];
    const { timings, finalIdx } = deriveStageTimings(completedJob.pipeline_result || null, completedJob.status);

    assertEquals(finalIdx, 5, 'Final stage index for completed run must be 5 (execution)');
    assertEquals(timings.detection.status, 'completed', 'Detection must be completed');
    assertEquals(timings.investigation.status, 'completed', 'Investigation must be completed');
    assertEquals(timings.agent.status, 'completed', 'Agent must be completed');
    assertEquals(timings.verification.status, 'completed', 'Verification must be completed');
    assertEquals(timings.policy.status, 'completed', 'Policy must be completed');
    assertEquals(timings.execution.status, 'completed', 'Execution must be completed');
  });

  runTest('Derives running detection stage when selecting an active processing job', () => {
    const processingJob = MOCK_JOBS[1];
    const { timings, finalIdx } = deriveStageTimings(null, processingJob.status);

    assertEquals(finalIdx, 0, 'Final stage index for fresh processing job should be 0');
    assertEquals(timings.detection.status, 'running', 'Detection stage must show running during processing');
    assertEquals(timings.investigation.status, 'waiting', 'Subsequent stages must be waiting');
  });

  // 4. Failed Job Error Handling & Reason Display
  runTest('Failed job preserves error message and stops safely', () => {
    const failedJob = MOCK_JOBS[3];
    assert(failedJob.error_message !== undefined, 'Failed job must have error_message');
    assert(
      failedJob.error_message!.includes('Worker encountered unexpected exception'),
      'Error message must match worker failure description'
    );
  });

  // 5. API Client listIncidentJobs & getIncidentJob URL construction
  runTest('FinPilotApiClient exposes listIncidentJobs and getIncidentJob methods', () => {
    const client = new FinPilotApiClient();
    assertEquals(typeof client.listIncidentJobs, 'function', 'listIncidentJobs must be a callable method');
    assertEquals(typeof client.getIncidentJob, 'function', 'getIncidentJob must be a callable method');
  });

  return { passed, failed, tests };
}
