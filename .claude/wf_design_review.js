export const meta = {
  name: 'design-new-review',
  description: 'Adversarial multi-agent review of design_new.md for refactor-blocking ambiguities',
  phases: [
    { title: 'Survey' },
    { title: 'Tenets' },
    { title: 'Dedup' },
    { title: 'Verify' },
    { title: 'Synthesize' },
  ],
}

const DOC = '/Users/Diwei/Documents/exp_self/atcs6/design_new.md'

const RUBRIC = `
You are reviewing design_new.md (path: ${DOC}), a REFACTOR design doc for a railway data/control model.
It is NOT a standalone greenfield spec. The reader is a repo-aware engineer who can read the current
code/other docs for UNCHANGED behavior, and may ask the author a FEW low-severity clarifying questions,
but must NOT have to make judgment calls on CHANGED cross-component contracts.

SOURCE OF TRUTH for changed behavior = design_new.md. Current code/docs = valid background for UNCHANGED behavior.

Backward compatibility is OUT OF SCOPE. Do NOT flag: missing migration shims, dual-write transitional fields,
alias retention, "legacy path X not addressed". A clean replacement of an old contract is the DESIRED outcome.

CORE DESIGN TENETS the doc must consistently express (flag drift/contradiction even if locally consistent):
1. Three-phase design canonical: P1 Setup/planning, P2 Execute/controller+physics, P3 Feedback/cleanup.
   Cross-phase work goes through controller-owned per-train flags/mailboxes/latches, NOT cross-phase function calls.
2. Flag/latch-based communication, not call-based: behavior self-resolves from controller state, latches, queued
   reports as the tick advances — not inline cross-component calls inside the wrong phase.
3. No single-use-case branches: every rule a general mechanism. Special cases for one workflow (a specific
   cancellation kind, route shape, operator action, "post-EB restart" arming, "this run type only" normalization,
   one-shot operator-retarget bypassing cancel-and-replan) are a smell.
4. Loose coupling: components talk via schemas/latches/registries/projections, not direct back-references or
   duplicated state. Flag duplicated ownership, hidden mirrors, component-to-component direct calls.
5. State machine clear and self-driving: each state has explicit entry/exit triggers and meaning; behavior under
   each external event (EB, cancel, power-off, route edit) derivable from the table without prose exceptions.
   Each state's meaning must be independent of which workflow caused entry.
6. Task/step generality: similar steps share one mechanism (e.g. dwell = general step finishing before route
   completion, not a special branch tied to terminal stops).

FLAG: contradictions inside the doc; changed method/payload/state contracts not propagated through the full
call chain; ambiguous new ownership splits; impossible/underspecified new workflow steps; stale rationale that
explains the new design using OLD mechanisms; changed terms used inconsistently; places the reader must infer
private author intent; tenet violations (phase-boundary leaks, call-based handoffs, single-use-case branches,
tight coupling, ambiguous state entry/exit, parallel-but-different mechanisms for similar concepts).

DO NOT FLAG: unrestated unchanged entity-local fields; un-redocumented unchanged helpers; unchanged
serialization/storage; obvious local impl details readable from repo; lack of greenfield completeness; lack of
backward-compat shims.

SEVERITY:
HIGH = contradictory/impossible/chain-broken; two parts conflict on a changed rule; required changed behavior
  not implementable as written; changed method/payload/state contract breaks in the chain; missing changed
  contract likely to cause divergent implementations; engineer needs a cross-component judgment call;
  unreachable/ambiguously-entered state; single-use-case branch contradicting the general mechanism.
MEDIUM = incomplete changed contract; ownership still unclear; missing trigger/teardown rule; changed concept
  relying on stale old-model explanation; misleading inconsistent terminology in a changed area; per-case branch
  where a general mechanism would cover; cross-phase handoff described as a direct call when it should be a
  latch/flag; coupling tighter than necessary.
LOW = non-blocking ambiguity the engineer could resolve with one non-critical clarifying question; weak
  cross-references / dead branches / vague failure text in a changed area.

CATEGORIES (use the best fit): H-CONTRADICTION, H-SIGNATURE, H-API-CHAIN, H-GUARD, H-DEAD-BRANCH,
H-UNREACHABLE-STATE, H-PHASE-BOUNDARY, H-SPECIAL-CASE, M-PAYLOAD, M-OWNER, M-CHAIN-CONTRACT, M-STATE-TRIGGER,
M-BYPASS-CONSISTENCY, M-TERMINOLOGY, M-RATIONALE, M-EXTERNAL-DEPENDENCY, M-CANONICAL-HOME, M-COUPLING,
M-FLAG-HANDOFF, M-GENERALITY, L-VACUOUS, L-UNRESOLVED-CONSTRAINT, L-DEAD-END, L-STALE-CONTEXT, L-CROSSREF.

When you cite, give real line numbers from the doc. Only raise a finding if the omission/mismatch affects the
CHANGED target model or could cause inconsistent refactors. Be precise and skeptical — do not pad.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'category', 'title', 'lines', 'canonical_rule', 'conflicting_locations', 'why_problem', 'fix'],
        properties: {
          severity: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
          category: { type: 'string' },
          title: { type: 'string', description: 'short title' },
          lines: { type: 'string', description: 'line number(s) in design_new.md, e.g. "412" or "1103-1120"' },
          canonical_rule: { type: 'string', description: 'the canonical changed rule the doc MUST state' },
          conflicting_locations: { type: 'string', description: 'the conflicting or incomplete locations, with line refs' },
          why_problem: { type: 'string', description: 'why this blocks/risks a repo-aware refactor engineer; judgment-call vs askable question' },
          fix: { type: 'string', description: 'the exact change needed in design_new.md (say X / replace Y with Z / state A owns B / express as flag handoff ...)' },
        },
      },
    },
  },
}

// ---- Phase 1+2: section readers + tenet auditors run together (all just read the doc) ----
const SECTIONS = [
  { key: 'front-matter', range: '1-186', what: 'About/Authoring Rules/Scope/Out-of-Scope/Glossary/Design Direction/Layered Architecture. Watch terminology definitions that later sections must honor, and scope claims that conflict with detailed rules later.' },
  { key: 'core-loop', range: '187-494', what: 'Core Runtime Loop: the three-phase per-train transaction, Planning Kernel steps, Phase ownership/step ordering, and the canonical Latch Table. THIS IS THE SPINE. Check every latch has writer phase+entity, consumer phase+entity, clear/no-op-clear rule, multi-tick survival; check phase boundaries are not crossed by direct calls.' },
  { key: 'entities-track', range: '495-722', what: 'Units, Canonical Entity Index, TrackResource, TrackSegment, Switch Entity, Flank Protection Computation, Platform/Station. Check ownership of flank protection computation/storage/teardown and switch resource locking contracts.' },
  { key: 'managers', range: '723-841', what: 'Block, BlockManager, SwitchManager, ResourceCoordinator. Check who owns lock acquisition/release, registry vs direct-call coupling.' },
  { key: 'path-dispatcher', range: '842-1102', what: 'Path And PathDispatcher: received_path_plan schema, path_tile entry schema, resource_sequence, pathfinder/current-span workflow, tie-breaks. Check payload field propagation and shared-workflow generality between PathDispatcher and RouteExecutor.' },
  { key: 'route-executor', range: '1103-1584', what: 'Route And RouteExecutor: waypoints, dwell, route completion, replan, cancellation, requested_destination_direction resolution, covered-task index advance. Check dwell generality, cancellation generality, completion gating, and the Phase-1 handoff to controller.' },
  { key: 'train-controller', range: '1585-2088', what: 'Train Entity and TrainController: the controller state machine, command drain, lookahead payload, ATO/ATC, target arming, EB, power-off, queued_progress_reports mailbox. Check state entry/exit triggers, EB-recovery generality, flag-based handoffs, and that state meaning is workflow-independent.' },
  { key: 'physics-ui', range: '2089-2288', what: 'TrainPhysics, SpeedProfile, Signals, Power, Data Flow To UI, End-To-End ATO Path Run, Logic Review Checklist. Check get_current_span ownership, projection-to-UI coupling, and that the end-to-end narrative matches the per-section contracts.' },
]

phase('Survey')
const surveyThunks = SECTIONS.map((s) => () =>
  agent(
    `${RUBRIC}\n\nYOUR ASSIGNMENT: You OWN the section at lines ${s.range} ("${s.key}"). ${s.what}\n\n` +
    `Read that section in full (Read ${DOC} with offset/limit covering ${s.range}; lines are long, read carefully). ` +
    `You MAY grep/read OTHER parts of the doc to check whether a contract your section depends on is honored elsewhere, ` +
    `or whether a cross-reference your section makes is stale. Report findings whose ROOT is in your section. ` +
    `Find real, refactor-blocking issues per the rubric. Do not invent backward-compat findings. ` +
    `Return findings (may be empty if the section is clean).`,
    { label: `survey:${s.key}`, phase: 'Survey', schema: FINDINGS_SCHEMA }
  ).then((r) => ({ source: `survey:${s.key}`, findings: (r && r.findings) || [] }))
)

const TENETS = [
  { key: 'phase-boundary', focus: 'TENET 1+2: phase-boundary leaks and call-based (not flag/latch) handoffs. Sweep the WHOLE doc for any rule where one phase mutates another phase\'s state directly, a cross-phase function call replaces a controller-owned flag/mailbox/latch, or a handoff between P1/P2/P3 is hand-waved. Cross-check the Latch Table against each described handoff.' },
  { key: 'special-case', focus: 'TENET 3: single-use-case branches. Sweep the WHOLE doc for rules carved out for ONE workflow that a general mechanism should cover (specific cancellation kind, specific route shape, specific operator action, post-EB restart arming, "this run type only" normalization, one-shot operator-retarget bypassing cancel-and-replan). For each, state whether the general flow already covers it.' },
  { key: 'coupling', focus: 'TENET 4: tight coupling / duplicated ownership. Sweep for duplicated state ownership, hidden mirrors of a value on two entities, component-to-component direct back-references where a registry/projection/latch lookup is the contract. Especially RouteExecutor reading mirrored runtime state on Train vs controller state.' },
  { key: 'state-machine', focus: 'TENET 5: state machine clarity. Sweep the controller (and any other) state machine for states with missing/ambiguous entry or exit triggers, unreachable states, transitions that exist only in prose, or a state whose MEANING depends on which workflow caused entry. Verify behavior under EB/cancel/power-off/route-edit is derivable from the table for every state.' },
  { key: 'generality', focus: 'TENET 6: parallel-but-different mechanisms for similar concepts. Sweep for similar steps defined two different ways: dwell vs waypoint-completion, EB-recovery vs ordinary cancellation-recovery, route-edit replan vs operator retargeting, terminal-stop vs mid-route stop. Flag where one general flow would do.' },
  { key: 'crossref-terms', focus: 'CROSS-REFERENCE & TERMINOLOGY drift: changed terms used inconsistently across sections; a contract (ownership, payload field, state transition, callback signature, latch consumer, recovery gate) updated in one site but stale in another; concepts with two canonical homes; stale rationale explaining the new design via the OLD mechanism. Sweep the whole doc.' },
]

phase('Tenets')
const tenetThunks = TENETS.map((t) => () =>
  agent(
    `${RUBRIC}\n\nYOUR ASSIGNMENT: cross-cutting audit across the ENTIRE doc. FOCUS: ${t.focus}\n\n` +
    `Read whatever spans you need (grep for the relevant terms, Read ranges around hits). Report only findings that ` +
    `match your focus and are refactor-blocking or contract-incomplete per the rubric. Cite exact line numbers. ` +
    `Return findings (may be empty).`,
    { label: `tenet:${t.key}`, phase: 'Tenets', schema: FINDINGS_SCHEMA }
  ).then((r) => ({ source: `tenet:${t.key}`, findings: (r && r.findings) || [] }))
)

// barrier: need ALL findings together to dedup across overlapping section/tenet coverage
const raw = (await parallel([...surveyThunks, ...tenetThunks])).filter(Boolean)
const allFindings = raw.flatMap((r) => r.findings.map((f) => ({ ...f, source: r.source })))
log(`Collected ${allFindings.length} candidate findings from ${raw.length} agents`)

// ---- Phase 3: dedup/cluster ----
phase('Dedup')
const DEDUP_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'category', 'title', 'lines', 'canonical_rule', 'conflicting_locations', 'why_problem', 'fix', 'merged_from'],
        properties: {
          severity: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
          category: { type: 'string' },
          title: { type: 'string' },
          lines: { type: 'string' },
          canonical_rule: { type: 'string' },
          conflicting_locations: { type: 'string' },
          why_problem: { type: 'string' },
          fix: { type: 'string' },
          merged_from: { type: 'string', description: 'comma-joined source labels this was merged from' },
        },
      },
    },
  },
}

const deduped = await agent(
  `${RUBRIC}\n\nYou are the DEDUP/CLUSTER step. Below is a JSON array of candidate findings from multiple reviewers ` +
  `(section readers + cross-cutting auditors), so the SAME issue may appear several times with different wording or ` +
  `line refs. Merge findings that describe the SAME underlying issue into one (union their line refs, keep the ` +
  `sharpest canonical_rule/fix, set merged_from to the combined source labels). Keep distinct issues separate even ` +
  `if nearby. Drop anything that is purely a backward-compat complaint or restates unchanged local behavior. ` +
  `Do NOT soften severities here. Return the merged list.\n\nCANDIDATES:\n${JSON.stringify(allFindings)}`,
  { label: 'dedup', phase: 'Dedup', schema: DEDUP_SCHEMA }
)
const candidates = (deduped && deduped.findings) || allFindings
log(`Deduped to ${candidates.length} unique candidate findings`)

// ---- Phase 4: adversarial verify each unique finding ----
phase('Verify')
const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'adjusted_severity', 'reasoning', 'refined'],
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'rejected', 'downgraded'] },
    adjusted_severity: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW', 'DROP'] },
    reasoning: { type: 'string', description: 'why confirmed/rejected; cite the doc lines you checked' },
    refined: {
      type: 'object',
      additionalProperties: false,
      required: ['title', 'category', 'lines', 'canonical_rule', 'conflicting_locations', 'why_problem', 'fix'],
      properties: {
        title: { type: 'string' },
        category: { type: 'string' },
        lines: { type: 'string' },
        canonical_rule: { type: 'string' },
        conflicting_locations: { type: 'string' },
        why_problem: { type: 'string' },
        fix: { type: 'string' },
      },
    },
  },
}

const verified = await parallel(
  candidates.map((f) => () =>
    agent(
      `${RUBRIC}\n\nYou are an ADVERSARIAL VERIFIER. A reviewer raised this candidate finding about design_new.md:\n\n` +
      `${JSON.stringify(f, null, 2)}\n\n` +
      `Your job is to REFUTE it. Go to the cited lines AND search the rest of the doc to check: (a) is the claimed ` +
      `gap/contradiction actually present, or does the doc resolve it at another site the reviewer missed? (b) is this ` +
      `merely a backward-compat / unchanged-local-behavior / greenfield-completeness complaint we must NOT flag? ` +
      `(c) is the severity justified, or should it be downgraded? Default to rejecting if the doc actually covers it ` +
      `or if it requires no judgment call by a repo-aware engineer. If it survives, return verdict=confirmed (or ` +
      `downgraded) with a refined, tightened version (sharper line refs, canonical_rule, and exact fix). If it does ` +
      `not survive, verdict=rejected, adjusted_severity=DROP.`,
      { label: `verify:${(f.title || '').slice(0, 40)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
    ).then((v) => ({ original: f, verdict: v }))
  )
)

