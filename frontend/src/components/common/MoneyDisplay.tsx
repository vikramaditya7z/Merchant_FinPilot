import React from 'react';

interface MoneyDisplayProps {
  paise: number | null | undefined;
  currency?: string;
  className?: string;
}

export const MoneyDisplay: React.FC<MoneyDisplayProps> = ({
  paise,
  currency = 'INR',
  className = '',
}) => {
  if (paise === null || paise === undefined) {
    return <span className={`text-slate-400 font-mono ${className}`}>—</span>;
  }

  const rupees = paise / 100;
  const formatted = new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: currency,
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
  }).format(rupees);

  return (
    <span className={`font-mono font-semibold tracking-tight ${className}`}>
      {formatted}
      <span className="text-[10px] text-slate-500 font-normal ml-1">({paise.toLocaleString()}p)</span>
    </span>
  );
};
