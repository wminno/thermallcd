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
