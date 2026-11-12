---
name: draft
description: Prepare Slack messages in the user's voice, show the exact wording for approval, then save unsent Slack drafts only after the user says yes.
---

# Draft

Use this skill when the user invokes $draft or asks for this Slack drafting workflow.

## Prepare the message

Read the relevant Slack thread or DM and its latest replies through the connected Slack tools. Resolve the recipient and parent thread from current evidence. Use the current chat when it already supplies enough context. Ask only for missing information that affects the message or destination.

Match the user's own messages and corrections, not earlier assistant drafts. Prefer short, casual paragraphs, contractions, familiar words and a direct request. Do not imitate typos or add slang, emojis or enthusiasm without support from their writing.

For a status update, explain what the user has done, what remains and what the recipient should do meanwhile. Use that order when it fits. Say "I've restored the missing tickets" only when the evidence supports it. Explain the practical consequence instead of listing audit terminology, counters or historical checks. Include exact amounts, IDs and technical detail when the recipient needs them to act safely.

Keep uncertainty and action boundaries intact. Do not turn proposed work into completed work or a passing check into proof of customer recovery. Do not invent commitments, dates, approvals or personal opinions.

Apply $unslop, then $avoid-ai-writing in composition mode as the final editing pass. Use the casual context and preserve the user's voice. If those skills are unavailable, apply the rules here and do not claim their tools ran. The external writing reference is https://github.com/conorbronsdon/avoid-ai-writing.

In the outgoing prose, do not use hyphens, any dash characters, colons or semicolons. Use periods, commas and short paragraphs instead. Do not use hyphen bullets. Preserve necessary URLs, Slack mention syntax, code and exact identifiers, even when they contain those characters. Never corrupt a link or identifier to satisfy a prose rule.

## Ask before saving

Show the complete proposed message in a chat_message writing block and identify its recipient or thread outside the block. Then ask "Is this ok to save as a Slack draft?"

Stop before any Slack draft creation or update. Invoking $draft or asking to prepare a message does not approve unseen wording. Wait for an explicit yes or equally clear approval of the displayed message and destination. Silence is not approval.

If the user requests edits, show the revised message and ask again. Approval covers only that version and destination. For multiple messages, show each separately and ask whether to save all or a named subset. Save only the approved messages.

## Save the approved draft

After approval, use Slack's native create draft action, currently slack_send_message_draft. Pass the verified DM or channel ID and the parent thread timestamp for a reply. Resolve any mentions to real user IDs. Save the approved text without adding a signature or extra wording.

Never use a send or schedule action. Do not overwrite, delete or replace an existing draft without showing the proposed replacement and obtaining approval. If Slack reports that a draft already exists, preserve it and explain the conflict. If the draft tool is unavailable, provide the approved copy and explain that it was not saved.

Confirm creation from the tool result. Reply briefly with the conversation link and say it is saved in Drafts & Sent and has not been sent. Do not claim that manually sending the draft will remove attribution unless verified.

## Privacy

Do not copy private message transcripts, recipient details or draft contents into tracked notes or reusable skill files. Keep any necessary task receipts local and ignored.
