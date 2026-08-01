# Repo Director — Product-Motion Pipeline

## When to Use

First stage of a product-motion run. You have a path to the user's product
repository. You produce the two artifacts everything downstream is grounded
in: **`design_system`** (the stage's canonical artifact) and
**`ui_inventory`**, plus `decision_log` entries.

**Both are mandatory.** The manifest declares them as the stage's
`required_outputs`, and `write_checkpoint` refuses a `completed` or
`awaiting_human` repo_analysis checkpoint that is missing either — every
downstream truthfulness claim and the assets fidelity gate read
`ui_inventory`. Use an `in_progress` checkpoint while the stage is still
being assembled.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Tool | `repo_design_extractor` | Deterministic scan: framework, token sources, CSS vars with file+line, screens/components index |
| Layer 3 skill | `.agents/skills/repo-design-extraction/SKILL.md` (+ its `references/`) | **Read before authoring** — reading guides per styling system, distillation rules, provenance discipline |
| Schemas | `schemas/artifacts/design_system.schema.json`, `ui_inventory.schema.json` | Artifact validation |

## Process

1. **Confirm scope.** v1 targets web frontends (React/Next/Vue/Tailwind). If
   the scan detects `framework: "other"` or no UI code, stop and tell the
   user what was found — do not force a backend repo through this pipeline.
2. **Run the scanner** with `output_path` under
   `projects/<id>/artifacts/repo_scan_report.json` — a path inside the
   product repo is rejected; the analyzed repo is never written to. Surface
   `truncated: true` and everything in `warnings[]` if reported.

   The scanner does **not** execute the product's code. If `tailwind_theme`
   is `null`, open the config and read it — do not reach for the
   `allow_config_execution` opt-in, which runs the repo's JavaScript. If you
   ever think a run genuinely needs it, that is a user decision: say what it
   does and get approval first.
   Screens whose `route_source` is `not_derivable` have no route the scanner
   can know; leave `route` unset in `ui_inventory` unless you read the router
   config yourself.
3. **Read the flagged sources** and author both artifacts per the
   `repo-design-extraction` skill. Non-negotiables:
   - every token value copied verbatim with provenance,
   - `gaps[]` lists everything derived (the glass spec almost always),
   - every screen actually opened (`reviewed: true` is a contract),
   - `ui_elements` labels are the source's real strings.
4. **Record the design read** — one paragraph in `design_system.summary`
   stating what this product's visual language *is*, from evidence. This
   seeds the taste work at proposal.
5. **Log decisions**: `decision_log` entry `category: "grounding_source"`,
   subject "Design-system grounding" — repo extraction chosen; note
   live-URL capture (`website-to-video`) as the option considered when the
   repo also has a deployed site.
6. **Validate** both artifacts (`validate_artifact`), self-review against the
   manifest's `review_focus`, then checkpoint `awaiting_human`.

## Presenting at the gate

Show the user, in plain language:

1. The design read (2-3 sentences) + a token table (color swatches described,
   fonts, radii) **with provenance columns**.
2. The screens found, and 3-5 `flagship_recommendations` with one-line whys.
3. The `gaps[]` ledger — what was derived and how.
4. Ask: which screens should the video feature? Any token corrections?

Then **end your turn** and wait.

## Failure modes to catch

- Thin design system (4 CSS vars, default Tailwind): present it honestly and
  lean on `component_styles`; do not pad with invented tokens.
- Monorepo: ask which package/app is the product before scanning everything.
- Tokens behind CSS vars referenced by Tailwind (`hsl(var(--primary))`):
  resolve through to the CSS file — the config key alone is not provenance.
