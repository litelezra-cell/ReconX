# Video analysis — shared protocol for all projects

Version: **2026-10-14.1** · Decision owner: maintainer · Status: binding procedure for the agent, not a statement that any particular engine already implements it.

Source: the video-to-skills protocol of an internal host pipeline, plus the corrections learned from an animation-plugin research run on 2026-10-08. The provenance stamp and hash are in `SOURCE.json`; machine-local paths live in `~/.claude/recon-local/` (not published). The host pipeline remains the source of truth for itself; this protocol defines portable analysis requirements, with no cross-project code import and no assumption that an ingest/v2 schema exists everywhere.

## Trigger, scope and completion criterion

Every request to analyse or understand a video, demo, reel, tutorial, motion, interface or plugin triggers this procedure. If only a focused question was asked, the scope may be limited and declared. "Miss nothing" means reviewing the full duration of the video and expanding to every event found, with no silent cap on moments; it is not a promise that every event was observed in every frame.

Before the run: write down what is to be understood, what the evidence will be, the time range, the content type and the tools that are actually available. Read the project's rules and the safety procedure. Do not choose or replace a model or quantisation because of a familiar name alone; use the registry or a measured, approved selection. In the original pipeline W1 is currently Qwen3-ASR+ForcedAligner and W2 is Whisper; this procedure does not pin a model name permanently.

**Done:** an identified evidence file, recorded coverage for every channel, every event found handled or marked as a hole, witness gaps preserved, conclusions anchored, and a record of what was run and skipped. A blocked source is not "analysis completed". A useful partial report is allowed, with the reason and the scope of what is missing.

## Stage 0 — intake and preliminary analysis, before the deep analysis (2026-10-13.1)

The same procedure serves very different goals: a tutorial for a sound agent is not a reel showing software for reverse engineering. So **ask the user** instead of guessing. And the questions are not only for classification: they are meant to make the architect think about what else can be extracted from the video and to propose it.

**The flow:** 0a opening questions ⇒ 0b preliminary analysis and research ⇒ 0c findings and follow-up questions ⇒ **deep analysis** (the rest of this procedure). Every stage is directly reachable: whoever explicitly asks only for the preliminary analysis, or for the deep analysis right away, gets that, and the skip is recorded.

### 0a — opening questions (before any capture)

- **One message, up to 4 closed questions** (multiple choice, including multi-select), and for each one **an option for an open answer**. In an interface that supports it, use a question tool with clickable options.
- **The questions:**
  1. **What is it, and why?** (the type determines the use)
     - A tutorial to learn from ⇒ skills or instructions for an agent (for example sound tutorials for a sound agent).
     - A tutorial or explanation of software/a plugin ⇒ research and reverse engineering.
     - An edit ⇒ **a ready template to which the user only adds video.** Sub-types: an editor screen with the timeline at the bottom and the edit above it · the finished edit alone · a slot template such as TikTok/CapCut. The sub-type can be settled in the preliminary analysis instead of in the question (`video-templates`, triage A/B/C).
     - Other (open).
  2. **What else can be extracted from it?** (multi-select) — **the architect writes the options for this video**, from the request and the project context: angles the user may not have thought of (comparison with a tool we already have, exact values, features nobody talks about, timing and motion, community research). Not a fixed checklist. **The default is to miss nothing;** the selection steers priorities and additions and does not remove channels.
  3. **Preliminary analysis first, or straight to deep?** Default: preliminary first.
  4. **Anything specific to watch for?** (open) A range, a part to skip (for example installation), what is already known.
- **Do not ask what was already said explicitly** in the request or in earlier decisions. Fill it in from the content and mark it "from the request". If everything is known ⇒ skip the questions and record the assumptions.
- **The answers determine which tracks are mandatory in the deep analysis:** reverse engineering ⇒ **control map** (below), including "what was not spoken about" · tutorial ⇒ a step log with values, two ASR engines, and every step tied to the screen · edit/template ⇒ structure per the shared `video-templates` contract · "just understand" ⇒ a declared, narrowed scope.

### 0b — preliminary analysis and research (what is accessible and cheap)

- **From the video:** metadata, the page description, source captions if they exist, a preliminary transcript (one ASR is allowed here), and a sparse visual navigation sheet for orientation. This is **not** the visual coverage and not the two audio witnesses; those belong to the deep analysis.
- **Research on the topic, before the deep analysis:** the official site (documentation, changelog, price, host application, version), and communities and forums related to the topic. Official source, marketing and community experience are kept separate. Every "tool X does Y" claim is checked against the primary source before it enters a document.
- **The output:** short findings: what it is, what is already known, what is surprising, and what to look for in the video and in which ranges. Recorded in the `preliminary` field of the RUN-RECORD.
- A preliminary analysis is **not** the analysis: it has no `complete_scope`. If you stop here, the gate is `partial_scope` with a declared scope.

