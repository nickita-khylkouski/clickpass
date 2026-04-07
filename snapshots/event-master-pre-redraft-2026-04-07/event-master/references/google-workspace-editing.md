# Google Workspace Editing

Use this reference when the task requires creating, copying, patching, or updating Google Docs or Google Sheets for event operations.

This is now a core event-ops surface, not an edge tool.

If the task is mainly about Google Docs / Sheets / Slides execution, use the dedicated `event-google-workspace` skill alongside `event-master`.
Use this file as the short summary, not the full execution skill.

Primary scripts:
- `python3 scripts/md_to_google_doc.py`
- `python3 scripts/google_doc_copy.py`
- `python3 scripts/google_doc_tabs.py`
- `python3 scripts/google_doc_patch.py`
- `python3 scripts/google_sheet_edit.py`
- `node scripts/google_docs_mcp_cli.mjs`

Underlying helpers:
- `python3 scripts/google_markdown.py` for markdown parsing/rendering
- `python3 scripts/google_native_export.py` for auth/export/read surfaces
- installed upstream MCP server: `@a-bonus/google-docs-mcp`

## What These Tools Are Good At

Google Docs:
- create a standalone Google Doc from local Markdown
- copy an existing Google Doc before experimenting or drafting
- inspect tabbed docs and find the right tab ID/title
- replace one tab body with structured markdown
- append structured markdown to one tab
- replace placeholders/text globally or within one tab
- use richer upstream docs verbs through the local MCP wrapper when you need comments, images, page breaks, or broader read modes

Google Sheets:
- inspect workbook/tab state
- set a range directly
- append rows
- keyed row upserts for checklists / trackers / run-of-show sheets

Formatting that is currently supported well in Google Docs flows:
- headings
- paragraphs
- bullets / numbered lists
- bold
- italic
- links
- inline code
- code blocks
- markdown tables as real Google Docs tables

Formatting and layout that should still be treated cautiously:
- deeply nested list fidelity
- merged table cells
- floating images
- pixel-perfect layout recreation
- highly designed docs where spacing / page layout is the product

Interpretation rule:
- these tools are good for event-ops docs, planning docs, participant guides, judge notes, details-page drafts, and approval docs
- they are not a full visual layout engine for every rich Google Docs feature

## Decision Rules

Use this order when deciding what to do:

1. If the user wants a reviewable draft and not a live official doc edit:
- keep the Markdown file local
- create a standalone Google Doc with `md_to_google_doc.py`

2. If the user wants to start from an existing Google Doc structure/template:
- copy it first with `google_doc_copy.py`
- inspect tabs with `google_doc_tabs.py`
- patch the copy, not the original

3. If the target is a multi-tab Google Doc:
- always inspect tabs first
- use `--tab-title` or `--tab-id`
- do not assume the first tab is the right tab

4. If the task is a simple placeholder replacement:
- use `google_doc_patch.py replace-text`

5. If the task is a structured section rewrite:
- use `google_doc_patch.py replace-body`

6. If the task is adding a new section to an existing doc:
- use `google_doc_patch.py append-body`

7. If the target is really operational data in rows/cells/tabs:
- do not force it through Docs
- use `google_sheet_edit.py`

8. If the task needs broader upstream Google Docs / Sheets / Drive verbs:
- use `node scripts/google_docs_mcp_cli.mjs`
- prefer this especially for comments, images, page breaks, markdown reads, and upstream MCP tool coverage
- fall back to the Python CLIs for copy-first event workflows and for multi-tab surgical patching when they are the safer path

## Safety Rules

Default behavior:
- local markdown draft first
- standalone Google Doc for review second
- official event doc patch only when the user explicitly wants the live doc updated

Before mutating an existing Google Doc:
- verify the document ID / URL
- verify whether it is single-tab or multi-tab
- if multi-tab, target the exact tab
- if the doc is sensitive or canonical, copy it first unless the user explicitly asked for an in-place official change

For multi-tab planning docs:
- `replace-text` can be scoped to one tab
- `replace-body` and `append-body` can now target a specific tab safely
- do not run body edits on a multi-tab doc without `--tab-title` or `--tab-id`

## Canonical Workflows

### 1. Create a review doc from local Markdown

```bash
python3 scripts/md_to_google_doc.py '/abs/path/to/draft.md' \
  --title 'OpenAI Singapore Participant Guide Draft' \
  --auth-if-missing
```

Use when:
- drafting participant-facing copy
- sharing an approval doc
- you want clean Docs collaboration without touching the official source doc

### 2. Copy a Google Doc before testing or adapting

```bash
python3 scripts/google_doc_copy.py 'https://docs.google.com/document/d/FILE_ID/edit' \
  --title 'Copy - Smoke Test' \
  --auth-if-missing
```

Use when:
- experimenting on a planning doc
- adapting a prior-event template
- testing formatting or tab behavior

### 3. Inspect a tabbed planning doc

```bash
python3 scripts/google_doc_tabs.py 'DOC_ID_OR_URL' --auth-if-missing
```

