# -*- coding: utf-8 -*-
"""Secretary Dashboard — a transient model backing the daily report view.

This is a singleton-style model: rather than store records, it exposes
computed fields summarizing the state of the lodge at a point in time.
The dashboard view calls these computes on open.
"""
import datetime

from odoo import api, fields, models


class ElksSecretaryDashboard(models.TransientModel):
    _name = "elks.secretary.dashboard"
    _description = "Elks Secretary Daily Dashboard"
    _rec_name = "display_name"

    display_name = fields.Char(
        compute="_compute_display_name",
    )

    @api.depends("report_date")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Secretary Daily Dashboard"

    # -- Config / date range --
    report_date = fields.Date(
        "Report Date", default=fields.Date.context_today,
    )

    # -- CLMS Work Queue counts --
    clms_pending_count = fields.Integer(
        "Payments Pending CLMS Entry",
        compute="_compute_counts",
    )
    clms_pending_amount = fields.Monetary(
        "Pending Amount",
        compute="_compute_counts",
        currency_field='currency_id',
    )
    today_payment_count = fields.Integer(
        "Payments Today", compute="_compute_counts",
    )
    today_payment_amount = fields.Monetary(
        "Cash Received Today",
        compute="_compute_counts",
        currency_field='currency_id',
    )
    week_payment_count = fields.Integer(
        "Payments This Week", compute="_compute_counts",
    )
    week_payment_amount = fields.Monetary(
        "Received This Week",
        compute="_compute_counts",
        currency_field='currency_id',
    )

    # -- Member activity --
    new_volunteers_today = fields.Integer(
        "Volunteers Added Today", compute="_compute_counts",
    )
    members_expiring_30d = fields.Integer(
        "Members Dues Expiring (30d)", compute="_compute_counts",
    )
    members_delinquent = fields.Integer(
        "Delinquent Members", compute="_compute_counts",
    )

    # -- Maintenance --
    open_work_orders = fields.Integer(
        "Open Work Orders", compute="_compute_counts",
    )
    high_priority_maintenance = fields.Integer(
        "High Priority Maintenance", compute="_compute_counts",
    )

    # -- Meeting Money (replaces the removed Budget Transfers tile) --
    meeting_money_project_dollars_ytd = fields.Monetary(
        "Project $ YTD (Lodge Year)",
        compute="_compute_counts",
        currency_field='currency_id',
    )
    meeting_money_this_month_count = fields.Integer(
        "Meeting Money Entries This Month",
        compute="_compute_counts",
    )

    # -- Charity Reporting --
    charity_pending_count = fields.Integer(
        "Charity Hours to Validate", compute="_compute_counts",
    )
    charity_activity_count = fields.Integer(
        "Active Charity Activities", compute="_compute_counts",
    )
    charity_total_hours = fields.Float(
        "Total Charity Hours (Year)", compute="_compute_counts",
    )
    charity_validated_hours = fields.Float(
        "Validated Charity Hours", compute="_compute_counts",
    )
    charity_total_miles = fields.Float(
        "Total Charity Miles (Year)", compute="_compute_counts",
    )

    # -- Membership Applications --
    proposed_members_count = fields.Integer(
        "Active Proposed Members", compute="_compute_counts",
    )
    initiated_pending_clms_count = fields.Integer(
        "Initiated – Pending CLMS", compute="_compute_counts",
    )

    # -- Purchase Approvals --
    board_queue_count = fields.Integer(
        "Requisitions Awaiting Board", compute="_compute_counts",
    )
    floor_queue_count = fields.Integer(
        "Requisitions Awaiting Floor", compute="_compute_counts",
    )

    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    def _get_delinquent_members(self):
        """Return delinquent members for the PDF report detail list."""
        today = fields.Date.context_today(self)
        return self.env['res.partner'].search([
            ('x_is_member', '=', True),
            ('x_detail_dues_paid_to_date', '<', today),
        ], order='name asc')

    def _get_department_income(self):
        """Return YTD income by department from journal entries.

        Lodge fiscal year runs April 1 – March 31.  Returns a list of
        dicts: [{'name': ..., 'income': float}, ...] for departments
        that have income accounts with posted journal entries.
        """
        today = fields.Date.context_today(self)
        # Fiscal year start: April 1
        if today.month >= 4:
            fy_start = today.replace(month=4, day=1)
        else:
            fy_start = today.replace(year=today.year - 1, month=4, day=1)

        # Key departments to report (ordered)
        dept_codes = ['40', '50', '61', '64']
        dept_labels = {
            '40': 'Bar / Lounge',
            '50': 'Food Service / Kitchen',
            '61': 'Athletic Facilities',
            '64': 'RV Park / Camping',
        }

        results = []
        try:
            Department = self.env['elks.department']
            JELine = self.env['elks.journal.entry.line']
            for code in dept_codes:
                dept = Department.search([('code', '=', code)], limit=1)
                if not dept:
                    results.append({
                        'name': dept_labels.get(code, code),
                        'income': 0.0,
                    })
                    continue
                # Income = credits on income accounts in this department
                income_lines = JELine.search([
                    ('account_id.department_id', '=', dept.id),
                    ('account_id.account_type', '=', 'income'),
                    ('entry_id.state', '=', 'posted'),
                    ('entry_id.date', '>=', fy_start),
                    ('entry_id.date', '<=', today),
                ])
                total = sum(l.credit - l.debit for l in income_lines)
                results.append({
                    'name': dept_labels.get(code, dept.name),
                    'income': total,
                })
        except (KeyError, ValueError):
            for code in dept_codes:
                results.append({
                    'name': dept_labels.get(code, code),
                    'income': 0.0,
                })
        return results

    @api.depends("report_date")
    def _compute_counts(self):
        Payment = self.env['elks.dues.payment']
        Partner = self.env['res.partner']

        # Detect maintenance module presence (optional dependency)
        try:
            Request = self.env['maintenance.request']
            has_maintenance = True
        except (KeyError, ValueError):
            Request = None
            has_maintenance = False

        today = fields.Date.context_today(self)
        week_start = today - datetime.timedelta(days=7)
        in_30d = today + datetime.timedelta(days=30)
        # Datetime range covering "today" — use real datetime objects so
        # microseconds and tz conversion don't drop matches at boundaries.
        today_start = datetime.datetime.combine(today, datetime.time.min)
        today_end = datetime.datetime.combine(today, datetime.time.max)

        for rec in self:
            # --- CLMS queue ---
            pending = Payment.search([
                ('state', '=', 'posted'),
                ('clms_status', '=', 'pending'),
            ])
            rec.clms_pending_count = len(pending)
            rec.clms_pending_amount = sum(pending.mapped('amount_total'))

            # --- Today ---
            today_paid = Payment.search([
                ('state', '=', 'posted'),
                ('payment_date', '=', today),
            ])
            rec.today_payment_count = len(today_paid)
            rec.today_payment_amount = sum(today_paid.mapped('amount_total'))

            # --- This week ---
            week_paid = Payment.search([
                ('state', '=', 'posted'),
                ('payment_date', '>=', week_start),
                ('payment_date', '<=', today),
            ])
            rec.week_payment_count = len(week_paid)
            rec.week_payment_amount = sum(week_paid.mapped('amount_total'))

            # --- New volunteers today ---
            # res.partner doesn't have a created_date filter easily; use
            # create_date instead.  Cast create_date to date for comparison.
            volunteers_today = Partner.search([
                ('x_is_volunteer', '=', True),
                ('create_date', '>=', today_start),
                ('create_date', '<=', today_end),
            ])
            rec.new_volunteers_today = len(volunteers_today)

            # --- Members with dues expiring in next 30 days ---
            expiring = Partner.search([
                ('x_is_member', '=', True),
                ('x_detail_dues_paid_to_date', '>=', today),
                ('x_detail_dues_paid_to_date', '<=', in_30d),
            ])
            rec.members_expiring_30d = len(expiring)

            # --- Delinquent (dues_paid_to < today) ---
            delinquent = Partner.search([
                ('x_is_member', '=', True),
                ('x_detail_dues_paid_to_date', '<', today),
            ])
            rec.members_delinquent = len(delinquent)

            # --- Maintenance ---
            if has_maintenance:
                open_req = Request.search([
                    ('stage_id.done', '=', False),
                ])
                rec.open_work_orders = len(open_req)
                rec.high_priority_maintenance = len(open_req.filtered(
                    lambda r: getattr(r, 'x_priority_score', 0) >= 12
                ))
            else:
                rec.open_work_orders = 0
                rec.high_priority_maintenance = 0

            # --- Meeting Money ---
            # Lodge year = Apr 1 – Mar 31.  Sum project_dollars_amount
            # across every meeting in the current lodge year, and count
            # entries recorded this calendar month for the "activity"
            # secondary metric.
            try:
                Meeting = self.env['elks.meeting.money']
                current_ly_start = today.year if today.month >= 4 else today.year - 1
                current_ly = f"{current_ly_start}-{current_ly_start + 1}"
                ly_meetings = Meeting.search(
                    [('lodge_year', '=', current_ly)]
                )
                rec.meeting_money_project_dollars_ytd = sum(
                    ly_meetings.mapped('project_dollars_amount')
                )
                month_start = today.replace(day=1)
                rec.meeting_money_this_month_count = Meeting.search_count([
                    ('meeting_date', '>=', month_start),
                    ('meeting_date', '<=', today),
                ])
            except (KeyError, ValueError):
                rec.meeting_money_project_dollars_ytd = 0.0
                rec.meeting_money_this_month_count = 0

            # --- Charity Reporting ---
            # Use x_charity_task_id directly instead of the stored
            # computed boolean — avoids stale-compute edge cases.
            rec.charity_pending_count = self.env['hr.attendance'].search_count([
                ('x_charity_task_id', '!=', False),
                ('x_validated', '=', False),
            ])

            # Current lodge year charity project and its activities
            try:
                Project = self.env['project.project']
                current_start = today.year if today.month >= 4 else today.year - 1
                current_ly = f"{current_start}-{current_start + 1}"
                charity_project = Project.search([
                    ('x_is_charity_parent', '=', True),
                    ('x_lodge_year', '=', current_ly),
                ], limit=1)
                if charity_project:
                    tasks = self.env['project.task'].search([
                        ('project_id', '=', charity_project.id),
                    ])
                    rec.charity_activity_count = len(tasks)
                    # Total hours and miles from charity attendance
                    charity_att = self.env['hr.attendance'].search([
                        ('x_charity_task_id', 'in', tasks.ids),
                    ])
                    rec.charity_total_hours = sum(
                        charity_att.mapped('x_charity_hours')
                    )
                    rec.charity_validated_hours = sum(
                        charity_att.filtered('x_validated').mapped('x_charity_hours')
                    )
                    rec.charity_total_miles = sum(
                        charity_att.mapped('x_miles')
                    )
                else:
                    rec.charity_activity_count = 0
                    rec.charity_total_hours = 0.0
                    rec.charity_validated_hours = 0.0
                    rec.charity_total_miles = 0.0
            except (KeyError, ValueError):
                rec.charity_activity_count = 0
                rec.charity_total_hours = 0.0
                rec.charity_validated_hours = 0.0
                rec.charity_total_miles = 0.0

            # --- Membership Applications ---
            try:
                Application = self.env['elks.membership.application']
                # Active pipeline: proposed through elected
                rec.proposed_members_count = Application.search_count([
                    ('stage', 'in', (
                        'proposed', 'investigation',
                        'balloting', 'elected',
                    )),
                ])
                # Initiated but not yet entered in CLMS (no member number)
                rec.initiated_pending_clms_count = Application.search_count([
                    ('stage', '=', 'initiated'),
                    ('member_number_assigned', '=', False),
                ])
            except (KeyError, ValueError):
                rec.proposed_members_count = 0
                rec.initiated_pending_clms_count = 0

            # --- Purchase Approval Queues ---
            try:
                PO = self.env['purchase.order']
                if 'x_approval_state' not in PO._fields:
                    raise KeyError('x_approval_state')
                rec.board_queue_count = PO.search_count([
                    ('x_approval_state', '=', 'board'),
                ])
                rec.floor_queue_count = PO.search_count([
                    ('x_approval_state', '=', 'floor'),
                ])
            except (KeyError, ValueError):
                rec.board_queue_count = 0
                rec.floor_queue_count = 0

    def _get_rv_summary(self):
        """Return current RV parking snapshot for the report."""
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        result = {
            'currently_parked': 0,
            'total_spaces': 0,
            'month_registrations': 0,
            'month_collected': 0.0,
        }
        try:
            Reg = self.env['elks.rv.registration']
            settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
            result['total_spaces'] = settings.rv_total_spaces if settings else 0

            # Currently parked (registered, check-in <= today, check-out >= today)
            parked = Reg.search_count([
                ('state', '=', 'registered'),
                ('check_in', '<=', today),
                ('check_out', '>=', today),
            ])
            result['currently_parked'] = parked

            # This month's registrations
            month_regs = Reg.search([
                ('state', '!=', 'cancelled'),
                ('check_in', '>=', month_start),
                ('check_in', '<=', today),
            ])
            result['month_registrations'] = len(month_regs)
            result['month_collected'] = sum(
                r.amount_paid or r.total_amount for r in month_regs
            )
        except (KeyError, ValueError):
            pass
        return result

    # ------------------------------------------------------------------
    # Actions — each opens a filtered list view
    # ------------------------------------------------------------------
    def action_open_clms_queue(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'CLMS Work Queue',
            'res_model': 'elks.dues.payment',
            'view_mode': 'list,form',
            'domain': [
                ('state', '=', 'posted'),
                ('clms_status', '=', 'pending'),
            ],
            'context': {'search_default_group_date': 1},
        }

    def action_open_today_payments(self):
        today = fields.Date.context_today(self)
        return {
            'type': 'ir.actions.act_window',
            'name': f"Today's Payments ({today})",
            'res_model': 'elks.dues.payment',
            'view_mode': 'list,form',
            'domain': [
                ('state', '=', 'posted'),
                ('payment_date', '=', today),
            ],
        }

    def action_open_week_payments(self):
        today = fields.Date.context_today(self)
        week_start = today - datetime.timedelta(days=7)
        return {
            'type': 'ir.actions.act_window',
            'name': "This Week's Payments",
            'res_model': 'elks.dues.payment',
            'view_mode': 'list,form',
            'domain': [
                ('state', '=', 'posted'),
                ('payment_date', '>=', week_start),
                ('payment_date', '<=', today),
            ],
        }

    def action_open_expiring(self):
        today = fields.Date.context_today(self)
        in_30d = today + datetime.timedelta(days=30)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Dues Expiring in 30 Days',
            'res_model': 'res.partner',
            'view_mode': 'list,form',
            'domain': [
                ('x_is_member', '=', True),
                ('x_detail_dues_paid_to_date', '>=', today),
                ('x_detail_dues_paid_to_date', '<=', in_30d),
            ],
        }

    def action_open_delinquent(self):
        today = fields.Date.context_today(self)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Delinquent Members',
            'res_model': 'res.partner',
            'view_mode': 'list,form',
            'domain': [
                ('x_is_member', '=', True),
                ('x_detail_dues_paid_to_date', '<', today),
            ],
        }

    def action_open_volunteers_today(self):
        today = fields.Date.context_today(self)
        today_start = datetime.datetime.combine(today, datetime.time.min)
        today_end = datetime.datetime.combine(today, datetime.time.max)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Volunteers Added Today',
            'res_model': 'res.partner',
            'view_mode': 'list,form',
            'domain': [
                ('x_is_volunteer', '=', True),
                ('create_date', '>=', today_start),
                ('create_date', '<=', today_end),
            ],
        }

    def action_open_meeting_money(self):
        """Jump to the Meeting Money entries list, defaulted to the
        current lodge year (Apr 1 – Mar 31)."""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Meeting Money',
            'res_model': 'elks.meeting.money',
            'view_mode': 'list,form',
            'context': {'search_default_this_lodge_year': 1},
        }

    def action_open_meeting_money_this_month(self):
        """Jump to Meeting Money entries recorded this calendar month."""
        today = fields.Date.context_today(self)
        month_start = today.replace(day=1)
        return {
            'type': 'ir.actions.act_window',
            'name': "This Month's Meeting Money",
            'res_model': 'elks.meeting.money',
            'view_mode': 'list,form',
            'domain': [
                ('meeting_date', '>=', month_start),
                ('meeting_date', '<=', today),
            ],
        }

    def action_open_board_queue(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Board Approval Queue',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('x_approval_state', '=', 'board')],
        }

    def action_open_floor_queue(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Floor Vote Queue',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('x_approval_state', '=', 'floor')],
        }

    def action_open_charity_validation(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Charity Hours to Validate',
            'res_model': 'hr.attendance',
            'view_mode': 'list,form',
            'domain': [
                ('x_charity_task_id', '!=', False),
                ('x_validated', '=', False),
            ],
        }

    def action_open_charity_activities(self):
        today = fields.Date.context_today(self)
        current_start = today.year if today.month >= 4 else today.year - 1
        current_ly = f"{current_start}-{current_start + 1}"
        project = self.env['project.project'].search([
            ('x_is_charity_parent', '=', True),
            ('x_lodge_year', '=', current_ly),
        ], limit=1)
        return {
            'type': 'ir.actions.act_window',
            'name': f'Charity Activities ({current_ly})',
            'res_model': 'project.task',
            'view_mode': 'list,form',
            'domain': [('project_id', '=', project.id)] if project else [('id', '=', 0)],
        }

    def action_open_all_charity_hours(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'All Charity Hours',
            'res_model': 'elks.charity.hours.report',
            'view_mode': 'list,pivot,graph',
        }

    def action_open_proposed_members(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Active Proposed Members',
            'res_model': 'elks.membership.application',
            'view_mode': 'list,form',
            'domain': [
                ('stage', 'in', (
                    'proposed', 'investigation',
                    'balloting', 'elected',
                )),
            ],
        }

    def action_open_initiated_pending_clms(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Initiated – Pending CLMS Entry',
            'res_model': 'elks.membership.application',
            'view_mode': 'list,form',
            'domain': [
                ('stage', '=', 'initiated'),
                ('member_number_assigned', '=', False),
            ],
        }

    def action_open_maintenance(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Open Work Orders',
            'res_model': 'maintenance.request',
            'view_mode': 'kanban,list,form',
            'domain': [('stage_id.done', '=', False)],
        }

    def action_print_daily_report(self):
        """Print the daily secretary PDF."""
        self.ensure_one()
        return self.env.ref(
            'elkssecretary.action_report_daily_secretary'
        ).report_action(self)

    @api.model
    def action_print_secretary_report(self):
        """Menu entry-point: create a fresh dashboard and print the PDF."""
        dashboard = self.create({})
        return self.env.ref(
            'elkssecretary.action_report_daily_secretary'
        ).report_action(dashboard)

    @api.model
    def action_open_dashboard(self):
        """Convenience entry point used by the main menu."""
        dashboard = self.create({})
        return {
            'type': 'ir.actions.act_window',
            'name': 'Secretary Dashboard',
            'res_model': 'elks.secretary.dashboard',
            'res_id': dashboard.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Bulk mark-processed action (used by CLMS queue list view)
    # ------------------------------------------------------------------
    @api.model
    def action_bulk_mark_processed(self):
        """Bulk-mark the active dues payment IDs as processed in CLMS."""
        active_ids = self.env.context.get('active_ids', [])
        payments = self.env['elks.dues.payment'].browse(active_ids)
        for p in payments.filtered(lambda p: p.clms_status == 'pending'):
            p.action_mark_clms_processed()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'CLMS Queue',
                'message': f'{len(payments)} payment(s) marked as processed.',
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