### 0c — findings and follow-up questions

- Bring the findings to the user and ask, in one message (up to 4), questions **born from the findings**: "I found there is also X — analyse it?", which part of the video, confirmation of the template sub-type, and whether to continue to the deep analysis.
- The deep analysis is planned from the answers: you know what to look for, in which ranges, and what needs to be understood.

### In all stages

- **The intake lowers no minimum** in the deep analysis: coverage ≥1fps, two ASR engines when speech is relevant, and channel separation remain in every case.
- The answers of both rounds are recorded **verbatim** (`intake.answers_verbatim`, `intake.round2_answers_verbatim`), and every conclusion states which goal it serves.

## Content tracks

| Content / task | The binding emphasis |
|---|---|
| UI demo / tool reconstruction | full visual coverage of the scope; **control map** (separate section, mandatory); action→state change→host result; OCR and source text; dense action regions |
| Tutorial with speech | two independent audio witnesses for all the audio in scope, a timed comparison, and every step tied to the screen |
| Edit ⇒ template | structure per the shared `video-templates` contract (slots, transitions, text, effects, speed, beats); on an editor screen, the timeline on the screen is the yardstick for the result above it |
| Silent reel / motion demo | events are also created from screen changes and motion; absence of speech is not absence of actions |
| Music / singing / speech inside playback | distinguish the speaker's instructions from the example audio, with focused handling of masking/singing; do not delete a sentence on a model label alone |
| Focused question | explicit range and question; no completeness conclusion about the part that was not reviewed |

## Source and evidence record

- Keep the URL/identifier/canonical/redirects as far as accessible, the creator, observed_at, the server date if present, duration, resolution, timebase/fps and SHA256. An identity/version gap is checked before a conclusion is attributed; do not infer age from the shape of a shortcode.
- Keep the source and the derivatives separately; state codec/re-encode/crop/resize. A derivative is not a lossless source. Rights to use in a product do not follow from the mere download; assets are recorded in PROVENANCE according to the project's rules.
- For every frame/segment: actual PTS, the source range, path and hash. With VFR/fractional frames do not assume index/fps; record the actual timestamp and the gap from the requested one.
- Count slots, unique frames and new frames separately. Cycling a frame into a quintuple is not another independent observation.
- Keep source captions if available; state missing/blocked/nonexistent instead of an empty field posing as success.

## Visual coverage — independent of the transcript

1. Extract a sequence of **≥1fps at source resolution over the whole scope**; measure start/end and a maximum gap of 1s, including the tail. No sparse sampling because of a token limit or only audio cues.
2. Review the whole sequence in rounds; navigation sheets are for orientation only. Open critical frames at source to read controls/values/handles. Record `captured` separately from `human_reviewed` and from `model_reviewed`.
3. Build an event log from transitions, selection, cursor/handles, layout changes and results as well. A luma/frame-diff signal is a candidate, not a ground-truth detector; a camera pan/cut is not an action in the plugin.
4. At every critical/ambiguous moment: a quintuple **t−1, t−0.5, t, t+0.5, t+1s**, keeping all five and handling the edges. Do not replace a quintuple with a before/after pair.
5. For every time-varying action: a window with margins, default ±3s, and samples at **≥2fps including the edges**. If the action is fast/a drag/an animation — increase the density and produce a clip at the source rate; 8fps is an example from the research, not a magic number.
6. No cap on the number of moments that deletes evidence. Work in batches and keep resume; if budget/capability is insufficient, record partial coverage and the ranges that were not checked.

1fps is not proof of coverage of every sub-second event. Quintuples are not a substitute for a sequence or a clip in time. Full capture is not full viewing, and sampled viewing is not viewing every source frame.

## Transcription and witness cross-checking

