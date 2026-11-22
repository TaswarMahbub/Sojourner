import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import FacePreviewPip from './preview/FacePreviewPip.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
    <FacePreviewPip corner="bottom-right" />
  </StrictMode>,
)
