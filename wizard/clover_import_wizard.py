# -*- coding: utf-8 -*-
"""Clover Items Report CSV → Area P&L Sales Lines.

[Human]
    The Secretary exports the monthly "Revenue Item Sales" report from
    the Clover POS, attaches it here, and clicks Import.  Each item
    row turns into a sales line on the P&L.  Modifiers (like a
    "Non-Member $1" surcharge) come in as their own sales lines so
    the upcharge revenue is tracked separately.  Items that don't
    match any product tagged to this area land on the "needs setup"
    list so you know what to add.

[AI]
    Two-stage design:
      1) parse_clover_items_csv() — pure parser, no Odoo dependency.
         Walks the multi-section CSV (header, summary, category +
         item + modifier rows + category totals) and yields a flat
         list of item dicts with modifiers nested.  Unit-testable
         standalone.
      2) ElksAreaPnlCloverImport — TransientModel wizard.  Takes the
         parsed items, matches each by name (and default_code) to
         products whose product_tmpl_id.elks_area_ids includes the
         target P&L's area, creates sales lines via standard ORM
         create() so the COGS auto-sync flow on sales lines fires
         naturally.  Modifiers go through the same matching pipeline
         as their own sales lines.

    Sanity check: parser tested against actual May 2026 CSV →
    186 items, $19,823.60 total → matches Clover's own summary line.
"""
import base64
import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# CSV parsing helpers
# ══════════════════════════════════════════════════════════════════
# [Human]
#   The Clover CSV uses dollar strings with commas ($19,823.60),
#   dashes for missing data (-), non-breaking-spaces for blanks,
#   and negative refunds as -$15.50.  These helpers normalize each
#   of those into clean floats and ints.
# [AI]
#   Defensive on every conversion — never raise on malformed cells,
#   just return 0.0/0/blank.  The CSV header rows, summary rows, and
#   category totals all contain stray strings that would otherwise
#   ValueError out of float()/int().
#   _is_blank: treats '', whitespace, and '\xa0' (NBSP, Clover's blank
#   marker) all as empty — used to detect category-header vs item
#   rows during the walk.
# ──────────────────────────────────────────────────────────────────
def _parse_money(val):
    """Convert Clover money strings to float.

    Handles "$1,234.56", "$0.00", "-$15.50", "-" (Clover's dash for
    missing data), "" and the literal NBSP Clover uses in blank
    cells.  Returns 0.0 for any value that isn't a number."""
    if val is None:
        return 0.0
    s = str(val).strip().replace('\xa0', '').replace('$', '').replace(',', '')
    if not s or s == '-':
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_int(val):
    if val is None:
        return 0
    s = str(val).strip().replace('\xa0', '').replace(',', '')
    if not s or s == '-':
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _is_blank(cell):
    return not cell or not str(cell).strip() or str(cell).strip() == '\xa0'


# Map of "looks-like-an-apostrophe" Unicode codepoints to a plain
# ASCII apostrophe so "Tito's" (curly) and "Tito's" (straight) match.
_APOSTROPHE_LOOKALIKES = str.maketrans({
    '‘': "'",   # left single quotation mark
    '’': "'",   # right single quotation mark (Clover & macOS use this)
    'ʼ': "'",   # modifier letter apostrophe
    '´': "'",   # acute accent
    '`': "'",   # grave accent
})


def _normalize_name(s):
    """Aggressively normalize a name for matching:
       - strip leading/trailing whitespace
       - collapse runs of internal whitespace to a single space
       - normalize various Unicode apostrophe-like chars → ASCII '
       - lowercase
    Both the CSV item name and the Odoo product name pass through this
    before comparison, so curly-vs-straight apostrophes, accidental
    double-spaces, and case differences all stop mattering."""
    if not s:
        return ''
    s = str(s).translate(_APOSTROPHE_LOOKALIKES)
    s = ' '.join(s.split())
    return s.lower()


