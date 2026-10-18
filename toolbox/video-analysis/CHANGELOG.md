# Changelog

## 2026-10-08.1

- Shared core based on an internal video-to-skills pipeline and on animation-plugin research.
- Two independent ASR engines when speech is relevant, even without captions; silent-first for UI.
- Per-channel coverage separation, genuine VLM density, raw/normalized and information holes.
- Source versus derivatives, the source of state in the UI, CLI safety, and operational state versus content correctness.
- The adapter for the witness engine declares that the automatic witnesses are not yet implemented.

## 2026-10-10.1

- Witness-verification lessons from a colour-plugin analysis: WAV/segments/words validation, the meaning of `-nt`, intermediate values and dense expansion.
- Separation of masks, PowerGrade and wipes; recording of rejected VLM interpretations.
- Verified expected batches/resume; a missing implementation stays partial.
- Explicit named-model routing for research and requested/effective limits; local modality models remain chosen by capability.
- Quality of research summaries, explicit image-input manifest and mapping of thumbnail references; sampled actions remain content holes.
- No retroactive change to runs, to the host pipeline, or full acceptance proof for the witness engine.

## 2026-10-12.1

- **Stage 0, intake from the user:** up to 4 closed questions plus an open answer (what it is, what not to miss, what it will be used for, what to watch for), before capture. Do not ask what was already said. The answers determine which tracks are mandatory, and do not lower the minimum. Recorded in `intake`. (Rationale: a sound tutorial is not a reel for reverse engineering.)
- **Control map mandatory on UI:** region crops at source, a row per control with status and a marked interpretation, state comparison, symmetric controls, a "what was not spoken about" list, and mapping to the build. A gate for `complete_scope`. Field `control_inventory`.
- Source: the control inventory of an animation-plugin project architect measured against the run of 2026-10-08, and that architect's control-inventory proposal (paths in `recon-local/protocol-provenance.json`).
- No retroactive change to runs that started in earlier versions, to the host pipeline or to the witness engine. The engines do not yet implement the two new fields (partial).

## 2026-10-13.1

- **Stage 0 in two rounds:** 0a opening questions ⇒ 0b preliminary analysis and research (metadata, captions, one ASR, a navigation sheet; official site, communities and forums) ⇒ 0c findings and follow-up questions ⇒ deep analysis. Every stage is directly reachable, and a skip is recorded. A preliminary analysis alone = `partial_scope`.
- **The questions are meant to widen, not only to classify:** "what is it" unifies type and use (tutorial ⇒ skills · software explanation ⇒ reverse engineering · edit ⇒ ready template, with a sub-type of editor screen / finished edit / slot template). "What not to miss" was replaced by "what else can be extracted from it", which the architect words for each video; the default: miss nothing.
- A row "edit ⇒ template" in the content-tracks table.
- RUN-RECORD: a `preliminary` field, and round-2 fields in `intake`.
- Source: the user's answers in a trial run of the `video-analysis` skill (2026-10-13). No retroactive change to earlier runs or to the engines.

## 2026-10-13.2

- **Iron rule — motion and creation are measured on every frame at the source rate.** Sampled images are navigation only. Motion windows are derived from the transcript, the captions and the screen; for each element: birth, end, displacement, duration, speed, overshoot, scale and smear; a flagged event is opened at the source. A `motion_source_rate` channel is mandatory when there is evidence of motion, and a mechanical gate (`gate_check.py`) blocks `complete_scope` without it.
- RUN-RECORD: `requirements.motion_source_rate`, `channels.motion_source_rate`, `motion_cues`, `motion_tracks`.
- Source: added after this step was skipped again in two motion-heavy analyses.

## 2026-10-13.3

- **Editorial, no change to the rules:** the protocol, the skill and the template carry no machine paths and no internal project names. The paths, examples and lineage sources moved to local settings `~/.claude/recon-local/` (`config.json`, `protocol-provenance.json`), which are not published with the skill.
- RUN-RECORD: the pipeline-audit field is now called `host_pipeline_audit`, and `record_kind` ⇒ `portable_analysis_record_not_host_ingest`. Existing records keep the previous names (the mapping is in `recon-local/protocol-provenance.json`); a reader that sees the previous name treats it as the same field.
- Source: maintainer decision, preparation for a public release of Recon.

## 2026-10-14.1

- **Windows outside the panel** in the control map (a row per window: what opens it, contents, closing, external/internal, lifecycle between versions). Field `windows`.
- **Named screenshots + INDEX**, derived from a walk through the transcript; a sharp, stable frame within ±1 second, and if blurred ⇒ before and after (`sharp_shot.py`); a check of name against image. Field `screenshots.index`.
- **VLM length calibration** on ground truth before a long run (measured: 8 seconds); numbers only from OCR. Field `vlm_calibration`.
- **OCR blind to Hebrew/Arabic** — check right-to-left text by eye.
- The gate (`gate_check.py`) enforces the three fields (design decision: every mandatory item = a check in code).
- Source: lessons from an analysis of a text-animation plugin demo, where these steps were found missing.
