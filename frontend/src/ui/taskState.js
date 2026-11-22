// What the task_state badge reads. The stop reason, when there is one, wins: it is how the watchdog
// and the latches are seen ("halted: watchdog").
export function taskStateText(view) {
  return view.haltReason ? `halted: ${view.haltReason}` : view.taskState
}
