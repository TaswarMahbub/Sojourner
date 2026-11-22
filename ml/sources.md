# Candidate public sources

This note records two collections a later phase might ask to use. It is not an adapter, a download, or a permission. No files from either collection are in this repository.

An `AuthorizationRecord` can later store a person's decision id. That record is not proof the decision was correct. A research citation, a research-use grant, or a public README does not by itself allow redistribution or commercial deployment.

The only format `ml.dataset` reads is `sojourner-feature-jsonl` at `schema-4`: one Schema 4 camera feature message per line, the object `perception` already writes. Anything else fails with `unsupported_source_format` and is not parsed.

## FaceGest

Public page: https://github.com/sonainjameel/FaceGest

What the public README describes:

- An HCI facial-gesture collection: expressions, head movement, lighting, and viewpoint.
- Three forms: deep features, depth maps, and labeled RGB videos.
- Deep features are loaded with the package the README calls `face-gest-loader` (`import face_gest_loader`). The same page also says to pip-install `face-gest-library`. The calls it shows are `Deep-Features-Mediapipe`, `Deep-Features-SqueezeNet`, and `Deep-Features-Inception`, each returning a table.
- Depth maps and RGB videos are not on that page. The README points at a request form, `https://docs.google.com/forms/d/e/1FAIpQLSeDBls0krHS7FgcEm26bjX_ySwQJ-PCYToYpO9tvMdz5ZBsRg/viewform`, and says download instructions arrive by email after approval.
- A sketched layout names `videos/`, `images/`, `annotations/` (landmarks and expression labels), `depth/`, and `metadata/` (subject details and recording conditions).
- The README says expression labels carry timestamps.
- The citation block is `Yaseen2025FaceGest`, CVPRW 2025.
- The benchmark table on that page is filled with `XX.XX`. Those are placeholders, not results.
- The README text retrieved for this note did not include a license.

Schema 4 compatibility: no. `Deep-Features-Mediapipe` is that loader's table. It is not a Schema 4 feature message, and it is not Sojourner's 162-number window vector (51 blendshapes other than `_neutral`, plus yaw, pitch, and roll, each as mean, population standard deviation, and newest minus oldest). The two representations are not interchangeable.

Ingestion would require all of the following, none of which this phase does:

- A person records an authorization decision after reading the current terms.
- Depth or RGB access, if those forms are the ones wanted, comes through the author's request. This pipeline does not submit that form and does not download the videos.
- The actual files are inspected. The folder sketch is not a verified schema.
- Participant and session ids are replaced with opaque tokens. Names and contact details from `metadata/` are not copied into this repository.
- Expression timestamps are converted into explicit onset and offset intervals. A clip-level label stays review-required.
- Features are produced by Sojourner's own Schema 4 path. Renaming the MediaPipe table is not that path.
- A split plan keeps population participants out of eval, and keeps a held-out person's calibration and eval in different sessions.

## York Facial Gestures

Public page: https://jtl.lassonde.yorku.ca/software/datasets/

This environment could not retrieve that page: the TLS certificate check failed. The file format, label names, participant fields, session fields, and license were therefore not verified. No York adapter is included, and nothing was downloaded.

Before any York file is ingested, a person has to open the page, read the access terms, and record an authorization decision. Research use, if it is offered, still has to be checked for redistribution and for commercial deployment. The files then have to be shown to match a documented layout. Until that happens, `ml.dataset` will refuse a York format string the same way it refuses FaceGest.

What ingestion would require is the same list as FaceGest: a recorded human decision, opaque participant and session tokens, explicit annotation intervals, Schema 4 features from Sojourner's own extractor, and a leakage-checked split plan.
