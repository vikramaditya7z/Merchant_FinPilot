import React from 'react';
import { IncidentDetails, StageExecutionTiming } from '../../api/types';
import { MoneyDisplay } from '../common/MoneyDisplay';

interface IncidentOverviewCardProps {
  incident: IncidentDetails | null;
  summary?: string;
  timing?: StageExecutionTiming;
  scenarioClassification?: {
    scenario_id: string;
    confidence: number;
    rationale?: string;
    is_incident?: boolean;
    is_action_eligible?: boolean;
  } | null;
}

export const IncidentOverviewCard: React.FC<IncidentOverviewCardProps> = ({
  incident,
  timing,
  scenarioClassification,
}) => {
  const status = timing?.status || (incident ? 'completed' : 'waiting');

  if (!incident && status === 'waiting') {
    return (
      <div id="stage-detection" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">01 / DETECTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Statistical Detection
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Statistical anomaly detector waiting for pipeline trigger...
        </p>
      </div>
    );
  }

  if (!incident && status === 'running') {
    return (
      <div id="stage-detection" className="w-full bg-[#0B1017] border border-blue-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-blue-400 font-medium">01 / DETECTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
              Statistical Detection
            </span>
          </div>
          <span className="text-[10px] font-mono text-blue-300 uppercase px-2 py-0.5 rounded-sm bg-blue-950/80 border border-blue-800/80 animate-pulse">
            RUNNING
          </span>
        </div>
        <p className="text-xs font-mono text-blue-300 py-4">
          Evaluating 1-hour window against 30-day baseline distributions...
        </p>
      </div>
    );
  }

  if (!incident) {
    return (
      <div id="stage-detection" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">01 / DETECTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Statistical Detection
            </span>
          </div>
          <span className="text-[10px] font-mono text-emerald-400 uppercase px-2 py-0.5 rounded-sm bg-emerald-950/40 border border-emerald-800/40">
            NORMAL (NO ANOMALY)
          </span>
        </div>
        <p className="text-xs font-mono text-slate-400 py-2">
          No statistical degradation detected. Transaction failure rates remain within 3-sigma normal baseline boundaries.
        </p>
      </div>
    );
  }

  const metrics = incident.metrics || {};

  // Safe rate calculations avoiding any NaN issues
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

  let zScoreStr = '—';
  if (metrics.significance?.z_score !== undefined && metrics.significance?.z_score !== null) {
    const z = Number(metrics.significance.z_score);
    zScoreStr = isNaN(z) ? '—' : z.toFixed(2);
  }

  const failedGmvPaise = metrics.revenue_risk?.failed_gmv_paise ?? metrics.failed_gmv_paise;

  return (
    <div id="stage-detection" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">01 / DETECTION</span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-slate-300 font-semibold">
            Statistical Signal Anomaly
          </span>
        </div>
        <div className="flex items-center gap-3">
          {scenarioClassification && (
            <span className="hidden sm:inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm bg-blue-950/40 text-blue-300 border border-blue-800/40 text-[10px] font-mono font-bold tracking-wider uppercase">
              <span>{scenarioClassification.scenario_id.replace(/_/g, ' ')}</span>
              <span className="text-slate-600">•</span>
              <span className="text-emerald-400">{Math.round(scenarioClassification.confidence * 100)}% CONFIDENCE</span>
            </span>
          )}
          <span className={`text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm border ${
            incident.severity === 'high' 
              ? 'bg-rose-950/40 text-rose-400 border-rose-800/40' 
              : 'bg-amber-950/40 text-amber-400 border-amber-800/40'
          }`}>
            {incident.severity} SEVERITY
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            {new Date(incident.detected_at).toLocaleTimeString()}
          </span>
        </div>
      </div>

      {/* 3-Column Layout: Signal / Deviation / Evidence */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* Left: Anomaly Signal & Primary Metrics (5 cols) */}
        <div className="lg:col-span-5 space-y-6">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Triggered Anomaly
            </span>
            <div className="flex items-center gap-3 flex-wrap">
              <div className="text-2xl font-bold tracking-tight text-white uppercase">
                {incident.incident_type.replace(/_/g, ' ')}
              </div>
              {scenarioClassification && (
                <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-sm bg-blue-950/60 text-blue-300 border border-blue-800/60 text-[10px] font-mono font-bold tracking-wider uppercase">
                  <span>{scenarioClassification.scenario_id.replace(/_/g, ' ')}</span>
                  <span className="text-slate-600">•</span>
                  <span className="text-emerald-400">{Math.round(scenarioClassification.confidence * 100)}% CONFIDENCE</span>
                </span>
              )}
            </div>
            <div className="text-xs font-mono text-slate-400 mt-1">
              Incident ID: {incident.incident_id}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 pt-4 border-t border-slate-800/60">
            <div>
              <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 block mb-1">
                Observed Rate
              </span>
              <span className="text-3xl font-mono font-bold text-rose-400">
                {failureRateStr}
              </span>
            </div>
            <div>
              <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 block mb-1">
                Historical Baseline
              </span>
              <span className="text-3xl font-mono font-bold text-slate-300">
                {baselineRateStr}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 pt-4 border-t border-slate-800/60">
            <div>
              <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 block mb-1">
                Z-Score
              </span>
              <span className="text-2xl font-mono font-bold text-amber-400">
                {zScoreStr}
              </span>
            </div>
            <div>
              <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 block mb-1">
                Failed GMV Exposure
              </span>
              <div className="text-2xl font-mono font-bold text-rose-300">
                {failedGmvPaise !== undefined && failedGmvPaise !== null ? (
                  <MoneyDisplay paise={failedGmvPaise} />
                ) : (
                  '—'
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Center: Dimensional Deviation & Focus (3 cols) */}
        <div className="lg:col-span-3 space-y-6 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Statistical Shift
            </span>
            <div className="text-3xl font-mono font-bold text-amber-400">
              {deviationStr}
            </div>
            <span className="text-[10px] text-slate-400 font-mono mt-0.5 block">
              Deviation from 30d baseline
            </span>
          </div>

          <div className="pt-4 border-t border-slate-800/60">
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Primary Concentration
            </span>
            <div className="text-base font-bold font-mono text-blue-300 uppercase truncate">
              {incident.primary_dimension_value || 'Blended / All Rails'}
            </div>
            {incident.primary_dimension && (
              <span className="text-[10px] font-mono text-slate-500 block mt-0.5">
                Dimension: {incident.primary_dimension}
              </span>
            )}
          </div>

          <div className="pt-4 border-t border-slate-800/60">
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Observation Window
            </span>
            <div className="text-xs font-mono text-slate-300">
              {new Date(incident.window.start).toLocaleTimeString()} – {new Date(incident.window.end).toLocaleTimeString()}
            </div>
            <span className="text-[10px] font-mono text-slate-500 block mt-0.5">
              1-Hour Window Bucket
            </span>
          </div>
        </div>

        {/* Right: Raw Evidence Records (4 cols) */}
        <div className="lg:col-span-4 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold">
              Evidence Payloads ({incident.evidence?.length || 0})
            </span>
            <span className="text-[9px] font-mono text-slate-500 uppercase">
              Deterministic Invariants
            </span>
          </div>

          <div className="space-y-3 max-h-[340px] overflow-y-auto pr-2">
            {incident.evidence && incident.evidence.length > 0 ? (
              incident.evidence.map((ev) => (
                <div
                  key={ev.evidence_id}
                  className="bg-[#0E141D] border border-slate-800/80 p-3 rounded-sm space-y-1.5"
                >
                  <div className="flex items-center justify-between text-[10px] font-mono">
                    <span className="text-blue-400 font-semibold">{ev.evidence_id}</span>
                    <span className="text-slate-500 uppercase">{ev.source_confidence} CONF</span>
                  </div>
                  <p className="text-xs text-slate-300 font-sans leading-relaxed">
                    {ev.summary}
                  </p>
                  {ev.dimension && (
                    <div className="text-[9px] font-mono text-slate-500">
                      Dim: {ev.dimension}
                    </div>
                  )}
                </div>
              ))
            ) : (
              <div className="text-xs font-mono text-slate-500 py-4">
                No supplemental evidence payloads.
              </div>
            )}
          </div>
        </div>

      </div>
    </div>
  );
};
