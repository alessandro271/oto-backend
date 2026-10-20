## prerequisite — a zeendoc user and the cabinet id

Zeendoc has no API key: the connector signs in as a **Zeendoc user** (the email and password of the web application) on a **cabinet**, whose id is the segment after `armoires.zeendoc.com/` in the address bar (`armoires.zeendoc.com/<cabinet id>/`). A Zeendoc administrator creates a user dedicated to oto, with read rights on the binders to serve; the three values are pasted into oto.
- byo-only: no shared oto account — these are your documents, each organization sets its own
- **the user's rights bound what is visible**: a binder the user cannot search is refused
- read-only: the connector never uploads, re-indexes or deletes a document
- the "test connection" button signs in and lists the user's binders: no side effect

## usage — find, count and read documents

- "which binders and fields are there" → `zeendoc(op="binders")`
- "the exact spelling of a supplier" → `zeendoc(op="values", binder="coll_2", field="custom_t2", term="acm")`
- "this supplier's invoices since January" → `zeendoc(op="search", binder="coll_2", criteria=[{"field": "custom_t2", "operator": "EQUALS", "value": "ACME"}, {"field": "custom_d4", "operator": "ABOVE", "value": "2026-01-01"}])`
- "how many documents mention plasterboard" → `zeendoc(op="count", binder="coll_2", criteria=[{"field": "Plain_Text", "operator": "CONTAINS", "value": "plasterboard"}])`
- "the lines and prices of this invoice" → `zeendoc(op="text", binder="coll_2", res_id=1234)`
- "the PDF of this invoice" → `zeendoc(op="file", binder="coll_2", res_id=1234)`

## note — fields, pages, what is not served

- field names (`custom_t2`) come from `op="binders"`, labels are the cabinet's own; a list field (site code, document type) is filtered by the **id** of its choice
- a search returns at most 50 documents per page: page with `offset` up to `nbDocs`; `stats` sums the amount fields over every match, not only the page
- `text` is Zeendoc's own extraction (OCR), laid out as on the page; `file` returns the PDF as its extracted text plus a short-lived link to the file
- **not served**: upload, index updates, deletion, saved searches, document history
