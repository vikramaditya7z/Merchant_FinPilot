import React, { useState, useEffect, useRef } from 'react';
import { StageStepper, StageId } from './components/common/StageStepper';
import { IncidentOverviewCard } from './components/dashboard/IncidentOverviewCard';
import { InvestigationCard } from './components/dashboard/InvestigationCard';
import { GeminiAgentCard } from './components/dashboard/GeminiAgentCard';
import { VerificationCard } from './components/dashboard/VerificationCard';
import { PolicyDecisionCard } from './components/dashboard/PolicyDecisionCard';
import { ExecutionResultCard } from './components/dashboard/ExecutionResultCard';
import { IncidentJobsConsole } from './components/dashboard/IncidentJobsConsole';
import { AuditTrailDrawer } from './components/audit/AuditTrailDrawer';
import { MoneyDisplay } from './components/common/MoneyDisplay';
import {
  IncidentJob,
  ProcessIncidentResponse,
  ScenarioMetadata,
  StageExecutionTiming,
  StageProgressEvent,
} from './api/types';
import { apiClient } from './api/client';
import { Play, Radio, X } from 'lucide-react';

const FALLBACK_SCENARIOS: ScenarioMetadata[] = [
  {
    scenario_id: 'upi_failure_spike',
    name: 'UPI Failure Spike',
    description: 'UPI failure rate jumps from ~4% to ~30% for one hour. Other methods normal.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'UPI rail degradation; issuer unavailability on UPI only.',
  },
  {
    scenario_id: 'card_failure_spike',
    name: 'Card Failure Spike',
    description: 'Card failure rate jumps from ~7% to ~35%. Cards are a minority of traffic.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'Card authentication failures, likely 3DS/issuer authentication.',
  },
  {
    scenario_id: 'evening_failure_spike',
    name: 'Evening Failure Spike',
    description: 'Overall failure rate increases across all methods during peak evening hours.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'Platform-level degradation affecting all rails equally.',
  },
  {
    scenario_id: 'regional_failure',
    name: 'Regional Failure (South India)',
    description: 'Failures concentrated in South India merchants, affecting multiple methods.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'Regional network infrastructure outage in South India.',
  },
  {
    scenario_id: 'provider_failure',
    name: 'Provider / Acquiring Bank Failure (HDFC)',
    description: 'Transactions routed through HDFC acquiring bank fail at high rates.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'HDFC gateway downtime or network disruption.',
  },
  {
    scenario_id: 'multiple_failures',
    name: 'Multiple Concurrent Failures',
    description: 'Simultaneous UPI spike and HDFC degradation with distinct timelines.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: true,
    expected_root_cause: 'Multiple overlapping issues: UPI rail failure and HDFC acquirer degradation.',
  },
  {
    scenario_id: 'false_alarm',
    name: 'False Alarm (Normal / Restraint)',
    description: 'Normal traffic variation; all metrics within 3-sigma tolerance.',
    is_incident: false,
    has_sufficient_data: true,
    expected_action_eligible: false,
    expected_root_cause: 'None: false alarm, traffic variation within normal bounds.',
  },
  {
    scenario_id: 'small_random_variation',
    name: 'Small Random Variation',
    description: 'Small metric movements that are not statistically significant (z-score < 3).',
    is_incident: false,
    has_sufficient_data: true,
    expected_action_eligible: false,
    expected_root_cause: 'None: insignificant random variation.',
  },
  {
    scenario_id: 'insufficient_data',
    name: 'Insufficient Data',
    description: 'Very low transaction volume; baseline calculation is unreliable.',
    is_incident: false,
    has_sufficient_data: false,
    expected_action_eligible: false,
    expected_root_cause: 'None: insufficient data to establish baseline confidence.',
  },
  {
    scenario_id: 'recovery_not_eligible',
    name: 'Recovery Not Eligible',
    description: 'Pre-existing outage; merchant has already disabled the failing rail.',
    is_incident: true,
    has_sufficient_data: true,
    expected_action_eligible: false,
    expected_root_cause: 'Merchant mitigation already active; no further automated intervention warranted.',
  },
  {
    scenario_id: 'normal',
    name: 'Normal Healthy Baseline',
    description: 'Standard healthy payment traffic with ~4% baseline failure rate.',
    is_incident: false,
    has_sufficient_data: true,
    expected_action_eligible: false,
    expected_root_cause: 'None: system operating within normal baseline parameters.',
  },
];

