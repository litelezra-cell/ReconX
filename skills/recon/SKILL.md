---
name: recon
description: "Run the shared video-analysis protocol (~/.claude/toolbox/video-analysis) on a video URL or file, in any project. Opens with the protocol's intake questions in one AskUserQuestion message, builds the research target folder, runs a cheap preliminary pass (metadata, captions, one transcript, official-site and community research), brings findings back with follow-up questions, then runs the deep analysis the content track requires and ends at the handoff gate. Use whenever the user asks to analyse, understand, break down, reverse-engineer, learn from or turn into a template a video, reel, short, YouTube/Instagram/TikTok link, tutorial, screen recording, UI or plugin demo, or an edit, even without the word protocol. Skip it for cutting, rendering or exporting a video, and for a plain transcript request with no analysis. The skill also triggers on equivalent requests in other languages."
metadata:
  version: "4"
  protocol: "2026-10-14.1"
---

# Recon — runner for the shared video-analysis protocol

The protocol at `~/.claude/toolbox/video-analysis/README.md` is the source of truth. This skill
runs it: reads it every time, asks the intake questions, builds the target folder, checks what
tools really exist, runs the preliminary pass, comes back with findings and questions, and then
walks the deep analysis to the handoff gate.

**Do not copy protocol text into this skill or into project files.** The protocol says copies
drift ("Routing the brief to each executor"). Steps below point at protocol sections by name.

**Language.** Write every message to the user in the user's own language from the first word:
status lines, questions, findings, the plan and the final report. Keep code, paths, commands and
technical terms as they are. After long stretches of English work it is easy to slip back into
English; check the first word of each message before sending.

**The flow** (protocol "Stage 0"): opening questions ⇒ preliminary analysis and research ⇒
findings and follow-up questions ⇒ deep analysis ⇒ handoff gate. Every stage is reachable
directly: if the user asks for only the preliminary pass, or for the deep analysis right away,
do that and record the skip.

## Route the target first

| What the user gave | First move | Mandatory output (details in Step 7 and the protocol) |
|---|---|---|
| Software / plugin demo | control map track; ask whether the product itself is on disk | control map + "what was not spoken about" |
| Tutorial to learn from | two independent ASR, step log with values | steps tied to the screen |
| An edit ⇒ ready template | `~/.claude/toolbox/video-templates/` triage A/B/C | template structure |
| Anything that moves (animation, drag, transition, creation) | **iron rule: every source frame** | `motion_source_rate` + `motion_tracks` |
| The product's installer / app is available | behaviour from the video, implementation from the artifact: [references/artifact-route.md](references/artifact-route.md) | claims about "how" marked `inferred` until the artifact confirms |
| Several products in one request | one target folder per product | separate reports, cross-linked |

## Step 1 — Preflight and read

0. **Resolve the target first.** If the user says "this video" / "the reel" without a link or path,
   or several videos are in play, ask which one. Never pick a video, product or example on his
   behalf; a link in an older report or a nearby folder does not select the target.

1. Run from the project the request came from:

   ```bash
   python3 ~/.claude/skills/recon/scripts/preflight.py --project "$PWD"
   ```

   It prints JSON (read-only): protocol path, version, sha256; RUN-RECORD template version; the
   project's root, rules files, video adapters (`.project_docs/protocols/*video*`), research
   index and existing targets; capture/ASR tools on disk; memguard.

2. **Exit code 2 = the protocol is missing.** Stop and tell the user, in their language, roughly:
   "I could not find the video-analysis protocol at `~/.claude/toolbox/video-analysis/README.md`.
   Without it I will not analyse, because the analysis would rest on memory instead of the
   procedure. The file needs to be restored from the Recon repository." Never improvise the
   protocol from memory.

3. Read the protocol README **in full** with the Read tool, then `RUN-RECORD.template.json`, then
   every rules file and adapter the preflight listed. A project adapter states what that project
   already implements; where it differs from the portable protocol, the adapter governs that
   project and the difference goes in the report.

4. This skill was written for protocol `2026-10-14.1`. If `protocol.differs_from_skill` is true,
   follow the protocol (it wins) and tell the user in one line which runner step you adapted. If
   `run_record_template.matches_protocol` is false, record both versions in the RUN-RECORD.

5. If `project.is_home_or_toolbox` is true there is no project to hold the folder: ask which
   project this serves, inside the Step 2 question message (it counts toward the four).

6. **Tools freshness.** If `tools_scan.stale` is true (last scan older than a week, or none), add the
   tools-scan question to the Step 2 message — run an update scan now, after the analysis, or not this
   time — and follow [references/tools-scan.md](references/tools-scan.md). **Only with the user's yes:**
   research only, no downloads or switches; the user chooses what to measure.

