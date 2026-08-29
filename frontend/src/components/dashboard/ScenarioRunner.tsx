import React from 'react';
import {
  Play,
  Loader2,
  Building,
  FileText,
} from 'lucide-react';
import { ScenarioMetadata } from '../../api/types';

interface ScenarioRunnerProps {
  scenarios: ScenarioMetadata[];
  selectedScenario: string;
  onSelectScenario: (id: string) => void;
  merchantId: string;
  onChangeMerchantId: (id: string) => void;
  contextNotes: string;
  onChangeContextNotes: (notes: string) => void;
  onRunPipeline: () => void;
  isLoading: boolean;
  activeStageIndex?: number;
  totalStages?: number;
}

export const ScenarioRunner: React.FC<ScenarioRunnerProps> = ({
  scenarios,
  selectedScenario,
  onSelectScenario,
  merchantId,
  onChangeMerchantId,
  contextNotes,
  onChangeContextNotes,
  onRunPipeline,
  isLoading,
  activeStageIndex = 0,
  totalStages = 6,
}) => {
  const currentScenario = scenarios.find((s) => s.scenario_id === selectedScenario);

  return (
    <div className="border-b border-slate-800/60 pb-4">
      {/* Compact Toolbar Row */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3 mb-3">
        {/* Merchant ID */}
        <div className="lg:col-span-3">
          <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
            Merchant
          </label>
          <div className="flex items-center bg-[#0A0E1A] border border-slate-700/60 rounded-md overflow-hidden">
            <div className="px-2 py-2 text-slate-500 bg-slate-800/30 border-r border-slate-700/40">
              <Building className="w-3.5 h-3.5" />
            </div>
            <input
              type="text"
              value={merchantId}
              onChange={(e) => onChangeMerchantId(e.target.value)}
              placeholder="merchant_razorpay_live_01"
              className="w-full bg-transparent px-2.5 py-1.5 text-xs text-slate-100 font-mono placeholder:text-slate-600 focus:outline-none"
            />
          </div>
        </div>

        {/* Scenario */}
        <div className="lg:col-span-4">
          <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
            Scenario
          </label>
          <select
            value={selectedScenario}
            onChange={(e) => onSelectScenario(e.target.value)}
            className="w-full bg-[#0A0E1A] border border-slate-700/60 rounded-md px-2.5 py-1.5 text-xs text-slate-100 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
          >
            {scenarios.map((scen) => (
              <option key={scen.scenario_id} value={scen.scenario_id}>
                {scen.name} {scen.is_incident ? '🚨' : '🟢'}
              </option>
            ))}
          </select>
        </div>

        {/* Context Notes */}
        <div className="lg:col-span-3">
          <label className="block text-[10px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
            Context
          </label>
          <div className="flex items-center bg-[#0A0E1A] border border-slate-700/60 rounded-md overflow-hidden">
            <div className="px-2 py-2 text-slate-500 bg-slate-800/30 border-r border-slate-700/40">
              <FileText className="w-3.5 h-3.5" />
            </div>
            <input
              type="text"
              value={contextNotes}
              onChange={(e) => onChangeContextNotes(e.target.value)}
              placeholder="Peak traffic notes..."
              className="w-full bg-transparent px-2.5 py-1.5 text-xs text-slate-100 placeholder:text-slate-600 focus:outline-none"
            />
          </div>
        </div>

        {/* Run Pipeline Button */}
        <div className="lg:col-span-2 flex items-end">
          <button
            onClick={onRunPipeline}
            disabled={isLoading || !merchantId.trim()}
            className={`w-full h-[34px] text-xs font-semibold rounded-md flex items-center justify-center gap-2 transition-all ${
              isLoading
                ? 'bg-blue-900/60 text-blue-200 border border-blue-700/50 cursor-wait'
                : 'bg-blue-600 hover:bg-blue-500 text-white active:scale-[0.98]'
            } disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            {isLoading ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-300" />
                <span className="truncate">Stage {activeStageIndex + 1}/{totalStages}...</span>
              </>
            ) : (
              <>
                <Play className="w-3.5 h-3.5 fill-white shrink-0" />
                <span>Run Pipeline</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Compact Scenario Descriptor */}
      {currentScenario && (
        <div className="flex items-center gap-2.5 text-xs px-1">
          <span className="font-semibold text-slate-200">{currentScenario.name}</span>
          <span
            className={`text-[10px] px-1.5 py-0.5 rounded-full font-mono uppercase font-bold border ${
              currentScenario.is_incident
                ? 'bg-rose-950/50 text-rose-300 border-rose-500/30'
                : 'bg-emerald-950/50 text-emerald-300 border-emerald-500/30'
            }`}
          >
            {currentScenario.is_incident ? 'Incident' : 'Normal'}
          </span>
          {currentScenario.expected_action_eligible === false && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full font-mono uppercase font-semibold bg-amber-950/50 text-amber-300 border border-amber-500/30">
              Ineligible
            </span>
          )}
          <span className="text-slate-500 text-[11px] truncate">{currentScenario.description}</span>
        </div>
      )}
    </div>
  );
};
