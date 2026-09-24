# Design red flags

Reject or revisit a candidate with shallow pass-through modules, leaked
internal state, time-ordered phases that should be data, repeated exceptions,
callers that know internal rules, or escape hatches that make invalid states
common. Explain the flag with a caller or type.
