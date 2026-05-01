import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Only proxy API endpoints, not the /admin page route (which Vite must serve
// itself so React can render the Admin component).
const proxy = Object.fromEntries(
  ['/predictions','/ev-bets','/bets','/stats','/fixtures','/run-model',
   '/digest-preview','/send-digest','/backtest','/backtest-result','/sync-data',
   '/admin/sync','/admin/scheduler','/admin/health',
   '/quota','/scheduler'
  ].map(p => [p, { target: 'http://localhost:8002', timeout: 0, proxyTimeout: 0 }])
)

export default defineConfig({
  plugins: [react()],
  server: { port: 3002, proxy },
})
