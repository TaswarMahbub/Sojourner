import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import LiveSimulator3D from './LiveSimulator3D.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <LiveSimulator3D />
  </StrictMode>,
)
