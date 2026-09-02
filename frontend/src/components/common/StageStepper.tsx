import React from 'react';
import { ProcessIncidentResponse, StageExecutionTiming, StageId } from '../../api/types';

export type { StageId };

export type StageStatus =
  | 'waiting'
  | 'running'
  | 'completed'
  | 'blocked'
  | 'failed'
  | 'duplicate'
  | 'skipped';

interface StageConfig {
  id: StageId;
  stageNumber: number;
  label: string;
  shortLabel: string;
}

const PIPELINE_STAGES: StageConfig[] = [
  { id: 'detection', stageNumber: 1, label: 'Detection', shortLabel: 'DETECT' },
  { id: 'investigation', stageNumber: 2, label: 'Investigation', shortLabel: 'INVESTIGATE' },
  { id: 'agent', stageNumber: 3, label: 'AI Reasoning', shortLabel: 'REASON' },
  { id: 'verification', stageNumber: 4, label: 'Verification', shortLabel: 'VERIFY' },
  { id: 'policy', stageNumber: 5, label: 'Policy Engine', shortLabel: 'AUTHORIZE' },
  { id: 'execution', stageNumber: 6, label: 'Execution', shortLabel: 'EXECUTE' },
];

const formatDuration = (durationMs?: number): string | null => {
  if (durationMs === undefined || durationMs === null || isNaN(durationMs)) return null;
  if (durationMs < 1) return '<1ms';
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  return `${(durationMs / 1000).toFixed(1)}s`;
};

interface StageStepperProps {
  response: ProcessIncidentResponse | null;
  isLoading: boolean;
  activeStageIndex: number;
  stageTimings?: Record<StageId, StageExecutionTiming>;
  selectedStage?: StageId | null;
  onSelectStage?: (stage: StageId) => void;
}