- If speech is relevant: **two independent ASR engines on the same in-scope audio**, with model/runtime identity, parameters, provenance, raw output and exit status. The absence of captions is not permission to skip W2. A focused W2 run is a diagnosis of W2, not a W3.
- No speech/no audio channel: N/A is allowed with evidence (a stream check/audio review/VAD as needed). Do not conclude "silence" from empty ASR output alone. The visual analysis continues.
- Source captions and burned-in captions are additional text/screen witnesses; they are not two independent ASR engines. If their origin is unknown, their independence is not proven.
- Perform a timed comparison: terms, product names, instructions, repetitions, omissions, sentences compressed into an implausible time, and segments that ended midway. Keep raw and normalized separately.
- Choose windows by the gap, return to the original audio and the screen; one focused rerun after a fix/window definition, and afterwards an explicit hole if there is no resolution. Another round requires a new reason and a bound, not an unbounded loop.
- Singing/masking/degradation/disagreement: listening and cross-checking first, and VAD/judge/stems as needed and with the approved capabilities. These are not mandatory for every video. In a host project's production pipeline keep that pipeline's defaults; an override is called an adaptation, not "the production pipeline passed".
- Example speech during playback is not automatically an ASR hallucination or an operating instruction. Label the source and the provider.
- Forced alignment helps locate evidence; check zero durations/backward timing. It is not a promise of frame accuracy or a latency measurement. Text similarity is not accuracy/recall, and high agreement is not suspicious in itself.

## Iron rule — motion and creation are measured on every frame at the source rate (2026-10-13.2)

Added after runs in which this step was skipped several times: without per-frame measurement you cannot know from where to where something was moved, or what was created.

- **What:** every claim about motion, change over time or creation (from where to where it moved, what was created and in what order, how fast, whether there is overshoot, smear or a size change) rests on **every frame at the source rate** in the window. Sampled images — even 4 per second — are navigation only, not evidence of motion.
- **When mandatory:** every window in which the transcript, the captions or the screen indicate motion (motion words such as animate/zoom/slide/bounce/blur/stagger/drag, an animation demo, a drag, a transition, creating or deleting an object). A ±3 second window, with the edges.
- **What to measure for every element that moves or is created:** birth (time, position, size) · end or rest (time, position, size) · displacement and duration · peak speed · overshoot · scale change · smear frames. Editing cuts of the video itself (zoom, transition) are separated from motion within the content.
- **Verification:** every event that the measurement flags is opened in its source frames. A measurement without opening is a candidate, not a finding.
- **Gate:** the channel `motion_source_rate` is required wherever there is evidence of motion. Without it there is no `complete_scope`, and no conclusion about motion gets the status `observed`. The gate checks the RUN-RECORD mechanically (`~/.claude/skills/recon/scripts/gate_check.py`).
- **Tool:** `~/.claude/skills/recon/scripts/motion_profile.py` (per-frame element tracking), or an equivalent tool documented in the RUN-RECORD.

## OCR, VLM and personal verification

- When interface text is needed: OCR on source frames, with bbox/confidence; for decisions on names/values open the source and cross-check audio/documentation. Record on which frames OCR ran. A count of items includes repetitions and errors and is not a count of features.
- A VLM is an additional visual witness, not a substitute for the source or for the coverage review. For every call keep model/precision, frames/timestamps/prompt and output limit/truncation.
- For a temporal claim send a chronological sequence/video according to what the model actually receives. A list of 6/8/7 images at 1fps is not a dense analysis or proof that the model watched the whole segment.
- A full review of the scope can combine a human and tools; **a VLM on every frame is not required**. State exactly its selective contribution. A truncated answer or an unsupported detail returns to the source and is not an exhaustive conclusion.
- **Length calibration before a long VLM run (2026-10-14.1):** before running a vision model over the whole video, measure on a segment with ground truth (which the architect writes from the frames and the OCR) windows of 2/4/8/16/24 seconds, and choose the longest one that does not invent. Measured 2026-10-14 (Qwen3.6-35B-A3B-8bit, 2fps, 720p, on two segments): **8 seconds**; at 16–24 the model invents values, reverses directions and describes times it did not receive. Recorded in `vlm_calibration` (the chosen window, the ground-truth segments, the files); the gate refuses a VLM channel without it.
- **Numbers only from OCR,** even in the calibrated window: the model invented 27 values in a full run with 8-second windows. The VLM is a witness to "what was done and what happened", not to values.
- **OCR is blind to right-to-left scripts:** RapidOCR PP-OCRv6 does not recognise Hebrew or Arabic at all (zero detections across twelve videos). Check direction, letter joining and word order by eye, frame by frame, with a named screenshot.
- In a UI demo separate the panel canvas, the native Viewer and the render result. Selection/colour/zoom/rest state/playhead time are not the same state. A visible button is not an effect that was triggered; a model that claims a transport/framework/latency without evidence is not adopted.

## Control map — mandatory on the "UI demo / tool reconstruction" track (2026-10-12.1)

The inventory is a deliverable with a format and a completion criterion, not a sentence in a report. (Lesson from an animation-plugin project: the run of 2026-10-08 recorded "5 icons", "sidebar with 5 icons, do not attribute functions", and the features that were not spoken about did not reach the owners in a usable form.)

