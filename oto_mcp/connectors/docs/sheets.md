## prerequisite — authorize Google Sheets on your Google account

from this card, click **connect**: Google asks you to authorize **Google Sheets only** (`spreadsheets` scope) on the account you choose. the Google account itself (address, token) is carried by the **Google account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- several Google accounts: each tool acts on the one the call names with `_account=<email>` (the tools' `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; an unknown address is refused, never replaced by another, and the response names the account that served (`_account`); `google_accounts` says which ones have authorized Google Sheets
- an account that has not authorized Google Sheets is refused by the tools, naming this card — come back here to authorize it

## usage — read and write a spreadsheet

`sheets_spreadsheet(op=metadata|read|write|clear|add_sheet|rename_sheet|delete_sheet|delete_rows|number_format)` and `sheets_create(title, sheets=[…])` — under the chosen account.
- "read the `leads` tab of this sheet"
- "write these rows after the end of the `suivi` tab"
- "create a spreadsheet `Prospects octobre` with the tabs `Hypothèses` and `P&L`" — name the tabs at creation: without `sheets`, Google creates one tab named in the account's locale (`Feuille 1`…), and a write to another tab name fails
- "add a tab `Q4`", "rename `Feuille 1` to `Synthèse`", "format `'P&L'!B2:D` as `#,##0.00`"
- `delete_sheet` and `delete_rows` are irreversible: the call is a dry run by default and says what would go ; `dry_run=false` deletes
- to send the workbook as a file (every tab), `drive_file(op=export, format=xlsx)` returns a signed URL
