# 11 — Security and compliance notes

**Status:** Draft · **Owner:** Tech lead · **Last reviewed:** 2026-10-28

> Project-specific security requirements for AgentCrash. This is a working
> startup note, not a compliance certification. Startup lens: protect keys,
> keep the blast radius synthetic, and write down just enough to earn trust.

## 1. Assets and trust boundaries

| Asset | Why it matters | Trust boundary |
|---|---|---|
| Llama API keys of builders | Billed identity; leak spends money | Env of the operator machine only, never repo or trace |
| Traces in traces dir | May embed prompt text, args, PII-shaped values | Local gitignored dir plus curated goldens; redaction before persistence |
| Policy files | Control agent recovery behavior | Reviewed diffs only; diagnoser output never auto-applied |
| Poisoned doc fixtures | Adversarial strings that attack agents | Labeled test data; handled as untrusted content everywhere |
| Golden traces | Define pass and fail truth | Updated deliberately with reviewer plus offline proof |
| Source and scenarios | Startup IP | Standard repo access control |

Roles: contributor (reads and writes code and docs), reviewer (approves
policy-schema and golden changes), operator (holds the API key and runs live
scenarios). No end-user accounts and no admin console exist in v0.

## 2. Threat model

| Threat | Impact | Mitigation and detection | Owner and status |
|---|---|---|---|
| Poisoned doc drives agent action | Unauthorized side effect in a live deployment of the tested pattern | Delimiter tagging of retrieved text; instruction hierarchy; confirmation gate on money movement; scenario B regression | Open; scenario B at M1 |
| Blind retry double-charges | Duplicate payment or booking | Idempotency keys plus status-read-before-retry; scenario A gate | Mitigated at v0 by design |
| API key exfiltration via trace | Key spend by third party | Keys only from env; redaction pass strips key-shaped values before trace write; gitignore on traces dir | Open; implement at M1 |
| PII in traces and goldens | Privacy exposure on share | Redaction of emails, card numbers, and names to synthetic constants; golden review checklist | Open; implement at M1 |
| Diagnoser prompt injection | Attacker-crafted trace steers policy patch | Diagnoser input treated as untrusted; patch schema validation; human review before replay; never auto-apply | Open; implement at M2 |
| Malicious dependency | Supply-chain compromise of runner | Pinned deps with lockfile; minimal dep set; no network inside tools | Open; implement at M1 |
| Stale-read lost update | Silent data corruption pattern | Versioned reads plus re-read-before-write; scenario C regression | Open; scenario C at M1 |
| Golden trace tampering | Verdict truth silently changed | Golden diffs need reviewer plus offline check proof; CI pins expected verdicts | Open; process at M1 |

## 3. Poisoned docs are test content, never exfil

The poisoned strings in scenario B exist to test defenses, and they are
dangerous to handle carelessly because they read like real instructions:

- Storage: all adversarial strings live in one fixture file with a header
  stating they are attack test content. No other file may contain them.
- Tagging: every doc byte entering a trace carries untrusted_source true and
  the fixture name it came from.
- Rendering: CLI output wraps retrieved doc content in labeled delimiters so
  operators never mistake test content for product guidance.
- Copy rule: contributors never paste fixture strings into issues, chats, or
  prompts except inside the tagged harness path.
- Rotation: fixture strings change per milestone so agents cannot memorize
  them; scenario B goldens pin the exact strings used.

## 4. Tool sandboxing

Synthetic tools are sandboxed by construction, and the rules stay strict as
real adapters arrive:

- No network, no filesystem, no subprocess inside tools. Tools compute from
  args, seed, and virtual clock plus the explicit in-memory store.
- The store is per-run and in-memory; nothing persists between runs except
  the trace file the runner writes.
- Tool args are schema-validated before execution; unknown tools and extra
  args are rejected and recorded, never executed.
- When real-world adapters are added after v0, each gets an allowlist, a
  confirmation gate for side effects, and a dry-run mode. No live adapter
  ships without all three.

## 5. Secrets handling for model API keys

