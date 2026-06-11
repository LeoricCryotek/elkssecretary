# -*- coding: utf-8 -*-
"""Lodge Operating Area — a cost / profit centre.

[Human]
    Each Area is a profit centre we track its own P&L for: Kitchen,
    Lounge, Banquet, Pull Tabs, etc.  This model is the lightweight
    config record — name, code, color, plus the HR departments that
    feed labor into it.
    The smart button at the top of the Area form jumps to the list
    of P&Ls for that area.

[AI]
    • Standalone model — no analytic-account link (intentional, can be
      wired later without breaking anything).
    • Referenced by: elks.area.pnl.area_id, hr.employee.elks_area_id,
      product.template.elks_area_ids (Many2many), and the timecard pull
      via department_ids → hr.employee.department_id.
    • Uniqueness: SQL constraint on name (Odoo 19 models.Constraint API).
    • action_open_pnls is the smart-button target — opens an act_window
      filtered to this area's P&Ls.
"""
from odoo import api, fields, models, _


class ElksArea(models.Model):
    _name = "elks.area"
    _description = "Lodge Operating Area"
    _order = "sequence, name"

    name = fields.Char(required=True, index=True, translate=False)
    code = fields.Char(
        help="Short code, e.g. KITCHEN, LOUNGE — used in reports.",
    )
    sequence = fields.Integer(default=10)
    color = fields.Integer("Color Index", default=0)
    active = fields.Boolean(default=True)
    description = fields.Text()

    # HR departments that roll up into this Area.  Used by the
    # Area P&L's "Pull Labor from Timecards" action to scope which
    # employees' attendances to total.
    department_ids = fields.Many2many(
        "hr.department", string="HR Departments",
        help="Employees in these HR departments are pulled into this "
             "area's labor lines when you click 'Pull Labor from "
             "Timecards'.",
    )

    # Smart-button counts
    pnl_count = fields.Integer(
        "P&Ls", compute="_compute_pnl_count",
    )
    pnl_validated_count = fields.Integer(
        "Validated", compute="_compute_pnl_count",
    )

    _name_uniq = models.Constraint(
        'unique(name)',
        'An area with that name already exists.',
    )

    def _compute_pnl_count(self):
        Pnl = self.env['elks.area.pnl']
        for rec in self:
            rec.pnl_count = Pnl.search_count(
                [('area_id', '=', rec.id)]
            )
            rec.pnl_validated_count = Pnl.search_count([
                ('area_id', '=', rec.id),
                ('state', '=', 'validated'),
            ])

    def action_open_pnls(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("%s — P&Ls") % self.name,
            'res_model': 'elks.area.pnl',
            'view_mode': 'list,form,kanban,pivot,graph',
            'domain': [('area_id', '=', self.id)],
            'context': {'default_area_id': self.id},
        }