Use when:
- the doc has tabs
- you need to patch `Prizes`, `Participant Resources`, `Event Page Copy`, etc.

### 4. Replace one tab body with structured markdown

```bash
python3 scripts/google_doc_patch.py replace-body 'DOC_ID_OR_URL' '/abs/path/to/prizes.md' \
  --tab-title 'Prizes' \
  --auth-if-missing
```

Use when:
- a whole tab/section should be regenerated from local Markdown

### 5. Append one section to a tab

```bash
python3 scripts/google_doc_patch.py append-body 'DOC_ID_OR_URL' '/abs/path/to/appendix.md' \
  --tab-title 'Event Page Copy' \
  --auth-if-missing
```

Use when:
- adding an approval appendix
- adding a new event note or checklist block

### 6. Replace exact placeholders or strings

```bash
python3 scripts/google_doc_patch.py replace-text 'DOC_ID_OR_URL' '/abs/path/to/replacements.json' \
  --tab-title 'Prizes' \
  --auth-if-missing
```

Example `replacements.json`:

```json
{
  "{{EVENT_NAME}}": "Global Codex Hackathon: Singapore",
  "{{DISCORD_INVITE}}": "https://discord.gg/example"
}
```

Use when:
- filling template placeholders
- applying small exact corrections

### 7. Update a checklist / tracker / run-of-show sheet

```bash
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py set-range 'SHEET_ID_OR_URL' "'Checklist'!A1:C3" '/abs/path/to/values.json' --auth-if-missing
python3 scripts/google_sheet_edit.py upsert-rows 'SHEET_ID_OR_URL' 'Checklist' '/abs/path/to/rows.json' \
  --key-column 'Task' --extend-header --auth-if-missing
```

Use when:
- the real system of record is a sheet, not a doc
- you are touching checklist status, owners, notes, dates, allocations, trackers

### 8. Use the installed Google Docs MCP wrapper

```bash
npm run events-index:google-docs-mcp -- doctor
npm run events-index:google-docs-mcp -- list-tools
npm run events-index:google-docs-mcp -- call-tool createDocument --args-json '{"title":"MCP Working Doc"}'
npm run events-index:google-docs-mcp -- call-tool readDocument --args-json '{"documentId":"DOC_ID","format":"markdown"}'
npm run events-index:google-docs-mcp -- call-tool appendMarkdown --args-json '{"documentId":"DOC_ID","markdown":"## New Section"}'
npm run events-index:google-docs-mcp -- call-tool addComment --args-json '{"documentId":"DOC_ID","startIndex":10,"endIndex":20,"content":"Review note"}'
```

Use when:
- you want the upstream MCP tool surface locally
- you need richer Docs verbs beyond the current Python CLIs
- you want a workspace-local wrapper that reuses the existing CV Google auth tokens

Notes:
- the wrapper auto-seeds auth from `.research/events-google-export/auth/token.json` or `token-cv.json`
- `doctor`, `list-tools`, `createDocument`, `readDocument`, `listTabs`, `appendText`, `appendMarkdown`, `insertPageBreak`, `addComment`, `listComments`, and `deleteFile` were live-tested
- some nested-tab targeted upstream tools are still weaker than the local Python tab-edit path, so prefer `google_doc_tabs.py` + `google_doc_patch.py` when precise tab surgery matters

## What Was Verified

These tools were live-tested on disposable Google Docs:
- single-tab create from markdown
- single-tab append
- single-tab replace-body with rich formatting
- markdown table import as a real Google Docs table
- multi-tab doc copy
- multi-tab `replace-body` scoped to one tab
- multi-tab `replace-text` scoped to one tab
- multi-tab `append-body` scoped to one tab
- verification that non-target tabs stayed unchanged
- MCP wrapper `doctor`
- MCP wrapper `list-tools`
- MCP wrapper `createDocument`
- MCP wrapper `readDocument`
- MCP wrapper `listTabs`
- MCP wrapper `appendText`
- MCP wrapper `appendMarkdown`
- MCP wrapper `insertPageBreak`
- MCP wrapper `addComment`
- MCP wrapper `listComments`
- MCP wrapper `deleteFile`

Local tests also cover:
- markdown rendering
- inline style preservation
- table parsing
- tab targeting helpers
- request construction and cleanup behavior

## Practical Limits

Use a DOCX-template workflow instead of direct Docs patching when:
- page layout is critical
- the document must look polished beyond normal Docs structure
- the artifact uses advanced tables or visual formatting that should not drift

Otherwise, prefer these Google-native tools for event ops because:
- they fit the actual CV workflow
- they let the agent work from local Markdown
- they keep review and live mutation separable
- they are now safe enough for tabbed planning docs when targeted correctly

## Recommended Event-Ops Pattern

For most event tasks:
1. write or update the local Markdown draft
2. if review is needed, create a standalone Google Doc
3. if the final destination is an existing Google Doc, copy it first unless the user explicitly wants a live in-place update
4. inspect tabs
5. patch only the intended tab
6. verify the resulting tab content before declaring it done
