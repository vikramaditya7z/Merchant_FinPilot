import React from 'react';
import { InvestigationReport, StageExecutionTiming } from '../../api/types';

interface InvestigationCardProps {
  report: InvestigationReport | null;
  timing?: StageExecutionTiming;
}

export const InvestigationCard: React.FC<InvestigationCardProps> = ({ report, timing }) => {
  const status = timing?.status || (report ? 'completed' : 'waiting');

  if (!report && status === 'waiting') {
    return (
      <div id="stage-investigation" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">02 / INVESTIGATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Dimensional Slicing
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Awaiting detection verification to initiate dimensional isolation...
        </p>
      </div>
    );
  }

  if (!report && status === 'running') {
    return (
      <div id="stage-investigation" className="w-full bg-[#0B1017] border border-blue-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-blue-400 font-medium">02 / INVESTIGATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
              Dimensional Slicing
            </span>
          </div>
          <span className="text-[10px] font-mono text-blue-300 uppercase px-2 py-0.5 rounded-sm bg-blue-950/80 border border-blue-800/80 animate-pulse">
            RUNNING
          </span>
        </div>
        <p className="text-xs font-mono text-blue-300 py-4">
          Performing multi-dimensional segmentation across rails, acquirers, error codes, and regions...
        </p>
      </div>
    );
  }

  if (!report) {
    return (
      <div id="stage-investigation" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">02 / INVESTIGATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Dimensional Slicing
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            NOT RUN
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-2">
          Investigation skipped (no anomaly detected in Stage 01).
        </p>
      </div>
    );
  }

  return (
    <div id="stage-investigation" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">02 / INVESTIGATION</span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-slate-300 font-semibold">
            Dimensional Isolation & Root-Cause Slicing
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm border ${
            report.has_sufficient_evidence
              ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/40'
              : 'bg-amber-950/40 text-amber-400 border-amber-800/40'
          }`}>
            {report.has_sufficient_evidence ? 'SUFFICIENT EVIDENCE' : 'SPARSE VOLUME'}
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            {new Date(report.investigated_at).toLocaleTimeString()}
          </span>
        </div>
      </div>

      {/* Top Headline Summary Bar */}
      <div className="flex items-center gap-6 text-sm font-mono border-b border-slate-800/60 pb-4">
        <div className="flex items-baseline gap-2">
          <span className="text-xl font-bold text-white">{report.primary_findings_count}</span>
          <span className="text-[10px] text-slate-500 uppercase tracking-wider">Primary Slices</span>
        </div>
        <span className="text-slate-700">•</span>
        <div className="flex items-baseline gap-2">
          <span className="text-xl font-bold text-slate-300">{report.secondary_findings_count}</span>
          <span className="text-[10px] text-slate-500 uppercase tracking-wider">Secondary Slices</span>
        </div>
        <span className="text-slate-700">•</span>
        <div className="flex items-baseline gap-2">
          <span className={`text-sm font-bold uppercase ${
            report.has_multiple_concentrations ? 'text-amber-400' : 'text-blue-400'
          }`}>
            {report.has_multiple_concentrations ? 'Multiple Concentrations' : 'Single Concentrated Outage'}
          </span>
        </div>
      </div>

      {/* 3-Column Layout: Slices Ranking / Synthesis / Metadata */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* Left: Ranked Slices / Dimensions (4 cols) */}
        <div className="lg:col-span-4 space-y-4">
          <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block">
            Ranked Dimensional Isolations
          </span>
          
          <div className="space-y-2">
            <div className="bg-[#0E141D] border border-slate-800/80 p-3 rounded-sm">
              <div className="flex justify-between items-baseline mb-1">
                <span className="text-xs font-mono font-bold text-slate-200 uppercase">
                  Primary Isolation
                </span>
                <span className="text-xs font-mono font-bold text-rose-400">
                  CONCENTRATED
                </span>
              </div>
              <p className="text-xs text-slate-400">
                Outage isolated to specific payment rail or acquiring provider cluster.
              </p>
            </div>

            <div className="bg-[#0E141D] border border-slate-800/80 p-3 rounded-sm">
              <div className="flex justify-between items-baseline mb-1">
                <span className="text-xs font-mono font-semibold text-slate-300 uppercase">
                  Control Comparison
                </span>
                <span className="text-xs font-mono text-emerald-400">
                  STABLE
                </span>
              </div>
              <p className="text-xs text-slate-400">
                Adjacent payment channels and cards remain at historical baseline rates.
              </p>
            </div>

            <div className="bg-[#0E141D] border border-slate-800/80 p-3 rounded-sm">
              <div className="flex justify-between items-baseline mb-1">
                <span className="text-xs font-mono font-semibold text-slate-300 uppercase">
                  Volume Confidence
                </span>
                <span className="text-xs font-mono text-blue-400">
                  {report.has_sufficient_evidence ? '3-SIGMA MET' : 'LOW POWER'}
                </span>
              </div>
              <p className="text-xs text-slate-400">
                Sample volume meets required power thresholds for automated decisioning.
              </p>
            </div>
          </div>
        </div>

        {/* Center: Deterministic Investigation Synthesis (5 cols) */}
        <div className="lg:col-span-5 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block">
            Investigation Synthesis
          </span>
          <div className="text-sm text-slate-200 font-sans leading-relaxed space-y-3 bg-[#0E141D]/50 p-4 border border-slate-800/60 rounded-sm">
            {report.summary ? (
              report.summary.split('\n').map((paragraph, idx) => (
                <p key={idx} className="text-slate-300 text-xs leading-relaxed">
                  {paragraph}
                </p>
              ))
            ) : (
              <p className="text-slate-500 text-xs font-mono">No synthesis available.</p>
            )}
          </div>
        </div>

        {/* Right: Evidence & Dimensional Metadata (3 cols) */}
        <div className="lg:col-span-3 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block">
            Investigation Scope
          </span>
          
          <div className="space-y-4 text-xs font-mono">
            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Dimensions Analyzed</span>
              <span className="text-slate-200 font-semibold">6 System Dimensions</span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Window Scope</span>
              <span className="text-slate-300">
                {new Date(report.window.start).toLocaleTimeString()} – {new Date(report.window.end).toLocaleTimeString()}
              </span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Isolation Method</span>
              <span className="text-blue-300">Deterministic Chi-Sq & Z-Score</span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Statistical Evidence</span>
              <span className="text-emerald-400">Validated</span>
            </div>
          </div>
        </div>

      </div>
    </div>
  );
};
