# Skills

Skills are local filesystem packages that influence prompting and capability requests without bloating the core runtime.

## Layout
- `skills/<skill-id>/SKILL.md`
- `skills/<skill-id>/manifest.json`

## MVP behavior
- skills are loaded at startup
- skill text is injected into session context
- skills do not mutate code automatically
- skills are visible in the API immediately, with web console and TUI views deferred
