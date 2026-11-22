# Issue tracker: GitHub Issues

Issues and specs for this repo live in GitHub Issues on `jaycdev/codie-homeassistant`. Use the `gh` CLI for every operation.

## Conventions

- A feature's spec is one issue with the `spec` label, titled `Spec: <feature>`.
- Implementation issues are one issue per ticket, never a single combined tickets issue. Each body starts with `Part of #<spec>`, and the spec's body keeps a task list (`- [ ] #<ticket>`) of its tickets in order.
- Triage state is a `status: <state>` label. Closing the issue marks it done.
- Comments and conversation history go in the issue's comments (`gh issue comment`).
- If a label doesn't exist yet, create it with `gh label create` before using it.

## When a skill says "publish to the issue tracker"

Create the issue with `gh issue create --title ... --body-file ... --label ...`. For a ticket, add it to its spec's task list.

## When a skill says "fetch the relevant ticket"

Read it with `gh issue view <number> --comments`. The user will normally pass the issue number or URL directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is an issue with one **child** issue per ticket.

- **Map**: an issue with the `map` label, titled `Map: <effort>`, whose body holds the Notes / Decisions-so-far / Fog sections.
- **Child ticket**: an issue whose body starts with `Part of #<map>` and states the question. A `type: research`/`type: prototype`/`type: grilling`/`type: task` label records its type. The map's body keeps a task list of its children.
- **Blocking**: a `Blocked by: #NN, #NN` line near the top of the body. A ticket is unblocked when every issue it lists is closed.
- **Frontier**: list the map's open children (`gh issue list --search "\"Part of #<map>\" in:body" --state open`) that are unblocked and have no `status: claimed` label; the lowest number wins.
- **Claim**: add the `status: claimed` label before any work.
- **Resolve**: comment the answer under an `## Answer` heading, close the issue, then add a context pointer (gist + link) to the map's Decisions-so-far by editing its body.
