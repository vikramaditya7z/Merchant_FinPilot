import React from 'react';
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
  Loader2,
  Copy,
  MinusCircle,
} from 'lucide-react';

interface StatusBadgeProps {
  status: string;
  label?: string;
  size?: 'xs' | 'sm' | 'md';
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({
  status,
  label,
  size = 'md',
}) => {
  const norm = (status || '').toLowerCase();
  const displayLabel = label || status.replace(/_/g, ' ');

  let colorClasses = 'bg-slate-800 text-slate-300 border-slate-700';
  let Icon = Clock;

  if (
    norm === 'completed' ||
    norm === 'allow' ||
    norm === 'verified' ||
    norm === 'healthy' ||
    norm === 'simulated' ||
    norm === 'succeeded' ||
    norm === 'pass'
  ) {
    colorClasses = 'bg-emerald-950/70 text-emerald-300 border-emerald-500/40 shadow-sm shadow-emerald-950';
    Icon = CheckCircle2;
  } else if (
    norm === 'failed' ||
    norm === 'block' ||
    norm === 'rejected' ||
    norm === 'p0_critical' ||
    norm === 'ineligible' ||
    norm === 'fail'
  ) {
    colorClasses = 'bg-rose-950/70 text-rose-300 border-rose-500/40 shadow-sm shadow-rose-950';
    Icon = XCircle;
  } else if (
    norm === 'stopped' ||
    norm === 'escalate' ||
    norm === 'escalated' ||
    norm === 'p1_high' ||
    norm === 'p2_medium' ||
    norm === 'warning'
  ) {
    colorClasses = 'bg-amber-950/70 text-amber-300 border-amber-500/40 shadow-sm shadow-amber-950';
    Icon = AlertTriangle;
  } else if (norm === 'queued' || norm === 'received') {
    colorClasses = 'bg-cyan-950/70 text-cyan-300 border-cyan-500/40 shadow-sm shadow-cyan-950';
    Icon = Clock;
  } else if (norm === 'skipped_duplicate' || norm === 'duplicate' || norm === 'idempotent') {
    colorClasses = 'bg-indigo-950/70 text-indigo-300 border-indigo-500/40 shadow-sm shadow-indigo-950';
    Icon = Copy;
  } else if (norm === 'running' || norm === 'processing') {
    colorClasses = 'bg-blue-950/70 text-blue-300 border-blue-500/40 animate-pulse';
    Icon = Loader2;
  } else if (norm === 'inconclusive' || norm === 'skipped' || norm === 'not_run' || norm === 'waiting') {
    colorClasses = 'bg-slate-900 text-slate-400 border-slate-800';
    Icon = MinusCircle;
  }

  const sizeClasses =
    size === 'xs'
      ? 'px-1.5 py-0.5 text-[10px]'
      : size === 'sm'
      ? 'px-2 py-0.5 text-xs'
      : 'px-2.5 py-1 text-xs md:text-sm';

  return (
    <span
      className={`inline-flex items-center gap-1.5 font-medium rounded-full border ${colorClasses} ${sizeClasses}`}
    >
      <Icon className={`w-3.5 h-3.5 shrink-0 ${norm === 'running' ? 'animate-spin text-blue-400' : ''}`} />
      <span className="capitalize">{displayLabel}</span>
    </span>
  );
};