export const StageStepper: React.FC<StageStepperProps> = ({
  response,
  isLoading,
  activeStageIndex,
  stageTimings,
  selectedStage,
  onSelectStage,
}) => {
  const getStageStatus = (stage: StageConfig, index: number): StageStatus => {
    // 1. Live stage timings take highest priority if actively running, completed, blocked, or failed
    if (stageTimings && stageTimings[stage.id]) {
      const liveStatus = stageTimings[stage.id].status;
      if (liveStatus && liveStatus !== 'waiting') {
        if (liveStatus === 'completed' && index === 5 && response?.execution_result?.status === 'skipped_duplicate') {
          return 'duplicate';
        }
        return liveStatus as StageStatus;
      }
    }

    // 2. Authoritative response derivation when completed or reporting intermediate stage
    if (response) {
      const stageOrder: StageId[] = [
        'detection',
        'investigation',
        'agent',
        'verification',
        'policy',
        'execution',
      ];
      const stageName = ((response as any).current_stage || response.final_stage || 'detection') as StageId;
      const targetIndex = stageOrder.indexOf(stageName);
      const finalStageIndex = response.is_completed ? 5 : (targetIndex >= 0 ? targetIndex : 0);

      if (response.is_completed) {
        if (index === 5 && response.execution_result?.status === 'skipped_duplicate') {
          return 'duplicate';
        }
        return 'completed';
      }

      if (index < finalStageIndex) return 'completed';
      if (index === finalStageIndex) {
        if (response.is_failed) return 'failed';
        if (response.is_stopped) return 'blocked';
        const st = (response as any).stage_status;
        if (st === 'running') return 'running';
        if (st === 'completed') return 'completed';
        return 'completed';
      }
      return 'waiting';
    }

    if (isLoading && index === activeStageIndex) return 'running';
    if (isLoading && index < activeStageIndex) return 'completed';
    return 'waiting';
  };

  const handleStageClick = (stageId: StageId) => {
    if (onSelectStage) {
      onSelectStage(stageId);
    }
    const elem = document.getElementById(`stage-${stageId}`);
    if (elem) {
      elem.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  return (
    <div className="w-full bg-[#0B1017] border border-slate-800/80 rounded-sm py-4 px-6 select-none sticky top-0 z-20 shadow-md">
      <div className="flex items-center justify-between min-w-[720px]">
        {PIPELINE_STAGES.map((stage, idx) => {
          const status = getStageStatus(stage, idx);
          const isSelected = selectedStage === stage.id;
          const isLast = idx === PIPELINE_STAGES.length - 1;
          const timing = stageTimings?.[stage.id];
          const durationStr = formatDuration(timing?.durationMs);

          let dotColor = 'bg-slate-700 border-slate-600';
          let textColor = 'text-slate-500';
          let badgeText = 'NOT RUN';
          let badgeColor = 'text-slate-500';
          let lineColor = 'bg-slate-800';

          if (status === 'completed') {
            dotColor = 'bg-emerald-500 border-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.4)]';
            textColor = 'text-slate-200';
            badgeText = stage.id === 'verification' && response?.verification_result 
              ? `${response.verification_result.checks.filter(c => c.passed).length}/${response.verification_result.checks.length}` 
              : (stage.id === 'policy' ? 'ALLOW' : (stage.id === 'execution' ? 'SIMULATED' : 'PASS'));
            badgeColor = 'text-emerald-400';
            lineColor = 'bg-emerald-500/40';
          } else if (status === 'duplicate') {
            dotColor = 'bg-slate-300 border-slate-200';
            textColor = 'text-slate-200';
            badgeText = 'IDEMPOTENT';
            badgeColor = 'text-slate-300';
            lineColor = 'bg-slate-600';
          } else if (status === 'running') {
            dotColor = 'bg-blue-400 border-blue-300 animate-pulse ring-4 ring-blue-500/20';
            textColor = 'text-blue-300 font-semibold';
            badgeText = 'RUNNING';
            badgeColor = 'text-blue-400 animate-pulse';
            lineColor = 'bg-slate-800';
          } else if (status === 'blocked') {
            dotColor = 'bg-rose-500 border-rose-400 shadow-[0_0_8px_rgba(244,63,94,0.4)]';
            textColor = 'text-rose-300 font-semibold';
            badgeText = 'BLOCKED';
            badgeColor = 'text-rose-400';
            lineColor = 'bg-slate-800';
          } else if (status === 'failed') {
            dotColor = 'bg-rose-600 border-rose-500 shadow-[0_0_8px_rgba(225,29,72,0.4)]';
            textColor = 'text-rose-400 font-semibold';
            badgeText = 'FAILED';
            badgeColor = 'text-rose-400';
            lineColor = 'bg-slate-800';
          } else if (status === 'waiting') {
            dotColor = 'bg-slate-800 border-slate-700';
            textColor = 'text-slate-500';
            badgeText = 'WAITING';
            badgeColor = 'text-slate-600';
            lineColor = 'bg-slate-800';
          }

          return (
            <React.Fragment key={stage.id}>
              {/* Stage Node Button */}
              <button
                type="button"
                onClick={() => handleStageClick(stage.id)}
                title={`Click to scroll to Stage 0${stage.stageNumber} (${stage.label})`}
                className={`flex-1 flex flex-col items-center text-center transition-all group relative px-2 py-1.5 rounded-sm cursor-pointer ${
                  isSelected ? 'bg-slate-800/40 ring-1 ring-blue-500/50' : 'hover:bg-slate-800/20'
                }`}
              >
                {/* Stage Number & Label */}
                <div className="flex items-center gap-1.5 mb-2">
                  <span className="text-[10px] font-mono text-slate-500 font-medium">0{stage.stageNumber}</span>
                  <span className={`text-[11px] font-mono tracking-wider uppercase font-semibold ${textColor}`}>
                    {stage.shortLabel}
                  </span>
                </div>

                {/* Node Dot */}
                <div className="relative flex items-center justify-center my-1">
                  <div className={`w-2.5 h-2.5 rounded-full border transition-all ${dotColor} ${isSelected ? 'scale-125' : ''}`} />
                </div>

                {/* Status & Duration */}
                <div className="flex items-center gap-1.5 mt-2 font-mono text-[10px]">
                  <span className={`font-bold tracking-tight uppercase ${badgeColor}`}>
                    ● {badgeText}
                  </span>
                  {durationStr && status !== 'skipped' && status !== 'waiting' && (
                    <span className="text-slate-400 font-normal">
                      {durationStr}
                    </span>
                  )}
                </div>

                {/* Active Indicator Underline */}
                {isSelected && (
                  <div className="absolute -bottom-4 left-1/2 -translate-x-1/2 w-12 h-0.5 bg-blue-500 shadow-[0_0_8px_rgba(59,130,246,0.6)]" />
                )}
              </button>

              {/* Connecting Line */}
              {!isLast && (
                <div className="w-8 lg:w-14 h-px relative flex items-center shrink-0 -mt-2">
                  <div className={`w-full h-px ${lineColor} transition-colors`} />
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
};
