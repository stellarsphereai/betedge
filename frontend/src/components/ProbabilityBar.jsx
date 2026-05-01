export default function ProbabilityBar({ home, draw, away, homeLabel = 'Home', awayLabel = 'Away' }) {
  const segs = [
    { id: 'home', value: home, label: homeLabel, cls: 'bg-accent' },
    { id: 'draw', value: draw, label: 'Draw', cls: 'bg-slate-500' },
    { id: 'away', value: away, label: awayLabel, cls: 'bg-good' },
  ]
  return (
    <div className="my-3">
      <div className="flex h-7 rounded-md overflow-hidden border border-ink-700">
        {segs.map(s => (
          <div
            key={s.id}
            className={`${s.cls} flex items-center justify-center text-[11px] font-medium text-white/95`}
            style={{ width: `${(s.value || 0) * 100}%` }}
            title={`${s.label} ${(s.value * 100).toFixed(1)}%`}
          >
            {s.value > 0.08 && `${(s.value * 100).toFixed(0)}%`}
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[10px] text-slate-500 mt-1">
        <span>{homeLabel}</span>
        <span>Draw</span>
        <span>{awayLabel}</span>
      </div>
    </div>
  )
}