const STAGE_ORDER: StageId[] = [
  'detection',
  'investigation',
  'agent',
  'verification',
  'policy',
  'execution',
];

const deriveStageTimings = (res: ProcessIncidentResponse | null, jobStatus?: string): {
  timings: Record<StageId, StageExecutionTiming>;
  finalIdx: number;
} => {
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
};

export const App: React.FC = () => {
  const [scenarios, setScenarios] = useState<ScenarioMetadata[]>(FALLBACK_SCENARIOS);
  const [selectedScenario, setSelectedScenario] = useState<string>('upi_failure_spike');
  const [merchantId, setMerchantId] = useState<string>('merchant_razorpay_live_01');
  const [contextNotes, setContextNotes] = useState<string>('Tier-1 enterprise merchant with high UPI checkout volume');
  const [response, setResponse] = useState<ProcessIncidentResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [activeStageIndex, setActiveStageIndex] = useState<number>(0);
  const [stageTimings, setStageTimings] = useState<Record<StageId, StageExecutionTiming>>({
    detection: { status: 'waiting' },
    investigation: { status: 'waiting' },
    agent: { status: 'waiting' },
    verification: { status: 'waiting' },
    policy: { status: 'waiting' },
    execution: { status: 'waiting' },
  });
  const [selectedStage, setSelectedStage] = useState<StageId | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isAuditOpen, setIsAuditOpen] = useState<boolean>(false);

  // Live Incident Jobs Feed State
  const [jobs, setJobs] = useState<IncidentJob[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [isJobsLoading, setIsJobsLoading] = useState<boolean>(false);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [consoleMode, setConsoleMode] = useState<'feed' | 'simulator'>('feed');

  // Active run and abort controller to prevent stale callbacks and orphaned streams
  const sessionRunIdRef = useRef<number>(0);
  const activeRunIdRef = useRef<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  // Selected job ref to prevent race conditions during async fetches and polling
  const selectedJobIdRef = useRef<string | null>(null);

  // Cleanup abort controller on component unmount
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, []);

  // Fetch Incident Jobs
  const fetchJobs = async (silent = false) => {
    if (!silent) setIsJobsLoading(true);
    try {
      const fetchedJobs = await apiClient.listIncidentJobs();
      setJobs(fetchedJobs);
      setLastRefreshedAt(new Date());
      setJobsError(null);

      // If a job is currently selected, progressively update its state from the fresh jobs list
      const currentSelectedId = selectedJobIdRef.current;
      if (currentSelectedId) {
        const activeJobInList = fetchedJobs.find((j) => j.job_id === currentSelectedId);
        if (activeJobInList && selectedJobIdRef.current === currentSelectedId) {
          const freshResult = activeJobInList.pipeline_result || null;
          setResponse(freshResult);
          const { timings, finalIdx } = deriveStageTimings(freshResult, activeJobInList.status);
          setStageTimings(timings);
          setActiveStageIndex(finalIdx);

          if (activeJobInList.status === 'failed') {
            setError(activeJobInList.error_message || 'Incident job execution failed');
          }
        }
      }
    } catch (err: any) {
      console.warn('Failed to fetch incident jobs:', err);
      setJobsError(err.message || 'Failed to sync incident jobs');
    } finally {
      if (!silent) setIsJobsLoading(false);
    }
  };

  // Initial fetch and auto-polling
  useEffect(() => {
    fetchJobs();
    const scensInit = async () => {
      try {
        const scens = await apiClient.listScenarios();
        if (scens && scens.length > 0) {
          setScenarios(scens);
        }
      } catch (err) {
        console.warn('Scenarios list fetch error, using fallback:', err);
      }
    };
    scensInit();
  }, []);

  // Polling interval: 2.5s if active jobs exist, 10s otherwise
  useEffect(() => {
    const hasActiveJobs = jobs.some((j) => j.status === 'queued' || j.status === 'processing');
    const intervalMs = hasActiveJobs ? 2500 : 10000;

    const intervalId = setInterval(() => {
      fetchJobs(true);
    }, intervalMs);

    return () => clearInterval(intervalId);
  }, [jobs, selectedJobId]);

  const handleSelectJob = async (job: IncidentJob) => {
    // 1. Immediately update ref and state so this selected job is the single source of truth
    selectedJobIdRef.current = job.job_id;
    setSelectedJobId(job.job_id);

    // 2. Immediately set or clear error
    if (job.status === 'failed') {
      setError(job.error_message || 'Incident job execution failed');
    } else {
      setError(null);
    }

    // 3. Immediately update or reset response and derive stage timings for THIS job.
    // If this job has no pipeline_result yet (e.g. queued or newly processing), setResponse(null)
    // guarantees that stale details from previously selected jobs are completely removed immediately.
    const initialResult = job.pipeline_result || null;
    setResponse(initialResult);
    const { timings, finalIdx } = deriveStageTimings(initialResult, job.status);
    setStageTimings(timings);
    setActiveStageIndex(finalIdx);

    // 4. Fetch the latest detailed record for this job (e.g. if updated on backend)
    try {
      const detailed = await apiClient.getIncidentJob(job.job_id);
      // Race condition protection: if user clicked another job while fetching, discard late response!
      if (selectedJobIdRef.current !== job.job_id) {
        return;
      }

      const freshResult = detailed.pipeline_result || null;
      setResponse(freshResult);
      const { timings: freshTimings, finalIdx: freshIdx } = deriveStageTimings(freshResult, detailed.status);
      setStageTimings(freshTimings);
      setActiveStageIndex(freshIdx);

      if (detailed.status === 'failed') {
        setError(detailed.error_message || 'Incident job execution failed');
      }
    } catch (err: any) {
      if (selectedJobIdRef.current === job.job_id) {
        console.warn(`Failed to fetch job detail for ${job.job_id}:`, err);
      }
    }
  };

  const handleRunPipeline = async () => {
    if (isLoading) return;

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    const currentSessionId = Date.now();
    sessionRunIdRef.current = currentSessionId;
    activeRunIdRef.current = null;
    selectedJobIdRef.current = null;
    setSelectedJobId(null);

    setIsLoading(true);
    setError(null);
    setResponse(null);
    setActiveStageIndex(0);
    setStageTimings({
      detection: { status: 'running', startedAt: new Date().toISOString() },
      investigation: { status: 'waiting' },
      agent: { status: 'waiting' },
      verification: { status: 'waiting' },
      policy: { status: 'waiting' },
      execution: { status: 'waiting' },
    });

    try {
      const handleLiveEvent = async (event: StageProgressEvent) => {
        if (sessionRunIdRef.current !== currentSessionId) return;

        if (!activeRunIdRef.current && event.run_id) {
          activeRunIdRef.current = event.run_id;
        }

        if (activeRunIdRef.current && event.run_id && activeRunIdRef.current !== event.run_id) {
          return;
        }

        if (event.stage === 'pipeline') return;

        const stageId = event.stage as StageId;
        const stageIdx = STAGE_ORDER.indexOf(stageId);
        if (stageIdx >= 0) {
          setActiveStageIndex(stageIdx);
        }

        const nowIso = event.timestamp || new Date().toISOString();

        if (event.status === 'running') {
          setStageTimings((prev) => ({
            ...prev,
            [stageId]: {
              ...prev[stageId],
              status: 'running',
              startedAt: nowIso,
              details: event.details,
            },
          }));
        } else if (event.status === 'completed') {
          setStageTimings((prev) => {
            const started = prev[stageId]?.startedAt
              ? new Date(prev[stageId].startedAt!).getTime()
              : new Date(nowIso).getTime();
            const ended = new Date(nowIso).getTime();
            const durationMs = Math.max(0, ended - started);
            return {
              ...prev,
              [stageId]: {
                ...prev[stageId],
                status: 'completed',
                completedAt: nowIso,
                durationMs,
                details: event.details,
              },
            };
          });
        } else if (event.status === 'blocked' || event.status === 'stopped') {
          setStageTimings((prev) => {
            const started = prev[stageId]?.startedAt
              ? new Date(prev[stageId].startedAt!).getTime()
              : new Date(nowIso).getTime();
            const ended = new Date(nowIso).getTime();
            const durationMs = Math.max(0, ended - started);
            return {
              ...prev,
              [stageId]: {
                ...prev[stageId],
                status: 'blocked',
                completedAt: nowIso,
                durationMs,
                details: event.details,
              },
            };
          });
        } else if (event.status === 'failed') {
          setStageTimings((prev) => {
            const started = prev[stageId]?.startedAt
              ? new Date(prev[stageId].startedAt!).getTime()
              : new Date(nowIso).getTime();
            const ended = new Date(nowIso).getTime();
            const durationMs = Math.max(0, ended - started);
            return {
              ...prev,
              [stageId]: {
                ...prev[stageId],
                status: 'failed',
                completedAt: nowIso,
                durationMs,
                details: event.details,
              },
            };
          });
        }

        // Allow the browser compositor and React to paint each distinct stage transition
        await new Promise<void>((resolve) => {
          if (typeof window !== 'undefined' && window.requestAnimationFrame) {
            window.requestAnimationFrame(() => resolve());
          } else {
            setTimeout(resolve, 0);
          }
        });
      };

      const finalRes = await apiClient.processIncidentStream(
        {
          merchant_id: merchantId.trim(),
          scenario_id: selectedScenario,
          context_notes: contextNotes.trim() || undefined,
        },
        handleLiveEvent,
        abortController.signal
      );

      if (sessionRunIdRef.current !== currentSessionId) return;

      const finalStageName = (finalRes.final_stage as StageId) || 'detection';
      const targetIndex = STAGE_ORDER.indexOf(finalStageName);
      const finalIdx = finalRes.is_completed ? 5 : (targetIndex >= 0 ? targetIndex : 0);

      setStageTimings((prev) => {
        const nextTimings = { ...prev };
        STAGE_ORDER.forEach((sId, idx) => {
          if (idx > finalIdx && nextTimings[sId].status === 'waiting') {
            nextTimings[sId] = { ...nextTimings[sId], status: 'skipped' };
          }
        });
        return nextTimings;
      });

      setActiveStageIndex(finalIdx);
      setResponse(finalRes);
      setIsLoading(false);

      // Refresh incident jobs list so the new trigger appears in the feed
      fetchJobs(true);
    } catch (err: any) {
      if (sessionRunIdRef.current !== currentSessionId) return;
      setIsLoading(false);
      setError(err.message || 'Failed to process incident.');
    }
  };

  const incident = response?.incident;
  const metrics = incident?.metrics || {};

  // Safe NaN-proof rate calculations
  let failureRateStr = '—';
  if (metrics.failure_rate && typeof metrics.failure_rate === 'object' && metrics.failure_rate.denominator) {
    failureRateStr = `${((metrics.failure_rate.numerator / metrics.failure_rate.denominator) * 100).toFixed(4)}%`;
  } else if (typeof metrics.failure_rate === 'number') {
    failureRateStr = `${(metrics.failure_rate * 100).toFixed(4)}%`;
  }

  let baselineRateStr = '—';
  if (metrics.baseline?.rate && typeof metrics.baseline.rate === 'object' && metrics.baseline.rate.denominator) {
    baselineRateStr = `${((metrics.baseline.rate.numerator / metrics.baseline.rate.denominator) * 100).toFixed(4)}%`;
  } else if (typeof metrics.baseline?.rate === 'number') {
    baselineRateStr = `${(metrics.baseline.rate * 100).toFixed(4)}%`;
  }

  let deviationStr = '—';
  if (metrics.deviation?.absolute_percentage_points) {
    const d = parseFloat(metrics.deviation.absolute_percentage_points);
    deviationStr = isNaN(d) ? '—' : `+${d.toFixed(2)}pp`;
  }

  const failedGmvPaise = metrics.revenue_risk?.failed_gmv_paise ?? metrics.failed_gmv_paise;

  return (
    <div className="min-h-screen bg-[#070A0F] text-slate-100 flex flex-col font-sans selection:bg-blue-600 selection:text-white">
      
      {/* ═══ SAFETY BOUNDARY STRIP ═══ */}
      <div className="h-6 bg-amber-950/30 border-b border-amber-900/40 flex items-center justify-center gap-4 text-[9px] font-mono tracking-widest text-amber-500 uppercase px-4 select-none">
        <span>● SIMULATION ENVIRONMENT</span>
        <span className="opacity-30">•</span>
        <span>FAIL-CLOSED</span>
        <span className="opacity-30">•</span>
        <span>NO LIVE MONEY MUTATION</span>
      </div>

      {/* ═══ MINIMAL APPLICATION HEADER ═══ */}
      <header className="h-12 border-b border-slate-800/80 bg-[#0B1017] px-6 lg:px-12 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-sm font-bold tracking-tight text-white">Merchant FinPilot</span>
          <span className="text-[10px] font-mono font-semibold px-1.5 py-0.2 rounded bg-slate-800 text-slate-400 border border-slate-700">
            v2.0
          </span>
        </div>

        <div className="flex items-center gap-5 text-[10px] font-mono">
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
            <span className="text-slate-400">API: <strong className="text-slate-200">HEALTHY</strong></span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />
            <span className="text-slate-400">AI: <strong className="text-slate-200">GEMINI 3.1</strong></span>
          </div>
          <button
            type="button"
            onClick={() => setIsAuditOpen(true)}
            className="flex items-center gap-1 text-[10px] font-mono font-semibold uppercase px-2.5 py-1 rounded-sm bg-slate-800/80 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors cursor-pointer"
          >
            ◇ AUDIT LEDGER
          </button>
        </div>
      </header>

      {/* ═══ MAIN WORKSPACE CONTAINER ═══ */}
      <main className="flex-1 max-w-[1360px] w-full mx-auto px-6 lg:px-12 py-8 space-y-8">
        
        {/* ═══ CONSOLE MODE SELECTOR TABS ═══ */}
        <div className="flex items-center gap-3 border-b border-slate-800/80 pb-3">
          <button
            type="button"
            onClick={() => setConsoleMode('feed')}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-sm text-xs font-mono font-bold tracking-wider uppercase transition-colors cursor-pointer ${
              consoleMode === 'feed'
                ? 'bg-blue-950/80 text-blue-300 border border-blue-500/50 shadow-sm shadow-blue-950'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 border border-transparent'
            }`}
          >
            <Radio className="w-3.5 h-3.5 text-blue-400" />
            <span>Live Incident Jobs Feed ({jobs.length})</span>
          </button>

          <button
            type="button"
            onClick={() => setConsoleMode('simulator')}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-sm text-xs font-mono font-bold tracking-wider uppercase transition-colors cursor-pointer ${
              consoleMode === 'simulator'
                ? 'bg-blue-950/80 text-blue-300 border border-blue-500/50 shadow-sm shadow-blue-950'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 border border-transparent'
            }`}
          >
            <Play className="w-3.5 h-3.5 text-emerald-400" />
            <span>Synthetic Scenario Simulator</span>
          </button>
        </div>

        {/* ═══ LIVE INCIDENT JOBS FEED ═══ */}
        {consoleMode === 'feed' && (
          <IncidentJobsConsole
            jobs={jobs}
            selectedJobId={selectedJobId}
            onSelectJob={handleSelectJob}
            isLoading={isJobsLoading}
            onRefresh={() => fetchJobs(false)}
            lastRefreshedAt={lastRefreshedAt}
            error={jobsError}
          />
        )}

        {/* ═══ COMMAND BAR (SYNTHETIC SCENARIO SIMULATOR) ═══ */}
        {consoleMode === 'simulator' && (
          <div className="bg-[#0B1017] border border-slate-800/80 p-4 rounded-sm">
            <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-4">
              
              {/* Operator Inputs Grid */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 flex-1">
                <div>
                  <label className="block text-[9px] font-mono tracking-widest text-slate-500 uppercase mb-1.5 font-semibold">
                    Merchant ID
                  </label>
                  <input
                    type="text"
                    value={merchantId}
                    onChange={(e) => setMerchantId(e.target.value)}
                    className="w-full bg-[#070A0F] border border-slate-700/60 rounded-sm px-3 py-1.5 text-xs text-slate-200 font-mono focus:outline-none focus:border-blue-500 transition-colors"
                    placeholder="merchant_id"
                  />
                </div>

                <div>
                  <label className="block text-[9px] font-mono tracking-widest text-slate-500 uppercase mb-1.5 font-semibold">
                    Scenario Selector
                  </label>
                  <select
                    value={selectedScenario}
                    onChange={(e) => setSelectedScenario(e.target.value)}
                    className="w-full bg-[#070A0F] border border-slate-700/60 rounded-sm px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors cursor-pointer"
                  >
                    {scenarios.map((s) => (
                      <option key={s.scenario_id} value={s.scenario_id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-[9px] font-mono tracking-widest text-slate-500 uppercase mb-1.5 font-semibold">
                    Context Notes
                  </label>
                  <input
                    type="text"
                    value={contextNotes}
                    onChange={(e) => setContextNotes(e.target.value)}
                    className="w-full bg-[#070A0F] border border-slate-700/60 rounded-sm px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
                    placeholder="Operational context notes"
                  />
                </div>
              </div>

              {/* Run Pipeline Action */}
              <div className="flex items-center gap-4 shrink-0">
                {response?.run_id && (
                  <div className="hidden xl:flex flex-col text-right font-mono text-[10px]">
                    <span className="text-slate-500 uppercase">Run ID</span>
                    <span className="text-slate-400">{response.run_id}</span>
                  </div>
                )}
                <button
                  type="button"
                  onClick={handleRunPipeline}
                  disabled={isLoading || !merchantId.trim()}
                  className="h-[34px] px-6 bg-slate-100 hover:bg-white text-slate-900 text-xs font-bold uppercase tracking-widest rounded-sm transition-all shadow-sm disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center cursor-pointer"
                >
                  {isLoading ? 'EXECUTING PIPELINE...' : 'RUN PIPELINE'}
                </button>
              </div>

            </div>
          </div>
        )}

        {/* Selected Job Active Indicator Banner */}
        {selectedJobId && (
          <div className="bg-[#0B1017] border border-blue-500/40 px-4 py-2.5 rounded-sm flex items-center justify-between gap-3 text-xs font-mono">
            <div className="flex items-center gap-3 flex-wrap">
              <span className="px-2 py-0.5 rounded bg-blue-950 text-blue-300 border border-blue-500/50 font-bold uppercase text-[10px]">
                Inspecting Incident Job
              </span>
              <span className="text-slate-200 font-bold">{selectedJobId}</span>
              {(() => {
                const selJob = jobs.find((j) => j.job_id === selectedJobId);
                const merchant = selJob?.merchant_id || response?.merchant_id;
                const status = selJob?.status;
                return (
                  <>
                    {merchant && <span className="text-slate-400">(@{merchant})</span>}
                    {status && (
                      <span className={`px-1.5 py-0.5 rounded text-[9px] uppercase font-bold tracking-wider ${
                        status === 'completed'
                          ? 'bg-emerald-950/60 text-emerald-400 border border-emerald-800/40'
                          : status === 'processing'
                          ? 'bg-blue-950/80 text-blue-300 border border-blue-500/50 animate-pulse'
                          : status === 'failed'
                          ? 'bg-rose-950/60 text-rose-400 border border-rose-800/40'
                          : 'bg-slate-800/80 text-slate-400 border border-slate-700'
                      }`}>
                        {status}
                      </span>
                    )}
                  </>
                );
              })()}
            </div>
            <button
              type="button"
              onClick={() => {
                selectedJobIdRef.current = null;
                setSelectedJobId(null);
                setResponse(null);
                setError(null);
                const { timings, finalIdx } = deriveStageTimings(null);
                setStageTimings(timings);
                setActiveStageIndex(finalIdx);
              }}
              className="flex items-center gap-1 text-[10px] text-slate-400 hover:text-slate-200 uppercase font-semibold transition-colors cursor-pointer"
            >
              <X className="w-3.5 h-3.5" />
              <span>Clear</span>
            </button>
          </div>
        )}

        {/* Global Pipeline Error Message */}
        {error && (
          <div className="bg-rose-950/40 border border-rose-800/60 p-3 rounded-sm text-xs font-mono text-rose-300">
            <strong>Pipeline Halt:</strong> {error}
          </div>
        )}

        {/* ═══ INCIDENT HERO SUMMARY ═══ */}
        {incident ? (
          <div className="border-b border-slate-800/80 pb-6 space-y-4">
            <div className="flex items-baseline justify-between flex-wrap gap-3">
              <div className="flex items-baseline gap-4 flex-wrap">
                <h1 className="text-3xl font-bold tracking-tight text-white uppercase">
                  {incident.incident_type.replace(/_/g, ' ')}
                </h1>
                <span className={`text-[10px] font-mono font-bold tracking-widest uppercase px-2 py-0.5 rounded-sm border ${
                  incident.severity === 'high'
                    ? 'bg-rose-950/60 text-rose-400 border-rose-800/60'
                    : 'bg-amber-950/60 text-amber-400 border-amber-800/60'
                }`}>
                  {incident.severity} SEVERITY
                </span>
                {response?.scenario_classification && (
                  <span className="text-[10px] font-mono font-bold tracking-wider uppercase px-2.5 py-0.5 rounded-sm bg-blue-950/60 text-blue-300 border border-blue-800/60 inline-flex items-center gap-1.5">
                    <span>{response.scenario_classification.scenario_id.replace(/_/g, ' ')}</span>
                    <span className="text-slate-600">•</span>
                    <span className="text-emerald-400">
                      {Math.round(response.scenario_classification.confidence * 100)}% CONFIDENCE
                    </span>
                  </span>
                )}
              </div>
              <span className="text-xs font-mono text-slate-500">
                ID: {incident.incident_id}
              </span>
            </div>

            {/* Horizontal Financial Metrics Summary */}
            <div className="flex items-center gap-8 lg:gap-12 pt-2 flex-wrap">
              <div>
                <div className="text-2xl font-mono font-bold text-rose-400">
                  {failureRateStr}
                </div>
                <div className="text-[10px] font-mono tracking-widest uppercase text-slate-500 mt-1">
                  FAILURE RATE
                </div>
              </div>

              <div className="w-px h-8 bg-slate-800/80 hidden sm:block" />

              <div>
                <div className="text-2xl font-mono font-bold text-slate-300">
                  {baselineRateStr}
                </div>
                <div className="text-[10px] font-mono tracking-widest uppercase text-slate-500 mt-1">
                  BASELINE
                </div>
              </div>

              <div className="w-px h-8 bg-slate-800/80 hidden sm:block" />

              <div>
                <div className="text-2xl font-mono font-bold text-amber-400">
                  {deviationStr}
                </div>
                <div className="text-[10px] font-mono tracking-widest uppercase text-slate-500 mt-1">
                  DEVIATION
                </div>
              </div>

              <div className="w-px h-8 bg-slate-800/80 hidden sm:block" />

              <div>
                <div className="text-2xl font-mono font-bold text-rose-300">
                  {failedGmvPaise !== undefined && failedGmvPaise !== null ? (
                    <MoneyDisplay paise={failedGmvPaise} />
                  ) : (
                    '—'
                  )}
                </div>
                <div className="text-[10px] font-mono tracking-widest uppercase text-slate-500 mt-1">
                  FAILED GMV
                </div>
              </div>

              <div className="w-px h-8 bg-slate-800/80 hidden sm:block" />

              <div>
                <div className="text-xl font-bold text-blue-300 uppercase truncate max-w-[240px]">
                  {incident.primary_dimension_value || 'BLENDED'}
                </div>
                <div className="text-[10px] font-mono tracking-widest uppercase text-slate-500 mt-1">
                  CONCENTRATION
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="border-b border-slate-800/80 pb-6">
            <h1 className="text-xl font-bold tracking-tight text-slate-400 uppercase">
              {selectedJobId ? 'INSPECTING INCIDENT JOB' : 'READY FOR EXECUTION'}
            </h1>
            <p className="text-xs text-slate-500 font-mono mt-1">
              {selectedJobId
                ? 'Displaying deterministic pipeline evaluation for the selected webhook trigger.'
                : 'Select an incident job from the Live Feed above or run a synthetic scenario from the simulator.'}
            </p>
          </div>
        )}

        {/* ═══ HORIZONTAL EXECUTION TRACE (NAVIGATES / SCROLLS) ═══ */}
        <StageStepper
          response={response}
          isLoading={isLoading}
          activeStageIndex={activeStageIndex}
          stageTimings={stageTimings}
          selectedStage={selectedStage}
          onSelectStage={setSelectedStage}
        />

        {/* ═══ CONTINUOUS SIX-STAGE INVESTIGATION RECORD ═══ */}
        <div className="space-y-6">
          {/* Stage 01: Detection */}
          <IncidentOverviewCard
            incident={response?.incident || null}
            summary={response?.summary || ''}
            timing={stageTimings.detection}
            scenarioClassification={response?.scenario_classification || null}
          />

          {/* Stage 02: Investigation */}
          <InvestigationCard
            report={response?.investigation_report || null}
            timing={stageTimings.investigation}
          />

          {/* Stage 03: AI Reasoning */}
          <GeminiAgentCard
            agentResponse={response?.agent_response || null}
            proposedIntent={response?.proposed_intent || null}
            isFailed={response?.is_failed}
            isStopped={response?.is_stopped}
            stopReason={response?.stop_reason}
            finalStage={response?.final_stage}
            timing={stageTimings.agent}
          />

          {/* Stage 04: Financial Verification */}
          <VerificationCard
            verification={response?.verification_result || null}
            timing={stageTimings.verification}
          />

          {/* Stage 05: Policy Engine */}
          <PolicyDecisionCard
            decision={response?.policy_decision || null}
            timing={stageTimings.policy}
          />

          {/* Stage 06: Simulated Execution */}
          <ExecutionResultCard
            execution={response?.execution_result || null}
            timing={stageTimings.execution}
          />
        </div>

      </main>

      {/* Cryptographic Audit Ledger Drawer */}
      <AuditTrailDrawer
        isOpen={isAuditOpen}
        onClose={() => setIsAuditOpen(false)}
        currentIncidentId={response?.incident?.incident_id}
      />

    </div>
  );
};

export default App;
