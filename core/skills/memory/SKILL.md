---
name: memory
description: Save or find facts that should outlive this session, such as decisions, preferences and lessons.
---

# Memory

Memory is one index, `MEMORY.md`, plus one small note per topic beside it, in the memory folder of this session.
By default Claude Code keeps one folder per project. If `memory.shared` is `true` in `~/.harness/profile/profile.json`,
every project uses `~/.harness/profile/memory/`, so a note from one project loads in all of them.
The first 200 lines of the index load in every session. Notes load only when you open them.

To save something:
1. Check the index. If a note covers the topic, update it instead of adding another.
2. Write the note as `<topic>.md`: the fact, why it holds, and where it came from.
3. Add one line to the index: `- [title](topic.md): what it is for`.

Keep out secrets, anything the code or git history already says, and the state of the current task.
Delete a note when it stops being true, and remove its line.