const survivors = verified
  .filter(Boolean)
  .filter((v) => v.verdict && v.verdict.verdict !== 'rejected' && v.verdict.adjusted_severity !== 'DROP')
  .map((v) => ({
    severity: v.verdict.adjusted_severity,
    ...v.verdict.refined,
    _reasoning: v.verdict.reasoning,
  }))
log(`${survivors.length}/${candidates.length} findings survived adversarial verification`)

// ---- Phase 5: synthesize final report ----
phase('Synthesize')
const report = await agent(
  `${RUBRIC}\n\nYou are the SYNTHESIS step. Below are the VERIFIED, surviving findings (each already adversarially ` +
  `checked). Produce the FINAL review report in EXACTLY this structure and nothing else:\n\n` +
  `1. Findings first, grouped by severity (HIGH, then MEDIUM, then LOW). For EACH finding use exactly this header ` +
  `line format:\n   "[Severity]-[Category] — short title — line(s)"\n   Then four labeled parts: the canonical changed ` +
  `rule the doc must state; the conflicting/incomplete locations (with line refs); why it's a problem for a ` +
  `repo-aware refactor engineer (judgment-call vs askable low-severity question); and the exact fix needed in ` +
  `design_new.md (use imperative "the doc must say X" / "replace Y with Z" / "state that A owns B" / "express as a ` +
  `flag-based handoff: writer X writes latch Y in phase P, consumer Z reads it in phase Q" / "express as a general ` +
  `mechanism rather than a per-case branch" language). Do NOT ask clarifying questions inside findings.\n` +
  `2. A "Silent Coverage Gaps" section: changed/shared contracts under-specified overall even if no single line ` +
  `captures it; document-spanning tenet violations (e.g. "EB recovery described in 5 places with 5 rules").\n` +
  `3. An "External-Context Dependencies" section: places where changed intent had to be inferred from current ` +
  `code/other docs because design_new.md did not fully encode it.\n\n` +
  `Order findings within each severity by importance. Keep every finding's line refs. Merge any near-duplicates that ` +
  `slipped through. Be concise but complete. Output clean GitHub-flavored markdown.\n\n` +
  `VERIFIED FINDINGS:\n${JSON.stringify(survivors, null, 2)}`,
  { label: 'synthesize', phase: 'Synthesize' }
)

return { report, counts: { raw: allFindings.length, unique: candidates.length, survivors: survivors.length } }
