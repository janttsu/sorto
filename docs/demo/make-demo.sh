#!/usr/bin/env bash
# Create GENERIC demo data for sorto screenshots. Runs inside the throwaway demo VM.
#
#   make-demo.sh            create whatever is missing (idempotent)
#   make-demo.sh --reset    wipe ~/demo, the demo rules and sorto's state, then recreate
#
# Everything here is fictional: "Alex Example", Northwind Energy, example.com /
# .example domains. Needs: python3, ImageMagick, exiftool.
set -euo pipefail

DEMO="${DEMO_DIR:-$HOME/demo}"
ARCHIVE="$DEMO/Archive"
INBOX="$DEMO/Inbox"
MESSY="$DEMO/Messy"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/sorto"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/sorto"

if [ "${1:-}" = "--reset" ]; then
  # Only ever run inside the throwaway VM: refuse on anything that is not the demo user.
  [ "$(id -un)" = "demo" ] || { echo "refusing --reset: not the demo user" >&2; exit 1; }
  rm -rf "$DEMO" "$STATE" "$CONF/rules.md"
fi

IM=convert
command -v magick >/dev/null 2>&1 && IM=magick

# ---------------------------------------------------------------- helpers
mkd() { for d in "$@"; do mkdir -p "$d"; done; }

# write FILE from stdin unless it already exists
put() { [ -e "$1" ] && { cat >/dev/null; return 0; }; mkdir -p "$(dirname "$1")"; cat >"$1"; }

# make_pdf OUT < text  -- minimal one-page text PDF (Helvetica) that pdftotext can read
make_pdf() {
  [ -e "$1" ] && { cat >/dev/null; return 0; }
  mkdir -p "$(dirname "$1")"
  local text; text="$(cat)"
  PDF_TEXT="$text" python3 - "$1" <<'PY'
import os, sys
lines = os.environ["PDF_TEXT"].rstrip("\n").split("\n")
def esc(s): return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
ops = ["BT", "/F1 11 Tf", "14 TL", "56 790 Td"]
for i, ln in enumerate(lines):
    if i == 0:
        ops += ["/F2 16 Tf", f"({esc(ln)}) Tj", "/F1 11 Tf", "T*", "T*"]
    else:
        ops += [f"({esc(ln)}) Tj", "T*"]
ops.append("ET")
stream = "\n".join(ops).encode("latin-1")
objs = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
    b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
    b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
]
out = bytearray(b"%PDF-1.4\n")
offs = []
for n, body in enumerate(objs, 1):
    offs.append(len(out))
    out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
xref = len(out)
out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
for o in offs:
    out += f"{o:010d} 00000 n \n".encode()
out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
open(sys.argv[1], "wb").write(out)
PY
}

# make_photo OUT "SIGN TEXT" "YYYY:MM:DD HH:MM:SS" LAT LON  -- synthetic street photo with EXIF/GPS
make_photo() {
  local out="$1" sign="$2" when="$3" lat="$4" lon="$5"
  [ -e "$out" ] && return 0
  mkdir -p "$(dirname "$out")"
  $IM -size 1600x1200 gradient:'#7fb2e5-#f3e7d3' \
    -fill '#d9c7a3' -draw 'rectangle 0,420 1600,1000' \
    -fill '#c8b48d' -draw 'rectangle 0,380 1600,430' \
    -fill '#4a5560' -draw 'rectangle 60,520 200,760 rectangle 300,520 440,760 rectangle 540,520 680,760 rectangle 780,520 920,760 rectangle 1020,520 1160,760 rectangle 1260,520 1400,760' \
    -fill '#2f3338' -draw 'rectangle 0,1000 1600,1200' \
    -fill '#e8e2d0' -draw 'rectangle 0,1080 1600,1090' \
    -fill '#1e3a8a' -stroke '#ffffff' -strokewidth 10 -draw 'roundrectangle 470,190 1130,360 18,18' \
    -stroke none -fill '#1a7f37' -draw 'rectangle 470,160 1130,190' \
    -fill white -font DejaVu-Sans-Bold -pointsize 72 -gravity north -annotate +0+235 "$sign" \
    -quality 88 "$out"
  exiftool -q -overwrite_original \
    -Make=ACME -Model="Pocket Camera 5" \
    -DateTimeOriginal="$when" -CreateDate="$when" \
    -GPSLatitude="$lat" -GPSLatitudeRef=N -GPSLongitude="$lon" -GPSLongitudeRef=E \
    "$out"
}

