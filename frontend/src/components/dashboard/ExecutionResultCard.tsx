import React, { useState } from 'react';
import { ExecutionResult, StageExecutionTiming } from '../../api/types';

interface ExecutionResultCardProps {
  execution: ExecutionResult | null;
  timing?: StageExecutionTiming;
}

export const ExecutionResultCard: React.FC<ExecutionResultCardProps> = ({
  execution,
  timing,
}) => {
  const [copied, setCopied] = useState(false);
  const status = timing?.status || (execution ? 'completed' : 'waiting');

  const isRazorpay = Boolean(
    execution?.provider_reference?.startsWith('plink_') ||
    execution?.message?.toLowerCase().includes('razorpay')
  );
  const isReconciled = Boolean(
    execution?.message?.toLowerCase().includes('verified paid') ||
    execution?.message?.toLowerCase().includes('reconciled')
  );
  const urlMatch = execution?.message ? execution.message.match(/(https:\/\/[^\s\)]+)/) : null;
  const paymentLinkUrl = urlMatch ? urlMatch[1] : null;

  if (!execution && status === 'waiting') {
    return (
      <div id="stage-execution" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">06 / SIMULATED EXECUTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Fail-Closed Execution Adapter
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Awaiting policy authorization before dispatching simulated execution adapter...
        </p>
      </div>
    );
  }

  if (!execution && status === 'running') {
    return (
      <div id="stage-execution" className="w-full bg-[#0B1017] border border-amber-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-amber-400 font-medium">06 / SIMULATED EXECUTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-amber-300 font-semibold">
              Fail-Closed Execution Adapter
            </span>
          </div>
          <span className="text-[10px] font-mono text-amber-300 uppercase px-2 py-0.5 rounded-sm bg-amber-950/80 border border-amber-800/80 animate-pulse">
            DISPATCHING SIMULATED ADAPTER...
          </span>
        </div>
        <p className="text-xs font-mono text-amber-300 py-4">
          Generating cryptographic digest, verifying idempotency key, and simulating mutation...
        </p>
      </div>
    );
  }

  if (!execution && status === 'completed') {
    return (
      <div id="stage-execution" className="w-full bg-[#0B1017] border border-amber-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-amber-400 font-medium">06 / SIMULATED EXECUTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-amber-300 font-semibold">
              Fail-Closed Execution Adapter
            </span>
          </div>
          <span className="text-[10px] font-mono text-amber-400 font-bold uppercase px-2 py-0.5 rounded-sm bg-amber-950/80 border border-amber-800/80">
            COMPLETED
          </span>
        </div>
        <p className="text-xs font-mono text-amber-300/80 py-4">
          Simulated execution completed. Idempotent action safely executed with non-production test adapter.
        </p>
      </div>
    );
  }

  if (!execution) {
    return (
      <div id="stage-execution" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">06 / SIMULATED EXECUTION</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Fail-Closed Execution Adapter
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            NOT RUN
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-2">
          Simulated execution not executed (pipeline halted before execution stage).
        </p>
      </div>
    );
  }

  const isDuplicate = execution.status === 'skipped_duplicate';

  const handleCopyDigest = () => {
    if (execution.response_digest) {
      navigator.clipboard.writeText(execution.response_digest);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div id="stage-execution" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">
            06 / {isRazorpay ? 'RAZORPAY TEST EXECUTION' : 'SIMULATED EXECUTION'}
          </span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-amber-400 font-semibold">
            {isRazorpay ? 'Razorpay Sandbox Execution & Reconciliation' : 'Simulated Execution Boundary & Mutator'}
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm bg-amber-950/40 text-amber-400 border border-amber-800/40">
            {isRazorpay ? 'RAZORPAY TEST MODE • TEST API ONLY' : 'FAIL-CLOSED • SIMULATED ONLY'}
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            {execution.completed_at ? new Date(execution.completed_at).toLocaleTimeString() : 'In-flight'}
          </span>
        </div>
      </div>

      {/* Execution Boundary Banner */}
      <div className="p-6 rounded-sm border bg-[#18120B] border-amber-900/60 text-amber-400 flex items-center justify-between">
        <div className="flex items-baseline gap-6">
          <div className="text-3xl font-mono font-bold tracking-tight uppercase">
            {isDuplicate
              ? 'IDEMPOTENT / CACHED'
              : isRazorpay
              ? 'RAZORPAY TEST EXECUTION'
              : 'SIMULATED EXECUTION'}
          </div>
          <div>
            <div className="text-xs font-mono font-bold tracking-widest uppercase">
              STATUS: {execution.status.toUpperCase()}
            </div>
            <div className="text-xs font-mono opacity-70">
              {isRazorpay
                ? 'Razorpay TEST Sandbox • Real Test Rail Mutation'
                : 'No live financial balance mutated • Pure simulated environment'}
            </div>
          </div>
        </div>

        <div className="px-4 py-2 rounded-sm border font-mono font-bold text-xs uppercase tracking-widest bg-amber-950/80 border-amber-700/80 text-amber-300">
          {isReconciled ? '✓ RECONCILED (PAID)' : '🔒 FAIL-CLOSED (TEST ONLY)'}
        </div>
      </div>

      {/* Financial Transaction Receipt / Audit Record */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* Left: Execution Receipt (7 cols) */}
        <div className="lg:col-span-7 space-y-4">
          <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block">
            Cryptographic Execution Receipt
          </span>

          <div className="bg-[#0E141D] border border-slate-800/80 p-4 rounded-sm space-y-4 font-mono text-xs">
            <div className="flex justify-between items-center border-b border-slate-800/60 pb-3">
              <span className="text-slate-500 text-[10px] uppercase">Action Dispatched</span>
              <span className="text-blue-300 font-bold uppercase">{execution.action}</span>
            </div>

            <div className="flex justify-between items-center border-b border-slate-800/60 pb-3">
              <span className="text-slate-500 text-[10px] uppercase">Outcome State</span>
              <span className={isReconciled ? 'text-emerald-400 font-bold' : isDuplicate ? 'text-indigo-300 font-bold' : 'text-emerald-400 font-bold'}>
                {isReconciled ? 'RECONCILED (PAID)' : isDuplicate ? 'IDEMPOTENT SKIPPED' : 'COMPLETED'}
              </span>
            </div>

            <div className="flex justify-between items-center border-b border-slate-800/60 pb-3">
              <span className="text-slate-500 text-[10px] uppercase">Provider Reference</span>
              <span className="text-slate-200 font-semibold">{execution.provider_reference}</span>
            </div>

            {paymentLinkUrl && (
              <div className="flex justify-between items-center border-b border-slate-800/60 pb-3">
                <span className="text-slate-500 text-[10px] uppercase">Payment Link URL</span>
                <a
                  href={paymentLinkUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-cyan-400 hover:text-cyan-300 font-semibold underline truncate max-w-[280px]"
                >
                  {paymentLinkUrl} ↗
                </a>
              </div>
            )}

            <div className="flex justify-between items-center border-b border-slate-800/60 pb-3">
              <span className="text-slate-500 text-[10px] uppercase">Idempotency Key</span>
              <span className="text-slate-300 text-[11px] truncate max-w-[280px]">
                {execution.idempotency_key}
              </span>
            </div>

            <div className="flex justify-between items-center">
              <span className="text-slate-500 text-[10px] uppercase">Execution ID</span>
              <span className="text-slate-400 text-[11px]">{execution.execution_id}</span>
            </div>
          </div>
        </div>

        {/* Right: Message & Digest (5 cols) */}
        <div className="lg:col-span-5 space-y-6 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Adapter Response Message
            </span>
            <p className="text-xs text-slate-200 font-sans leading-relaxed bg-[#0E141D] p-4 border border-slate-800/80 rounded-sm">
              {execution.message}
            </p>
          </div>

          <div className="space-y-2 font-mono text-xs">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-slate-500 uppercase font-semibold">
                SHA-256 Digest
              </span>
              <button
                onClick={handleCopyDigest}
                className="text-[10px] text-blue-400 hover:text-blue-300 font-mono transition-colors"
              >
                {copied ? '✓ COPIED' : 'COPY'}
              </button>
            </div>
            <div className="bg-[#0A0E14] p-3 border border-slate-800/60 rounded-sm text-[10px] text-slate-400 break-all leading-relaxed">
              {execution.response_digest}
            </div>
          </div>
        </div>

      </div>
    </div>
  );
};
