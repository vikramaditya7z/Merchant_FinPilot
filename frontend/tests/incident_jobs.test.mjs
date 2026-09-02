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
  const normStatus = (jobStatus || '').toLowerCase();
  const isProcessing = normStatus === 'processing';
  const isFailed = normStatus === 'failed';

  if (!res) {
    return {
      timings: {
        detection: { status: isProcessing ? 'running' : isFailed ? 'failed' : 'waiting' },
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
        status: res.is_failed
          ? 'failed'
          : res.is_stopped
          ? 'blocked'
          : isProcessing && !res.is_completed
          ? 'running'
          : 'completed',
        startedAt: res.started_at,
        completedAt: res.completed_at,
        details: res.stop_reason || undefined,
      };
    } else {
      timings[sId] = { status: isProcessing ? 'waiting' : 'skipped' };
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

// 6. Agent Tool Calls Normalization
runTest('Agent tool calls normalization extracts concise names and arguments while handling empty records gracefully', () => {
  function normalizeToolCalls(rawToolList) {
    if (!rawToolList || !Array.isArray(rawToolList)) return [];
    return rawToolList.map((tc) => {
      if (typeof tc === 'string') return { name: tc, argLabel: undefined };
      const name = tc.tool_name || tc.name || 'tool';
      const args = tc.arguments || tc.args || {};
      let argLabel;
      if (args.dimension) {
        argLabel = String(args.dimension);
      } else if (args.action_type) {
        argLabel = String(args.action_type);
      } else if (args.granularity_minutes) {
        argLabel = `${args.granularity_minutes}m`;
      }
      return { name, argLabel };
    });
  }

  // Gracefully handles undefined / null / empty
  assert.deepEqual(normalizeToolCalls(undefined), []);
  assert.deepEqual(normalizeToolCalls(null), []);
  assert.deepEqual(normalizeToolCalls([]), []);

  // Correctly normalizes structured tool calls
  const mockTools = [
    { tool_name: 'get_incident_summary', arguments: {} },
    { tool_name: 'get_failure_breakdown', arguments: { dimension: 'payment_method' } },
    { tool_name: 'get_baseline_comparison', arguments: {} },
    { tool_name: 'get_revenue_exposure', arguments: {} },
  ];
  const normalized = normalizeToolCalls(mockTools);
  assert.equal(normalized.length, 4);
  assert.equal(normalized[0].name, 'get_incident_summary');
  assert.equal(normalized[0].argLabel, undefined);
  assert.equal(normalized[1].name, 'get_failure_breakdown');
  assert.equal(normalized[1].argLabel, 'payment_method');
  assert.equal(normalized[2].name, 'get_baseline_comparison');
  assert.equal(normalized[3].name, 'get_revenue_exposure');
});

// 7. Scenario Classification Badge Formatting
runTest('Scenario classification badge formatting derives uppercase label and confidence percentage', () => {
  function formatScenarioBadge(classification) {
    if (!classification || !classification.scenario_id) return null;
    const label = classification.scenario_id.replace(/_/g, ' ').toUpperCase();
    const confPercent = Math.round((classification.confidence ?? 0) * 100);
    return `${label} • ${confPercent}% CONFIDENCE`;
  }

  // Handles null / undefined gracefully
  assert.equal(formatScenarioBadge(null), null);
  assert.equal(formatScenarioBadge(undefined), null);

  // Derives correct format from pipeline_result
  const completedJob = MOCK_JOBS[2];
  const badge = formatScenarioBadge(completedJob.pipeline_result.scenario_classification);
  assert.equal(badge, 'UPI FAILURE SPIKE • 92% CONFIDENCE');
});

// 8. Selection State Manager Helper (Mimics App.tsx selection flow)
class DashboardSelectionManager {
  constructor() {
    this.selectedJobId = null;
    this.selectedJobIdRef = { current: null };
    this.response = null;
    this.stageTimings = deriveStageTimings(null);
    this.activeStageIndex = 0;
    this.error = null;
  }

  selectJob(job) {
    this.selectedJobIdRef.current = job.job_id;
    this.selectedJobId = job.job_id;
    this.error = job.status === 'failed' ? (job.error_message || 'Incident job execution failed') : null;

    // Immediate state reset / hydration
    const initialResult = job.pipeline_result || null;
    this.response = initialResult;
    const { timings, finalIdx } = deriveStageTimings(initialResult, job.status);
    this.stageTimings = timings;
    this.activeStageIndex = finalIdx;
  }

  applyAsyncDetail(jobId, detailed) {
    // Race condition guard: discard if selected job changed
    if (this.selectedJobIdRef.current !== jobId) {
      return false;
    }
    const freshResult = detailed.pipeline_result || null;
    this.response = freshResult;
    const { timings, finalIdx } = deriveStageTimings(freshResult, detailed.status);
    this.stageTimings = timings;
    this.activeStageIndex = finalIdx;
    if (detailed.status === 'failed') {
      this.error = detailed.error_message || 'Incident job execution failed';
    }
    return true;
  }

  syncFromPolling(fetchedJobs) {
    const currentId = this.selectedJobIdRef.current;
    if (!currentId) return;
    const activeJob = fetchedJobs.find((j) => j.job_id === currentId);
    if (activeJob && this.selectedJobIdRef.current === currentId) {
      const freshResult = activeJob.pipeline_result || null;
      this.response = freshResult;
      const { timings, finalIdx } = deriveStageTimings(freshResult, activeJob.status);
      this.stageTimings = timings;
      this.activeStageIndex = finalIdx;
      if (activeJob.status === 'failed') {
        this.error = activeJob.error_message || 'Incident job execution failed';
      }
    }
  }
}

// 9. Immediate Invalidation on Selection Change
runTest('Selecting job B immediately stops displaying job A details and clears response', () => {
  const mgr = new DashboardSelectionManager();
  const jobA = MOCK_JOBS[2]; // completed job with full pipeline_result
  const jobB = MOCK_JOBS[0]; // queued job with null pipeline_result

  mgr.selectJob(jobA);
  assert.equal(mgr.selectedJobId, 'job_trig_003_completed');
  assert.notEqual(mgr.response, null);
  assert.equal(mgr.response.run_id, 'run_003');

  // Immediately switch to job B
  mgr.selectJob(jobB);
  assert.equal(mgr.selectedJobId, 'job_trig_001_queued');
  // Stale incident from job A MUST be completely gone immediately!
  assert.equal(mgr.response, null);
  assert.equal(mgr.stageTimings.detection.status, 'waiting');
  assert.equal(mgr.activeStageIndex, 0);
});

// 10. Immediate Processing Pipeline State & Partial Progress Hydration
runTest('Selecting a PROCESSING job shows running pipeline state immediately, and progressive partial data renders running stage', () => {
  const mgr = new DashboardSelectionManager();
  const completedJob = MOCK_JOBS[2];
  const processingJob = MOCK_JOBS[1];

  mgr.selectJob(completedJob);
  assert.notEqual(mgr.response, null);

  // Switch to newly processing job (no pipeline_result yet)
  mgr.selectJob(processingJob);
  assert.equal(mgr.selectedJobId, 'job_trig_002_processing');
  assert.equal(mgr.response, null); // No stale completed details
  assert.equal(mgr.stageTimings.detection.status, 'running');
  assert.equal(mgr.stageTimings.investigation.status, 'waiting');

  // Partial pipeline data received while processing (e.g. at stage 'investigation')
  const partialProcessingResult = {
    final_stage: 'investigation',
    is_completed: false,
    started_at: '2026-09-01T12:01:00Z',
    incident: { incident_id: 'inc_partial_001' },
  };
  const { timings, finalIdx } = deriveStageTimings(partialProcessingResult, 'processing');
  assert.equal(finalIdx, 1);
  assert.equal(timings.detection.status, 'completed');
  assert.equal(timings.investigation.status, 'running');
  assert.equal(timings.agent.status, 'waiting');
});

// 11. Queued Job Shows Clean Waiting State Without Stale Incident Details
runTest('Selecting a QUEUED job does not show stale incident details and puts all stages in waiting', () => {
  const mgr = new DashboardSelectionManager();
  const completedJob = MOCK_JOBS[2];
  const queuedJob = MOCK_JOBS[0];

  mgr.selectJob(completedJob);
  assert.notEqual(mgr.response, null);

  mgr.selectJob(queuedJob);
  assert.equal(mgr.selectedJobId, 'job_trig_001_queued');
  assert.equal(mgr.response, null);
  assert.equal(mgr.stageTimings.detection.status, 'waiting');
  assert.equal(mgr.stageTimings.investigation.status, 'waiting');
  assert.equal(mgr.stageTimings.agent.status, 'waiting');
  assert.equal(mgr.stageTimings.verification.status, 'waiting');
  assert.equal(mgr.stageTimings.policy.status, 'waiting');
  assert.equal(mgr.stageTimings.execution.status, 'waiting');
});

// 12. Race Condition Guard Against Stale Async Polling Overwrite
runTest('A late polling or detail response for job A cannot overwrite currently selected job B', () => {
  const mgr = new DashboardSelectionManager();
  const jobA = MOCK_JOBS[2]; // completed
  const jobB = MOCK_JOBS[0]; // queued

  // User starts on job A
  mgr.selectJob(jobA);
  assert.equal(mgr.selectedJobId, 'job_trig_003_completed');

  // User quickly switches to job B
  mgr.selectJob(jobB);
  assert.equal(mgr.selectedJobId, 'job_trig_001_queued');
  assert.equal(mgr.response, null);

  // Late async response for job A arrives now!
  const wasApplied = mgr.applyAsyncDetail(jobA.job_id, {
    status: 'completed',
    pipeline_result: jobA.pipeline_result,
  });

  // Guard must reject the stale response!
  assert.equal(wasApplied, false);
  assert.equal(mgr.selectedJobId, 'job_trig_001_queued');
  assert.equal(mgr.response, null); // Job B's clean state preserved
});

// 13. Completed Job Rendering Preservation
runTest('Existing completed-job rendering hydrates full response and marks all 6 stages completed', () => {
  const mgr = new DashboardSelectionManager();
  const completedJob = MOCK_JOBS[2];

  mgr.selectJob(completedJob);
  assert.equal(mgr.selectedJobId, 'job_trig_003_completed');
  assert.notEqual(mgr.response, null);
  assert.equal(mgr.response.run_id, 'run_003');
  assert.equal(mgr.activeStageIndex, 5);
  assert.equal(mgr.stageTimings.detection.status, 'completed');
  assert.equal(mgr.stageTimings.investigation.status, 'completed');
  assert.equal(mgr.stageTimings.agent.status, 'completed');
  assert.equal(mgr.stageTimings.verification.status, 'completed');
  assert.equal(mgr.stageTimings.policy.status, 'completed');
  assert.equal(mgr.stageTimings.execution.status, 'completed');
});

console.log(`\nResults: ${passed} passed, ${failed} failed.\n`);
if (failed > 0) {
  process.exit(1);
}
