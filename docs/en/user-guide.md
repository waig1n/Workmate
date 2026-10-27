# User guide

[English](../en/user-guide.md) | [简体中文](../zh-CN/user-guide.md)

Workmate brings Agents, tools, and files into one workspace. Choose an Agent, describe the outcome you need, approve sensitive actions when prompted, and review or download the results.

## Start a task

1. Select an Agent you can use, or create a personal Agent.
2. Choose an available model and describe your goal. Attach any files the Agent needs.
3. Send your message to create the conversation. Follow tool calls in Activity and review answers and artifacts in the main panel.

For example, ask an Agent to summarize uploaded material, analyze a spreadsheet, or prepare a report. The Agent needs the relevant tools and file-processing capabilities, configured or shared by an administrator.

Tasks in a conversation run in order; additional tasks queue while it is busy. You can search, rename, and delete conversations. Wait for active tasks to finish, or stop them, before deleting a conversation.

## Choose and manage Agents

Personal Agents belong to their owner. Creating, editing, sharing, or deleting organization and department Agents requires permission in the relevant scope. Agent management, execution, and access to bound Skills, MCP connections, and knowledge bases are checked separately. See [administration and security](administration.md).

Available models depend on the platform configuration and the Agent's allowed choices. Uploaded attachments become working files for the conversation.

## Approve actions and stop tasks

Choose an approval mode before sending a task. The task retains that mode during execution. When confirmation is needed, the approval card shows the tool, arguments, and available decisions. Review these before approving or denying the action. Workmate checks current permissions again before resuming.

Full Access still respects Agent deny rules and mandatory confirmation for platform management operations.

Stop begins a cancellation process. The final status is updated after execution and cleanup finish. Check the task status in the workspace: an accepted Stop request can still leave an external tool finishing its shutdown.

## Review and download artifacts

The artifact panel collects files delivered by the Agent. Common text, code, structured data, images, and PDFs support previews; other formats are available for download. Downloads require account authorization, and HTML previews run in an isolated context.

When authoring an Agent or Skill, use `present_files` to declare deliverables. The default delivery mode also detects changes under `output/`; strict mode accepts only explicitly declared files. Recovery of working files that have not been delivered depends on [file checkpoint configuration](runtime.md).

## Resolve unavailable resources

A disabled, revoked, or out-of-scope Skill, MCP connection, or knowledge base can block a new task. Review resource diagnostics, correct bindings, or ask an administrator to restore access before retrying. Existing conversation history does not preserve revoked execution rights.

## Schedule work

Choose an Agent, describe the task, and set a one-time, daily, or weekly schedule with a time zone. Schedules support pausing, resuming, and execution history. Each occurrence creates a separate conversation and checks current account and resource permissions.

Scheduled tasks run unattended and reject actions that require human approval. With a channel binding configured, results can be sent to the owner's IM direct messages. Failed notifications can be retried without rerunning the task. See [integrations](integrations.md) for setup.

Long-term memory for the same user and Agent can span conversations; starting a new conversation does not clear that memory.