## Step 2 — Opening questions (protocol 0a)

The questions do two jobs: aim the analysis, and make you think about what else this video can
give the project. The opening questions should help the agent think, understand and widen what it
can add. A fixed checklist does not do that; questions written for this video and this project do.

1. **Extract what is already answered** from the request, the project's decisions and earlier
   messages. Fill those in, mark them "from the request", and do not ask them again.

1a. **Pick the question level.** People who run this skill are not always experts, and a question
   they cannot parse wastes the round (a trial run used a question full of node and task names that
   the user could not parse). Two levels:
   - **Simple (default):** everyday words. No internal names in labels (no task ids like EM-004,
     no API calls, no node or module names). Each option's description says in one sentence what
     you would look at and why it helps, with a concrete example from this video.
   - **Expert:** technical terms allowed. Use it only when the user asks for it or clearly writes
     in that register.
   If the user answers "I don't understand the question", rephrase it more simply in the next
   round; do not treat the non-answer as a choice. Record the level in `intake.question_level`.

2. **One AskUserQuestion call** with the remaining questions (max 4), in the user's language, built from the
   protocol's 0a list as you just read it. Practical mapping to the tool:
   - `header` ≤ 12 characters. The tool adds an "Other" free-text option to every question:
     that is the protocol's open answer, so say "…or write it in Other" in the question.
   - **"What is it, and why?"** — the protocol's kinds as options (tutorial to learn from ⇒ skills ·
     software explanation ⇒ research and reverse engineering · an edit ⇒ a ready template the
     user only adds video to). The template sub-kind can wait for the preliminary pass.
   - **"What else can be extracted from it?"** — `multiSelect: true`. **You write these options** for this
     video: 3–4 concrete angles the user may not have thought of, each tied to something real in
     the project (an existing tool to compare with, a decision in the blueprint, a known gap).
     Examples of the shape at the simple level, not text to reuse: "Compare it with what we have
     already built and find what we lack", "Check whether it also works in Hebrew", "Read what users
     complain about in the forums". The default is to miss nothing; these choices set priority and additions, they
     never remove channels.
   - **"Preliminary first, or straight to deep?"** — options: preliminary first (default) · straight
     to the deep analysis · preliminary analysis only.
   - **"Anything specific to watch for?"** — open; give two options such as "No — as marked"
     and "Yes — I will write it in Other".
   - If any protocol list ever has more than four options, split it across two questions or pair
     neighbours in one label that keeps both phrases. Never drop an option silently.

3. **If everything is already known**, skip the call, record `intake.asked: false`,
   `skipped_reason` and the assumptions, and tell the user the assumptions in one line.

4. **Record answers verbatim** (labels, Other text, notes) in `intake.answers_verbatim` and in
   CONTEXT.md. Users often answer with a paragraph in Other: keep it whole, then derive
   `intake.content_type`, `intended_use`, `stages_requested` and `required_tracks_derived` from it.

