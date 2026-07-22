# Repository agent rules

## Route-inference input contract

- Route inference MUST use only visual or textual facts extracted from the supplied PDF pages.
- The PDF path, filename, material code, case identifier, database row identity, and command-line metadata MAY identify a run, but MUST NOT become decision facts.
- Runtime recommendation MUST NOT read or receive `docs/index.csv`, `docs/routes_1.csv`, `docs/routes_2.csv`, `docs/cases/**`, golden routes, review files, PLM/BOM exports, or manually prepared external facts.
- Historical routes MAY be loaded only after the recommendation and all inference evidence have been persisted. They are evaluation data, never inference context.
- If a required fact cannot be established from the PDF, return `unable_to_judge`, `partial`, or `error`. NEVER fill it from a CSV, filename, nearby sample, expected answer, or human-prepared case file.

## Anti-cheating and anti-hardcoding

- NEVER key a rule, prompt, lookup, branch, or route template by material code, PDF filename, sample ID, CSV row number, or exact golden-answer identity.
- NEVER inject a known route, route prefix, operation list, or answer-derived label into a Reader prompt or runtime fact.
- Product-family knowledge is allowed only when its predicates are observable from the PDF, use stable domain features, are supported by multiple positive samples where available, and have explicit negative-example review. Exact sample matching is prohibited.
- A fact printed in the PDF must be read from the PDF. The same value appearing in `docs/index.csv` does not permit using the CSV as an OCR or classification fallback.
- Do not weaken evaluation, omit unmatched operations, or relabel a partial result as passed to make a sample green.

## Feature-derived decision contract

- Every recommended operation, its order, and every repetition MUST be traceable to process-neutral manufacturing features visibly or textually established on the PDF and to an explicit rule that maps those features to the operation.
- Drawing numbers, title-block names, material codes, filenames, document families, sample identities, and known route identities MUST NOT select a route family, route module, route template, operation, ordering, or repetition. They MAY be persisted or displayed only as non-decisional run metadata or evidence text.
- A Reader MUST extract observations, not process answers. It MUST NOT emit facts such as “needs boring”, “use wire cutting”, a route family, a route module, or a known operation sequence. Examples of valid observations include stock form, global geometry, dimensions, tolerances, hole or profile topology, weld-joint relationships, accessibility, material grade, and explicit surface requirements.
- Material grade MAY participate as a manufacturing feature only when paired with a documented process rationale and other observable predicates. Exact material-code matching and a one-sample material-grade route lookup are prohibited.
- A feature rule intended to generalize MUST be reviewed against multiple positive samples where available and explicit negative examples. Tests MUST demonstrate both identity invariance (changed drawing number/name with the same features) and feature sensitivity (same or similar identity with changed features).
- Historical or golden routes MUST NOT influence Reader prompts, runtime facts, feature extraction, rule selection, route assembly, or retry behavior. A mismatch MAY be evaluated only after the PDF-only recommendation and its evidence have been persisted.
- If the PDF does not establish the features needed for an operation or its ordering, the system MUST omit the unsupported certainty and return `unable_to_judge`, `partial`, or `error`; it MUST NOT recover the expected operation from identity or history.
- Any run whose decision path used an identity-keyed rule, answer-derived label, or route template is invalid for acceptance and MUST be explicitly retracted and rerun after the dependency is removed.

## Acceptance and verification

- The primary smoke-test interface is exactly `draw-route route /path/to/drawing.pdf`, with no material-code, external-facts, case-context, or answer-bearing arguments.
- A case is PDF-only passed only when that bare command produces the claimed route behavior within the performance gate.
- Tests for inference MUST construct observations that could have come from a PDF Reader; they MUST NOT bypass product-family recognition with PLM facts.
- Every reported pass must include the run ID, status, elapsed time, Reader statuses, and the exact predicted operation sequence or candidates.
- Any experiment that used non-PDF inference input must be labeled invalid for the PDF-only contract and rerun before being reported as passed.

## Scope

- Current work covers operation names, order, repetition, candidates, and evidence only.
- Material quota, cutting dimensions, equipment, teams, work hours, and parent-drawing-based final transfer are out of scope unless the user explicitly changes the contract.
