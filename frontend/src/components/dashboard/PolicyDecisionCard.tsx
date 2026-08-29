import React from 'react';
import { PolicyDecision, StageExecutionTiming } from '../../api/types';

interface PolicyDecisionCardProps {
  decision: PolicyDecision | null;
  timing?: StageExecutionTiming;
}

export const PolicyDecisionCard: React.FC<PolicyDecisionCardProps> = ({
  decision,
  timing,
}) => {
  const status = timing?.status || (decision ? 'completed' : 'waiting');

  if (!decision && status === 'waiting') {
    return (
      <div id="stage-policy" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">05 / POLICY ENGINE</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Governance & Authority Ruleset
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Awaiting financial verification pass to evaluate merchant policy boundaries...
        </p>
      </div>
    );
  }

  if (!decision && status === 'running') {
    return (
      <div id="stage-policy" className="w-full bg-[#0B1017] border border-blue-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-blue-400 font-medium">05 / POLICY ENGINE</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
              Governance & Authority Ruleset
            </span>
          </div>
          <span className="text-[10px] font-mono text-blue-300 uppercase px-2 py-0.5 rounded-sm bg-blue-950/80 border border-blue-800/80 animate-pulse">
            EVALUATING RULES & GUARDRAILS...
          </span>
        </div>
        <p className="text-xs font-mono text-blue-300 py-4">
          Evaluating rate limits, action eligibility, and merchant velocity restrictions...
        </p>
      </div>
    );
  }

  if (!decision) {
    return (
      <div id="stage-policy" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">05 / POLICY ENGINE</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Governance & Authority Ruleset
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            NOT RUN
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-2">
          Policy evaluation skipped (pipeline halted prior to policy engine).
        </p>
      </div>
    );
  }

  const isAllowed = decision.verdict === 'allow' || decision.authorizes_execution;
  const violationCount = decision.violations ? decision.violations.length : 0;

  return (
    <div id="stage-policy" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">05 / POLICY ENGINE</span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-purple-300 font-semibold">
            Merchant Governance & Authority Ruleset
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm border ${
            isAllowed 
              ? 'bg-purple-950/40 text-purple-300 border-purple-800/40' 
              : 'bg-rose-950/40 text-rose-400 border-rose-800/40'
          }`}>
            {isAllowed ? 'AUTHORIZATION GRANTED' : 'AUTHORIZATION DENIED'}
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            Ruleset: {decision.rule_set_version}
          </span>
        </div>
      </div>

      {/* Primary Authorization Banner */}
      <div className={`p-6 rounded-sm border flex items-center justify-between ${
        isAllowed 
          ? 'bg-[#120D1A] border-purple-900/60 text-purple-300' 
          : 'bg-[#180B0F] border-rose-900/60 text-rose-400'
      }`}>
        <div className="flex items-baseline gap-6">
          <div className="text-4xl font-mono font-bold tracking-tight uppercase">
            {isAllowed ? 'AUTHORIZED' : 'DENIED'}
          </div>
          <div>
            <div className="text-xs font-mono font-bold tracking-widest uppercase">
              {decision.verdict.toUpperCase()} DECISION
            </div>
            <div className="text-xs font-mono opacity-70">
              {violationCount === 0 ? 'Zero governance violations detected' : `${violationCount} policy violations flagged`}
            </div>
          </div>
        </div>

        <div className={`px-4 py-2 rounded-sm border font-mono font-bold text-xs uppercase tracking-widest ${
          isAllowed 
            ? 'bg-purple-950/80 border-purple-700/80 text-purple-200' 
            : 'bg-rose-950/80 border-rose-700/80 text-rose-300'
        }`}>
          {isAllowed ? 'EXECUTION ALLOWED' : 'EXECUTION BLOCKED'}
        </div>
      </div>

      {/* 2-Column Layout: Governance Context & Rules */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* Left: Policy Rationale & Authority Ruleset (5 cols) */}
        <div className="lg:col-span-5 space-y-6">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Policy Evaluation Rationale
            </span>
            <p className="text-xs text-slate-200 font-sans leading-relaxed bg-[#0E141D] p-4 border border-slate-800/80 rounded-sm">
              {decision.rationale}
            </p>
          </div>

          <div className="space-y-4 pt-4 border-t border-slate-800/60 font-mono text-xs">
            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Decision ID</span>
              <span className="text-slate-200">{decision.decision_id}</span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Evaluated At</span>
              <span className="text-slate-300">
                {new Date(decision.evaluated_at).toLocaleTimeString()}
              </span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Bound Intent Hash</span>
              <span className="text-[10px] text-slate-500 break-all">{decision.intent_hash}</span>
            </div>
          </div>
        </div>

        {/* Right: Policy Violations & Guardrails (7 cols) */}
        <div className="lg:col-span-7 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block">
            Policy Violations & Guardrails ({violationCount})
          </span>

          {violationCount > 0 ? (
            <div className="space-y-3">
              {decision.violations.map((v, i) => (
                <div
                  key={i}
                  className="bg-rose-950/20 border border-rose-900/40 p-3 rounded-sm space-y-1.5 font-mono text-xs"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-rose-400 font-bold">{v.rule_id}</span>
                    <span className="text-[10px] uppercase text-rose-300 bg-rose-950 px-1.5 py-0.5 rounded-sm">
                      {v.effect}
                    </span>
                  </div>
                  <div className="text-xs text-rose-200 font-sans">{v.message}</div>
                  {v.detail && (
                    <div className="text-[10px] text-rose-400/80 font-sans">{v.detail}</div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="bg-[#0E141D] border border-slate-800/80 p-4 rounded-sm space-y-3">
              <div className="flex items-center gap-2 text-xs font-mono text-emerald-400 font-semibold">
                <span>✓</span>
                <span>ZERO GOVERNANCE VIOLATIONS</span>
              </div>
              <p className="text-xs text-slate-400 leading-relaxed font-sans">
                Proposed remediation conforms to merchant autonomy boundaries, velocity guardrails, and financial policy invariants.
              </p>
            </div>
          )}
        </div>

      </div>
    </div>
  );
};