5. **Turn the request into a questions ledger** — every question the user asked or implied (plus
   the protocol's mandatory ones: what moves, what is unspoken, what is the product), each with the
   evidence that would answer it. Write it in `CONTEXT.md` and as `questions_ledger` in the
   RUN-RECORD (`[{"question", "required", "evidence", "status": "open|answered|partial|unresolved"}]`);
   `gate_check.py` refuses `complete_scope` while a required question is not `answered`. This is
   what catches a skipped stage: a question like "from where to where does it move" has no answer
   without every-frame evidence.

## Step 3 — Target folder

Convention: one folder per research target, with a handoff document. Worked example of a finished
folder: `local.examples.finished_target` from the preflight (read-only).

1. Pick a **stable target id**: lowercase kebab-case from the product, creator or video id, never
   a date. Check `project.existing_targets`: the same product or URL is the same target (new
   `runs/<date>/` inside it); a different product gets its own folder.

2. Create the skeleton. It never overwrites; re-running only adds what is missing:

   ```bash
   python3 ~/.claude/skills/recon/scripts/scaffold_target.py \
     --project "<project root>" --target-id "<id>" --name "<human name>" \
     --kind "<tutorial / software demo / edit-template / other>" \
     --source "video=<exact URL or path>" [--source "site=<URL>"]
   ```

   It writes `README.md` (map), `CONTEXT.md`, `HANDOFF.md`, `TARGET.json`,
   `evidence/{site,media,frames}/`, `runs/<date>/RUN-RECORD.json` (from the template, stamped
   with the protocol version), `deliverables/`, a `.gitignore` inside the target that keeps
   video, audio, images and frames out of git, and a row in `.project_docs/research/README.md`.
   `--dry-run` shows the plan without writing.

3. Fill in the user's request verbatim (CONTEXT.md and HANDOFF.md §2), the intake table, and the
   RUN-RECORD `intake` and `scope`.

4. Prove the media rule: `git -C "<project root>" check-ignore -v "<target>/evidence/media/x.mp4"`
   must print a matching rule. Not a git repo ⇒ say so in the HANDOFF.

**Reuse before re-running.** On a target that already has `runs/`, read the existing manifests,
witness outputs and RUN-RECORD first; re-run a channel only when its input or tool changed, and say
which. Large raw outputs (OCR, motion profiles, info.json) are read through small summary scripts —
never dumped whole into the conversation.

## Step 4 — Capability check

Map each channel you will need to a tool that exists **and** runs on this input. The preflight
lists what is on disk; existence is not proof, so run each tool once on the real input before
calling a channel available.

- **Never install tools, models or packages on your own initiative.** A missing capability is a
  question to the user, or a `partial_scope` / `blocked` channel; never N/A.
- Known state, checked 2026-10-12/13 (re-check with the preflight): the `yt-dlp` CLI is not
  installed. The `yt_dlp` library exists inside existing project venvs
  (`tools.yt_dlp_in_venvs`; run `<venv>/bin/python -m yt_dlp`). Running an existing install is
  not a new install; write only into the target (`-o "<target>/evidence/media/%(id)s.%(ext)s"`),
  never browser cookies. Without a JS runtime YouTube may hide formats; record the warning.
  `ffmpeg`/`ffprobe` and a whisper.cpp build: `tools.*` in the preflight. Machine paths come from the
  local config (`local.*` in the preflight; keys in `scripts/config.example.json`); a key in
  `local.not_configured` is a channel to mark `partial_scope` or a question, never a guessed path.
- **The witnesses (ASR W1/W2, OCR, VLM) run through a guarded runner** (`local.values.witness_runner`
  in the preflight), with memguard leases, the approved models and outputs written into the target
  folder. Read [references/witness-runner.md](references/witness-runner.md) (the contract) and the
  local notes in `local.notes` (how it is met on this machine) before planning any model channel,
  and prefer that route over building a new one.
- **A refused or failed witness:** read the guard report's `worker_stderr_tail` (and the stage's
  `*-stderr.txt`) before anything else, fix the observed cause, then retry once with the changed
  input. Never retry an unchanged call; a second failure goes to the user.
- Whole-pipeline engines (a host project's ingest script, the witness runner's own API) write
  to their own state. Check where a CLI really writes before using it (protocol "Safety and use of existing tools").
- **Model loads only through a memory guard** (the `memguard` package, or an equivalent broker you already use). Before
  any ASR/OCR/VLM load check `ollama ps` and any model daemon's `/health`; one huge model
  at a time. Model and quantisation choice is the user's: use the project's approved, measured
  choice, or bring candidates; do not pick a new one.

