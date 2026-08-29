import React from 'react';
import { ShieldAlert, Lock } from 'lucide-react';

export const SimulationBanner: React.FC = () => {
  return (
    <div className="bg-amber-950/60 border-b border-amber-900/50 py-1 px-4 text-xs flex items-center justify-center gap-3 whitespace-nowrap overflow-hidden">
      <ShieldAlert className="w-3.5 h-3.5 text-amber-500" />
      <span className="font-semibold text-amber-500 tracking-wider">SIMULATION MODE</span>
      <div className="h-3 w-px bg-amber-800/50 mx-1" />
      <div className="flex items-center gap-1.5 bg-amber-900/40 px-2 rounded border border-amber-800/30">
        <Lock className="w-3 h-3 text-amber-400" />
        <span className="text-amber-400 font-mono text-[10px]">FAIL-CLOSED</span>
      </div>
      <div className="flex items-center gap-1.5 bg-amber-900/40 px-2 rounded border border-amber-800/30">
        <span className="text-amber-400 font-mono text-[10px]">SimulatedExecutionAdapter</span>
      </div>
    </div>
  );
};
