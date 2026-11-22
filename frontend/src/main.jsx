import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import BackgroundOrb from './components/BackgroundOrb.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
    <BackgroundOrb />
  </StrictMode>,
)
