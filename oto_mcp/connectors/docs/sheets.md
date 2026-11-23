## prerequisite — authorize Google Sheets on your Google account

from this card, click **connect**: Google asks you to authorize **Google Sheets only** (`spreadsheets` scope) on the account you choose. the Google account itself (address, token) is carried by the **Google account** connector — one account can authorize several services, one service at a time, without re-authorizing the others.
- several Google accounts: each tool acts on the one the call names with `_account=<email>` (the tools' `account=<email>` is the same choice — two different addresses are refused), otherwise the one the project pins, otherwise the default; an unknown address is refused, never replaced by another, and the response names the account that served (`_account`); `google_accounts` says which ones have authorized Google Sheets
- an account that has not authorized Google Sheets is refused by the tools, naming this card — come back here to authorize it

## usage — read and write a spreadsheet

`sheets_spreadsheet(op=…)` for the cells, tabs, rows and columns, their formatting and the data of a spreadsheet, `sheets_chart(op=list|add|delete)` for its charts, and `sheets_create(title, sheets=[…])` — under the chosen account.
- "read the `leads` tab of this sheet"
- "write these rows after the end of the `suivi` tab"
- "create a spreadsheet `Prospects octobre` with the tabs `Hypothèses` and `P&L`" — name the tabs at creation: without `sheets`, Google creates one tab named in the account's locale (`Feuille 1`…), and a write to another tab name fails
- "add a tab `Q4`", "rename `Feuille 1` to `Synthèse`", "format `'P&L'!B2:D` as `#,##0.00`"
- "build the follow-up file in the layout of my template" — `copy_sheet` copies a tab (values, widths, colours, frozen rows, drop-downs) from the template into the produced file, the template only read; then write the data into the copy
- "grey and wrap the header, freeze it, widen columns B to D, a drop-down `Payé / À relancer` on F" — `format`, `freeze`, `column_width`, `dropdown`
- an .xlsx already formatted by a service becomes a Google Sheet as is with `drive_file(op=upload, source=…, convert=true)`
- "delete columns C to E", "insert 3 rows before row 10", "hide columns H:J" — `delete_columns`, `insert_rows`/`insert_columns`, `hide`/`show`
- "sort by amount, largest first", "put a filter on the table", "replace `En cours` with `Relancé`" — `sort`, `filter`, `find_replace` (formulas untouched)
- "put `=B2*C2` on every row of column F", "mark F2:F120 `À relancer`" — `fill(range, value)`: one value or one formula on a whole range in one call, the formula's references shifting row by row
- "borders on the table, merge the title cells, negatives in red, fit the columns" — `format` with `borders`, `merge`, `conditional_format`, `auto_fit`
- "a column chart of cash-in by month, on the `Synthèse` tab" — `sheets_chart(op=add, range=…, type=column, anchor="'Synthèse'!H2")`: first row = headers, first column = categories, each other column a series
- `delete_sheet`, `delete_rows` and `delete_columns` are irreversible: the call is a dry run by default and says what would go ; `dry_run=false` deletes ; `merge` keeps only the top-left value
- to send the workbook as a file (every tab), `drive_file(op=export, format=xlsx)` returns a signed URL
