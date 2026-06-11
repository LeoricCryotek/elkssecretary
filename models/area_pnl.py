# -*- coding: utf-8 -*-
"""Area P&L — monthly profit and loss for an operating area.

[Human]
    One record per (Area, Year, Month) — and optionally per Event.
    The record holds four line-item tables (Sales, Labor, COGS, Other),
    computes totals + Elks-AA-Manual KPI traffic-lights, and goes
    through a Draft → Validated state machine.  Validated P&Ls are
    locked, feed the dashboard, and represent the lodge's official
    monthly numbers for that area.

[AI]
    Models defined here:
      • elks.area.pnl              — header / parent
      • elks.area.pnl.sales.line   — revenue lines (incl. COGS sync trigger)
      • elks.area.pnl.labor.line   — labor (hours × rate)
      • elks.area.pnl.cogs.line    — cost of goods (incl. auto-sync flag)
      • elks.area.pnl.other.line   — utilities / supplies / misc
    External dependencies:
      • elks.area (parent), project.task (optional event), hr.attendance
        (timecard pull), hr.employee (cost rate), product.product (line
        product link + area filter via elks_area_ids).
    Constraint regime:
      • Uniqueness: (area_id, period_year, period_month, event_id)
        enforced in Python (_check_unique_period); no SQL constraint.
      • Locking: write/unlink guards block edits when state='validated'
        except for an _UNLOCKED_FIELDS allowlist used by the actions.
    Sales→COGS auto-sync: see ElksAreaPnlSalesLine._sync_cogs_from_self
        + ElksAreaPnlCogsLine.auto_synced flag + context-flag
        'elks_pnl_auto_sync' used to bypass the manual-edit detector.
"""
from datetime import datetime, time
from calendar import monthrange

from odoo import api, fields, models, _
from odoo.exceptions import UserError


_MONTH_SELECTION = [
    ('1',  'January'),   ('2',  'February'),  ('3',  'March'),
    ('4',  'April'),     ('5',  'May'),       ('6',  'June'),
    ('7',  'July'),      ('8',  'August'),    ('9',  'September'),
    ('10', 'October'),   ('11', 'November'),  ('12', 'December'),
]
_MONTH_LABEL = dict(_MONTH_SELECTION)


