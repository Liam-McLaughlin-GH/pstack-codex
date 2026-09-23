# Project-local Codex roles

The role files in `agents/` use the current Codex custom-role TOML format. The
config file registers both roles explicitly, including the case-sensitive
`Comment Sicko` name.

Project-local role auto-discovery was not treated as proven for the installed
Codex CLI 0.144.0. Use the explicit registrations in `config.toml`, or run the
offline adapter and its tests before starting a child.

The adapter checks the local model catalog before selecting a model. If the
requested model is unavailable, a fallback must be supplied and is recorded in
the result. It never silently substitutes a model.
