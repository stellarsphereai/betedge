import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import Admin from './Admin.jsx'
import './index.css'

// Path-based router: only two routes exist. /admin (or #admin) shows the
// password-gated emergency console; everything else shows the main dashboard.
const onAdmin = window.location.pathname.startsWith('/admin') ||
                window.location.hash === '#admin'
const Root = onAdmin ? Admin : App

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
)
