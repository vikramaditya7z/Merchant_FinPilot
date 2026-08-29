import React from 'react';
import { AgentResponseData, ProposedIntent, StageExecutionTiming } from '../../api/types';

interface GeminiAgentCardProps {
  agentResponse: AgentResponseData | null;
  proposedIntent: ProposedIntent | null;
  isFailed?: boolean;
  isStopped?: boolean;
  stopReason?: string | null;
  finalStage?: string | null;
  timing?: StageExecutionTiming;
}

export const GeminiAgentCard: React.FC<GeminiAgentCardProps> = ({
  agentResponse,
  proposedIntent,
  isFailed,
  isStopped,
  stopReason,
  finalStage,
  timing,
}) => {
  const status = timing?.status || (agentResponse ? 'completed' : 'waiting');

  if (!agentResponse && status === 'waiting') {
    return (
      <div id="stage-agent" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">03 / AI REASONING</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Autonomous Incident Agent
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            WAITING
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-4">
          Awaiting investigation synthesis before initiating autonomous LLM reasoning loop...
        </p>
      </div>
    );
  }

  if (!agentResponse && status === 'running') {
    return (
      <div id="stage-agent" className="w-full bg-[#0B1017] border border-blue-900/60 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-blue-400 font-medium">03 / AI REASONING</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
              Autonomous Incident Agent (Gemini 3.1 Flash Lite)
            </span>
          </div>
          <span className="text-[10px] font-mono text-blue-300 uppercase px-2 py-0.5 rounded-sm bg-blue-950/80 border border-blue-800/80 animate-pulse">
            RUNNING MULTI-TURN REASONING LOOP...
          </span>
        </div>
        <p className="text-xs font-mono text-blue-300 py-4">
          Synthesizing evidence, querying tool endpoints, and constructing remediation intent...
        </p>
      </div>
    );
  }

  if (!agentResponse) {
    const isStage3Failure = finalStage === 'agent' && (isFailed || isStopped) && !!stopReason;

    if (isStage3Failure) {
      return (
        <div id="stage-agent" className="w-full bg-[#0B1017] border border-rose-900/60 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
          <div className="flex items-center justify-between border-b border-rose-800/60 pb-4">
            <div className="flex items-center gap-3">
              <span className="text-xs font-mono text-rose-400 font-medium">03 / AI REASONING</span>
              <span className="text-rose-700">•</span>
              <span className="text-xs font-mono uppercase tracking-wider text-rose-300 font-semibold">
                Autonomous Loop Halt
              </span>
            </div>
            <span className="text-[10px] font-mono font-bold tracking-widest uppercase px-2 py-0.5 rounded-sm bg-rose-950/60 text-rose-400 border border-rose-800/60">
              {isFailed ? 'FAILED' : 'STOPPED'}
            </span>
          </div>
          <div className="bg-rose-950/20 border-l-2 border-rose-500 p-4 rounded-r-sm">
            <span className="text-[10px] font-mono uppercase tracking-widest text-rose-400 block mb-1">
              Stop Diagnostic Reason
            </span>
            <p className="text-sm font-mono text-rose-200">{stopReason}</p>
          </div>
        </div>
      );
    }

    return (
      <div id="stage-agent" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-4 scroll-mt-24">
        <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
          <div className="flex items-center gap-3">
            <span className="text-xs font-mono text-slate-500 font-medium">03 / AI REASONING</span>
            <span className="text-slate-700">•</span>
            <span className="text-xs font-mono uppercase tracking-wider text-slate-400 font-semibold">
              Autonomous Incident Agent
            </span>
          </div>
          <span className="text-[10px] font-mono text-slate-500 uppercase px-2 py-0.5 rounded-sm bg-slate-900 border border-slate-800">
            NOT RUN
          </span>
        </div>
        <p className="text-xs font-mono text-slate-500 py-2">
          AI reasoning loop not triggered (pipeline terminated prior to agent stage).
        </p>
      </div>
    );
  }

  const confidencePercent = proposedIntent?.confidence
    ? (parseFloat(proposedIntent.confidence) * 100).toFixed(0)
    : null;

  return (
    <div id="stage-agent" className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm p-6 lg:p-8 space-y-6 scroll-mt-24">
      {/* Stage Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-slate-500 font-medium">03 / AI REASONING</span>
          <span className="text-slate-700">•</span>
          <span className="text-xs font-mono uppercase tracking-wider text-blue-300 font-semibold">
            Autonomous Incident Agent ({agentResponse.model_id || 'Gemini 3.1 Flash Lite'})
          </span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-mono uppercase tracking-widest font-bold px-2 py-0.5 rounded-sm bg-blue-950/40 text-blue-400 border border-blue-800/40">
            NON-BINDING • ADVISORY ONLY
          </span>
          <span className="text-[10px] font-mono text-slate-500">
            {agentResponse.iterations_count || 1} {agentResponse.iterations_count === 1 ? 'Turn' : 'Turns'}
          </span>
        </div>
      </div>

      {/* Horizontal Agent Micro-Flow */}
      <div className="flex items-center justify-between bg-[#0E141D] border border-slate-800/80 px-6 py-2.5 rounded-sm text-xs font-mono">
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-400" />
          <span className="text-slate-400">1. OBSERVE</span>
        </div>
        <span className="text-slate-700">→</span>
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-slate-400" />
          <span className="text-slate-400">2. SYNTHESIZE</span>
        </div>
        <span className="text-slate-700">→</span>
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
          <span className="text-blue-300 font-semibold">3. PROPOSE ACTION</span>
        </div>
        <span className="text-slate-700">→</span>
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          <span className="text-emerald-400 font-semibold">4. VERIFY FACTS</span>
        </div>
      </div>

      {/* Horizontal 3-Part Analysis Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
        
        {/* PART 1: PROPOSED ACTION (4 cols) */}
        <div className="lg:col-span-4 space-y-6">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-slate-500 font-semibold block mb-1">
              Autonomous Remediation Intent
            </span>
            {proposedIntent ? (
              <>
                <div className="text-2xl font-bold tracking-tight text-white uppercase text-blue-300 font-mono">
                  {proposedIntent.action.replace(/_/g, ' ')}
                </div>
                <div className="flex items-center gap-2 mt-2">
                  <span className="text-[10px] font-mono font-bold uppercase tracking-wider px-2 py-0.5 rounded-sm bg-blue-950/60 text-blue-300 border border-blue-800/60">
                    RECOMMEND ONLY
                  </span>
                  {confidencePercent && (
                    <span className="text-[10px] font-mono font-bold uppercase tracking-wider px-2 py-0.5 rounded-sm bg-emerald-950/60 text-emerald-300 border border-emerald-800/60">
                      {confidencePercent}% CONFIDENCE
                    </span>
                  )}
                </div>
              </>
            ) : (
              <div className="text-sm font-mono text-slate-500">No action proposed.</div>
            )}
          </div>

          {proposedIntent && (
            <div className="space-y-4 pt-4 border-t border-slate-800/60 font-mono text-xs">
              <div>
                <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Remediation Target</span>
                {proposedIntent.target ? (
                  <span className="text-slate-200 font-semibold">
                    {proposedIntent.target.entity_type}: <span className="text-blue-300">{proposedIntent.target.entity_id}</span>
                  </span>
                ) : (
                  <span className="text-slate-500">—</span>
                )}
              </div>

              <div>
                <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Cited Evidence Records</span>
                <span className="text-slate-300">
                  {proposedIntent.evidence_refs?.length || 0} payloads bound to intent
                </span>
              </div>

              <div>
                <span className="text-[9px] text-slate-500 uppercase block mb-0.5">Intent Content Hash</span>
                <span className="text-[10px] text-slate-500 break-all font-mono">
                  {proposedIntent.content_hash || proposedIntent.intent_id}
                </span>
              </div>
            </div>
          )}
        </div>

        {/* PART 2: AI DIAGNOSTIC SYNTHESIS (5 cols) */}
        <div className="lg:col-span-5 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <span className="text-[10px] font-mono uppercase tracking-widest text-blue-400 font-semibold block">
            AI Diagnostic Synthesis & Reasoning
          </span>

          {proposedIntent?.reason && (
            <div className="bg-[#0E141D] border-l-2 border-blue-500 p-3.5 rounded-r-sm space-y-1">
              <span className="text-[9px] font-mono uppercase tracking-widest text-blue-400 block font-semibold">
                Action Justification
              </span>
              <p className="text-xs text-slate-200 leading-relaxed font-sans italic">
                "{proposedIntent.reason}"
              </p>
            </div>
          )}

          <div className="space-y-2">
            <span className="text-[9px] font-mono uppercase tracking-widest text-slate-500 block">
              Model Diagnostic Output
            </span>
            <div className="text-xs text-slate-300 leading-relaxed font-sans max-h-[260px] overflow-y-auto pr-2 space-y-2">
              {agentResponse.reasoning ? (
                agentResponse.reasoning.split('\n\n').map((para, i) => (
                  <p key={i}>{para}</p>
                ))
              ) : (
                <p className="text-slate-500 italic">No reasoning prose captured.</p>
              )}
            </div>
          </div>
        </div>

        {/* PART 3: VERIFIED EMPIRICAL FACTS (3 cols) */}
        <div className="lg:col-span-3 space-y-4 lg:border-l lg:border-slate-800/60 lg:pl-8">
          <div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-emerald-400 font-semibold block">
              Verified Ground Truth
            </span>
            <span className="text-[9px] font-mono text-slate-500 uppercase block mt-0.5">
              Deterministic Verification Data
            </span>
          </div>

          <div className="space-y-2.5 max-h-[340px] overflow-y-auto pr-1">
            {agentResponse.verified_facts && agentResponse.verified_facts.length > 0 ? (
              agentResponse.verified_facts.map((fact, idx) => (
                <div key={idx} className="flex items-start gap-2 text-xs font-sans text-slate-200">
                  <span className="text-emerald-400 font-mono font-bold shrink-0 mt-0.5">✓</span>
                  <span className="leading-snug text-slate-300">{fact}</span>
                </div>
              ))
            ) : (
              <div className="text-xs font-mono text-slate-500 py-2">
                No factual citations extracted.
              </div>
            )}
          </div>
        </div>

      </div>
    </div>
  );
};
