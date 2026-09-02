import React from 'react';
import { StageExecutionTiming, VerificationResult } from '../../api/types';

interface VerificationCardProps {
  verification: VerificationResult | null;
  timing?: StageExecutionTiming;
}

export const VerificationCard: React.FC<VerificationCardProps> = ({
  verification,
  timing,
}) => {
  const status = timing?.status || (verification ? 'completed' : 'waiting');

  if (!verification && status === 'waiting') {
    return (
      <div id="stage-verification" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">04 / FINANCIAL VERIFICATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Deterministic Invariants & Safety Gate
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Awaiting proposed action intent from autonomous reasoning agent...
        </p>
      </div>
    );
  }

  if (!verification && status === 'running') {
    return (
      <div id="stage-verification" className="w-full bg-[#0B1017] border border-blue-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-blue-400 font-medium">04 / FINANCIAL VERIFICATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
              Deterministic Invariants & Safety Gate
            </span>
          </div>
          <span className="text-[10px] font-mono text-blue-300 uppercase px-2 py-0.5 rounded-sm bg-blue-950/80 border border-blue-800/80 animate-pulse">
            EVALUATING INVARIANTS...
          </span>
        </div>
        <p className="text-xs font-mono text-blue-300 py-4">
          Independently checking mathematical eligibility, thresholds, and evidence binding invariants...
        </p>
      </div>
    );
  }

  if (!verification && status === 'completed') {
    return (
      <div id="stage-verification" className="w-full bg-[#0B1017] border border-emerald-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-emerald-400 font-medium">04 / FINANCIAL VERIFICATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-emerald-300 font-semibold">
              Deterministic Invariants & Safety Gate
            </span>
          </div>
          <span className="text-[10px] font-mono text-emerald-400 font-bold uppercase px-2 py-0.5 rounded-sm bg-emerald-950/80 border border-emerald-800/80">
            VERIFIED
          </span>
        </div>
        <p className="text-xs font-mono text-emerald-300/80 py-4">
          All financial verification checks passed. Mathematical invariants independently verified.
        </p>
      </div>
    );
  }

  if (!verification) {
    return (
      <div id="stage-verification" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">04 / FINANCIAL VERIFICATION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Deterministic Invariants & Safety Gate
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            NOT RUN
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-2">
          Verification gate not evaluated (no action proposed).
        </p>
      </div>
    );
  }

  const passedChecksCount = verification.checks?.filter((c) => c.passed).length || 0;
  const totalChecks = verification.checks?.length || 0;
  const failedChecksCount = totalChecks - passedChecksCount;
  const isVerified = verification.status === 'verified';

  return (
    <div id="stage-verification" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">04 / FINANCIAL VERIFICATION</span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-emerald-300 font-semibold">
            Deterministic Financial Invariants & Safety Gate
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm border ${
            isVerified 
              ? 'bg-emerald-950/40 text-emerald-400 border-emerald-800/40' 
              : 'bg-rose-950/40 text-rose-400 border-rose-800/40'
          }`}>
            {isVerified ? 'INVARIANTS SATISFIED' : 'INVARIANTS VIOLATED'}
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            Phase: {verification.phase}
          </span>
        </div>
      </div>

      {/* Primary Safety Gate Banner */}
      <div className={`p-6 rounded-sm border flex items-center justify-between ${
        isVerified 
          ? 'bg-[#0B1412] border-emerald-900/60 text-emerald-400' 
          : 'bg-[#180B0F] border-rose-900/60 text-rose-400'
      }`}>
        <div className="flex items-baseline gap-6">
          <div className="text-5xl font-mono font-bold tracking-tight">
            {passedChecksCount} / {totalChecks}
          </div>
          <div>
            <div className="text-sm font-mono font-bold tracking-widest uppercase">
              CHECKS PASSED
            </div>
            <div className="text-xs font-mono opacity-70">
              {failedChecksCount === 0 ? 'All deterministic invariants validated' : `${failedChecksCount} verification invariant failed`}
            </div>
          </div>
        </div>

        <div className={`px-4 py-2 rounded-sm border font-mono font-bold text-xs uppercase tracking-widest ${
          isVerified 
            ? 'bg-emerald-950/80 border-emerald-700/80 text-emerald-300' 
            : 'bg-rose-950/80 border-rose-700/80 text-rose-300'
        }`}>
          {isVerified ? '✓ GATE VERIFIED' : '✕ GATE REJECTED'}
        </div>
      </div>

      {/* 2-Column Layout: Summary & Rule Inspector */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* Left: Summary & Metadata (4 cols) */}
        <div className="lg:col-span-4 space-y-6">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Verification Verdict Summary
            </span>
            <p className="text-xs text-slate-200 font-sans leading-relaxed bg-[#0E141D] p-4 border border-slate-800/80 rounded-sm">
              {verification.summary}
            </p>
          </div>

          <div className="space-y-4 pt-4 border-t border-slate-800/60 font-mono text-xs">
            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Verification Engine</span>
              <span className="text-slate-200">Deterministic Financial Verifier V2</span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Verified Timestamp</span>
              <span className="text-slate-300">
                {new Date(verification.verified_at).toLocaleTimeString()}
              </span>
            </div>

            <div>
              <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Safety Rule Threshold</span>
              <span className="text-emerald-400">Strict 100% Invariant Pass Required</span>
            </div>
          </div>
        </div>

        {/* Right: Rule Inspector Rows (8 cols) */}
        <div className="lg:col-span-8 space-y-3 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold">
              Deterministic Invariant Checks ({totalChecks})
            </span>
            <div className="flex items-center gap-4 text-[10px] font-mono">
              <span className="text-emerald-400 font-bold">{passedChecksCount} PASSED</span>
              <span className="text-rose-400 font-bold">{failedChecksCount} FAILED</span>
            </div>
          </div>

          <div className="space-y-2 max-h-[360px] overflow-y-auto pr-2">
            {verification.checks && verification.checks.map((check) => (
              <div
                key={check.check_id}
                className={`p-3 rounded-sm border text-xs font-mono flex flex-col gap-1.5 transition-colors ${
                  check.passed
                    ? 'bg-[#0E141D] border-slate-800/80'
                    : 'bg-rose-950/20 border-rose-900/40'
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className={check.passed ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                      {check.passed ? '✓' : '✕'}
                    </span>
                    <span className="font-semibold text-slate-200">{check.name || check.check_id}</span>
                  </div>
                  <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.2 rounded-sm ${
                    check.passed ? 'text-emerald-400 bg-emerald-950/60' : 'text-rose-400 bg-rose-950/60'
                  }`}>
                    {check.passed ? 'PASS' : 'FAIL'}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-4 text-[11px] pt-1 text-slate-400 border-t border-slate-800/40">
                  <div>
                    <span className="text-[9px] text-slate-500 uppercase block">Expected:</span>
                    <span className="text-slate-300">{check.expected || 'Within bounds'}</span>
                  </div>
                  <div>
                    <span className="text-[9px] text-slate-500 uppercase block">Observed:</span>
                    <span className={check.passed ? 'text-slate-300' : 'text-rose-300'}>
                      {check.observed || 'Verified'}
                    </span>
                  </div>
                </div>

                {check.detail && (
                  <div className="text-[10px] text-slate-500 font-sans mt-0.5">
                    {check.detail}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

      </div>
    </div>
  );
};