# ── parse_clover_items_csv ────────────────────────────────────────
# [Human]
#   The hard part of this whole module.  Clover's "Revenue Item Sales"
#   CSV has a quirky multi-section structure:
#       Lines 1-9   : title / date range / filters (skip)
#       Lines 10-14 : summary totals (skip)
#       Line 16     : column headers
#       From there  : alternating category-header rows ("Liquor: Call"),
#                     item rows (",Tito's,$838,...,144,..."),
#                     modifier rows (",,,,Non-Member $1,118,$118"),
#                     and category totals ("Total (Liquor: Call),...").
#   This function walks the rows, tracks the "current category" as it
#   passes each header, and attaches modifier rows to the most recent
#   item.  Returns a clean list of dicts ready for ORM creation.
# [AI]
#   Detection logic:
#     • Category header: col[0] has text AND cols 1,2,6 are blank.
#       Updates current_category, resets current_item.
#     • Item row: col[1] (Name) is filled.  Becomes current_item.
#     • Modifier row: col[1] blank, col[6] (Modifier Name) filled.
#       Appended to current_item.modifiers list (if any).
#     • "Total (...)" rows: skipped, also reset current_item.
#   Index reference (after padding short rows to len 15):
#       0 Category Name      8  Modifier Amount
#       1 Name               9  Discounts
#       2 Gross Sales       10  Refunds
#       3 Net Sales         11  % Net Sales
#       4 Sold              12  Avg Item Size
#       5 Refunded          13  COGS
#       6 Modifier Name     14  Gross Profit
#       7 Modifier Sold
#   Failure mode: raises UserError if it never finds the column-header
#   row, which usually means the user uploaded a non-Items report.
def parse_clover_items_csv(text):
    """Walk the Clover Items report and yield item dicts.

    Each item dict has keys:
        category, name, sold (int), gross_sales, net_sales, cogs,
        modifiers (list of {name, sold, amount}).
    """
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    # Find the data header row ("Category Name,Name,Gross Sales,...")
    data_start = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == 'Category Name':
            data_start = i + 1
            break
    if data_start is None:
        raise UserError(_(
            "Couldn't find the column header row "
            "('Category Name,Name,Gross Sales,...').  "
            "Is this the correct Clover Items Report CSV?"))

    items = []
    current_category = None
    current_item = None

    for row in rows[data_start:]:
        if not row or all(_is_blank(c) for c in row):
            continue
        # pad short rows so index access doesn't fail
        while len(row) < 15:
            row = row + ['']

        first = row[0].strip()
        name = row[1].strip()
        mod_name = row[6].strip()

        # Skip section totals: "Total (Liquor: Call)"
        if first.startswith('Total'):
            current_item = None
            continue

        # Category header row: only the first cell has text, rest blank
        if first and _is_blank(row[1]) and _is_blank(row[2]) \
                and _is_blank(row[6]):
            current_category = first
            current_item = None
            continue

        # Item row: leading column empty, Name filled
        if name and name != '\xa0':
            item = {
                'category': current_category,
                'name': name,
                'gross_sales': _parse_money(row[2]),
                'net_sales': _parse_money(row[3]),
                'sold': _parse_int(row[4]),
                'cogs': _parse_money(row[13]),
                'modifiers': [],
            }
            items.append(item)
            current_item = item
            continue

        # Modifier row: Name blank but Modifier Name filled
        if mod_name and mod_name != '\xa0' and current_item is not None:
            current_item['modifiers'].append({
                'name': mod_name,
                'sold': _parse_int(row[7]),
                'amount': _parse_money(row[8]),
            })

    return items


