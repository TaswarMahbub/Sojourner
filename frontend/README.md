# Frontend

Owner: Davina.

This directory will hold the React application: the operator dashboard, the 2D rover simulator, and the visualizations of features, gestures, and commands.

Planned responsibilities:

- Show tracking state, named features, head pose, gesture label, confidence, and model version.
- Drive the 2D simulator from the motor-command contract, including sequence, task state, and the stop flag.
- Display a rejected or expired command as not applied. The simulator uses the same acceptance rules as the firmware.
- Keep on-screen wording free of clinical, diagnostic, and therapeutic claims.

The simulator is a view of the command stream. It does not publish a second command format and it does not bypass a stop.

No React application is included in this revision.