# ---------------------------------------------------------------- JD target (Archive)
mkd "$ARCHIVE/00-09 System/00 System/00.00 JDex" \
    "$ARCHIVE/00-09 System/00 System/00.01 Inbox" \
    "$ARCHIVE/10-19 Life admin/11 Money/11.01 Inbox" \
    "$ARCHIVE/10-19 Life admin/11 Money/11.11 Bills/2024" \
    "$ARCHIVE/10-19 Life admin/11 Money/11.11 Bills/2025" \
    "$ARCHIVE/10-19 Life admin/11 Money/11.12 Bank statements" \
    "$ARCHIVE/10-19 Life admin/11 Money/11.13 Taxes" \
    "$ARCHIVE/10-19 Life admin/12 Health/12.11 Medical records" \
    "$ARCHIVE/10-19 Life admin/12 Health/12.12 Insurance" \
    "$ARCHIVE/10-19 Life admin/14 Travel/14.11 Paris 2025" \
    "$ARCHIVE/10-19 Life admin/14 Travel/14.12 Lisbon 2024" \
    "$ARCHIVE/20-29 Work/21 Projects/21.11 Website redesign" \
    "$ARCHIVE/20-29 Work/21 Projects/21.12 Quarterly reports" \
    "$ARCHIVE/50-59 Media/51 Photos/51.11 Photos" \
    "$ARCHIVE/50-59 Media/51 Photos/51.12 Screenshots" \
    "$ARCHIVE/90-99 Archive/93 Email/93.11 Email"

put "$ARCHIVE/00-09 System/00 System/00.00 JDex/JDex.md" <<'EOF'
# JDex

- `00.01` Inbox — anything not sorted yet
- `11.11` Bills — utility bills, invoices and receipts, one folder per year
- `11.12` Bank statements — monthly statements from Example Bank
- `11.13` Taxes — tax returns and tax office letters
- `12.11` Medical records — doctor's notes, lab results, prescriptions
- `12.12` Insurance — health and travel insurance policies
- `14.11` Paris 2025 — trip to Paris in June 2025: bookings, tickets, photos
- `14.12` Lisbon 2024 — trip to Lisbon in 2024
- `21.11` Website redesign — the example.com website redesign project
- `21.12` Quarterly reports — work reports
- `51.11` Photos — everyday photos that belong to no trip or project
- `51.12` Screenshots — screen captures
- `93.11` Email — saved email messages that fit nowhere else
EOF

# ---------------------------------------------------------------- source inbox
mkd "$INBOX"

make_pdf "$INBOX/document(3).pdf" <<'EOF'
Northwind Energy - Electricity Invoice
Northwind Energy Ltd, 1 Harbour Road, Example City
Invoice number: NE-2025-004817        Invoice date: 2025-03-05
Customer: Alex Example, 12 Sample Street, Example City
Customer number: 0048213
Billing period: 2025-02-01 - 2025-02-28
Electricity consumption: 412 kWh x 0.1650 EUR = 67.98 EUR
Standing charge: 4.90 EUR
Total due: 72.88 EUR
Due date: 2025-03-20
Pay to IBAN XX00 0000 0000 0000 00, reference 48213 0325
Questions? billing@northwind.example
EOF

make_pdf "$INBOX/statement_feb.pdf" <<'EOF'
Example Bank - Account Statement
Account holder: Alex Example
Account: XX00 1234 5678 9012 34 (current account)
Statement period: 2025-02-01 to 2025-02-28
Opening balance: 2 340.15 EUR
2025-02-03  Salary, Example Corp            +3 120.00
2025-02-07  Grocery Market                     -84.20
2025-02-14  Northwind Energy                   -69.10
2025-02-21  Rent, Sample Housing Ltd          -950.00
Closing balance: 4 356.85 EUR
Example Bank plc, www.bank.example
EOF

