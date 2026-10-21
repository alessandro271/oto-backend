## prerequisite — connect with your Microsoft 365 account

on the "OneNote" card, click **Authorize OneNote** and choose your work account. Nothing else to install or register. The account itself is the "Microsoft 365 account" connector: OneNote borrows it and only adds its own permission (read and write the notebooks you can reach).
- the agent acts **with your rights**: your notebooks, and those of your teams and SharePoint sites that you can open; each person in the org connects their own account
- no administrator approval is needed for OneNote itself — but ⚠️ **many organizations require a Microsoft 365 administrator to authorize oto a first time.** If Microsoft shows "admin approval required", an administrator approves oto once for the whole organization (`microsoft_admin_consent`, see the "Microsoft 365 account" connector)
- an account already linked for another Microsoft service only needs to authorize this one; an account that has not authorized OneNote is refused by the tools, naming this card

## usage — find, read, write

`onenote` reads (`op=notebooks|sections|pages|page`), `onenote_act` writes (`op=create_page|update_page|create_section|create_notebook`), under the account the call names (`_account=`), otherwise the default one.
- "what is in my notebooks" → `onenote()` (the notebooks), then `onenote(op="sections", notebook_id=…)`, then `onenote(op="pages", section_id=…)`
- "the notebook of the Sales team" → the team's `id` from `teams_spaces` is its group: `onenote(group_id=…)`, and pass the same `group_id` on every following call
- "read the meeting notes of Monday" → `onenote(op="pages", section_id=…, title="Monday")`, then `onenote(op="page", page_id=…)` (plain text)
- "write today's minutes" → `onenote_act(op="create_page", section_id=…, title="Minutes 21/10", body="…markdown…")`
- "add the decisions at the end of that page" → `onenote_act(op="update_page", page_id=…, body="…")`

## note — what is misleading

- every id is valid only **in the place it came from**: a page of a team notebook is not found without its `group_id` (or `site_id`) — pass it again
- listing the pages without `section_id` fails once there are many sections: go through a section
- `title` filters on the page title only, not on its text
- to change a given paragraph, read the page with `format="html"`: its elements carry the ids that `target` names; OneNote rewrites the HTML it is given (layout, styles)
- a page just created can take a few seconds to show up in a listing
- what is written in a shared notebook is seen by everyone who shares it

## note — scope

notebooks, sections and pages you can open: read, create a page, add to or change one, create a section or a notebook. No deletion (remove a page in OneNote), no section groups creation, no search in the page text. Files are the "SharePoint & OneDrive" connector, the notebooks' teams the "Teams" one.
