# Events Drive Index

Use the Events index as the primary search surface for event docs.

Core files:
- DB: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/events.db`
- Tree: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/tree.txt`
- Stats: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/stats.json`
- Extracted text dir: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/extracted`
- Google-native export cache: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-google-export`

Primary CLI:
- `python3 scripts/events_drive_index.py`
- npm aliases are available in `package.json`

Useful commands:

```bash
npm run events-index:stats
npm run events-index:tree -- --max-depth 3 --sort count
npm run events-index:search -- "partner package"
npm run events-index:search -- "master planning document" --semantic
python3 scripts/events_drive_index.py show --file-id 'file:...'
python3 scripts/google_native_export.py export-pointer '/abs/path/to/file.gdoc'
python3 scripts/google_native_export.py export-event-folder '/abs/path/to/event-folder'
python3 scripts/google_native_export.py export-doc-id 'DOC_ID' --kind spreadsheets --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_drive_search.py "hackathon master checklist" --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_export_inspect.py search "resource blast"
python3 scripts/google_export_inspect.py show 'DOC_ID' --lines 60
python3 scripts/google_doc_copy.py 'DOC_ID_OR_URL' --title 'Copy - Working Draft' --auth-if-missing
python3 scripts/google_doc_tabs.py 'DOC_ID_OR_URL' --auth-if-missing
python3 scripts/md_to_google_doc.py '/abs/path/to/local.md' --title 'Standalone Draft' --auth-if-missing
python3 scripts/google_doc_patch.py replace-body 'DOC_ID_OR_URL' '/abs/path/to/local.md' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_doc_patch.py append-body 'DOC_ID_OR_URL' '/abs/path/to/appendix.md' --tab-title 'Event Page Copy' --auth-if-missing
python3 scripts/google_doc_patch.py replace-text 'DOC_ID_OR_URL' '/abs/path/to/replacements.json' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py set-range 'SHEET_ID_OR_URL' "'Checklist'!A1:C3" '/abs/path/to/values.json' --auth-if-missing
python3 scripts/google_sheet_edit.py upsert-rows 'SHEET_ID_OR_URL' 'Checklist' '/abs/path/to/rows.json' --key-column 'Task' --extend-header --auth-if-missing
```

What is indexed well:
- filenames
- paths
- Google pointer metadata and URLs
- extracted text from text-ish local files
- extracted text from `docx`, `pptx`, `xlsx`
- vector embeddings over extracted chunks
- exported Google-native bodies when they exist in the export cache

What it is especially good at in practice:
- finding the canonical template file
- finding the closest prior completed example
- finding sibling docs in the same event folder
- finding event-specific subfolders like `[POST-HACKATHON]`, `Partner Package`, `Winner Email`, `Master Planning`, `Run of Show`, `Proposal`, `Contract`

Recommended retrieval stack:
1. local events index for fast filename/path/example search
2. exported Google-native bodies for exact private content
3. semantic retrieval over extracted/exported text for fuzzy matching
4. Google Drive API lexical search as a fallback when the local cache missed a file or the export has not happened yet

What is not fully indexed by default:
- full remote Google Doc / Sheet / Slides bodies until you export them
- large scanned PDF OCR unless explicitly extracted later

Critical implication:
- exported Google-native bodies are searchable only after they exist on disk
- if you add a new export and want it to influence the main events DB immediately, rebuild or refresh the index against the export cache
- for Google Sheets, inspect the exported tab CSVs and `indexed.txt`; many event checklists hide the real action items there
- Google Drive search can search inside file contents, but it is lexical, not semantic; use it to find candidates, then export/read the winning docs locally
- if you already exported the doc and need to inspect the actual body quickly, use `google_export_inspect.py` instead of re-exporting blindly

Critical rule:
- A `.gdoc`, `.gsheet`, or `.gslides` file is not the document body.
- It is a local pointer containing a doc ID.
- If you need the actual private content, run the Google-native exporter first.

Typical search targets:
- `partner package`
- `master planning document`
- `winner email`
- `partner handbook`
- `proposal`
- `contract`
- `event page`
- `social copy`
- `judge`
- `venue`

Template / example search rule:
- Do not stop at the first hit.
- Search once for the template artifact name.
- Search again for the same artifact inside a specific event folder.
- Search a third time for prior finished examples across other events.

For participant-facing docs, add a fourth search:
- search inside the event's exported planning doc body for embedded draft sections before relying on generic examples
- if ownership, rationale, or recent decision context is still unclear after the docs, search Granola before guessing

Typical useful searches:

```bash
npm run events-index:search -- "partner package"
npm run events-index:search -- "winner email"
npm run events-index:search -- "master planning"
npm run events-index:search -- "run of show"
npm run events-index:search -- "proposal"
npm run events-index:search -- "contract"
```

When handling an inbound ask:
- search for the artifact family first
- then search for the specific event
- then search for the nearest finished example

For details-page / participant-resources asks:
- export the event's planning doc if needed
- search that export for `Participant Resources`, `Discord`, `Problem Statements`, `Provided Resources`, `Judging`, `Prizes`, `1st Blast`, `2nd Blast`, or `Details`
- inspect surrounding comments/placeholders for unresolved operator notes
- if there is a checklist or run-of-show sheet for the same event, export that too and inspect the relevant tabs before finalizing the draft
- after that, check one or two shipped live `PlatformEvent.details` pages via the platform DB so you normalize against what CV actually published, not just what a planning doc drafted

Example:
- first: `npm run events-index:search -- "partner package"`
- then: `npm run events-index:search -- "OpenAI Codex partner package"`
- then use `event_ops_brief.py` to connect that to nodes and next actions

Interpretation rule:
- If a result has `google_url`, return that to the user alongside the local path.
- If you only need path discovery, the events index is enough.
- If you need exact private body text from a Google-native file, export it first and then reason from the cached export.
- If one account token cannot read the file, retry with the alternate token path for the account that actually has access.
- If the local pointer file is empty or broken, export directly by doc ID / URL instead of treating the file as unavailable.
- If you need to inspect the exported doc body or checklist tabs quickly, use `google_export_inspect.py search` or `show` before doing more broad search.
- If you need to turn a local Markdown approval draft into a standalone Google Doc, use `md_to_google_doc.py` after upgrading the Google token with write scopes.
- If you need to patch an existing Google Doc template in place, inspect tabs first and then use `google_doc_patch.py` for body replacement, appends, or placeholder replacement.
- If you need to safely experiment on an existing Google Doc, copy it first with `google_doc_copy.py`.
- If the target Google Doc is multi-tab, use `google_doc_tabs.py` and scope edits with `--tab-title` or `--tab-id`.
- If you need to update a checklist, judge tracker, run-of-show, or template sheet in place, use `google_sheet_edit.py` for range writes, row appends, and keyed upserts.
- If you need workflow meaning, move to node search next.
- If you need to actually make or update the artifact, also return:
  - the best matching template
  - the best matching prior completed example
  - the event folder they belong to