make_pdf "$INBOX/scan0007.pdf" <<'EOF'
Hotel Example Louvre - Booking Confirmation
Booking reference: HEL-77120
Guest: Alex Example
Arrival: Thursday 2025-06-12    Departure: Monday 2025-06-16 (4 nights)
Room: Double room, city view, breakfast included
Address: 99 Rue de l'Exemple, 75001 Paris, France
Total paid: 612.00 EUR (card ending 4242)
Free cancellation until 2025-06-05.
See you in Paris!
EOF

put "$INBOX/note.txt" <<'EOF'
Riverside Health Clinic
22 Park Lane, Example City

Date: 2025-04-08
Patient: Alex Example (born 1990-01-01)

Medical certificate

The patient was examined today for acute bronchitis and is unfit for work
from 2025-04-08 to 2025-04-11 inclusive. Prescribed rest and fluids;
follow-up only if symptoms persist after one week.

Dr. Jamie Doe, general practitioner
EOF

put "$INBOX/kickoff notes.md" <<'EOF'
# Website redesign - kickoff meeting

Date: 2025-05-06
Attendees: Alex, Sam, Robin (design), Kim (content)

## Decisions
- New site for example.com launches in September 2025
- Keep the current CMS, replace the theme
- Robin delivers wireframes for the home and pricing pages by 2025-05-20

## Action items
- [ ] Alex: collect analytics for the top 20 pages
- [ ] Kim: content inventory spreadsheet
- [ ] Sam: staging environment on staging.example.com
EOF

put "$INBOX/msg-20250305-0932.eml" <<'EOF'
From: Northwind Energy <billing@northwind.example>
To: Alex Example <alex@example.com>
Subject: Your Northwind Energy invoice for February 2025
Date: Wed, 05 Mar 2025 09:32:14 +0100
Message-ID: <inv-0048213-202503@northwind.example>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

Hello Alex,

your electricity invoice NE-2025-004817 for February 2025 is ready.

Amount due: 72.88 EUR
Due date:   20 March 2025

You can view and pay the invoice in the customer portal at
https://my.northwind.example/invoices.

Kind regards,
Northwind Energy customer service
EOF

make_photo "$INBOX/IMG_4821.jpg" "Rue de Rivoli" "2025:06:14 15:22:10" 48.8606 2.3376

# screenshot-looking PNG: a fake app window with an order confirmation
if [ ! -e "$INBOX/Screenshot 2025-03-12 101422.png" ]; then
  $IM -size 1280x800 xc:'#e9edf2' \
    -fill '#ffffff' -stroke '#c5ccd6' -draw 'roundrectangle 140,80 1140,720 12,12' \
    -stroke none -fill '#dfe3e8' -draw 'roundrectangle 140,80 1140,130 12,12' \
    -fill '#ff5f57' -draw 'circle 170,105 178,105' -fill '#febc2e' -draw 'circle 195,105 203,105' \
    -fill '#28c840' -draw 'circle 220,105 228,105' \
    -fill '#555555' -font DejaVu-Sans -pointsize 18 -annotate +520+112 'shop.example - Order' \
    -fill '#1a7f37' -font DejaVu-Sans-Bold -pointsize 40 -annotate +200+220 'Order confirmed' \
    -fill '#222222' -font DejaVu-Sans -pointsize 24 \
    -annotate +200+290 'Order #12345 - 12 March 2025' \
    -annotate +200+340 '1 x USB-C charger 65 W .......... 39.90 EUR' \
    -annotate +200+380 '1 x Laptop sleeve 14" ............ 24.50 EUR' \
    -annotate +200+440 'Total: 64.40 EUR (paid by card)' \
    -annotate +200+500 'Delivery to: Alex Example, 12 Sample Street' \
    -fill '#0969da' -annotate +200+580 'Track your parcel at track.shop.example' \
    "$INBOX/Screenshot 2025-03-12 101422.png"
