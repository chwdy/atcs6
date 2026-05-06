# 系统设计审查 Prompt（用于 design_new.md 类文档）

## 用法

把下面"Prompt 正文"整段复制到任何 AI agent session（推荐用 general-purpose agent 或 senior code reviewer）。它会输出一份分类的设计问题清单。

每隔几轮 doc 重大改动跑一次。也可以做完一个 plan 之后跑一次，确保新增内容没有引入旧 anti-pattern。

**注意这份 prompt 检查的是"doc 内部一致性"和"doc 中的设计 anti-pattern"。它不审查 doc-vs-code 一致性——那是 conformance_audit.md 的职责。两个审查工具配合用。**

---

## Prompt 正文（从下面这一行开始复制）

---

You are a senior systems architect reviewing a design document for an AI-assisted single-developer project. Read the document carefully and identify system design issues using the categories below.

### Project context (DO NOT ignore this)

This is a hardware-adjacent project (model railway control system) where the developer:
- Cannot read all AI-written code (code volume far exceeds review bandwidth)
- Uses this design document as the canonical contract between intent and code
- Verifies behavior via UI inspector that surfaces runtime entity state

The design document is the **input** to AI code generation, not a description of finished code. Therefore:

- Length of the document is NOT a problem on its own. A 2000-line doc that passes review is preferable to a 500-line doc that leaves AI guessing.
- "Make it shorter" / "rewrite from scratch" / "split component X into N modules" are NOT valid recommendations.
- Recommendations must be **surgical** (specific line edits, deletable chunks), not architecture overhauls.

### Document to review

`/Users/Diwei/Documents/exp_self/atcs6/design_new.md`

(Replace with current path if different.)

### What to look for

Scan for each of the 8 patterns below. For each finding, cite line numbers and propose a surgical fix.

---

#### Pattern 1: Derived data stored as fields with multiple writers

A "derived property" is anything whose value can be computed from other state. If the document specifies that a value is derived but ALSO lists multiple writers, that is a stale-data bug factory.

Examples this project has hit historically:
- `travel_direction` declared "derived from (cab, span)" but had `set_cab(...)` and `depart()` as writers → cab switch produced wrong direction when one writer was missed
- `received_path_plan` declared "active path projection" but had push semantics with multiple `receive_path_plan(...)` callers → lookahead read stale path
- `lookahead` declared "computed from inputs" but had pre-physics cache + post-physics refresh + on-demand recompute → three sources disagreed

**Check for**: every field in TrainController / Train / Path / RouteRun / Block / Switch schemas. For each:
- Is it @property (computed live) or stored (with writers)?
- If stored: is its value actually a function of other fields? → should be @property
- If declared @property: are there any "writes X" / "sets X" / "updates X" / "atomically refreshed" references? → contradiction

Output: `<field_name> | declared_as | actual_writers | suggested_fix`.

---

#### Pattern 2: Latches — safety-encoding vs software-self-created

A latch (flag/payload written in one phase, consumed in another) is **legitimate** if it encodes a real-world constraint (e.g., "cannot release block reservations while train is moving"). It is **illegitimate** if it only encodes "phase X writes, phase Y reads" with no underlying physical/safety reason — that's software-self-imposed asynchrony.

Examples:
- LEGITIMATE: `pending_path_cancel_request` gated by `is_stopped() == True` — encodes the physical safety rule that authority can't be released mid-motion
- ILLEGITIMATE (correctly deleted earlier): `departure_requested` was Phase 1→Phase 2 with no physical gating — pure software-internal communication that became a direct method call

**Check for**: every entry in the Latch Table. For each:
- Does the gating condition reference real physical state (speed, position, hardware response)?
- If gating is "None; always consumed on next phase pass" → SOFTWARE-LATCH
- If gating involves `is_stopped()` or hardware ack or external timeout → SAFETY-LATCH

Output: `<latch> | classification | gating | proposed action (KEEP / replace with direct call)`.

---

#### Pattern 3: Cross-reference drift

The document likely has authoring rules: each concept defined once, referenced elsewhere by name.

**Check for**: every concept defined in a schema/table/canonical section. Search the rest of the document for restatements (partial or full) of the same rule. Each restatement is a future drift source.

Output: `<concept> | canonical home (line#) | drift restatements (line#s) | replace with cross-reference`.

---

#### Pattern 4: Dead or redundant states

The document may describe states or branches that are not reachable in v1, or whose entry/exit conditions are fully covered by another state with a derived predicate.

Examples this project hit:
- `RUNNING_WAITING` could be derived from `RUNNING_MOVING + speed==0 + ATC zero clearance`, with no unique behavior → deleted
- `departure_confirmation_required` was a UI-layer concern smuggled into controller state → deleted

**Check for**: every state in any state-machine table. For each:
- Entry conditions: does another state (with derived predicate) already cover them?
- Exit transitions: does any handle scenarios no other state handles?
- If neither: redundant.

Output: `<state> | overlapping equivalent | deletion proposal`.

---

#### Pattern 5: Per-case branches in shared contracts

