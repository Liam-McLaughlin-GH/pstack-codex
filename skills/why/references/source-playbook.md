# Local source playbook

Check each category that has a current read surface. Record empty and
unavailable categories instead of skipping them silently.

- [Code archaeology](sources/code-archaeology.md) uses code, tests, blame, and
  commit history.
- [Incident review](sources/incident-postmortem.md) uses supplied incident
  notes and runtime evidence.
- [Ticketing](sources/linear.md) uses a supplied ticket or connected tracker.
- [Documents](sources/notion.md) uses supplied design notes or documents.
- [Chat](sources/slack.md) uses supplied decision records or a connected chat.
- [Observability](sources/datadog.md) uses supplied metrics or logs.
- [Error tracking](sources/sentry.md) uses supplied exception artifacts.
- [Analytics](sources/databricks.md) uses supplied product or data reports.

The connectors are optional. A missing connector is a documented gap, not
permission to invent a result.