fi

# ---------------------------------------------------------------- user rules
if [ ! -e "$CONF/rules.md" ]; then
  mkdir -p "$CONF"
  cat >"$CONF/rules.md" <<'EOF'
<!-- sorto rules for the demo archive. Comments are not sent to the model. -->

- Emails from billing@northwind.example are bills: file them in 11.11, in the year folder of the invoice.
- Screenshots always go to 51.12, never to 51.11.
- Everything about the June 2025 Paris trip (bookings, tickets, photos taken in Paris) goes to 14.11.
EOF
fi

# ---------------------------------------------------------------- messy tree for "reorganize in place"
mkd "$MESSY/00-09 System/00 System/00.00 JDex" \
    "$MESSY/10-19 Life admin/11 Money/11.01 Inbox" \
    "$MESSY/10-19 Life admin/11 Money/11.11 Bills/2025" \
    "$MESSY/10-19 Life admin/11 Money/11.12 Bank statements" \
    "$MESSY/10-19 Life admin/12 Health/12.11 Medical records" \
    "$MESSY/10-19 Life admin/14 Travel/14.11 Paris 2025" \
    "$MESSY/50-59 Media/51 Photos/51.11 Photos" \
    "$MESSY/50-59 Media/51 Photos/51.12 Screenshots"

put "$MESSY/00-09 System/00 System/00.00 JDex/JDex.md" <<'EOF'
# JDex

- `11.11` Bills — utility bills, invoices and receipts, one folder per year
- `11.12` Bank statements — monthly statements from Example Bank
- `12.11` Medical records — doctor's notes, lab results, prescriptions
- `14.11` Paris 2025 — trip to Paris in June 2025
- `51.11` Photos — everyday photos
- `51.12` Screenshots — screen captures
EOF

# correctly filed (should stay put)
make_pdf "$MESSY/10-19 Life admin/11 Money/11.11 Bills/2025/Northwind_invoice_2025-01.pdf" <<'EOF'
Northwind Energy - Electricity Invoice
Invoice date: 2025-01-06   Customer: Alex Example
Billing period: 2024-12-01 - 2024-12-31
Total due: 81.45 EUR   Due date: 2025-01-20
EOF

# correctly filed directly in its ID (reorganize at depth 0 should leave it alone)
make_pdf "$MESSY/10-19 Life admin/11 Money/11.12 Bank statements/statement_jan.pdf" <<'EOF2'
Example Bank - Account Statement
Account holder: Alex Example
Statement period: 2025-01-01 to 2025-01-31
Opening balance: 1 905.40 EUR    Closing balance: 2 340.15 EUR
Example Bank plc, www.bank.example
EOF2

# misfiled: a bill sitting in Photos
make_pdf "$MESSY/50-59 Media/51 Photos/51.11 Photos/invoice-0425.pdf" <<'EOF'
Northwind Energy - Electricity Invoice
Invoice number: NE-2025-006102        Invoice date: 2025-04-04
Customer: Alex Example
Billing period: 2025-03-01 - 2025-03-31
Total due: 70.12 EUR   Due date: 2025-04-20
EOF

# misfiled: a Paris photo sitting in the money inbox
make_photo "$MESSY/10-19 Life admin/11 Money/11.01 Inbox/IMG_5120.jpg" "Place Vendome" "2025:06:15 11:05:42" 48.8675 2.3294

# misfiled: a doctor's note among bank statements
put "$MESSY/10-19 Life admin/11 Money/11.12 Bank statements/lab results.txt" <<'EOF'
Riverside Health Clinic - Laboratory results
Patient: Alex Example        Sample taken: 2025-02-18
Haemoglobin 142 g/L (ref 134-167)   CRP < 3 mg/L   Glucose 5.1 mmol/L
All values within the reference range. - Dr. Jamie Doe
EOF

echo "demo data ready under $DEMO (rules: $CONF/rules.md)"