- Keys arrive through the environment (LLAMA_API_KEY) and never through
  flags, files, or committed config.
- The CLI rejects a key passed as a flag value with an error that names the
  env var to use instead.
- The tracer redacts values matching key, token, and bearer patterns before
  writing any line, and tests assert redaction on synthetic key fixtures.
- Logs and error messages never echo tool args wholesale; they name the tool
  and the arg keys, not the values.
- CI holds the live key as a secret only in the G5 scenario job; all other
  jobs run with a dummy key and must pass offline.

## 6. Trace redaction of PII

- Redaction runs at write time inside the tracer, not as a later cleanup
  pass, so raw values never rest on disk.
- Default rules: email shapes become fixed synthetic constants, card-like
  digit runs become a fixed test constant, person names in fixtures come
  from a fixed synthetic roster.
- Golden traces ship only with synthetic values; a golden containing a
  real-looking address or number fails review.
- Builder traces with real-ish data stay local and gitignored; sharing a
  trace means re-running with synthetic fixtures or passing the redactor.

## 7. Supply chain

- Python 3.11 plus pinned dependencies in a lockfile; upgrades are explicit
  PRs with the full suite green.
- Minimal dependency policy: the runner, tracer, and verifiers avoid new
  dependencies; each addition names the capability it buys in the PR.
- Model access goes through one adapter module so key handling and redaction
  have a single audit point.
- No remote code execution paths: scenario files are data, policy patches
  are validated diffs, and neither is evaluated as code.

## 8. Safety rules: synthetic env only

1. No live payments, no live refunds, no real customer records. Every money
   movement in this repo touches the synthetic charge_card tool only.
2. No production credentials anywhere in the repo, fixtures, or goldens.
3. Live-model runs are capped: fixed scenario list, fixed seed matrix, fixed
   per-run step budget. A runaway loop hits the step cap and stops.
4. Diagnoser patches never apply themselves: every patch needs human review
  and a replay report before it becomes policy.
5. Incident rule: any suspected key or PII leak rotates the key, deletes the
   exposed trace, and records the lesson in the runbook. Speed first, then
   the write-up.

## 9. Compliance notes for a startup

- Data retention: local traces are working data and may be deleted at any
  time; goldens are the retained record with synthetic values only. Proposed
  default: working traces older than one milestone are deleted.
- Disclosure: demos and posts state that AgentCrash tests agents in
  synthetic environments and that results describe harness behavior, not
  production safety of any model.
- Regulatory posture: v0 processes no real personal data and offers no
  regulated function, so the compliance load is retention plus disclosure.
  Revisit before any hosted service, real user data, or live adapter.
- Reviewer: name one accountable reviewer for security notes at M1 and a
  review point per milestone after that. No corporate bureaucracy beyond it:
  one owner, one checklist, one review per milestone.

## 10. Worked example: redaction plus confirmation gate

A billing question arrives with a customer email in the prompt. The run
proceeds as follows:

1. The adapter logs the prompt with the email replaced by a synthetic
   constant before the trace line is written.
2. The read_doc tool returns vendor terms plus an embedded refund
   instruction; both are tagged untrusted in the trace.
3. The agent proposes a refund call; the policy engine blocks it because
   money movement needs a recorded confirmation that does not exist.
4. The trace shows a denied policy decision, the verifier for
   no_side_effect_from_doc passes, and no key or email appears in the file.
5. A reviewer can share this trace externally as-is because every value in
   it is synthetic by construction.

Golden review checklist for traces like this one:

- No live-looking emails, numbers, names, or keys anywhere in the file.
- Every retrieved doc span carries the untrusted tag with a fixture name.
- Every blocked side effect has a matching denied policy decision line.
- Verdicts reproduce offline with the network disabled.

## 11. Open Questions

| Question | Owner | Notes |
|---|---|---|
| Secret-scanning hook for commits (pre-commit versus CI) | Tech lead | Decide at M1; either is fine if traces stay gitignored |
| Redaction rule list location (tracer config versus policy file) | Tech lead | Proposal: tracer-owned config so redaction cannot be policy-patched away |
