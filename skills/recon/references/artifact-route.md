# When the product itself is available — from "what it does" to "how it does it"

Read this at Step 4 when the video shows a product (plugin, panel, app, installer) and a copy of it
exists on this machine or can be obtained legally.

A video proves **behaviour**: what the user sees, in what order, how it moves. It cannot prove
**implementation**: which API is called, how a value is computed, what is written to the host. When a
claim in the report is about implementation ("it is a Qt app talking through a bridge", "it creates
the nodes one by one"), the video makes it `inferred` at best. The artifact can make it `observed`.

## Where the artifacts are

- `reverse_engineering_dir` in the local config — a read-only reference project of artifacts already
  studied. Read its rules files and toolchain notes before using anything there, and list what it
  already covers before claiming anything is new. **Never write there.**
- Into a product project only the **neutral** specs go (a `neutral/` folder or similar): behavioural
  specs without product names or code. Files that carry product names are not fed to executors.
  Clean-room: understand *what*, implement *how* ourselves.
- A new artifact is a task for that reference project (its own session), not for this skill.
  Purchasing or downloading a paid product is the user's call.

## REA (morluto/rea) — evaluated 2026-10-13, NOT installed

- Clone (read-only, nothing executed): `<third_party_dir>/rea` (commit `f09bb8a`, v6.4.0).
  License: **MIT** (LICENSE file). Node `^22.19 || ^24.11 || >=26` (checked against Node 22.23.1).
- What it is: a local MCP server + CLI that inspects shipped artifacts and returns findings with their
  evidence and limits. Routes by target: extracted JavaScript/Electron app or ASAR
  (`analyze_javascript_application` — no native engine needed), archives/packages (ZIP/DMG/MSIX,
  `open_binary`), .NET assemblies, Android APKs, native binaries through Ghidra/Hopper/IDA, browser
  pages and HAR captures, firmware, packet/signal recordings.
- Fit for us: **Resolve Workflow Integration plugins are JavaScript** — the JS route reads them
  without decompilation. Native/Qt/PyInstaller products (e.g. Text-FLOW's 35 MB Windows installer)
  gain little beyond what Ghidra already gives a reverse-engineering project.
- No telemetry endpoints were found in `src/` (only test URLs and the optional Hopper download page).
- Installing it is a configuration change that needs the user's approval:
  `npm install --global rea-agents` (CLI only), or `npx rea-agents setup` (registers the MCP server
  in the agent config and copies its own skill to `~/.claude/skills/reverse-engineer-anything`; it
  shows a plan and makes backups first). Do not run either without an explicit yes.
- Patterns worth copying into our skills (already applied in `SKILL.md`): route by target type first;
  connect tools only when needed; a claim closes only when a verifier with comparable authority
  supports it (their "reconstruction obligation ledger" ≈ our `gate_check.py`).