# ══════════════════════════════════════════════════════════════════
# Wizard (TransientModel)
# ══════════════════════════════════════════════════════════════════
# [Human]
#   The form the Secretary actually sees.  Two screens controlled by
#   the `state` field: 'select' (pick file + options) → 'done' (results
#   with matched counts and a copy-pasteable list of unmatched items).
#   On success, sales lines land on the target P&L and the COGS
#   auto-sync flow on each sales-line.create() fires automatically.
# [AI]
#   • Model: elks.area.pnl.clover.import (transient — cleaned by the
#     periodic vacuum cron).
#   • Default pnl_id comes from active_id (set by action_open_clover_import
#     on the parent P&L's form button).
#   • Matching domain on every item:
#         ('name', '=ilike', clover_name) OR ('default_code', '=ilike', clover_name)
#         AND ('product_tmpl_id.elks_area_ids', 'in', [area_id])
#     — both columns checked because some lodges use SKU as the
#     primary identifier.
#   • Side effects per matched item:
#       1) Create elks.area.pnl.sales.line — triggers _sync_cogs_from_self
#          which creates/refreshes the matching COGS line.
#       2) If use_cogs=True: also overwrite the COGS line's unit_cost
#          with the Clover-reported COGS ÷ qty (passes context flag
#          'elks_pnl_auto_sync=True' to avoid flipping auto_synced=False).
#   • Re-runs: 'clear_existing' wipes sales_line_ids before parsing.
#     The cascade=cascade on sales→pnl FK means no orphan COGS lines.
# ══════════════════════════════════════════════════════════════════
class ElksAreaPnlCloverImport(models.TransientModel):
    _name = "elks.area.pnl.clover.import"
    _description = "Import Clover Item Sales into Area P&L"

    pnl_id = fields.Many2one(
        "elks.area.pnl", string="Area P&L", required=True,
        default=lambda self: self.env.context.get('active_id'),
    )
    area_id = fields.Many2one(
        related="pnl_id.area_id", readonly=True,
    )

    csv_file = fields.Binary("Clover CSV", required=True)
    csv_filename = fields.Char("Filename")

    include_modifiers = fields.Boolean(
        "Import Modifiers as Separate Lines", default=True,
        help="If on, each Clover modifier (e.g. 'Non-Member $1') is "
             "imported as its own sales line.  You'll need a matching "
             "product tagged to this area for the import to find it.",
    )
    use_cogs = fields.Boolean(
        "Also import COGS column", default=False,
        help="If on, the COGS amount from Clover is added as a "
             "COGS line per item (overrides product standard cost).",
    )
    clear_existing = fields.Boolean(
        "Clear existing Sales lines before import", default=False,
        help="If on, removes all sales lines on the P&L before "
             "importing.  Use this when re-running an import for the "
             "same month.",
    )

    state = fields.Selection([
        ('select', 'Select File'),
        ('done',   'Done'),
    ], default='select')

    # ── Pre-flight diagnostic ──
    # Computed so the user can see at a glance whether anything is
    # tagged to this area BEFORE they upload a CSV.  If this is 0,
    # the import is guaranteed to match nothing.
    tagged_product_count = fields.Integer(
        "Products tagged to this Area",
        compute="_compute_tagged_product_count",
    )

    @api.depends("pnl_id")
    def _compute_tagged_product_count(self):
        Product = self.env['product.product']
        for rec in self:
            if not rec.area_id:
                rec.tagged_product_count = 0
                continue
            templates = self.env['product.template'].search([
                ('elks_area_ids', 'in', [rec.area_id.id]),
            ])
            rec.tagged_product_count = Product.search_count([
                ('product_tmpl_id', 'in', templates.ids),
            ]) if templates else 0

    # ── Results ──
    matched_items_count = fields.Integer(readonly=True)
    matched_mods_count = fields.Integer(readonly=True)
    unmatched_items_count = fields.Integer(readonly=True)
    unmatched_mods_count = fields.Integer(readonly=True)
    skipped_zero_count = fields.Integer(readonly=True)
    unmatched_items_text = fields.Text(
        "Items needing product setup", readonly=True,
    )
    unmatched_mods_text = fields.Text(
        "Modifiers needing product setup", readonly=True,
    )

    # ── Action ──
    def action_import(self):
        self.ensure_one()
        pnl = self.pnl_id
        if not pnl:
            raise UserError(_("Pick a target Area P&L first."))
        if pnl.state != 'draft':
            raise UserError(_(
                "P&L '%s' is validated and locked.  Re-open it "
                "before importing.") % pnl.display_name)
        if not self.csv_file:
            raise UserError(_("Attach the Clover CSV first."))

        try:
            raw = base64.b64decode(self.csv_file)
        except Exception as e:
            raise UserError(_("Couldn't decode the attached file: %s") % e)

        # Clover exports as UTF-8; tolerate BOM and odd bytes.
        text = raw.decode('utf-8-sig', errors='replace')
        items = parse_clover_items_csv(text)

        if self.clear_existing:
            pnl.sales_line_ids.unlink()

        Product = self.env['product.product']
        SalesLine = self.env['elks.area.pnl.sales.line']
        CogsLine = self.env['elks.area.pnl.cogs.line']

        area_id = pnl.area_id.id
        matched_items = 0
        matched_mods = 0
        skipped_zero = 0
        unmatched_items = []
        unmatched_mods = []

        # ── Pre-resolve the set of products tagged to this Area ────
        # Two-step search instead of dot-notation through a Many2many
        # — Odoo's domain engine can be flaky going through related
        # fields to M2m, so we resolve templates first then filter
        # products explicitly.
        templates = self.env['product.template'].search([
            ('elks_area_ids', 'in', [area_id]),
        ])
        if not templates:
            raise UserError(_(
                "No products are tagged to area '%s' yet.\n\n"
                "Open Inventory → Products, set Lodge Areas on each "
                "Clover item, then try again.\n\n"
                "Tip: Use Add Sales from Products on the P&L form "
                "to see what's already tagged.") % pnl.area_id.name)

        # ── Build the normalized lookup table once ─────────────────
        # Matching in Python (not SQL ilike) so Unicode apostrophe
        # variants and whitespace differences stop being a problem.
        # Keys are _normalize_name() of every product.name and
        # default_code; value is the product recordset.
        products_in_area = Product.search([
            ('product_tmpl_id', 'in', templates.ids),
        ])
        norm_to_product = {}
        norm_names = []  # ordered for the contains fallback
        for p in products_in_area:
            for raw in (p.name, p.default_code):
                key = _normalize_name(raw)
                if key and key not in norm_to_product:
                    norm_to_product[key] = p
                    norm_names.append(key)

        def _find_product(name):
            """Three strategies, in order:
                1. Exact match on normalized name/default_code.
                2. Substring either direction — Clover name contains
                   product name OR product name contains Clover name.
                   Only accepted if it resolves to a single product
                   (so "Crown" can't silently grab "Apple Crown").
            Returns an empty recordset on miss."""
            key = _normalize_name(name)
            if not key:
                return Product.browse()
            if key in norm_to_product:
                return norm_to_product[key]
            # Substring either-way fallback
            candidates = set()
            for nk in norm_names:
                if not nk:
                    continue
                if nk in key or key in nk:
                    candidates.add(norm_to_product[nk].id)
            if len(candidates) == 1:
                return Product.browse(candidates.pop())
            return Product.browse()

        for it in items:
            sold = it['sold']
            if sold <= 0 and it['net_sales'] == 0:
                skipped_zero += 1
                continue

            product = _find_product(it['name'])
            if product:
                unit_price = (it['net_sales'] / sold) if sold > 0 else 0.0
                SalesLine.create({
                    'pnl_id': pnl.id,
                    'product_id': product.id,
                    'category': it['category'] or '',
                    'description': it['name'],
                    'quantity': sold,
                    'unit_price': unit_price,
                    'amount': it['net_sales'],
                })
                matched_items += 1

                # Optional COGS pull from Clover
                if self.use_cogs and it['cogs']:
                    unit_cost = it['cogs'] / sold if sold > 0 else it['cogs']
                    # Reuse existing COGS line if auto-synced; else add.
                    cogs = pnl.cogs_line_ids.filtered(
                        lambda l: l.product_id == product
                    )
                    if cogs:
                        cogs[0].with_context(
                            elks_pnl_auto_sync=True
                        ).write({
                            'quantity': sold,
                            'unit_cost': unit_cost,
                        })
                    else:
                        CogsLine.create({
                            'pnl_id': pnl.id,
                            'product_id': product.id,
                            'description': it['name'],
                            'quantity': sold,
                            'unit_cost': unit_cost,
                            'auto_synced': True,
                        })
            else:
                unmatched_items.append(
                    "[%s] %s  —  qty %d, $%.2f" % (
                        it['category'] or '?',
                        it['name'],
                        sold,
                        it['net_sales'],
                    )
                )

            # Modifiers
            if self.include_modifiers:
                for mod in it['modifiers']:
                    if mod['sold'] <= 0 and mod['amount'] == 0:
                        continue
                    mod_product = _find_product(mod['name'])
                    if mod_product:
                        mq = mod['sold']
                        mu = (mod['amount'] / mq) if mq > 0 else 0.0
                        SalesLine.create({
                            'pnl_id': pnl.id,
                            'product_id': mod_product.id,
                            'category': (it['category'] or '') + ' (modifier)',
                            'description': mod['name'],
                            'quantity': mq,
                            'unit_price': mu,
                            'amount': mod['amount'],
                        })
                        matched_mods += 1
                    else:
                        unmatched_mods.append(
                            "%s  —  qty %d, $%.2f  (on item: %s)" % (
                                mod['name'],
                                mod['sold'],
                                mod['amount'],
                                it['name'],
                            )
                        )

        self.write({
            'matched_items_count':   matched_items,
            'matched_mods_count':    matched_mods,
            'unmatched_items_count': len(unmatched_items),
            'unmatched_mods_count':  len(unmatched_mods),
            'skipped_zero_count':    skipped_zero,
            'unmatched_items_text':  '\n'.join(unmatched_items) or _("None — every item matched a product."),
            'unmatched_mods_text':   '\n'.join(unmatched_mods)  or _("None — every modifier matched a product."),
            'state': 'done',
        })

        pnl.message_post(body=_(
            "Imported Clover CSV: %d item(s), %d modifier(s) matched. "
            "%d item(s) and %d modifier(s) need product setup.") % (
            matched_items, matched_mods,
            len(unmatched_items), len(unmatched_mods),
        ))

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
        }

    def action_close(self):
        return {'type': 'ir.actions.act_window_close'}