- **Is the product itself available?** Check the reverse-engineering reference directory (`reverse_engineering_dir` in the local config) and the user's folders
  before claiming anything about implementation. Read
  [references/artifact-route.md](references/artifact-route.md) (where artifacts live, clean-room limits,
  the evaluated REA tool, which is **not installed** — installing needs the user's yes).

## Step 5 — Preliminary analysis and research (protocol 0b)

Cheap and fast; its job is to learn what to look for. It is orientation, not coverage.

- **From the video:** metadata, the page description, source captions if any, one transcript
  (one ASR is enough here; the guarded `asr_w2` stage is the light one), and a sparse
  navigation sheet. Save under the target, list paths in
  `preliminary` of the RUN-RECORD.
- **Capture the official pages before anyone reads them** (product page, docs, changelog, the
  creator's page). A fetch-and-summarise tool keeps only what a small model chose to say — on the
  first run it hid an embedded official demo video and three product images. Save the page itself:
  `<local.values.python_site_capture> ~/.claude/skills/recon/scripts/site_capture.py --out <target>/evidence/site URL …`
  (raw HTML, every visible word verbatim, full-page screenshots at 3 viewports, images/videos/
  iframes/links, network log, status, sha256 manifest; layout = the scraping protocol in `local.values.scraping_protocol`).
  **Blocked** (challenge page, login, 4xx): it is recorded as blocked — do not work around it. Open
  the page in the built-in browser pane as ordinary browsing and save `get_page_text` verbatim plus
  the media list; if the page asks for a CAPTCHA or a login, stop and ask the user. Never bypass
  bot detection. Download the product's own images into `evidence/site/<slug>/assets/` (git-ignored).
  Every embedded video found here is a new source: list it for the follow-up questions.
- **Research on the topic, in parallel:** spawn research subagents on the **research model** — one
  for the official site (docs, changelog, price, host app, version), one for communities and
  forums on the topic. The research model is the user's choice (`research_model` in the local
  config). If it is not set, use a model one tier below the main model: cheap enough to run several
  agents in parallel, but not the smallest tier — the smallest misses and invents too much for
  evidence work. Some users prefer their largest model for research; that is their call, and the
  config wins over this default. Each brief carries the target folder, the
  **captured page files** (`evidence/site/<slug>/data/text*.md`, `media*.json`) to read first,
  what is already known, a distinct scope per agent, and the instruction to keep official, marketing
  and community sources apart with URLs, and to return three lists: sources, conclusions (each with
  its URL and quoted phrase), and unresolved gaps. Record the requested model and only the effective model the tool result
  reports.
- **Verify before it enters a document:** every "tool X does Y" claim from a research-agent report is
  checked against the primary source (the page, the docs, the code). In an earlier analysis a research report
  said a repo writes Resolve's retime curve; the code called functions that do not exist.
- Write `runs/<date>/preliminary-findings.md`: what this is, what is already known, what is
  surprising, and what to look for in the video and where (time ranges).

## Step 6 — Findings and follow-up questions (protocol 0c)

1. Give the user the findings in a few lines, surprises first.
2. One AskUserQuestion call (max 4) with questions **born from the findings**: "I also found X —
   analyse it?", which part of the video, the template sub-kind if it is an edit, and whether to go on
   to the deep analysis. Record in `intake.round2_questions` and `intake.round2_answers_verbatim`.
3. If the user stops here, the gate is `partial_scope` with the stated scope. Go to Step 8.

## Step 7 — Deep analysis by track

Plan from the answers: what to look for, in which ranges. Run what the intake and the protocol's
"Content tracks" table make mandatory; the details live in the protocol sections named here:

| Track | Mandatory output (protocol section) |
|---|---|
| Software / plugin demo, reverse engineering | **control map** ("Control map"), including "what was not spoken about". Good examples: `local.examples.control_maps` from the preflight |
| Tutorial to learn from | **two independent ASR** on the same audio, timed comparison, step log with values, every step tied to the screen ("Transcription and witness cross-checking") |
| Edit ⇒ ready template | structure per `~/.claude/toolbox/video-templates/` (read its README triage A/B/C first); on an editor screen the on-screen timeline is the ground truth for the result above it |
| Silent reel / motion demo | events from screen and motion changes; no speech is not no actions ("Visual coverage") |
| Focused question | stated range and question; no completeness claim outside it |

### Screens, windows and screenshots (protocol 2026-10-14.1)

Walk the transcript and mark every moment where a screen or window opens or a function is shown that the
architect will need. For each: `<python_motion> scripts/sharp_shot.py VIDEO <target>/screenshots <video-tag>
mm:ss:what-is-seen …` (best sharp, still frame within ±1s; soft ⇒ also the sharp frames right before and
after). Open every saved image and check its name says what it shows; fix wrong names. Every window outside
the main panel gets its own row (what opens it, contents, how it closes, external app or internal dialog,
when it appeared/disappeared across versions). Fill `screenshots.index` and `windows` — the gate refuses a
UI track without them.

### VLM witness — calibrate first (protocol 2026-10-14.1)

Before a long VLM run, write ground truth for one segment (from frames + OCR) and run 2/4/8/16/24-second
windows on it; take the longest window that does not invent. Measured 14.10: 8s. Numbers come only from OCR.
The OCR does not read Hebrew or Arabic — check right-to-left text by eye. Fill `vlm_calibration`.

### Iron rule — motion and creation are analysed on every source frame

Added after runs in which this step was skipped: without per-frame measurement you cannot know from
where to where something moved or what was created. Sampled stills, even 4 fps, are navigation only: they miss smear
frames, overshoot, the order in which things are created and where an element came from.

1. **Find the motion windows** from the transcript, captions and screen (animate, zoom, slide, bounce,
   blur, stagger, drag, explode, appear, move …), ±3 s. Record them in `motion_cues`.
2. **Measure every frame** in those windows:
   `<venv with numpy+opencv>/bin/python ~/.claude/skills/recon/scripts/motion_profile.py VIDEO --window a:b … --out runs/<date>/motion-profile.json`
   (auto-finds the viewer per frame, tracks each element: birth → end, displacement, duration, peak
   speed, overshoot, scale, smear; `--hsv` for coloured text on a non-dark background; `--crop` for a
   fixed stage). The venv with both: `local.values.python_motion`.
3. **Open every flagged event in the source frames** — every-frame contact sheets of the episode,
   cropped to the stage. Measurement is a candidate; only what you opened becomes a finding.
   Watch for: edited zooms/pans of the screen recording (camera motion, not content), paused or
   scrubbed moments (not an animation), letters merging into one component.
   Pitfalls seen on the first runs (2026-10-13): auto-picked episodes can land on a paused or scrubbed
   moment — check that the stage content actually changes before reading a sheet; tiles narrower
   than ~400 px cannot show rotation, zoom or blur, so crop to the stage
   (`stage` rect from `motion-profile.json` × source/analysis width); with `ffmpeg … tile`, limit
   *input* frames with `trim=end_frame=N` and write one sheet with `-frames:v 1`.
4. Record `channels.motion_source_rate`, `motion_tracks` (each with `flagged` / `opened_in_source`) and
   write `runs/<date>/motion-review.md`: per demo, what moved, from where to where, how long.
5. **Footage speed (speed ramps, time remaps)** — `scripts/ramp_speed_profile.py VIDEO --out
   runs/<date>/ramp-speed-profile.json [--window a:b] [--crop x,y,w,h]` measures dense optical flow on
   every decoded frame and flags where the picture's apparent speed changes ≥3× within 1.5 s (cuts
   excluded). Lesson from its first real run (a 16-min speed-ramp tutorial, 2026-10-18; 357 events):
   on a screen-recorded tutorial the whole-frame profile mostly measures the narrator's gestures, the
   screencast's own zoom/pan and playback inside the viewer — three opened events, none was a footage
   ramp. Run it on full-screen example segments (intros, before/after cards) or with `--crop` on the
   viewer box (the box moves when the screencast zooms), and open every event before it counts.

