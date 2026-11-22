import { taskStateText } from './taskState.js'
import './TaskStateBadge.css'

// The task_state badge the confidence ring goes around (Mridhul, 22 Nov: "the element that reads
// manual, or halted: watchdog"). DOM only (I7).

export default function TaskStateBadge({ view }) {
  const halted = Boolean(view.haltReason)
  return (
    <span className={`task-badge${halted ? ' task-badge--halted' : ''}`}>
      <span className="visually-hidden">Task state: </span>
      {taskStateText(view)}
    </span>
  )
}