# ═══════════════════════════════════════════════════════════════════
# Main model
# ═══════════════════════════════════════════════════════════════════
class ElksAreaPnl(models.Model):
    _name = "elks.area.pnl"
    _description = "Area P&L (Monthly)"
    _order = "period_year desc, period_month desc, area_id"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(compute="_compute_name", store=True)
    area_id = fields.Many2one(
        "elks.area", string="Area", required=True, index=True,
        tracking=True, ondelete="restrict",
    )
    # Optional event scope.  When set:
    #   - The P&L represents just that one event (not the whole month).
    #   - If elksevent is installed and the task has x_event_date,
    #     period dates auto-narrow to the event day.
    #   - Multiple per-event P&Ls in the same (Area, Month) are allowed.
    # When elksevent isn't installed, this is just a plain project.task
    # link — the event-specific behaviour gracefully no-ops.
    event_id = fields.Many2one(
        "project.task", string="Event",
        ondelete="set null", index=True,
        help="Optional — pick the event task to scope this P&L to a "
             "single event.  Leave blank for the standard monthly "
             "area P&L.",
    )
    event_date = fields.Date(
        "Event Date", compute="_compute_event_date", store=True,
        help="Date of the linked event (from elksevent), if any.",
    )
    period_year = fields.Selection(
        selection="_get_year_selection",
        string="Year", required=True, tracking=True,
        default=lambda self: str(fields.Date.context_today(self).year),
    )

    @api.model
    def _get_year_selection(self):
        # Years 2020 → current + 5, so we always have room ahead.
        current_year = fields.Date.context_today(self).year
        return [(str(y), str(y)) for y in range(2020, current_year + 6)]
    period_month = fields.Selection(
        _MONTH_SELECTION, string="Month", required=True, tracking=True,
        default=lambda self: str(fields.Date.context_today(self).month),
    )
    period_start = fields.Date(
        "Period Start", compute="_compute_period_dates", store=True,
    )
    period_end = fields.Date(
        "Period End", compute="_compute_period_dates", store=True,
    )
    lodge_year = fields.Char(
        "Lodge Year", compute="_compute_lodge_year",
        store=True, index=True,
    )

    # ── State machine & locking ───────────────────────────────────
    # [Human]
    #   Draft = anyone in the Reception group can edit.  Validated =
    #   the books are closed; the form goes read-only and a green
    #   ribbon appears.  Only the Secretary group can re-open and
    #   the reopen is logged to chatter.
    # [AI]
    #   • state values: 'draft' | 'validated' (no other transitions).
    #   • Enforcement: write()/unlink() overrides at the bottom of the
    #     class block writes to most fields when state='validated'.
    #     The allowlist _UNLOCKED_FIELDS exists so action_validate /
    #     action_reopen themselves can still mutate state, validated_by,
    #     validated_date without tripping the guard.
    #   • Re-open requires group 'elkssecretary.group_elkssecretary_secretary'.
    #   • Buttons that respect the state: every action_* method begins
    #     with `if self.state != 'draft': raise UserError(...)`.
    state = fields.Selection([
        ('draft',     'Draft'),
        ('validated', 'Validated'),
    ], default='draft', tracking=True, index=True)
    validated_by = fields.Many2one(
        "res.users", string="Validated By",
        readonly=True, copy=False,
    )
    validated_date = fields.Datetime(
        "Validated On", readonly=True, copy=False,
    )

    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    # ── Line items ────────────────────────────────────────────────
    sales_line_ids = fields.One2many(
        "elks.area.pnl.sales.line", "pnl_id", string="Sales",
        copy=True,
    )
    labor_line_ids = fields.One2many(
        "elks.area.pnl.labor.line", "pnl_id", string="Labor",
        copy=True,
    )
    cogs_line_ids = fields.One2many(
        "elks.area.pnl.cogs.line", "pnl_id", string="COGS",
        copy=True,
    )
    other_line_ids = fields.One2many(
        "elks.area.pnl.other.line", "pnl_id", string="Other Expenses",
        copy=True,
    )

    # ── Computed totals ───────────────────────────────────────────
    gross_sales = fields.Monetary(
        "Gross Sales", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_labor = fields.Monetary(
        "Total Labor", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_cogs = fields.Monetary(
        "Total COGS", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_other = fields.Monetary(
        "Total Other", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    total_expenses = fields.Monetary(
        "Total Expenses", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    net_income = fields.Monetary(
        "Net Income", compute="_compute_totals", store=True,
        currency_field="currency_id",
    )
    # Stored as decimals (0.6627 = 66.27%) so the form's
    # widget="percentage" can render them correctly.
    gross_margin_pct = fields.Float(
        "Gross Margin %", compute="_compute_totals", store=True,
        digits=(6, 4),
    )
    cogs_pct = fields.Float(
        "COGS % of Sales", compute="_compute_totals", store=True,
        digits=(6, 4),
    )
    labor_pct = fields.Float(
        "Labor % of Sales", compute="_compute_totals", store=True,
        digits=(6, 4),
    )
    prime_cost_pct = fields.Float(
        "Prime Cost % (COGS + Labor)",
        compute="_compute_totals", store=True, digits=(6, 4),
    )
    net_margin_pct = fields.Float(
        "Net Margin %", compute="_compute_totals", store=True,
        digits=(6, 4),
    )

    # ── KPI traffic-light indicators ──────────────────────────────
    # [Human]
    #   Each key ratio gets a green / yellow / red badge against the
    #   targets the Elks AA Manual sets per department (CoGS ≤ 35%,
    #   Labor ≤ 35%), plus hospitality-industry rules-of-thumb for
    #   prime cost (≤ 65%) and net margin (≥ 5%).  Trustees can scan
    #   the dashboard in seconds.
    # [AI]
    #   • Decimals, not percents — fields store 0.6627 etc. so the
    #     form's widget="percentage" renders correctly (×100 inside
    #     the widget).  See _compute_totals.
    #   • Statuses computed in _compute_kpi_status; thresholds live
    #     there as constants, not in this declaration block.
    #   • 'na' = no sales recorded; prevents misleading flags on
    #     empty draft P&Ls.
    #   • Form: badges decorated with success/warning/danger via the
    #     widget="badge" attribute on view fields.
    KPI_STATUS_SELECTION = [
        ('green',  '🟢 On Target'),
        ('yellow', '🟡 Caution'),
        ('red',    '🔴 Off Target'),
        ('na',     '— No Sales'),
    ]
    cogs_status = fields.Selection(
        KPI_STATUS_SELECTION, "COGS Status",
        compute="_compute_kpi_status", store=True,
    )
    labor_status = fields.Selection(
        KPI_STATUS_SELECTION, "Labor Status",
        compute="_compute_kpi_status", store=True,
    )
    prime_cost_status = fields.Selection(
        KPI_STATUS_SELECTION, "Prime Cost Status",
        compute="_compute_kpi_status", store=True,
    )
    net_margin_status = fields.Selection(
        KPI_STATUS_SELECTION, "Net Margin Status",
        compute="_compute_kpi_status", store=True,
    )
    variance_status = fields.Selection([
        ('up',   '▲ Improving'),
        ('flat', '→ Flat'),
        ('down', '▼ Declining'),
        ('na',   '— No Prior'),
    ], "Variance Status", compute="_compute_variance_status",
    )

    # ── Prior-period comparison (computed, not stored) ────────────
    prior_net_income = fields.Monetary(
        "Prior Month Net", compute="_compute_prior",
        currency_field="currency_id",
    )
    variance_vs_prior = fields.Monetary(
        "Δ vs Prior", compute="_compute_prior",
        currency_field="currency_id",
    )

    # ── Uniqueness constraint ─────────────────────────────────────
    # [Human]
    #   Stops you from accidentally creating two Kitchen May 2026
    #   monthly P&Ls.  But it allows a Kitchen May 2026 monthly *and*
    #   a Kitchen May 2026 per-event P&L (Smith Wedding) to coexist,
    #   so events can have their own books.
    # [AI]
    #   • Python @api.constrains, not SQL — because NULL semantics for
    #     event_id don't work with PostgreSQL's UNIQUE on plain columns
    #     (multiple NULLs would all be allowed → duplicates possible).
    #     COALESCE-in-unique-index is fragile in Odoo's auto-schema, so
    #     we keep it in Python.
    #   • Pre-migration migrations/19.0.2.8/pre-migrate.py drops the
    #     old SQL constraint `elks_area_pnl_uniq_period` from the
    #     19.0.2.0 build.
    #   • Two distinct error messages for clarity: one when an event
    #     conflict, one when a monthly conflict.
    @api.constrains("area_id", "period_year", "period_month", "event_id")
    def _check_unique_period(self):
        for rec in self:
            if not (rec.area_id and rec.period_year and rec.period_month):
                continue
            dup = self.search([
                ('area_id', '=', rec.area_id.id),
                ('period_year', '=', rec.period_year),
                ('period_month', '=', rec.period_month),
                ('event_id', '=', rec.event_id.id or False),
                ('id', '!=', rec.id),
            ], limit=1)
            if dup:
                if rec.event_id:
                    raise UserError(_(
                        "A P&L already exists for %s in %s %s for "
                        "event '%s'.") % (
                            rec.area_id.name,
                            _MONTH_LABEL.get(rec.period_month, ''),
                            rec.period_year,
                            rec.event_id.name,
                        ))
                raise UserError(_(
                    "A monthly P&L already exists for %s in %s %s.  "
                    "Pick an Event to make a per-event P&L instead.") % (
                        rec.area_id.name,
                        _MONTH_LABEL.get(rec.period_month, ''),
                        rec.period_year,
                    ))

    # ── Computes ──────────────────────────────────────────────────
    @api.depends("area_id", "period_year", "period_month",
                 "event_id", "event_id.name")
    def _compute_name(self):
        for rec in self:
            if rec.area_id and rec.period_year and rec.period_month:
                base = "%s — %s %s" % (
                    rec.area_id.name,
                    _MONTH_LABEL.get(rec.period_month, rec.period_month),
                    rec.period_year,
                )
                if rec.event_id:
                    rec.name = "%s — %s" % (base, rec.event_id.name)
                else:
                    rec.name = base
            else:
                rec.name = _("New Area P&L")

    @api.depends("event_id")
    def _compute_event_date(self):
        """Pull the event's date from elksevent if that module is
        installed and the linked task carries x_event_date.  Falls
        back to False otherwise."""
        for rec in self:
            ev = rec.event_id
            rec.event_date = getattr(ev, 'x_event_date', False) or False

    @api.depends("period_year", "period_month", "event_id", "event_date")
    def _compute_period_dates(self):
        for rec in self:
            # Event-scoped P&L: narrow to the event's single day if
            # we know it (elksevent installed); otherwise fall back to
            # the month range so the record still computes cleanly.
            if rec.event_id and rec.event_date:
                rec.period_start = rec.event_date
                rec.period_end = rec.event_date
                continue
            if rec.period_year and rec.period_month:
                try:
                    y = int(rec.period_year)
                    m = int(rec.period_month)
                except (TypeError, ValueError):
                    rec.period_start = False
                    rec.period_end = False
                    continue
                _wd, last = monthrange(y, m)
                rec.period_start = fields.Date.from_string(
                    "%04d-%02d-01" % (y, m)
                )
                rec.period_end = fields.Date.from_string(
                    "%04d-%02d-%02d" % (y, m, last)
                )
            else:
                rec.period_start = False
                rec.period_end = False

    @api.depends("period_start")
    def _compute_lodge_year(self):
        for rec in self:
            d = rec.period_start
            if not d:
                rec.lodge_year = False
                continue
            ystart = d.year if d.month >= 4 else d.year - 1
            rec.lodge_year = "%d-%d" % (ystart, ystart + 1)

    @api.depends(
        "sales_line_ids.amount",
        "labor_line_ids.total",
        "cogs_line_ids.total",
        "other_line_ids.amount",
    )
    def _compute_totals(self):
        for rec in self:
            sales = sum(rec.sales_line_ids.mapped('amount'))
            labor = sum(rec.labor_line_ids.mapped('total'))
            cogs = sum(rec.cogs_line_ids.mapped('total'))
            other = sum(rec.other_line_ids.mapped('amount'))
            rec.gross_sales = sales
            rec.total_labor = labor
            rec.total_cogs = cogs
            rec.total_other = other
            rec.total_expenses = labor + cogs + other
            rec.net_income = sales - rec.total_expenses
            if sales:
                # Decimal values: widget="percentage" multiplies by 100.
                rec.gross_margin_pct = (sales - cogs) / sales
                rec.cogs_pct = cogs / sales
                rec.labor_pct = labor / sales
                rec.prime_cost_pct = (cogs + labor) / sales
                rec.net_margin_pct = rec.net_income / sales
            else:
                rec.gross_margin_pct = 0.0
                rec.cogs_pct = 0.0
                rec.labor_pct = 0.0
                rec.prime_cost_pct = 0.0
                rec.net_margin_pct = 0.0

    @api.depends("gross_sales", "cogs_pct", "labor_pct",
                 "prime_cost_pct", "net_margin_pct")
    def _compute_kpi_status(self):
        """Traffic-light each KPI per Elks AA Manual targets.

        Thresholds:
            COGS  % of sales:  ≤35 green, 35-40 yellow, >40 red
            Labor % of sales:  ≤35 green, 35-40 yellow, >40 red
            Prime % of sales:  ≤65 green, 65-70 yellow, >70 red
            Net margin     :   ≥5  green, 0-5  yellow, <0  red
        Each is 'na' if there are no sales (avoids misleading flags).
        """
        for rec in self:
            if not rec.gross_sales:
                rec.cogs_status = 'na'
                rec.labor_status = 'na'
                rec.prime_cost_status = 'na'
                rec.net_margin_status = 'na'
                continue
            # COGS (lower is better)
            c = rec.cogs_pct * 100
            rec.cogs_status = (
                'green'  if c <= 35 else
                'yellow' if c <= 40 else
                'red'
            )
            # Labor (lower is better)
            l = rec.labor_pct * 100
            rec.labor_status = (
                'green'  if l <= 35 else
                'yellow' if l <= 40 else
                'red'
            )
            # Prime Cost (lower is better)
            p = rec.prime_cost_pct * 100
            rec.prime_cost_status = (
                'green'  if p <= 65 else
                'yellow' if p <= 70 else
                'red'
            )
            # Net Margin (higher is better)
            n = rec.net_margin_pct * 100
            rec.net_margin_status = (
                'green'  if n >= 5  else
                'yellow' if n >= 0  else
                'red'
            )

    @api.depends("prior_net_income", "net_income")
    def _compute_variance_status(self):
        for rec in self:
            if not rec.prior_net_income:
                rec.variance_status = 'na'
                continue
            if rec.net_income > rec.prior_net_income:
                rec.variance_status = 'up'
            elif rec.net_income < rec.prior_net_income:
                rec.variance_status = 'down'
            else:
                rec.variance_status = 'flat'

    @api.depends("area_id", "period_year", "period_month", "net_income")
    def _compute_prior(self):
        for rec in self:
            if not (rec.area_id and rec.period_year and rec.period_month):
                rec.prior_net_income = 0.0
                rec.variance_vs_prior = 0.0
                continue
            try:
                y = int(rec.period_year)
                m = int(rec.period_month)
            except (TypeError, ValueError):
                rec.prior_net_income = 0.0
                rec.variance_vs_prior = 0.0
                continue
            prior_y = y - 1 if m == 1 else y
            prior_m = 12 if m == 1 else m - 1
            prior = self.search([
                ('area_id', '=', rec.area_id.id),
                ('period_year', '=', str(prior_y)),
                ('period_month', '=', str(prior_m)),
                ('id', '!=', rec.id or 0),
            ], limit=1)
            rec.prior_net_income = prior.net_income or 0.0
            rec.variance_vs_prior = rec.net_income - rec.prior_net_income

    # ══════════════════════════════════════════════════════════════
    # Actions (form buttons)
    # ══════════════════════════════════════════════════════════════
    # [Human]
    #   All the green/blue buttons in the form header are wired here.
    #   Each one returns either True/dict (Odoo action descriptor),
    #   raises a friendly UserError, or just mutates the record + posts
    #   a chatter message.
    # [AI]
    #   • Order shown in the form (top to bottom):
    #     Pull Labor • Import Clover CSV • Add Sales/COGS from Products
    #     • Re-sync COGS from Sales • Preview P&L • Validate&Lock • Re-open.
    #   • Every action's first line is `self.ensure_one()` + a state
    #     check; pattern intentional so any action triggered by mass
    #     server actions fails fast on the wrong record.
    #   • Preview/Print: action_print_pnl returns the HTML report
    #     descriptor; PDF is reached via right-click on the record
    #     (binding_model_id on the PDF ir.actions.report record).

    def action_validate(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_(
                    "Only draft P&Ls can be validated."))
            rec.write({
                'state': 'validated',
                'validated_by': self.env.user.id,
                'validated_date': fields.Datetime.now(),
            })
            rec.message_post(body=_("P&L validated and locked."))

    def action_reopen(self):
        for rec in self:
            if rec.state != 'validated':
                raise UserError(_(
                    "Only validated P&Ls can be re-opened."))
            if not self.env.user.has_group(
                'elkssecretary.group_elkssecretary_secretary'
            ):
                raise UserError(_(
                    "Only the Secretary group can re-open a "
                    "validated P&L."))
            rec.write({'state': 'draft'})
            rec.message_post(
                body=_("P&L re-opened for editing by %s.")
                % self.env.user.name
            )

    # ── Pull Labor from Timecards ─────────────────────────────────
    # [Human]
    #   At month-end the Secretary clicks one button and labor
    #   populates from the timeclock system.  Only employees in the
    #   relevant area get pulled.  Manually entered labor lines (say,
    #   for a volunteer with no timecard) are preserved.
    # [AI]
    #   • Source: hr.attendance rows where check_in is within
    #     [period_start 00:00, period_end 23:59].
    #   • Worked hours: summed via Python loop (att.worked_hours).
    #     Avoids read_group dependency on whether worked_hours is
    #     stored or computed-only in this Odoo version.
    #   • Rate: hr.employee.hourly_cost (modern Odoo).  Guarded with
    #     getattr for portability — falls back to 0 if missing.
    #   • Area scoping precedence:
    #       1) Area.department_ids → employees with matching department
    #       2) hr.employee.elks_area_id → employees tagged to area
    #       3) Otherwise pull everyone; user prunes manually.
    #   • Idempotency: deletes any existing labor lines with
    #     source='attendance' before creating fresh ones.  Manual
    #     lines (source='manual') are untouched.
    def action_pull_labor_from_timecards(self):
        """Read hr.attendance rows whose check_in falls inside this
        P&L's period, sum worked_hours per employee, multiply by
        each employee's `hourly_cost`, and create labor lines tagged
        source=attendance.  Existing attendance-sourced lines are
        replaced; manual lines are preserved."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Only draft P&Ls can pull from timecards."))
        if not self.period_start or not self.period_end:
            raise UserError(_(
                "Set the period (year + month) before pulling timecards."))

        Attendance = self.env['hr.attendance']
        start_dt = datetime.combine(self.period_start, time.min)
        end_dt = datetime.combine(self.period_end, time.max)

        domain = [
            ('check_in', '>=', start_dt),
            ('check_in', '<=', end_dt),
            ('employee_id', '!=', False),
        ]
        # Scoping precedence:
        #   1. If the Area has HR Departments mapped → only employees
        #      whose department_id is in that set.
        #   2. Else if any employees are tagged with this Area via
        #      hr_employee.elks_area_id → only those.
        #   3. Else pull all employees so the user can prune manually.
        Employee = self.env['hr.employee']
        if self.area_id.department_ids:
            scoped = Employee.search([
                ('department_id', 'in', self.area_id.department_ids.ids),
            ])
            domain.append(('employee_id', 'in', scoped.ids))
        else:
            scoped = Employee.search(
                [('elks_area_id', '=', self.area_id.id)]
            )
            if scoped:
                domain.append(('employee_id', 'in', scoped.ids))

        attendances = Attendance.search(domain)
        hours_by_emp = {}
        for att in attendances:
            emp_id = att.employee_id.id
            hours_by_emp[emp_id] = (
                hours_by_emp.get(emp_id, 0.0) + (att.worked_hours or 0.0)
            )

        # Wipe previous attendance-sourced lines so re-running refreshes
        old = self.labor_line_ids.filtered(
            lambda l: l.source == 'attendance'
        )
        old.unlink()

        Employee = self.env['hr.employee']
        new_vals = []
        for emp_id, hours in hours_by_emp.items():
            if not hours or hours <= 0:
                continue
            employee = Employee.browse(emp_id)
            # `hourly_cost` is on hr.employee in modern Odoo; fall back
            # to 0 if the field isn't present for any reason.
            rate = getattr(employee, 'hourly_cost', 0.0) or 0.0
            new_vals.append({
                'pnl_id': self.id,
                'employee_id': emp_id,
                'hours': hours,
                'rate': rate,
                'source': 'attendance',
            })

        if new_vals:
            self.env['elks.area.pnl.labor.line'].create(new_vals)

        self.message_post(body=_(
            "Pulled %d labor line(s) from timecards (%s – %s).") % (
            len(new_vals), self.period_start, self.period_end,
        ))
        return True

    def action_add_sales_from_products(self):
        """Create a blank sales line for each product tagged with
        this Area.  Description and unit_price prefilled; quantity
        defaults to 0 so the user just types in how many were sold.

        Existing lines that already reference a given product are
        skipped so re-running doesn't create duplicates."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Only draft P&Ls can pull from products."))

        Product = self.env['product.product']
        products = Product.search([
            ('product_tmpl_id.elks_area_ids', 'in', [self.area_id.id]),
        ])
        if not products:
            raise UserError(_(
                "No products are tagged to area '%s' yet.  "
                "Open a product and set its Lodge Area first.")
                % self.area_id.name)

        existing = set(
            self.sales_line_ids.mapped('product_id').ids
        )
        new_vals = []
        for p in products:
            if p.id in existing:
                continue
            new_vals.append({
                'pnl_id': self.id,
                'product_id': p.id,
                'description': p.display_name,
                'unit_price': p.list_price or 0.0,
                'quantity': 0.0,
            })

        if not new_vals:
            raise UserError(_(
                "All products tagged to '%s' already have a sales "
                "line on this P&L.") % self.area_id.name)

        self.env['elks.area.pnl.sales.line'].create(new_vals)
        self.message_post(body=_(
            "Added %d sales line(s) from products tagged to '%s'."
        ) % (len(new_vals), self.area_id.name))
        return True

    def action_add_cogs_from_products(self):
        """Same idea as action_add_sales_from_products but for COGS:
        creates a blank COGS line per tagged product, prefilled with
        the product's standard cost."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Only draft P&Ls can pull from products."))

        Product = self.env['product.product']
        products = Product.search([
            ('product_tmpl_id.elks_area_ids', 'in', [self.area_id.id]),
        ])
        if not products:
            raise UserError(_(
                "No products are tagged to area '%s' yet.")
                % self.area_id.name)

        existing = set(self.cogs_line_ids.mapped('product_id').ids)
        new_vals = []
        for p in products:
            if p.id in existing:
                continue
            new_vals.append({
                'pnl_id': self.id,
                'product_id': p.id,
                'description': p.display_name,
                'unit_cost': p.standard_price or 0.0,
                'quantity': 0.0,
            })

        if not new_vals:
            raise UserError(_(
                "All products already have a COGS line."))

        self.env['elks.area.pnl.cogs.line'].create(new_vals)
        self.message_post(body=_(
            "Added %d COGS line(s) from products tagged to '%s'."
        ) % (len(new_vals), self.area_id.name))
        return True

    def action_resync_cogs_from_sales(self):
        """Reset every COGS line's quantity to match its corresponding
        sales line (creating the COGS line if missing) and re-enable
        auto-sync.  Use this after bulk edits if you want a clean
        slate, or before validating to make sure cost matches sales."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Only draft P&Ls can re-sync COGS."))
        CogsLine = self.env['elks.area.pnl.cogs.line']
        synced = 0
        for sl in self.sales_line_ids:
            if not sl.product_id or sl.quantity <= 0:
                continue
            cogs = self.cogs_line_ids.filtered(
                lambda l: l.product_id == sl.product_id
            )
            if cogs:
                cogs[0].with_context(
                    elks_pnl_auto_sync=True
                ).write({
                    'quantity': sl.quantity,
                    'auto_synced': True,
                })
            else:
                CogsLine.create({
                    'pnl_id': self.id,
                    'product_id': sl.product_id.id,
                    'description': sl.product_id.display_name,
                    'quantity': sl.quantity,
                    'unit_cost': sl.product_id.standard_price or 0.0,
                    'auto_synced': True,
                })
            synced += 1
        self.message_post(body=_(
            "Re-synced %d COGS line(s) from Sales.") % synced)
        return True

    def action_open_clover_import(self):
        """Open the Clover CSV import wizard for this P&L."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Clover Sales CSV"),
            'res_model': 'elks.area.pnl.clover.import',
            'view_mode': 'form',
            'target': 'new',
            'context': {'active_id': self.id, 'default_pnl_id': self.id},
        }

    def action_print_pnl(self):
        """Open the HTML SUMMARY of the P&L so the user can preview
        before printing.  The line-by-line detailed report is still
        available via action_print_pnl_detailed and via the right-click
        "Print" menu (binding_model_id on the detailed PDF action)."""
        self.ensure_one()
        return self.env.ref(
            'elkssecretary.action_report_area_pnl_summary_html'
        ).report_action(self)

    def action_print_pnl_detailed(self):
        """Open the HTML DETAILED report (every line item shown)."""
        self.ensure_one()
        return self.env.ref(
            'elkssecretary.action_report_area_pnl_html'
        ).report_action(self)

    # ── Reporting helpers ─────────────────────────────────────────
    # [Human]
    #   QWeb templates can't do dict/groupby/sort inline cleanly.
    #   These methods pre-aggregate line items so the summary report
    #   template can just t-foreach over the result.
    # [AI]
    #   • get_sales_summary_by_category: returns list of
    #     (category_name, total_amount) tuples, sorted by amount desc.
    #   • get_cogs_summary_by_category: same shape for COGS.
    #   • get_other_summary_by_category: same shape for Other Expenses.
    #   • All return a Python list so t-foreach works in the report
    #     template without needing odoo.osv.expression tricks.
    def get_sales_summary_by_category(self):
        self.ensure_one()
        totals = {}
        for line in self.sales_line_ids:
            cat = line.category or _("Uncategorized")
            totals[cat] = totals.get(cat, 0.0) + (line.amount or 0.0)
        return sorted(totals.items(), key=lambda kv: -kv[1])

    def get_cogs_summary_by_category(self):
        """COGS lines have no category column today, so we group by
        product category (or 'Uncategorized') instead."""
        self.ensure_one()
        totals = {}
        for line in self.cogs_line_ids:
            cat = (
                line.product_id.categ_id.display_name
                if line.product_id and line.product_id.categ_id
                else _("Uncategorized")
            )
            totals[cat] = totals.get(cat, 0.0) + (line.total or 0.0)
        return sorted(totals.items(), key=lambda kv: -kv[1])

    def get_other_summary_by_category(self):
        self.ensure_one()
        totals = {}
        for line in self.other_line_ids:
            cat = line.category or _("Uncategorized")
            totals[cat] = totals.get(cat, 0.0) + (line.amount or 0.0)
        return sorted(totals.items(), key=lambda kv: -kv[1])

    # ── Write/unlink guards for validated records ─────────────────
    # [Human]
    #   Once a P&L is validated, nothing can edit or delete it until
    #   the Secretary re-opens it.  Even mass-update tools or
    #   server-action scripts will hit this wall.
    # [AI]
    #   • _UNLOCKED_FIELDS: the only writable fields when validated.
    #     Includes the state machine's own fields (so action_reopen
    #     can flip back to draft) plus mail.thread housekeeping (so
    #     chatter messages can still be posted to validated records).
    #   • write(): if any key outside the allowlist is set on a
    #     validated record, raise UserError naming the record.
    #   • unlink(): blanket block on validated records — re-open first.
    #   • View-level readonly attrs (readonly="state == 'validated'")
    #     prevent UI editing; the Python guards are the second line of
    #     defence against API/RPC calls.
    _UNLOCKED_FIELDS = {
        'state', 'validated_by', 'validated_date',
        'message_follower_ids', 'message_ids', 'activity_ids',
        'message_main_attachment_id',
    }

    def write(self, vals):
        if vals and (set(vals.keys()) - self._UNLOCKED_FIELDS):
            locked = self.filtered(lambda r: r.state == 'validated')
            if locked:
                raise UserError(_(
                    "P&L '%s' is validated and locked.  "
                    "Re-open it before editing.") % locked[0].display_name)
        return super().write(vals)

    def unlink(self):
        locked = self.filtered(lambda r: r.state == 'validated')
        if locked:
            raise UserError(_(
                "Cannot delete a validated P&L.  Re-open it first."))
        return super().unlink()


# ═══════════════════════════════════════════════════════════════════
# Line models
# ═══════════════════════════════════════════════════════════════════
# [Human]
#   Four child tables hanging off elks.area.pnl, one per money type:
#     Sales — what came in
#     Labor — payroll
#     COGS  — what we paid for the things we sold
#     Other — utilities, supplies, etc.
#   Each line type has its own table because the columns differ —
#   labor has hours+rate, COGS has qty+unit_cost, sales has product+
#   qty+price, other is just description+amount.
# [AI]
#   Cascade behavior: pnl_id is ondelete='cascade', so deleting a
#   parent P&L cleans up all four child tables in one go.
#   currency_id is related to pnl_id.currency_id with store=True so
#   each line gets indexed for fast sum aggregations.
#   Sales↔COGS coupling: see ElksAreaPnlSalesLine._sync_cogs_from_self
#   and ElksAreaPnlCogsLine.auto_synced flag below.
# ═══════════════════════════════════════════════════════════════════
class ElksAreaPnlSalesLine(models.Model):
    _name = "elks.area.pnl.sales.line"
    _description = "Area P&L — Sales Line"
    _order = "id"

    pnl_id = fields.Many2one(
        "elks.area.pnl", required=True,
        ondelete="cascade", index=True,
    )
    # Optional product link.  If set, name & unit_price default in;
    # amount = quantity * unit_price.  If blank, use a free-form
    # description and type the amount directly.
    product_id = fields.Many2one(
        "product.product", string="Product",
        help="Optional — pick a product to default in its name and "
             "list price.",
    )
    category = fields.Char(help="e.g. Food, Beverage, Bingo, etc.")
    description = fields.Char()
    quantity = fields.Float("Qty", digits=(12, 2), default=0.0)
    unit_price = fields.Monetary(
        "Unit Price", currency_field="currency_id",
    )
    amount = fields.Monetary(
        "Amount", compute="_compute_amount",
        store=True, readonly=False,
        currency_field="currency_id",
        help="Auto = qty × unit price when both set; otherwise type "
             "the amount in directly.",
    )
    currency_id = fields.Many2one(
        related="pnl_id.currency_id", store=True,
    )

    @api.depends("quantity", "unit_price")
    def _compute_amount(self):
        for rec in self:
            if rec.quantity and rec.unit_price:
                rec.amount = rec.quantity * rec.unit_price
            # else: leave whatever the user typed alone (readonly=False)

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for rec in self:
            if rec.product_id:
                if not rec.description:
                    rec.description = rec.product_id.display_name
                if not rec.unit_price:
                    rec.unit_price = rec.product_id.list_price or 0.0
                if not rec.quantity:
                    rec.quantity = 1.0

    # ── COGS auto-sync ────────────────────────────────────────────
    # [Human]
    #   You type "20 burgers sold" on a sales line.  The COGS line for
    #   the burger product gets created automatically with qty=20 and
    #   the product's standard cost.  If you later edit the COGS line
    #   to "15" (because 5 were comp'd), that manual edit is sticky —
    #   even if you change the sales line later, the COGS won't get
    #   overwritten.  A blanket "Re-sync COGS from Sales" button on
    #   the form is available if you want to reset everything.
    # [AI]
    #   Three pieces work together:
    #     1) This method (_sync_cogs_from_self), invoked from sales-line
    #        create() and write() when product_id or quantity changes.
    #     2) ElksAreaPnlCogsLine.auto_synced — True while the COGS line
    #        is mirroring sales; False once the user has edited it.
    #     3) Context flag 'elks_pnl_auto_sync' — set when this method
    #        calls cogs.write({'quantity': ...}) so the CogsLine.write
    #        override knows that change came from us (and doesn't flip
    #        auto_synced to False).
    #   Idempotency / no-touch rules:
    #     • Skips sales lines without product_id.
    #     • Skips when quantity <= 0 (e.g. Add-from-Products creates qty=0).
    #     • Skips when parent P&L is validated (defensive — the parent
    #       write guard would already block, but we don't even try).
    #     • Leaves COGS lines with auto_synced=False completely alone.
    #   Related actions:
    #     • action_resync_cogs_from_sales (parent model) — manual nuke
    #       that re-flips all matching COGS lines back to auto_synced=True
    #       and overwrites the qty.
    def _sync_cogs_from_self(self):
        """Create or refresh a matching COGS line for each sales line
        in this recordset (if it has a product + quantity > 0)."""
        CogsLine = self.env['elks.area.pnl.cogs.line']
        for sl in self:
            if not sl.product_id or not sl.pnl_id:
                continue
            if not sl.quantity or sl.quantity <= 0:
                continue
            if sl.pnl_id.state != 'draft':
                continue  # don't touch locked P&Ls
            cogs = sl.pnl_id.cogs_line_ids.filtered(
                lambda l: l.product_id == sl.product_id
            )
            if not cogs:
                # No matching COGS yet — create one mirroring sales.
                CogsLine.create({
                    'pnl_id': sl.pnl_id.id,
                    'product_id': sl.product_id.id,
                    'description': sl.product_id.display_name,
                    'quantity': sl.quantity,
                    'unit_cost': sl.product_id.standard_price or 0.0,
                    'auto_synced': True,
                })
            elif cogs[0].auto_synced:
                # Existing line still auto-syncing — refresh its qty.
                cogs[0].with_context(
                    elks_pnl_auto_sync=True
                ).write({'quantity': sl.quantity})
            # else: user manually adjusted the COGS — leave alone.

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_cogs_from_self()
        return records

    def write(self, vals):
        res = super().write(vals)
        if {'product_id', 'quantity'} & set(vals.keys()):
            self._sync_cogs_from_self()
        return res


class ElksAreaPnlLaborLine(models.Model):
    _name = "elks.area.pnl.labor.line"
    _description = "Area P&L — Labor Line"
    _order = "id"

    pnl_id = fields.Many2one(
        "elks.area.pnl", required=True,
        ondelete="cascade", index=True,
    )
    employee_id = fields.Many2one("hr.employee", string="Employee")
    hours = fields.Float("Hours", digits=(10, 2))
    rate = fields.Monetary("Hourly Rate", currency_field="currency_id")
    total = fields.Monetary(
        "Total", compute="_compute_total", store=True,
        currency_field="currency_id",
    )
    source = fields.Selection([
        ('manual',     'Manual'),
        ('attendance', 'Pulled from Timecards'),
    ], default='manual', required=True)
    notes = fields.Char()
    currency_id = fields.Many2one(
        related="pnl_id.currency_id", store=True,
    )

    @api.depends("hours", "rate")
    def _compute_total(self):
        for rec in self:
            rec.total = (rec.hours or 0.0) * (rec.rate or 0.0)

    @api.onchange("employee_id")
    def _onchange_employee_id(self):
        for rec in self:
            if rec.employee_id and not rec.rate:
                rec.rate = (
                    getattr(rec.employee_id, 'hourly_cost', 0.0) or 0.0
                )


class ElksAreaPnlCogsLine(models.Model):
    _name = "elks.area.pnl.cogs.line"
    _description = "Area P&L — COGS Line"
    _order = "id"

    pnl_id = fields.Many2one(
        "elks.area.pnl", required=True,
        ondelete="cascade", index=True,
    )
    # Optional product link.  If set, description and unit_cost
    # default in from the product master.
    product_id = fields.Many2one(
        "product.product", string="Product",
        help="Optional — pick a product to default in its name and "
             "standard cost.",
    )
    description = fields.Char(required=True)
    quantity = fields.Float("Quantity", digits=(12, 2), default=1.0)
    unit_cost = fields.Monetary(
        "Unit Cost", currency_field="currency_id",
    )
    total = fields.Monetary(
        "Total", compute="_compute_total", store=True,
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        related="pnl_id.currency_id", store=True,
    )
    # Auto-sync flag: True while the COGS line's quantity is being
    # mirrored from the matching sales line.  Set to False the moment
    # the user manually edits the quantity (so gifted-item adjustments
    # aren't clobbered by later sales-qty changes).
    auto_synced = fields.Boolean(
        "Auto-Synced from Sales", default=False, copy=False,
        help="When checked, this COGS line's quantity tracks the "
             "matching sales line.  Editing the quantity manually "
             "turns this off so your adjustment sticks.",
    )

    @api.depends("quantity", "unit_cost")
    def _compute_total(self):
        for rec in self:
            rec.total = (rec.quantity or 0.0) * (rec.unit_cost or 0.0)

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for rec in self:
            if rec.product_id:
                if not rec.description:
                    rec.description = rec.product_id.display_name
                if not rec.unit_cost:
                    rec.unit_cost = rec.product_id.standard_price or 0.0

    def write(self, vals):
        # If the user changes the quantity outside the auto-sync path,
        # drop the auto-sync flag so we don't overwrite their edit
        # next time the matching sales line changes.
        if 'quantity' in vals and not self.env.context.get(
            'elks_pnl_auto_sync'
        ):
            vals = dict(vals)
            vals.setdefault('auto_synced', False)
        return super().write(vals)


class ElksAreaPnlOtherLine(models.Model):
    _name = "elks.area.pnl.other.line"
    _description = "Area P&L — Other Expense Line"
    _order = "id"

    pnl_id = fields.Many2one(
        "elks.area.pnl", required=True,
        ondelete="cascade", index=True,
    )
    category = fields.Char(
        help="e.g. Utilities, Supplies, Maintenance, etc.",
    )
    description = fields.Char()
    amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        related="pnl_id.currency_id", store=True,
    )
