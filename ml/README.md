# Machine learning

Owner: Hardware/ML.

This directory will hold training and the personalized gesture classifier that consumes the camera feature stream and publishes the gesture classification message in `docs/contracts.md`.

Planned responsibilities:

- Train and evaluate classifiers outside this repository's tracked files. Datasets, recordings, checkpoints, and weights stay local and are gitignored.
- Publish `label`, `confidence`, and `model_version` together with the source window: the oldest and newest feature timestamps that were inputs, and the count of those frames. A one-frame model sets the start and end to the same timestamp and the count to 1.
- Treat a frame that is not `tracking` as `unknown`. The label `unknown` is not a motion request.
- Add a reviewed label list for a real `model_version` before any consumer depends on a label other than `neutral` or `unknown`.

Schema 1 does not define a control mapping from labels to motor commands. That mapping belongs to shared control and needs its own reviewed change.

No training code, weights, or reported results are included in this revision.
