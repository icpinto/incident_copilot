// CSS variables keep every Recharts view aligned with the workspace design tokens.
export const chartTheme = {
  accent: 'var(--accent)',
  secondary: 'var(--blue)',
  grid: 'var(--border)',
  cursor: { fill: 'var(--surface)' },
  tick: { fill: 'var(--muted)', fontSize: 13 },
};

export const chartTooltip = {
  background: 'var(--panel-inset)',
  border: '1px solid var(--border-hover)',
  borderRadius: 10,
  color: 'var(--text)',
  fontSize: '0.875rem',
  padding: '10px 14px',
  boxShadow: 'var(--shadow-raised)',
};