1. **Region crops at source:** for every surface (title, bars, canvas, blocks, host window) crop from a full-resolution source frame, at least at two different moments (before and after an action). Keep path, PTS and hash.
2. **A row for every control:** id · region · visual description (shape, label, icon — enough to draw it) · observed states (selected / checked / hover / disabled, with PTS) · **status:** `demonstrated` (an action and a result were seen) / `visible` (exists, not triggered) / `inferred` (interpretation from an icon or context) / `unknown` · **a marked interpretation** (mandatory also for `visible`, and for `unknown` when there is a hypothesis) + what it rests on · **what would confirm or refute it**.
3. **State comparison:** compare every region crop across all the sampled frames (pixel-diff or a documented manual comparison). Every state change of a control is an event in the log, even if it was not spoken about.
4. **Symmetric controls:** blocks that look duplicated (In/Out, left/right) are compared control against control, and every difference is recorded explicitly.
5. **"What was not spoken about":** a separate list of every control that was not mentioned by any audio witness or caption. A mandatory deliverable, not a note.
6. **Mapping to the build (when the goal is reconstruction or inspiration):** for every control: built / not built until evidence / phase. `unknown` is not built. `inferred` is built only as our own product decision, with a label.

7. **Windows outside the panel (2026-10-14.1):** every window, dialog or external application opened from the tool gets its own row: which button opens it (and from which tab) · every control inside it · how it closes and what returns to the panel · external process or internal dialog · when it was added and when it was removed between versions. In the RUN-RECORD `windows` (a list; an explicit `[]` if there are none) — the gate refuses without it.
8. **Named screenshots (2026-10-14.1):** all frames are kept locally; in addition a `screenshots/` folder with the moments in which something was found, named `<video>_<mmss>_<what-is-seen>.png`, and `screenshots/INDEX.md` (name · time · why it matters) — the context for any agent/architect who comes later. **Walk through the transcript** and mark every moment when a screen opens or a function is demonstrated that the architect will need; the screenshot is chosen from all the source frames within ±1 second by sharpness and stability (`scripts/sharp_shot.py`); a blurred screenshot ⇒ also the sharp frame immediately before and after. **A name is a claim:** open every image and check that the name says what is in it. In the RUN-RECORD `screenshots.index`; the gate refuses a UI track without it.

The rule "a visible button is not a demonstrated effect" remains. **Interpretation is permitted and required, as long as it is marked,** and is never promoted to `demonstrated` without evidence of an action and a result.
**Gate:** a UI track does not come out `complete_scope` without a control map that covers every region that was cropped, including the "what was not spoken about" list. Examples on the machine: `examples.control_maps` in the local settings (`recon-local/config.json`).

## Coverage log and delivery conditions

Use `RUN-RECORD.template.json` as a portable reporting format, not as a fake conversion to ingest/v2. Fill in `requirements` and availability before the run, and `result` and the evidence after the run. Every record states the protocol version and scope.

For every channel: status (`planned/running/done/partial/blocked/not_applicable`), tool/model, ranges or actual timestamps, paths, exit codes, skipped reason and truncation indication. In particular separate source/capture/ASR W1/W2/captions/OCR/VLM/human review/action clips/audit.

For every event: time, selection/action/result, evidence paths, status `observed/spoken/corroborated/inferred/unknown`, and the remaining gap. A finding is not evidence. External research separates official source, marketing and community experience; versions/packages are not merged because of an identical product name.

**Handoff gate:**

| State | Meaning and reporting |
|---|---|
| `complete_scope` | all requirements marked as needed for the scope were filled in and checked; selective channels are identified; holes/uncertainties are still reported. No guarantee of zero misses |
| `partial_scope` | a required channel, review range or required event remained missing; a partial report is delivered with the list of gaps, without the label "the full protocol passed" |
| `blocked` | no access to the source or not enough evidence to answer the goal; keep what was collected and state the blocker |

Mark N/A only when it is not required for the content/goal, with a justification — not because the engine is missing. There is a separate field for `host_pipeline_audit` (`passed/not_run/not_applicable/failed`; in records from before 2026-10-13.3 the field has its previous name — the mapping is in the local lineage); a portable research `complete_scope` is not an approval of the original host pipeline.

Personally and mechanically verify the expected against the existing: hashes, duration/PTS/max gap, non-empty words/outputs, independent witness identities, every moment found, missing/truncated/exit codes. An audit matches the schema and is not inferred from a status table. A file audit verifies integrity/scope; an audit on selected moments does not check recall of event detection. An `info_hole` counted as covered is still a content hole; it gets no automatic upgrade to a substantive pass.

