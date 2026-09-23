### Pause safely

You own a clean stop. Use this playbook only after an explicit pause request,
not after an ordinary task turn.

1. Finish the current atomic step or back it out. Do not stop in a known
   broken state. Cancel or release any local worker that can still write.
2. Take no irreversible action to pause. Do not push, create a PR, merge,
   deploy, message, delete, or discard work.
3. If the user explicitly asked for a durable checkpoint, write one local
   wip commit on the current branch. Describe a broken tree in the commit
   body. A normal continuation does not create a checkpoint commit.
4. Write a local resume note outside the code scope. Include intent, paths,
   verified facts, current state, next action, and gotchas. Use a supplied
   artifact path instead of private session storage.
5. Read the note back and confirm the tree, local audit trail, and side-effect
   log. A pause is complete only when a cold-start session can identify its
   first action.

UNSUPPORTED-SEAM: no session-resume store or worker cancellation service is
assumed. Use a bounded file note and confirm local processes yourself. If a
process cannot be stopped safely, report BLOCKED instead of pretending the
pause is clean.

Reply with the safe boundary, on-disk paths, checkpoint state, tree status,
side-effect log, and the first action on resume.
