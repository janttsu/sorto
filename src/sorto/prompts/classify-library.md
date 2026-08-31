LIBRARY SCHEME (dest_scheme=library). Use this layout — it matches a real long-lived archive:

Top-level (PascalCase, existing names — do not invent new top-level buckets):
- Photos/YYYY/YYYY-MM/            camera photos, phone pics, heic/jpg/png (not screenshots)
- Photos/YYYY/YYYY-MM-Screenshots/  Screenshot*, Screen Shot*
- Photos/YYYY/YYYY-MM-Signal/       signal-*.jpg
- Photos/YYYY/YYYY-MM-WhatsApp/     IMG-*-WA*, VID-*-WA*
- Videos/YYYY/YYYY-MM/
- Audio/YYYY/YYYY-MM/
- Documents/<Johnny.Decimal id>/  only when the filename clearly matches an ID below
- Documents/_quarry/YYYY/YYYY-MM/ all other documents (pdf/odt/doc/rtf/xls/csv)
- Code/YYYY/YYYY-MM/              source files, not git internals
- Emails/YYYY/                    .eml .mbox
- Archives/YYYY/YYYY-MM/          zip/tar/7z/rar (do not unpack)
- TempAndCache/YYYY/YYYY-MM/      caches, tmp, thumbnails, .DS_Store, *.tmp
- Backup-Garbage/                 firmware .ucode/.fw, dpkg debris, maildir crumbs, .pyc/.ko/.class
- To-Annex/Large-Files/YYYY/YYYY-MM/  files ≳ 80 MB (video/archives)
- GitRepositories/ OS-Extracts/ WebsiteBackups/ Wepardi/ Int2000/ Media/ — do not re-home files already under these

Johnny.Decimal (Documents only, filename evidence required):
- 10-19 Life admin/13 Money/13.11 Invoices and receipts — lasku, invoice, kuitti, receipt
- 10-19 Life admin/13 Money/13.31 Aktia statements — tiliote, aktia
- 10-19 Life admin/13 Money/13.21 Tax — vero, verohallinto
- 10-19 Life admin/13 Money/13.41 Kela
- 10-19 Life admin/11 Identity and legal/11.12 Living will — hoitotahto
- 10-19 Life admin/11 Identity and legal/11.11 Identity — passi, luottokielto
- 10-19 Life admin/17 Scans inbox/17.01 Unnamed scans — Scan*, SwiftScan, CCF_
- 20-29 Work papers/22 Vendor invoices/22.11 Hetzner or 22.12 Cloudflare
- 20-29 Work papers/24 Account exports/24.11 int2000 or 24.12 wepardi
If unsure about a PDF/ODT, dest is Documents/_quarry/YYYY/YYYY-MM/<filename> (never invent a new JD id).

Year/month from the packet mtime. Do not invent event-name folders except Screenshots, Signal, WhatsApp.
Keep original filename. Keep original extension.
dest_rel examples:
  Photos/2024/2024-08/IMG_1234.jpg
  Documents/_quarry/2023/2023-04/notes.pdf
  Documents/10-19 Life admin/13 Money/13.11 Invoices and receipts/2023/fortum-lasku.pdf
  TempAndCache/2022/2022-03/.DS_Store
