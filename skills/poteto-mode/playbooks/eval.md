### Eval

You own the experiment design. Keep candidate work isolated and blind the
judgment metadata.

Blinding rules:

- Candidate-visible paths and prompts contain no eval, test, judge,
  experiment, rubric, score, compare, benchmark, candidate, or arena labels.
- The prompt describes an ordinary user goal, not what is being measured.
- Do not ask the candidate to list skills or principles. Judge the artifact
  and the files it read.
- Use project-shaped directory names and separate candidate directories.
- Do not tell one candidate that another exists.
- The judge sees sanitized labels and a private rubric, never model identity.
- A comparison uses one scale and one pass over both labeled outputs.

Steps:

1. Frame the variant, success behavior, and three to six judge-only criteria.
2. Create one temporary directory per candidate. Plant only the context an
   ordinary task would expose. Keep evaluation metadata outside those paths.
3. Write one organic prompt. Record its hash or local path outside candidate
   directories.
4. Run available local Codex workers in separate directories. If the worker
   capability is missing, mark that candidate BLOCKED rather than substituting
   a self-report.
5. Give the judge sanitized output paths and the rubric. It must not infer
   identity from directory names, environment variables, or metadata.
6. Inspect the files each candidate actually read and every output yourself.
   A local transcript or log must be supplied explicitly; never scan private
   session stores.
7. Reconcile the judge with the evidence. Record dropouts, disagreement,
   synthesis, and promotion recommendation.

UNSUPPORTED-SEAM: named model-family routing and private session transcript
stores are not assumed. Use explicit local output paths and supplied logs, or
report BLOCKED.

Reply with the variant, judge-only rubric, candidate labels and results,
dropouts, evidence paths, synthesis, and promotion recommendation.
