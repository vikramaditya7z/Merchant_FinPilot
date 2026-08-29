import React from 'react';
import {
  ShieldCheck,
  Lock,
  Sparkles,
  Activity,
} from 'lucide-react';
import { HealthResponse } from '../../api/types';

interface SystemHealthBarProps {
  health: HealthResponse | null;
  onOpenAudit: () => void;
  geminiModel?: string;
  isRealMode?: boolean;
}

export const SystemHealthBar: React.FC<SystemHealthBarProps> = ({
  health,
  onOpenAudit,
  geminiModel = 'gemini-3.1-flash-lite',
  isRealMode = true,
}) => {
  return (
    <header className="bg-[#0C1019] border-b border-slate-800/60 px-4 md:px-6 py-2 flex items-center justify-between">
      {/* Brand */}
      <div className="flex items-center gap-3">
        <div className="w-7 h-7 rounded-lg bg-gradient-to-tr from-blue-600 via-indigo-600 to-violet-600 flex items-center justify-center shadow-lg shadow-blue-500/20 text-white shrink-0">
          <Activity className="w-3.5 h-3.5 text-blue-100" />
        </div>
        <div className="flex items-baseline gap-2">
          <h1 className="text-sm font-bold text-slate-100 tracking-tight">Merchant FinPilot</h1>
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700/50">
            v2.0
          </span>
        </div>
      </div>

      {/* Operational Indicators */}
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-900/80 border border-slate-800/60 text-[11px]">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-slate-400">API:</span>
          <span className="font-mono font-semibold text-slate-200 uppercase">{health?.status || 'HEALTHY'}</span>
        </div>

        <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-blue-950/40 border border-blue-800/30 text-[11px]">
          <Sparkles className="w-3 h-3 text-blue-400" />
          <span className="font-mono text-blue-300">{isRealMode ? 'REAL' : 'MOCK'} ({geminiModel})</span>
        </div>

        <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-amber-950/40 border border-amber-800/30 text-[11px]">
          <Lock className="w-3 h-3 text-amber-400" />
          <span className="font-mono text-amber-300">SIMULATED</span>
        </div>

        <button
          onClick={onOpenAudit}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 border border-slate-700/60 text-[11px] text-slate-200 transition-colors"
          title="Inspect SHA-256 hash-chained audit trail"
        >
          <ShieldCheck className="w-3 h-3 text-emerald-400" />
          <span className="font-mono">Audit</span>
        </button>
      </div>
    </header>
  );
};
