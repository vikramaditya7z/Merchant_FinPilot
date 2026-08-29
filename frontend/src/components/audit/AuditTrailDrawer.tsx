import React, { useState, useEffect } from 'react';
import {
  X,
  ShieldCheck,
  Hash,
  User,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  ArrowDown,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react';
import { AuditTrailResponse } from '../../api/types';
import { apiClient } from '../../api/client';

interface AuditTrailDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  currentIncidentId?: string | null;
}

export const AuditTrailDrawer: React.FC<AuditTrailDrawerProps> = ({
  isOpen,
  onClose,
  currentIncidentId,
}) => {
  const [auditData, setAuditData] = useState<AuditTrailResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [expandedEvents, setExpandedEvents] = useState<Record<string, boolean>>({});

  const fetchAudit = async () => {
    setIsLoading(true);
    try {
      const data = await apiClient.getAuditTrail();
      setAuditData(data);
    } catch (err) {
      console.error('Failed to fetch audit trail:', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchAudit();
    }
  }, [isOpen, currentIncidentId]);

  const toggleExpand = (id: string) => {
    setExpandedEvents((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/70 backdrop-blur-sm transition-opacity">
      <div className="w-full max-w-2xl bg-[#0C101C] border-l border-slate-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-300">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-800 flex items-center justify-between bg-[#0F1424]">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-emerald-950/80 border border-emerald-500/40 text-emerald-400">
              <ShieldCheck className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-sm md:text-base font-bold text-slate-100">
                  Cryptographic Audit Ledger
                </h2>
                <span className="text-[10px] uppercase font-mono px-2 py-0.2 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/40 font-semibold">
                  SHA-256 Chained
                </span>
              </div>
              <p className="text-[11px] text-slate-400">
                Immutable hash-chained audit records for end-to-end incident lifecycle
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={fetchAudit}
              disabled={isLoading}
              className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition-colors"
              title="Refresh audit ledger"
            >
              <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin text-blue-400' : ''}`} />
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Cryptographic Integrity Verification Banner */}
        <div className="px-6 py-3 border-b border-slate-800 bg-[#090D18] flex items-center justify-between text-xs">
          <div className="flex items-center gap-2">
            {auditData?.is_valid ? (
              <span className="flex items-center gap-1.5 font-bold text-emerald-400 text-xs">
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                ✓ Cryptographic Hash Chain Verified Valid (Zero Tampering)
              </span>
            ) : (
              <span className="flex items-center gap-1.5 font-bold text-rose-400 text-xs">
                <AlertTriangle className="w-4 h-4 text-rose-400" />
                Audit Integrity Verification Error
              </span>
            )}
          </div>
          <span className="text-slate-400 font-mono text-[11px] bg-slate-900 px-2 py-0.5 rounded border border-slate-800">
            {auditData?.count || 0} Chained Blocks
          </span>
        </div>

        {/* Chained Event List */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
          {isLoading && !auditData ? (
            <div className="flex items-center justify-center py-16 text-slate-400 text-xs">
              <RefreshCw className="w-4 h-4 animate-spin mr-2 text-blue-400" />
              Verifying cryptographic ledger blocks...
            </div>
          ) : auditData?.events?.length === 0 ? (
            <div className="text-center py-16 text-slate-500 text-xs italic">
              No audit events recorded in this database. Run a synthetic incident pipeline to generate chained records.
            </div>
          ) : (
            auditData?.events.map((event, idx) => {
              const isExpanded = !!expandedEvents[event.event_id];
              return (
                <div key={event.event_id} className="relative">
                  {/* Visual Hash Link Line between events */}
                  {idx > 0 && (
                    <div className="flex items-center justify-center my-1 text-slate-600">
                      <ArrowDown className="w-3 h-3 text-slate-700" />
                    </div>
                  )}

                  <div className="bg-[#111726] border border-slate-800/90 rounded-lg p-3 text-xs transition-all hover:border-slate-700">
                    <div
                      className="flex items-center justify-between cursor-pointer select-none"
                      onClick={() => toggleExpand(event.event_id)}
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[10px] px-1.5 py-0.2 rounded bg-slate-900 text-blue-400 font-bold border border-slate-800">
                          #{event.sequence}
                        </span>
                        <span className="font-bold text-slate-200 uppercase font-mono text-[11px]">
                          {event.event_type.replace(/_/g, ' ')}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 text-slate-400 text-[10px] font-mono">
                        <span>{new Date(event.occurred_at).toLocaleTimeString()}</span>
                        {isExpanded ? (
                          <ChevronDown className="w-3.5 h-3.5 text-slate-400" />
                        ) : (
                          <ChevronRight className="w-3.5 h-3.5 text-slate-400" />
                        )}
                      </div>
                    </div>

                    <div className="mt-1.5 text-slate-300 font-sans text-xs">{event.summary}</div>

                    <div className="mt-2 pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-2 text-[10px] text-slate-400 font-mono">
                      <span className="flex items-center gap-1">
                        <User className="w-3 h-3 text-slate-500" />
                        Actor: <strong className="text-slate-300 font-semibold">{event.actor}</strong>
                      </span>
                      <span className="flex items-center gap-1 truncate max-w-[240px]" title={event.payload_digest}>
                        <Hash className="w-3 h-3 text-slate-500" />
                        SHA-256: {event.payload_digest.slice(0, 16)}...
                      </span>
                    </div>

                    {isExpanded && (
                      <div className="mt-3 pt-2 border-t border-slate-800 bg-[#080C16] p-2.5 rounded text-[11px] font-mono text-slate-300 overflow-x-auto">
                        <div className="text-[10px] text-slate-400 uppercase mb-1 font-sans font-bold flex items-center justify-between">
                          <span>Payload JSON:</span>
                          <span className="text-slate-500 text-[9px]">Event ID: {event.event_id}</span>
                        </div>
                        <pre className="text-blue-300/90 whitespace-pre-wrap text-[10px]">
                          {JSON.stringify(event.payload, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-slate-800 bg-[#0F1424] text-[11px] text-slate-400 font-mono flex items-center justify-between">
          <span>Backend: InMemory / SQLite Chained Ledger</span>
          <span className="text-emerald-400 font-semibold">✓ Cryptographically Sealed</span>
        </div>
      </div>
    </div>
  );
};
