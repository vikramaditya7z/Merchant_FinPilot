import React, { useState, useMemo } from 'react';
import {
  AlertCircle,
  ChevronRight,
  Radio,
  RefreshCw,
  Zap,
} from 'lucide-react';
import { IncidentJob } from '../../api/types';
import { StatusBadge } from '../common/StatusBadge';

interface IncidentJobsConsoleProps {
  jobs: IncidentJob[];
  selectedJobId: string | null;
  onSelectJob: (job: IncidentJob) => void;
  isLoading: boolean;
  onRefresh: () => void;
  lastRefreshedAt: Date | null;
  error?: string | null;
}

export const IncidentJobsConsole: React.FC<IncidentJobsConsoleProps> = ({
  jobs,
  selectedJobId,
  onSelectJob,
  isLoading,
  onRefresh,
  lastRefreshedAt,
  error,
}) => {
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');

  const activeCount = useMemo(
    () => jobs.filter((j) => j.status === 'queued' || j.status === 'processing').length,
    [jobs]
  );

  const filteredJobs = useMemo(() => {
    return jobs.filter((job) => {
      if (statusFilter !== 'all' && job.status.toLowerCase() !== statusFilter.toLowerCase()) {
        return false;
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const matchesJobId = job.job_id.toLowerCase().includes(q);
        const matchesIncidentId = (job.incident_id || '').toLowerCase().includes(q);
        const matchesPaymentId = (job.payment_id || '').toLowerCase().includes(q);
        const matchesMerchantId = (job.merchant_id || '').toLowerCase().includes(q);
        const matchesEventType = (job.event_type || '').toLowerCase().includes(q);
        return matchesJobId || matchesIncidentId || matchesPaymentId || matchesMerchantId || matchesEventType;
      }
      return true;
    });
  }, [jobs, statusFilter, searchQuery]);

  const formatTime = (isoString?: string | null) => {
    if (!isoString) return '—';
    try {
      const d = new Date(isoString);
      return d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return isoString;
    }
  };

  return (
    <div className="bg-[#0B1017] border border-slate-800/80 rounded-sm overflow-hidden flex flex-col">
      {/* ═══ CONSOLE HEADER & CONTROLS ═══ */}
      <div className="px-4 py-3 border-b border-slate-800/80 bg-[#070A0F]/60 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Radio className={`w-4 h-4 ${activeCount > 0 ? 'text-emerald-400 animate-pulse' : 'text-blue-400'}`} />
            <span className="text-xs font-bold tracking-wider text-slate-200 uppercase font-mono">
              Live Incident Jobs Feed
            </span>
          </div>

          <div className="flex items-center gap-1.5 text-[10px] font-mono">
            {activeCount > 0 && (
              <span className="px-2 py-0.5 rounded-full bg-blue-950/80 text-blue-300 border border-blue-500/40 animate-pulse font-semibold">
                {activeCount} ACTIVE
              </span>
            )}
            <span className="px-2 py-0.5 rounded bg-slate-800/80 text-slate-400 border border-slate-700 font-semibold">
              {jobs.length} TOTAL
            </span>
          </div>
        </div>

        {/* Filter and Refresh Controls */}
        <div className="flex items-center gap-2.5">
          <div className="relative">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search pay_id, job_id..."
              className="w-36 md:w-48 bg-[#070A0F] border border-slate-700/60 rounded-sm px-2.5 py-1 text-[11px] font-mono text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
            />
          </div>

          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="bg-[#070A0F] border border-slate-700/60 rounded-sm px-2 py-1 text-[11px] font-mono text-slate-300 focus:outline-none focus:border-blue-500 transition-colors cursor-pointer"
          >
            <option value="all">ALL STATUSES</option>
            <option value="processing">PROCESSING</option>
            <option value="queued">QUEUED</option>
            <option value="completed">COMPLETED</option>
            <option value="failed">FAILED</option>
            <option value="escalated">ESCALATED</option>
          </select>

          <button
            type="button"
            onClick={onRefresh}
            disabled={isLoading}
            title={lastRefreshedAt ? `Last refreshed: ${lastRefreshedAt.toLocaleTimeString()}` : 'Refresh feed'}
            className="flex items-center gap-1 text-[11px] font-mono font-semibold px-2.5 py-1 rounded-sm bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition-colors disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw className={`w-3 h-3 ${isLoading ? 'animate-spin text-blue-400' : 'text-slate-400'}`} />
            <span className="hidden md:inline">REFRESH</span>
          </button>
        </div>
      </div>

      {/* API Error Warning */}
      {error && (
        <div className="px-4 py-2 bg-rose-950/30 border-b border-rose-900/40 flex items-center justify-between gap-2 text-xs font-mono text-rose-300">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-3.5 h-3.5 shrink-0 text-rose-400" />
            <span>Feed sync error: {error}</span>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            className="underline hover:text-rose-200 uppercase text-[10px]"
          >
            Retry
          </button>
        </div>
      )}

      {/* ═══ JOBS LIST FEED ═══ */}
      <div className="divide-y divide-slate-800/60 max-h-72 overflow-y-auto font-mono text-xs">
        {filteredJobs.length === 0 ? (
          <div className="p-8 text-center text-slate-500 space-y-1">
            <p className="text-xs font-semibold">No incident jobs found</p>
            <p className="text-[11px] text-slate-600">
              {jobs.length === 0
                ? 'Webhook events from Razorpay TEST will automatically queue and display here.'
                : 'No jobs match the current filter selection.'}
            </p>
          </div>
        ) : (
          filteredJobs.map((job) => {
            const isSelected = selectedJobId === job.job_id;
            const res = job.pipeline_result;
            const scenarioClass = res?.scenario_classification?.scenario_id || res?.incident?.incident_type;
            const verifiedCount = res?.verification_result?.checks?.filter((c) => c.passed).length;
            const checksCount = res?.verification_result?.checks_count || res?.verification_result?.checks?.length;
            const policyVerdict = res?.policy_decision?.verdict;
            const isSimulated = res?.execution_result?.is_simulation ?? true;

            return (
              <div
                key={job.job_id}
                onClick={() => onSelectJob(job)}
                className={`px-4 py-2.5 flex flex-col sm:flex-row sm:items-center justify-between gap-3 transition-colors cursor-pointer ${
                  isSelected
                    ? 'bg-blue-950/40 border-l-2 border-l-blue-500'
                    : 'hover:bg-slate-900/60 border-l-2 border-l-transparent'
                }`}
              >
                {/* Left Column: Job Identity & Telemetry */}
                <div className="flex items-start sm:items-center gap-3 min-w-0 flex-1">
                  <div className="p-1.5 rounded bg-slate-800/80 border border-slate-700 shrink-0">
                    <Zap className="w-3.5 h-3.5 text-amber-400" />
                  </div>

                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-bold text-slate-200 tracking-tight">
                        {job.payment_id || job.event_id || job.job_id}
                      </span>
                      <span className="text-[10px] text-slate-400 font-mono px-1.5 py-0.2 rounded bg-slate-800 border border-slate-700/60">
                        {job.event_type}
                      </span>
                      <span className="text-[10px] text-slate-500">
                        @{job.merchant_id}
                      </span>
                    </div>

                    <div className="flex items-center gap-3 text-[10px] text-slate-400">
                      <span>Job: <strong className="text-slate-300 font-normal">{job.job_id.slice(0, 18)}...</strong></span>
                      <span>•</span>
                      <span>Created: {formatTime(job.created_at)}</span>
                      {job.completed_at && (
                        <>
                          <span>•</span>
                          <span>Completed: {formatTime(job.completed_at)}</span>
                        </>
                      )}
                    </div>

                    {/* Pipeline Summary Badges */}
                    {res && (
                      <div className="flex items-center gap-2 pt-0.5 text-[9px] text-slate-400 flex-wrap">
                        {scenarioClass && (
                          <span className="text-indigo-300 uppercase font-semibold">
                            [{scenarioClass.replace(/_/g, ' ')}]
                          </span>
                        )}
                        {checksCount !== undefined && (
                          <span className={verifiedCount === checksCount ? 'text-emerald-400' : 'text-rose-400'}>
                            ✓ {verifiedCount}/{checksCount} Verified
                          </span>
                        )}
                        {policyVerdict && (
                          <span className={policyVerdict === 'allow' ? 'text-purple-300' : 'text-amber-400'}>
                            Policy: {policyVerdict.toUpperCase()}
                          </span>
                        )}
                        {res.execution_result && (
                          <span className="text-amber-300">
                            {isSimulated ? 'Simulated' : 'Razorpay TEST'}
                          </span>
                        )}
                      </div>
                    )}

                    {/* Error Message callout */}
                    {job.error_message && (
                      <div className="text-[10px] text-rose-400 bg-rose-950/40 px-2 py-0.5 rounded border border-rose-900/40 inline-block mt-0.5">
                        Error: {job.error_message}
                      </div>
                    )}
                  </div>
                </div>

                {/* Right Column: Status & Action */}
                <div className="flex items-center gap-3 shrink-0 self-end sm:self-center">
                  <StatusBadge status={job.status} size="xs" />
                  <div className="flex items-center text-[10px] text-slate-400 font-semibold">
                    <span className={isSelected ? 'text-blue-400 font-bold' : 'text-slate-500'}>
                      {isSelected ? 'INSPECTING' : 'INSPECT'}
                    </span>
                    <ChevronRight className={`w-3.5 h-3.5 ${isSelected ? 'text-blue-400' : 'text-slate-600'}`} />
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