Every track also keeps the other minimums: ≥1fps at source resolution over the whole scope,
quintuples and ±3s action windows where the protocol asks, channels recorded separately.
The preliminary transcript does not count as one of the two witnesses unless it was run as a
proper W1 under the protocol's witness rules.

## Step 8 — Handoff gate

1. Personally verify what the protocol lists ("Coverage log and delivery conditions"): hashes,
   duration/PTS/max gap, non-empty witness outputs, exit codes, every found moment handled or
   marked as a hole. Fill `result` in the RUN-RECORD.
2. Gate: `complete_scope` / `partial_scope` / `blocked`, with the reason. A software-demo track
   without a control map covering every cropped region is not `complete_scope`. **Run the mechanical
   gate before writing `complete_scope` anywhere:**
   `python3 ~/.claude/skills/recon/scripts/gate_check.py runs/<date>/RUN-RECORD.json --transcript runs/<date>/source-captions.txt`
   Exit 1 = not allowed (it refuses when there is motion evidence without `motion_source_rate`, tracks,
   or opened events). A prompt instruction is not a gate; this script is.
3. **Revisit the questions ledger** (Step 2.5): mark each question answered / partially answered /
   unresolved, with the evidence path. A broad analysis is not "complete" while a required question
   is open; bounded negative searches stay bounded ("not found in X", never "does not exist").
4. Cross-check identity before handing off: the request ↔ `TARGET.json` ↔ report title ↔
   source URL. A mismatch blocks the handoff.
5. Update `HANDOFF.md` (every section, full paths), the `README.md` map, `TARGET.json.status`
   and the research index row.
6. In the REPORT, every conclusion carries its status — observed / corroborated / inferred / unknown —
   and the evidence path it rests on; design recommendations are marked as the architect's judgment.
7. Report to the user in their language, result first: the gate and what it means, what was not covered
   and why, the folder path and the entry file. If another architect continues, give the paste
   panel in a ```bash fence (so chat clients render a copy button).

## What this skill does not do

- Install anything, download model weights, or choose models.
- Act as an engine: capture, ASR, OCR and VLM run with tools already on the machine, and the
  RUN-RECORD says exactly which ones ran.
- Write into a host pipeline's ingest state: the RUN-RECORD is a portable record, not a host ingest.
- Hold a copy of the protocol. When the protocol changes, the skill reads the new version; only
  these runner steps may need updating (bump `SKILL_WRITTEN_FOR` in `scripts/preflight.py`).
