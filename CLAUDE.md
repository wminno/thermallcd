# thermallcd

Open Linux dashboard platform for the Thermalright Trofeo Vision 9.16" panel
(USB 0416:5408, 1920x462). Hardware driver lives in `thermallcd/`; the
dashboard platform design is `docs/designs/dashboard-platform.md`.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. Route only to skills in the session's available-skills list; answer directly for quick questions or small scoped edits.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec

## Hygiene

- Never commit secrets, tokens, or usernames. `deploy/thermallcd.service` is a
  @USER@/@REPO@ template filled by `deploy/install.sh`; `.gitignore` covers
  `*.local .env .env.* *.key *.pem *.token secrets/`.
- `reference/` is gitignored upstream source (436 MB), do not commit it.
- Repo-local git identity: `Jeroen <jeroen@AI-MiniPC>`; amend email before any
  GitHub publish.

## GBrain Search Guidance (configured by /sync-gbrain)
<!-- gstack-gbrain-search-guidance:start -->

This worktree's pinned code source answered a source-scoped page read. This
does not verify semantic search or write availability. Prefer gbrain over Grep
when the question is semantic or when you don't know the exact identifier yet;
if a query fails, report that failure rather than assuming the index is healthy.

**This worktree is pinned to a worktree-scoped code source** via the
`.gbrain-source` file in the repo root (kubectl-style context).
`gbrain code-def`, `code-refs`, `code-callers`, `code-callees`, `search`, and
`query` from anywhere under this worktree route to that source by default —
no `--source` flag needed (gbrain >= 0.41.38.0; on older gbrain the call-graph
commands need `--source "$(cat .gbrain-source)"`). Conductor sibling worktrees
of the same repo each have their own pin and their own indexed pages, so
semantic results match the code on disk here.

Call-graph queries (`code-callers`/`code-callees`) also need the graph to be
built first. A `count: 0` has several causes, and only one is fixed by
`/sync-gbrain --dream` (or `--full`): the graph was never built. Check the others
first: the source may hold no code at all (`gbrain code-def <any-symbol>
--source <id>` answers `status: out_of_scope`), the symbol may be dotted (these
verbs take a bare name), or `--all-sources` was used. `--dream` also needs a
schema pack that extracts code symbols; on another pack it completes, the graph
stays empty and it reports a WARN. `code-def`/`code-refs` need the same
extraction.

Two indexed corpora available via the `gbrain` CLI:
- This worktree's code (auto-pinned via `.gbrain-source`).
- `~/.gstack/` curated memory (registered as `gstack-brain-<user>` source via
  the existing federation pipeline).

Prefer gbrain when:
- "Where is X handled?" / semantic intent, no exact string yet:
    `gbrain search "<terms>"` or `gbrain query "<question>"`
- "Where is symbol Y defined?" / symbol-based code questions:
    `gbrain code-def <symbol>` or `gbrain code-refs <symbol>`
- "What calls Y?" / "What does Y depend on?":
    `gbrain code-callers <symbol>` / `gbrain code-callees <symbol>`
- "What did we decide last time?" / past plans, retros, learnings:
    `gbrain search "<terms>" --source gstack-brain-<user>`

Grep is still right for known exact strings, regex, multiline patterns, and
file globs. Run `/sync-gbrain` after meaningful code changes; for ongoing
auto-sync across all worktrees, run `gbrain autopilot --install` once per
machine — gbrain's daemon handles incremental refresh on a schedule.

Safety: don't run `/sync-gbrain` while `gbrain autopilot` is active — the
orchestrator refuses destructive source ops when it detects a running autopilot
to avoid racing it. Prefer registering user repos with `gbrain sources
add --path <dir>` (no `--url`): URL-managed sources can auto-reclone, and the
sync code walk for them requires an explicit `--allow-reclone` opt-in.

<!-- gstack-gbrain-search-guidance:end -->