If a workflow step is written as "if it's case A, do X; if it's case B, do Y; …" without a unifying mechanism, the spec will grow O(N) branches forever.

**Check for**: any step that branches by `cancellation_kind` or `task_kind` or other discriminator AT THE WORKFLOW LEVEL. The discriminator should determine data, not control flow at the spec layer.

Output: `<branch site> (line#) | discriminator | branches | propose general mechanism OR justify exception`.

---

#### Pattern 6: Method signatures that hide intent

If a method has >5 args, mixes mandatory + optional + transitional, or returns `bool` for an operation that can fail in multiple distinct ways — the contract is unclear.

**Check for**: every method signature in API tables. For each:
- >5 params? → propose payload dataclass
- `bool` return for multi-failure operation? → propose result type with `success` + diagnostic fields

Output: `<method> | issue | proposed signature`.

---

#### Pattern 7: Make-illegal-states-unrepresentable violations

If two fields can disagree about the same fact (e.g., `cab` says "B" while `travel_direction` says "forward-of-A"), the spec relies on rules ("must be kept in sync") instead of structure. Rules drift; structure doesn't.

**Check for**:
- Pairs of fields that encode partial overlap of the same fact
- Use of `Optional[X]` where the None case is excluded by other invariants (the type is too weak)
- Boolean flags whose value is implied by other state (`is_OFF` when there's already a `state` enum with `OFF`)

Output: `<field pair OR field>  | redundancy | propose merger or stronger type`.

---

#### Pattern 8: Cross-cutting concerns that should be separated

Some logic may be cross-cutting (notification dispatch, error logging, command serialization) without being explicitly factored out.

**Check for**: repeated patterns ("X emits Y notification", "Z is logged with reason", "if W fails, return diagnostic R") across multiple components. If a pattern appears in 4+ places, it's a cross-cutting concern that should be a shared mechanism.

Output: `<pattern> | 4+ occurrences (line#s) | factoring proposal`.

---

### What NOT to do (avoid these common review errors)

- DO NOT recommend rewriting from scratch
- DO NOT recommend splitting components into smaller modules unless a specific concrete bug or debugging pain drives it
- DO NOT propose new abstraction layers (HAL, command bus, event sourcing, CQRS, etc.) unless the document has a SPECIFIC pain that the abstraction directly solves
- DO NOT recommend "make it shorter" — length is fine; contradiction and drift are the problems
- DO NOT propose YAGNI cuts unless the unused branch is genuinely unreachable in v1
- DO NOT confuse hardware-imposed constraints with software-imposed complexity:
  - Latches gated by `is_stopped()` or hardware ack are NOT astronaut — they encode safety
  - Latches gated by "next phase will consume" ARE astronaut — they're synthetic asynchrony

If you find yourself wanting to do any of the above, STOP and re-read this section.

### Output format

```markdown
# Design Review: <doc_path>

## Summary
- Patterns checked: 8
- Total findings: N
- High-impact (would directly cause runtime bugs): N
- Medium-impact (drift, redundancy, unclear contracts): N
- Low-impact (signature clarity, factoring): N

## High-impact findings
[ordered list, each with: pattern #, location (line#), evidence, surgical fix]

## Medium-impact findings
[same form]

## Low-impact findings
[same form]

## Patterns that found nothing
[brief note for each — proves the review was thorough]

## Considered but rejected (avoiding astronaut traps)
[brief list of suggestions you considered but rejected, with rationale]
```

### Final guidelines

- Cite line numbers EVERYWHERE
- Be honest about uncertainty — if something might be a violation but you're not sure, mark UNCLEAR and ask
- Prefer "delete X" over "add Y" — additions in design docs are expensive
- The reviewer is a single human + AI working iteratively. Recommendations should be doable in <1 hour each. If a fix needs >500 lines of doc/code change, break it into smaller surgical pieces.
- The two real bugs that motivated this design (lookahead staleness, cab direction wrong-after-switch) were both Pattern 1 violations. Pattern 1 should always be your first pass.

---

## Prompt 正文结束（复制到这一行）

---

## 怎么解读输出

每一项 finding 都应该可以独立处理：

| 影响 | 如何处理 |
|---|---|
| High-impact | 立即修；多半是 derived-but-stored 或 cross-reference drift |
| Medium-impact | 下一轮 doc 编辑时一起修；多半是冗余 state 或 per-case branch |
| Low-impact | 累积起来再批量修；多半是 method signature 或 factoring |

每修一条 = doc 里删几行/改几行 + 让 AI 翻译到代码（如果代码已经存在）。**不需要立即重构所有代码**——doc 是先动的层。

## 与 conformance_audit 的区别

- **本 prompt（design review）**：检查 doc 内部一致性 / 设计 anti-pattern / 概念结构
- **conformance_audit**：检查代码是否如实实现了 doc 的规则

两个审查的关系：

```
design review → 修 doc → AI 翻译 → conformance audit → 修代码
       ↑                                                   │
       └───────────────────────────────────────────────────┘
                    (找到 doc 也错的地方时)
```

定期做 design review 让 doc 不腐败。定期做 conformance audit 让代码不偏离 doc。两个都做，循环长期收敛。