## Safety and use of existing tools

- Sensitive media locally only; no sending of a private screen to the cloud. A public source does not cancel the machine's privacy rules.
- Before any CLI check where it actually writes, not only which env vars it receives. State/vault/tmp are isolated, and other applications (such as the video editor) are blocked at the connection point in tests. Video analysis is not permission to modify an open project.
- Heavy model: preflight, AC/swap/memory gates, one global broker, a reserve for the lifetime of the process, loads in sequence and verified release. Do not disable a gate and do not unload the user's model to improve output.
- No automatic installs/weight downloads/dependencies to complete a protocol. If a capability is missing, use an existing safe route or report partial. No dummy tools.
- A blocked/login source requires an existing authorised route; no cookie extraction or Safari/session workaround on your own initiative.
- Reusable evidence and resume before rerun. No side API/CLI that bypasses the project's contracts and sandbox. A manual analysis/adaptation that does not go through the engine is called that explicitly.

## Lessons the procedure prevents

Lessons from the animation plugin: silent/UI cues do not depend on speech; a spatial canvas is not an exact-frame copy of the Viewer; two ASR versions may disagree about Flicker; Whisper can repeat an instruction; a VLM can invent an effect name or arrive with a truncated answer. Backup/coverage/evidence are not replaced by the promise "per the original pipeline". A live connection is proven in a host experiment; recording a reel is not a ms measurement.

## Routing the brief to each executor

Attach this protocol and the project adapter to the brief. The global rules point here; the project declares what is implemented today and what is only a plan. A file that is not accessible in the executor's environment ⇒ attach the content in the brief/a versioned copy, and not an instruction to read a path that it has no access to.

Future change: update the version, SOURCE/CHANGELOG and the adapters. Do not copy the whole procedure into every project and let the copies drift. No retroactive change to old run records; mark them legacy/adapted.

## Witness-verification lessons — not only the file (2026-10-10)

- **ASR timestamps:** check what the binary's flags actually mean. `whisper-cli -nt` disables timing; a JSON that was produced is not a valid timing witness merely because it exists. Against the real WAV duration (`frames/sample_rate`) check finite, bounds, order and duration of segments and of words separately. Valid words do not validate an invalid segment. Keep the raw output and the defect; do not clamp timestamps into range to whitewash them. A zero-duration alignment is recorded separately and is not proof of accuracy of a word or of the silence around it.
- **Fast slider:** two 1fps samples can be intermediate values. Expand a sequence in the action region at a density measured from the source; report requested versus actual PTS, spacing and the frames that were actually checked. Do not call two values "the slider range" or endpoints before you have seen the extremes. Downscaled navigation images are not an opening of every source frame; critical text/value is checked at source.
- **Mask versus promo:** a Masks title, a node named Masking or a wipe line are three different pieces of evidence. Tie them to the header, the product version and the chronology; OFX, DCTL and PowerGrade are not the same entity. A mask editor requires evidence of a tool/handles/gesture/changing region. A change in appearance along an edited wipe is not proof of live editing or tracing.
- **A VLM is an interpretive witness:** keep also rejected interpretations and the counterevidence. Do not adopt "mask", "real time", a transport or an algorithm because the model phrased it confidently. An audit of all selected moments and exit 0 are not recall, accuracy or live-host proof.
- **Long run:** record expected/actual batches and the prefix that was kept, without a silent cap and without rerunning batches that were completed. Resume must go through the API and the memory gates with source/frame hashes and verified cleanup. If resume is not implemented, that is a capability gap and `partial_scope`; writing a spec is not called implementation.
- **Quality of cheap research:** exit 0, cue ids and a repeating general summary are not review evidence. A specific visual description, input/paths identity and a list of uncertainties are required. An image-tool trace that is not shown in the CLI JSONL proves neither use nor non-use; public images can be passed explicitly with an order/hash manifest. Thumbnail references are recorded as such, and frames_used is not a claim that the source was opened in full. Partial information about an action stays an info_hole even if an artifact audit covers it.
- **Research routing:** a global rule may require a named research model for research/collection, with requested/effective model and limits on knowledge of billing/runtime. That does not replace local ASR/OCR/VLM. Availability of a model in a delegation tool and availability of the CLI through which the run was already launched are different checks; no silent fallback and no invented effective model.

This change does not retroactively alter the protocol of runs that started with 2026-10-08.1, the host pipeline's ingest, or the engine acceptance rulings. The source and the new reports are in SOURCE.json.
