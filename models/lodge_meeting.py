# -*- coding: utf-8 -*-
"""Lodge Meeting Agenda — one record per regular lodge meeting.

[Human]
    Pick a date, click Generate, and this model auto-populates from
    Meeting Money, Membership Applications, Deaths queue, and Events.
    You review/edit, then click Download Agenda to get a filled-in
    Word document ready for the meeting.

[AI]
    Models:
      • elks.lodge.meeting                — parent record
      • elks.lodge.meeting.officer        — roll call O2m
      • elks.lodge.meeting.committee.line — committee reports O2m
      • elks.lodge.meeting.proposition    — proposed / balloting O2m
    Generation flow:
      1) User creates via wizard (date + optional prior meeting).
      2) _populate_from_system() reaches into sibling modules
         defensively (try/except so a missing model doesn't break
         creation).  Meeting Money, membership stages, deaths queue,
         event tasks are all pulled if their modules are installed.
    Docx export:
      • Uses python-docx (declared in manifest external_dependencies).
      • Template lives at static/src/lodge_meeting/template.docx.
      • _fill_template() does paragraph text replacement + table row
        insertion for propositions/balloting.  See action_download_docx.
"""
import base64
import io
import logging
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# python-docx is declared as an external dependency in the manifest,
# but we still guard the import so the module loads on servers where
# it's missing — the download button then raises a clear error.
try:
    from docx import Document as _DocxDocument  # type: ignore
    HAS_DOCX = True
except ImportError:
    _DocxDocument = None
    HAS_DOCX = False


def _first_attr(rec, *dotted_paths, default=''):
    """Try each dotted attribute path; return first non-empty value.

    Example:
        _first_attr(app, 'partner_id.name', 'candidate_name',
                    default='Unknown')

    Traversal is safe — a missing attribute anywhere in the chain
    (or a Falsey intermediate like None/False) skips to the next
    candidate.  Returns the value coerced to str."""
    for path in dotted_paths:
        obj = rec
        ok = True
        for part in path.split('.'):
            obj = getattr(obj, part, None)
            if obj is None or obj is False:
                ok = False
                break
        if ok and obj not in (None, False, ''):
            return str(obj)
    return default


def _compose_city_state(rec):
    """Build 'City, State' — tries every plausible location the data
    might live in:
        1. Direct city/state fields on the application itself
           (x_city, x_state, x_state_id, city, state_id, etc.)
        2. Via a linked partner record (partner_id, candidate_id,
           applicant_id, etc.)
    Returns '' if nothing resolves."""
    # Direct-on-application fields first
    direct_city = ''
    for f in ('city', 'x_city', 'applicant_city', 'candidate_city',
              'proposed_city', 'x_applicant_city'):
        val = getattr(rec, f, None)
        if val and isinstance(val, str):
            direct_city = val
            break
    direct_state = ''
    # First try string state fields
    for f in ('state', 'x_state', 'state_name', 'x_state_name'):
        val = getattr(rec, f, None)
        if val and isinstance(val, str):
            direct_state = val
            break
    # Then try M2o state_id
    if not direct_state:
        for f in ('state_id', 'x_state_id', 'applicant_state_id'):
            state_rec = getattr(rec, f, None)
            name = getattr(state_rec, 'name', None) if state_rec else None
            if name:
                direct_state = name
                break
    if direct_city or direct_state:
        parts = [x for x in (direct_city, direct_state) if x]
        return ", ".join(parts)

    # Fall back to linked partner
    try:
        partner = None
        for path in ('partner_id', 'proposed_member_id', 'candidate_id',
                     'applicant_id', 'member_id',
                     'prospective_member_id'):
            candidate = getattr(rec, path, None)
            if candidate and getattr(candidate, 'id', None):
                partner = candidate
                break
        if not partner:
            return ''
        city = getattr(partner, 'city', '') or ''
        state = getattr(getattr(partner, 'state_id', None), 'name', '') or ''
        parts = [x for x in (city, state) if x]
        return ", ".join(parts)
    except (AttributeError, ValueError):
        return ''


def _looks_like_sequence(value):
    """True if the string looks like an Odoo sequence (e.g. APP/2026/0005).

    Used to REJECT display_name / name fallbacks that would print the
    application's reference number instead of the applicant's actual
    name.  Any string containing '/YYYY/' or starting with common
    lodge-application prefixes gets rejected."""
    if not value:
        return True
    s = str(value).strip()
    import re
    # Sequences like APP/2026/0005 or MEMBER-2026-0001 — a YYYY 4-digit
    # year sandwiched between / or - separators is a strong signal.
    if re.search(r'[/\-]\d{4}[/\-]\d+', s):
        return True
    # UPPERCASE prefix followed by / or - (2-10 letters covers APP,
    # MEMBER, REQ, PROP, INVEST, BALLOT, etc.).
    if re.match(r'^[A-Z]{2,10}[/\-]', s):
        return True
    return False


def _resolve_candidate_name(app):
    """Return the applicant's actual name from an elks.membership.
    application record, trying every plausible field.

    Skips 'name' / 'display_name' if they look like a sequence
    identifier (APP/2026/0005) — that's the auto-generated record
    reference, not the person's name."""
    # 1. Try direct name fields first
    direct = _first_attr(
        app,
        'proposed_member_id.name', 'candidate_id.name',
        'partner_id.name', 'applicant_id.name', 'member_id.name',
        'prospective_member_id.name',
        'applicant_name', 'candidate_name', 'full_name',
        'proposed_name', 'x_applicant_name',
        default='',
    )
    if direct and not _looks_like_sequence(direct):
        return direct
    # 2. Try composing from first_name + last_name pairs
    for first_path, last_path in (
        ('first_name', 'last_name'),
        ('x_first_name', 'x_last_name'),
        ('proposed_first_name', 'proposed_last_name'),
        ('applicant_first_name', 'applicant_last_name'),
    ):
        first = _first_attr(app, first_path, default='')
        last = _first_attr(app, last_path, default='')
        if first or last:
            composed = (first + ' ' + last).strip()
            if composed and not _looks_like_sequence(composed):
                return composed
    # 3. Last resort — display_name, but only if it doesn't look
    #    like a sequence.  If it does, we honestly say so.
    dn = getattr(app, 'display_name', '') or ''
    if dn and not _looks_like_sequence(dn):
        return str(dn)
    n = getattr(app, 'name', '') or ''
    if n and not _looks_like_sequence(n):
        return str(n)
    return "(applicant name not on record %s)" % (n or 'unknown')


def _resolve_city_state(app):
    """Try direct city_state fields, else compose from partner."""
    direct = _first_attr(
        app,
        'city_state', 'x_city_state',
        default='',
    )
    if direct:
        return direct
    return _compose_city_state(app)


# ═══════════════════════════════════════════════════════════════════
# Officer roll call defaults
# ═══════════════════════════════════════════════════════════════════
# Positions and names as they appear in the current lodge template.
# When a meeting is generated, one officer line is created per
# position with these defaults.  Users can override per-meeting.
DEFAULT_OFFICERS = [
    ("Exalted Ruler",             "Christopher Huddleston"),
    ("Esteemed Leading Knight",   "Dean Panttaja"),
    ("Esteemed Loyal Knight",     "Tammy Mathews"),
    ("Esteemed Lecturing Knight", "Michael Grittner"),
    ("Lodge Secretary",           "Daniel Santiago"),
    ("Treasurer",                 "Mike Miltenberger"),
    ("Esquire",                   "Danielle Sauve"),
    ("Tiler",                     "Judith Wutzke"),
    ("Chaplain",                  "Alice Gwinn"),
    ("Inner Guard",               "Jim Scully"),
    ("One Year Trustee",          "Christopher Spataro"),
    ("Two Year Trustee",          "Denis Freudenthal"),
    ("Three Year Trustee",        "Mike Gwinn"),
    ("Organist",                  "Dana Lohrey"),
]

DEFAULT_COMMITTEES = [
    ("Auditing",                 "Chris Noland"),
    ("Youth Activities",         "Dr Brian Ruddell"),
    ("Antler Program",           "Michael Grittner"),
    ("Hoop Shoot Sub-Committee", "Mike Miltenberger"),
    ("Fraternal",                "Mike Miltenberger"),
    ("ENF Sub-Committee",        "Diana R Smith"),
    ("Flag Day Sub-Committee",   ""),
    ("Accident Prevention",      "Samantha Musser"),
    ("PER",                      "Jim Scully"),
    ("Veterans Services",        "Alice Gwinn"),
    ("Standing Relief",          "Mike Gwinn"),
    ("Public Relations",         "Chris Huddleston"),
    ("Trustees Report",          ""),
    ("Board Report",             "Chris Huddleston"),
]


class ElksLodgeMeeting(models.Model):
    _name = "elks.lodge.meeting"
    _description = "Lodge Regular Meeting Agenda"
    _order = "meeting_date desc"
    _inherit = ["mail.thread"]

    name = fields.Char(compute="_compute_name", store=True)
    meeting_date = fields.Date(
        "Meeting Date", required=True,
        default=fields.Date.context_today, index=True, tracking=True,
    )
    prior_meeting_id = fields.Many2one(
        "elks.lodge.meeting", string="Prior Meeting",
        help="The meeting whose minutes are read at this meeting.",
    )
    state = fields.Selection([
        ('draft',     'Draft'),
        ('finalized', 'Finalized'),
    ], default='draft', tracking=True, index=True)

    # ── Opening / Closing ─────────────────────────────────────────
    open_time = fields.Char("Called to Order Time", default="7:00 pm")
    close_time = fields.Char("Closed Time")
    members_present_open = fields.Integer("Members Present (Open)")
    members_present_close = fields.Integer("Members Present (Close)")
    exalted_ruler_name = fields.Char(
        "Exalted Ruler", default="Christopher Huddleston",
    )

    # ── Substitutions / visitors / minutes ────────────────────────
    substitutions = fields.Text("Substitutions / Fill-Ins")
    visitors_note = fields.Text(
        "Recognition of former ERs, honorees, visitors",
    )
    minutes_read_by = fields.Char("Minutes Read By")
    minutes_accepted = fields.Boolean(
        "Minutes accepted as read", default=True,
    )

    # ── Members: sickness / distress / deaths ────────────────────
    sickness_distress = fields.Text("Sickness and Distress")
    deaths_of_members = fields.Text("Deaths of Members")

    # ── Community / events / reports ─────────────────────────────
    community_activities = fields.Text("Community Activities / Upcoming Events")
    communications = fields.Text("Reading of Communications (Mail/Email)")
    unfinished_business = fields.Text("Unfinished Business")
    new_business = fields.Text("New Business")
    good_of_order = fields.Text("Good of the Order")

    # ── Meeting Money ────────────────────────────────────────────
    project_dollars_amount = fields.Monetary(
        "Project Dollars", currency_field="currency_id",
    )
    fines_amount = fields.Monetary(
        "Fines", currency_field="currency_id",
    )
    meeting_money_id = fields.Many2one(
        "elks.meeting.money", string="Meeting Money Record",
        help="The Meeting Money submission for this meeting date.  "
             "Linked when the agenda is generated (if one exists) or "
             "created automatically on Finalize.  Use this to jump to "
             "the pulled report / running lodge-year totals.",
    )

    # ── Treasurer's Report ───────────────────────────────────────
    bills_amount = fields.Monetary(
        "Bills",
        compute="_compute_bills_amount", store=True, readonly=False,
        currency_field="currency_id",
        help="Sum of Floor-Vote POs + manual bill lines.  You can "
             "type over this to override, but adding manual lines is "
             "usually cleaner so the breakdown stays visible.",
    )
    bill_ids = fields.Many2many(
        "purchase.order", string="Bills from Floor-Vote Queue",
        help="Purchase orders currently in Floor-Vote approval state.  "
             "Auto-populated on Generate.  Click through to review "
             "individual line items.",
    )
    manual_bill_ids = fields.One2many(
        "elks.lodge.meeting.bill", "meeting_id",
        string="Additional / Manual Bills",
        help="Bills to pay that AREN'T in the Floor-Vote purchase "
             "queue (adhoc invoices, one-off payments, etc.).  These "
             "add to the Bills total alongside the queue POs.",
    )

    @api.depends("bill_ids.amount_total", "manual_bill_ids.amount")
    def _compute_bills_amount(self):
        for rec in self:
            rec.bills_amount = (
                sum(rec.bill_ids.mapped('amount_total'))
                + sum(rec.manual_bill_ids.mapped('amount'))
            )
    assets_amount = fields.Monetary(
        "Assets", currency_field="currency_id",
    )
    liabilities_amount = fields.Monetary(
        "Liabilities", currency_field="currency_id",
    )
    equity_amount = fields.Monetary(
        "Equity", currency_field="currency_id",
    )
    total_le_amount = fields.Monetary(
        "Total Liabilities & Equity",
        compute="_compute_total_le", store=True,
        currency_field="currency_id",
    )
    bills_motion_by = fields.Char("Motion to Pay Bills — Made By")
    bills_seconded_by = fields.Char("Motion Seconded By")

    currency_id = fields.Many2one(
        "res.currency",
        default=lambda self: self.env.company.currency_id,
    )

    # ── Related lines ────────────────────────────────────────────
    officer_ids = fields.One2many(
        "elks.lodge.meeting.officer", "meeting_id", string="Officer Roll Call",
        copy=True,
    )
    substitution_ids = fields.One2many(
        "elks.lodge.meeting.substitution", "meeting_id",
        string="Substitutions / Fill-Ins",
        copy=True,
    )
    committee_ids = fields.One2many(
        "elks.lodge.meeting.committee.line", "meeting_id",
        string="Committee Reports", copy=True,
    )
    proposition_ids = fields.One2many(
        "elks.lodge.meeting.proposition", "meeting_id",
        string="Membership Items", copy=True,
    )
    motion_ids = fields.One2many(
        "elks.lodge.meeting.motion", "meeting_id",
        string="Motions Requiring Vote",
        copy=True,
        help="Business motions raised on the floor that need a "
             "mover, a seconder, and a pass/fail result.  Filled in "
             "during the meeting; printed in the finalized minutes.",
    )
    # Report log: every record that gets read to the floor at this
    # meeting (death, proposition, event, etc.) is stamped here on
    # Generate Minutes.  Future meetings' auto-populate skips any
    # record already in a prior meeting's log so nothing gets read
    # twice.
    report_log_ids = fields.One2many(
        "elks.lodge.meeting.report.log", "meeting_id",
        string="Records Reported at This Meeting", copy=False,
        help="Each row records a source record (a death, a "
             "proposition, an event) that appeared in this meeting's "
             "agenda / minutes.  Populated on Generate Minutes.  "
             "Future agendas won't re-list any record already in "
             "any prior meeting's log.",
    )

    # ── Output ───────────────────────────────────────────────────
    agenda_docx = fields.Binary("Generated Agenda", attachment=True)
    agenda_docx_filename = fields.Char()
    minutes_docx = fields.Binary("Generated Minutes", attachment=True)
    minutes_docx_filename = fields.Char()
    minutes_generated_date = fields.Datetime(
        "Minutes Generated On", readonly=True, copy=False,
    )
    minutes_generated_by = fields.Many2one(
        "res.users", string="Minutes Generated By",
        readonly=True, copy=False,
    )

    # ── Computes / lifecycle ─────────────────────────────────────
    @api.depends("meeting_date")
    def _compute_name(self):
        for rec in self:
            rec.name = (
                "Lodge Meeting — %s" % rec.meeting_date
                if rec.meeting_date else _("New Meeting")
            )

    @api.depends("liabilities_amount", "equity_amount")
    def _compute_total_le(self):
        for rec in self:
            rec.total_le_amount = (
                (rec.liabilities_amount or 0.0)
                + (rec.equity_amount or 0.0)
            )

    # ── Auto-population from other modules ────────────────────────
    def _populate_from_system(self):
        """Reach into sibling modules to pre-fill agenda sections.

        Each pull is guarded — a missing model or field is silently
        skipped so the meeting record still gets created with the
        officer/committee defaults."""
        for rec in self:
            rec._populate_officers()
            rec._populate_committees()
            rec._populate_meeting_money()
            rec._populate_prior_meeting_link()
            rec._populate_propositions()
            rec._populate_deaths()
            rec._populate_events()
            rec._populate_bills_from_purchase()
            # NB: Assets / Liabilities / Equity are left as manual
            # entry.  Secretary types the Treasurer's numbers directly
            # onto the form before the meeting.

    def _populate_officers(self):
        """Populate the officer roll call from the CURRENT active
        officer terms in elkscontacts (elks.officer.term), falling
        back to the hardcoded DEFAULT_OFFICERS list only when that
        model isn't available.

        For each active term:
          • Position label is mapped from the officer term's short
            name ('Leading Knight') to the template's ceremonial form
            ('Esteemed Leading Knight').
          • Officer name is the linked member's name; 'VACANT' if the
            term is flagged vacant or has no linked member.
          • Positions are ordered in the traditional Elks roll-call
            sequence, so template layout stays consistent regardless
            of the term-record ID order in the DB.

        Status is intentionally left BLANK — the Secretary fills it
        in during roll call at the meeting."""
        Line = self.env['elks.lodge.meeting.officer']

        # Try the elkscontacts officer terms model first.
        term_lines = self._resolve_active_officer_terms()
        if term_lines:
            for i, (pos_label, officer_name, seq) in enumerate(term_lines):
                Line.create({
                    'meeting_id': self.id,
                    'sequence': seq or ((i + 1) * 10),
                    'position': pos_label,
                    'officer_name': officer_name,
                })
            return

        # Fallback: hardcoded defaults
        for i, (pos, name) in enumerate(DEFAULT_OFFICERS):
            Line.create({
                'meeting_id': self.id,
                'sequence': (i + 1) * 10,
                'position': pos,
                'officer_name': name,
            })

    # Map short-form position (as stored on elks.officer.term) to the
    # ceremonial label used in the Lodge meeting template and roll
    # call.  Case-insensitive substring match against the term's
    # position string.  The int is the traditional roll-call
    # sequence order.
    _OFFICER_POSITION_MAP = [
        # (match_keyword, template_label, sequence)
        ('exalted ruler',    'Exalted Ruler',              10),
        ('leading knight',   'Esteemed Leading Knight',    20),
        ('loyal knight',     'Esteemed Loyal Knight',      30),
        ('lecturing knight', 'Esteemed Lecturing Knight',  40),
        ('secretary',        'Lodge Secretary',            50),
        ('treasurer',        'Treasurer',                  60),
        ('esquire',          'Esquire',                    70),
        ('tiler',            'Tiler',                      80),
        ('chaplain',         'Chaplain',                   90),
        ('inner guard',      'Inner Guard',               100),
        ('1 year trustee',   'One Year Trustee',          110),
        ('one year trustee', 'One Year Trustee',          110),
        ('2 year trustee',   'Two Year Trustee',          120),
        ('two year trustee', 'Two Year Trustee',          120),
        ('3 year trustee',   'Three Year Trustee',        130),
        ('three year trustee', 'Three Year Trustee',      130),
        ('organist',         'Organist',                  140),
    ]

    def _resolve_active_officer_terms(self):
        """Pull officer terms from elks.officer.term that are ACTIVE
        as of this meeting's date.

        An active term is one where:
          • term_start <= meeting_date <= term_end, when both dates
            are populated;
          • OR the term's lodge_year matches the meeting's lodge
            year (computed as YYYY-YYYY+1 where the boundary is
            April 1 for Elks), when the dates aren't set.

        Returns a list of (position_label, officer_name, sequence)
        tuples ordered by _OFFICER_POSITION_MAP sequence, or empty
        list if the model isn't available."""
        try:
            Term = self.env['elks.officer.term']
        except (KeyError, ValueError):
            return []

        md = self.meeting_date or fields.Date.context_today(self)

        # Compute Elks lodge year label like '2026-2027' — starts Apr 1.
        try:
            year_start = md.year if md.month >= 4 else md.year - 1
            lodge_year_label = "%d-%d" % (year_start, year_start + 1)
        except AttributeError:
            lodge_year_label = ''

        terms = Term.search([])
        if not terms:
            return []

        # Filter to only ACTIVE terms as of the meeting date.
        active = []
        for t in terms:
            # Date-range wins when both are set.
            ts = getattr(t, 'term_start', False)
            te = getattr(t, 'term_end', False)
            if ts and te:
                if ts <= md <= te:
                    active.append(t)
                continue
            # Otherwise match on lodge_year (Char or Selection)
            ly = getattr(t, 'lodge_year', False)
            if ly and str(ly) == lodge_year_label:
                active.append(t)
                continue
            # Neither dates nor lodge_year → skip (probably historical)

        if not active:
            return []

        # Map each active term to a (label, name, seq).  Multiple
        # terms may match the same slot (e.g. a partial-year
        # succession — Chaplain vacant Apr 1-Jul 21, then someone
        # takes over).  Prefer the LATEST-starting term for a slot
        # since that's who's actually sitting on meeting_date.
        slot_to_pick = {}   # position_label -> (term, seq)
        for t in active:
            raw_pos = (getattr(t, 'position', '') or '').strip().lower()
            match = None
            for kw, label, seq in self._OFFICER_POSITION_MAP:
                if kw in raw_pos:
                    match = (label, seq)
                    break
            if not match:
                continue
            label, seq = match
            existing = slot_to_pick.get(label)
            if existing is None:
                slot_to_pick[label] = (t, seq)
            else:
                # Prefer the later-starting term as "who currently sits"
                new_ts = getattr(t, 'term_start', False)
                old_ts = getattr(existing[0], 'term_start', False)
                if new_ts and (not old_ts or new_ts > old_ts):
                    slot_to_pick[label] = (t, seq)

        # Build the result rows.  For every mapped slot we include a
        # line even if vacant, so the template's roll call still has
        # all traditional positions.
        result_by_seq = {}
        for label, (t, seq) in slot_to_pick.items():
            vacant = bool(getattr(t, 'vacant', False))
            member = getattr(t, 'member_id', False) \
                or getattr(t, 'partner_id', False)
            name = ''
            if not vacant and member and getattr(member, 'name', ''):
                name = member.name
            if vacant or not name:
                name = 'VACANT'
            result_by_seq[seq] = (label, name)

        # Also include any position from the map that had NO active
        # term at all — mark as VACANT so the roll call is
        # complete.
        for kw, label, seq in self._OFFICER_POSITION_MAP:
            if seq not in result_by_seq \
                    and label not in {v[0] for v in result_by_seq.values()}:
                # Only add missing traditional roll-call positions;
                # skip the duplicate keyword aliases (they share a seq).
                already_labels = {v[0] for v in result_by_seq.values()}
                if label not in already_labels:
                    result_by_seq[seq] = (label, 'VACANT')

        # Sort by sequence and emit.
        rows = []
        for seq in sorted(result_by_seq.keys()):
            label, name = result_by_seq[seq]
            rows.append((label, name, seq))
        return rows

    def _populate_committees(self):
        """Build the committee list.

        Strategy:
          1. If elks.committee exists (elkscontacts), iterate its
             records and pull chairperson from whichever field it
             uses — chairperson_id / chair_id / chairman_id.
          2. Otherwise fall back to the hardcoded DEFAULT_COMMITTEES
             list and resolve chairperson names against res.partner
             so at least those match to real member records.
        """
        Line = self.env['elks.lodge.meeting.committee.line']
        Partner = self.env['res.partner']

        Committee = None
        try:
            Committee = self.env['elks.committee']
        except (KeyError, ValueError):
            Committee = None

        # Path 1: dynamic pull from elks.committee
        if Committee is not None:
            committees = Committee.search([])
            if committees:
                for i, com in enumerate(committees):
                    # Try the common field names for chairperson —
                    # whichever one is populated wins.
                    chair_rec = None
                    for path in ('chairperson_id', 'chair_id',
                                 'chairman_id', 'chair', 'chairperson'):
                        val = getattr(com, path, None)
                        if val and hasattr(val, 'id') and val.id:
                            chair_rec = val
                            break
                    Line.create({
                        'meeting_id': self.id,
                        'sequence': (i + 1) * 10,
                        'committee_id': com.id,
                        'committee_name': com.name,
                        'chairperson_id':
                            chair_rec.id if chair_rec else False,
                    })
                return

        # Path 2: hardcoded fallback with name→partner resolution
        for i, (name, chair_name) in enumerate(DEFAULT_COMMITTEES):
            chair_partner = False
            if chair_name:
                p = Partner.search(
                    [('name', '=', chair_name)], limit=1,
                )
                if p:
                    chair_partner = p.id
            Line.create({
                'meeting_id': self.id,
                'sequence': (i + 1) * 10,
                'committee_name': name,
                'chairperson_id': chair_partner,
                'chairperson': chair_name,  # keeps display if partner not found
            })

    def _populate_meeting_money(self):
        """Link (or pull from) the elks.meeting.money record for this
        meeting date.  If one already exists, use its values; if not,
        the Secretary fills in Project Dollars / Fines on this agenda
        form and a Meeting Money record is created at finalize time
        via action_finalize()."""
        try:
            Meeting = self.env['elks.meeting.money']
        except (KeyError, ValueError):
            return
        # First: is there already a Meeting Money record for this date?
        exact = Meeting.search(
            [('meeting_date', '=', self.meeting_date)],
            limit=1,
        )
        if exact:
            self.meeting_money_id = exact.id
            self.project_dollars_amount = exact.project_dollars_amount
            self.fines_amount = exact.fines_amount
            return
        # Otherwise: pre-fill with the most recent prior record's
        # values so the Secretary has something to start from.
        recent = Meeting.search(
            [('meeting_date', '<', self.meeting_date)],
            order='meeting_date desc', limit=1,
        )
        if recent:
            self.project_dollars_amount = recent.project_dollars_amount
            self.fines_amount = recent.fines_amount

    def _populate_prior_meeting_link(self):
        """Link the prior meeting record (whose minutes get read)."""
        prior = self.search([
            ('meeting_date', '<', self.meeting_date),
            ('id', '!=', self.id),
        ], order='meeting_date desc', limit=1)
        if prior:
            self.prior_meeting_id = prior.id
            self.minutes_read_by = self.exalted_ruler_name or ''

    def _populate_propositions(self):
        """Pull active membership pipeline into Propositions section.

        Categorises by stage:
          • 'proposed' | 'investigation' → For Membership
          • 'balloting'                  → Balloting on Candidates
          • 'elected' (not yet inducted) → Members to be Inducted
        Skips cleanly if elks.membership.application isn't installed."""
        try:
            App = self.env['elks.membership.application']
        except (KeyError, ValueError):
            return
        Line = self.env['elks.lodge.meeting.proposition']
        stage_to_section = {
            'proposed':      'membership',
            'investigation': 'membership',
            'balloting':     'balloting',
            'elected':       'induction',
        }
        for app in App.search([
            ('stage', 'in', list(stage_to_section)),
        ]):
            section = stage_to_section[app.stage]
            Line.create({
                'meeting_id': self.id,
                'section':    section,
                # Auto-check induction for already-elected candidates;
                # Balloting candidates start unchecked (their fate is
                # unknown until the vote at the meeting).
                'will_be_inducted': section == 'induction',
                'candidate_name': _resolve_candidate_name(app),
                'city_state': _resolve_city_state(app),
                'employer': _first_attr(
                    app,
                    # Direct fields on the application itself
                    'employer', 'x_employer', 'company_name',
                    'x_company_name', 'applicant_employer',
                    'candidate_employer', 'proposed_employer',
                    'x_applicant_employer',
                    # Related through linked partner
                    'partner_id.parent_id.name',
                    'partner_id.x_employer',
                    'partner_id.company_name',
                    'proposed_member_id.parent_id.name',
                    'proposed_member_id.x_employer',
                    'proposed_member_id.company_name',
                    'candidate_id.parent_id.name',
                    'candidate_id.company_name',
                    default='',
                ),
                'occupation': _first_attr(
                    app,
                    # Direct fields on the application itself
                    'occupation', 'x_occupation', 'job_title',
                    'x_job_title', 'function',
                    'applicant_occupation', 'candidate_occupation',
                    'proposed_occupation', 'x_applicant_occupation',
                    # Related through linked partner (Odoo's res.partner
                    # uses `function` for job title)
                    'partner_id.function',
                    'partner_id.x_occupation',
                    'partner_id.x_job_title',
                    'proposed_member_id.function',
                    'proposed_member_id.x_occupation',
                    'candidate_id.function',
                    'candidate_id.x_occupation',
                    default='',
                ),
                'sponsor': _first_attr(
                    app,
                    'sponsor_id.name', 'sponsor.name',
                    'sponsor_name', 'x_sponsor_name',
                    'proposer_id.name', 'proposed_by_id.name',
                    default='',
                ),
            })

    def _populate_deaths(self):
        """Pull member deaths that need to be read on the floor at
        this meeting.

        Deaths are tracked on res.partner via x_date_of_death and
        x_death_clms_status.  We surface anyone whose date_of_death
        is set, who hasn't already been read on the floor at a prior
        meeting (x_date_read_on_floor is False AND not in this
        module's report log), and who died within the last 90 days
        as a safety window."""
        try:
            Partner = self.env['res.partner']
            if 'x_date_of_death' not in Partner._fields:
                return
        except (KeyError, ValueError):
            return
        since = self.meeting_date - timedelta(days=90)
        already_reported = self._ids_already_reported(
            'res.partner', category='death')
        domain = [
            ('x_date_of_death', '!=', False),
            ('x_date_of_death', '>=', since),
            ('x_date_of_death', '<=', self.meeting_date),
            ('x_date_read_on_floor', '=', False),
        ]
        if already_reported:
            domain.append(('id', 'not in', list(already_reported)))
        deaths = Partner.with_context(active_test=False).search(domain)
        if deaths:
            lines = []
            for d in deaths:
                dod = d.x_date_of_death
                lines.append("• %s — %s" % (d.name, dod))
            self.deaths_of_members = "\n".join(lines)

    # ══════════════════════════════════════════════════════════════
    # Report log — track which source records have been read on the
    # floor at which meeting, so future agendas skip them.
    # ══════════════════════════════════════════════════════════════
    @api.model
    def _ids_already_reported(self, res_model, category=None):
        """Return the set of res_ids for `res_model` that appear in
        ANY meeting's report_log_ids (optionally filtered to a single
        category: 'death', 'proposition', 'event').  Used by the
        _populate_* methods to skip records already read on the
        floor at a prior meeting."""
        Log = self.env['elks.lodge.meeting.report.log'].sudo()
        domain = [('res_model', '=', res_model)]
        if category:
            domain.append(('category', '=', category))
        return set(Log.search(domain).mapped('res_id'))

    def _log_reported_records(self):
        """Called from action_generate_minutes.  Writes a report-log
        entry for every source record that appeared on this meeting's
        agenda: deaths, propositions, community events.  Idempotent
        via a (meeting, model, id) unique-per-meeting check — pressing
        Generate Minutes a second time won't create duplicates.

        Also stamps res.partner.x_date_read_on_floor for deaths so
        the existing Deaths — Pending CLMS queue view (which shows
        that field directly) stays in sync."""
        Log = self.env['elks.lodge.meeting.report.log'].sudo()
        existing_keys = set()
        for row in self.report_log_ids:
            existing_keys.add((row.res_model, row.res_id))

        # Cache the meeting's own name once — used in the chatter
        # note posted to each source record.
        meeting_label = self.name or "Lodge Meeting %s" % (
            self.meeting_date or '')
        meeting_url = "/odoo/action-elkssecretary.action_lodge_meetings/%d" % self.id

        def _add(category, res_model, res_id, title, chatter_body=None):
            if not res_id:
                return
            key = (res_model, res_id)
            if key in existing_keys:
                return
            Log.create({
                'meeting_id': self.id,
                'category': category,
                'res_model': res_model,
                'res_id': res_id,
                'title': title or '',
                'reported_date': self.meeting_date,
            })
            existing_keys.add(key)
            # Post a chatter note on the source record so their
            # history sheet shows this meeting.  Silently ignored
            # if the target model doesn't inherit mail.thread.
            if not chatter_body:
                cat_label = {
                    'death':       "Read on the floor — Deaths of Members",
                    'proposition': "Read on the floor — Membership Proposition",
                    'event':       "Announced on the floor — Community Event",
                    'other':       "Read on the floor",
                }.get(category, "Read on the floor")
                chatter_body = (
                    '%s at <a href="%s">%s</a> '
                    'on %s.'
                ) % (
                    cat_label,
                    meeting_url,
                    meeting_label,
                    self.meeting_date or '',
                )
            try:
                target = self.env[res_model].sudo().browse(res_id)
                if target.exists() and hasattr(target, 'message_post'):
                    target.message_post(body=chatter_body)
            except (KeyError, ValueError, AttributeError):
                pass

        # ── Deaths ──────────────────────────────────────────────
        # Re-run the same death query used at populate time so we
        # log exactly what got printed.  Anyone the Secretary added
        # to the text field by hand won't be in this list — that's
        # fine, the log is for source-record tracking, not the raw
        # text.
        try:
            Partner = self.env['res.partner']
            if 'x_date_of_death' in Partner._fields:
                since = self.meeting_date - timedelta(days=90)
                already = self._ids_already_reported(
                    'res.partner', category='death')
                domain = [
                    ('x_date_of_death', '!=', False),
                    ('x_date_of_death', '>=', since),
                    ('x_date_of_death', '<=', self.meeting_date),
                    ('x_date_read_on_floor', '=', False),
                ]
                if already:
                    domain.append(('id', 'not in', list(already)))
                deaths = Partner.with_context(active_test=False).search(
                    domain)
                for d in deaths:
                    _add('death', 'res.partner', d.id, d.name)
                    # Keep the existing x_date_read_on_floor flag in
                    # sync so the Deaths CLMS queue view still works.
                    if not d.x_date_read_on_floor:
                        try:
                            d.sudo().write({
                                'x_date_read_on_floor':
                                    self.meeting_date,
                            })
                        except Exception:
                            _logger.warning(
                                "Could not stamp x_date_read_on_floor "
                                "on partner %s", d.id)
        except (KeyError, ValueError):
            pass

        # ── Propositions ────────────────────────────────────────
        for p in self.proposition_ids:
            # Prefer the linked application record when we have one,
            # so a candidate isn't re-listed at every meeting until
            # they're inducted.
            app_ref = False
            for path in ('application_id', 'x_application_id',
                         'candidate_id', 'proposed_member_id',
                         'partner_id'):
                val = getattr(p, path, False)
                if val and hasattr(val, 'id') and val.id:
                    app_ref = (val._name, val.id)
                    break
            if app_ref:
                model_name, rec_id = app_ref
                _add('proposition', model_name, rec_id,
                     p.candidate_name or '')
            else:
                # Fall back to logging the proposition line itself so
                # we at least have SOMETHING to reference.
                _add('proposition', p._name, p.id,
                     p.candidate_name or '')

            # ALSO post a chatter note on the linked partner (if
            # any) — even if the log entry went against the
            # application record.  That way a Secretary looking at
            # the candidate's contact card sees this meeting in
            # their history sheet.
            linked_partner = False
            for path in ('partner_id', 'proposed_member_id',
                         'candidate_id.partner_id',
                         'application_id.partner_id'):
                # Walk dotted path to resolve.
                cur = p
                for attr in path.split('.'):
                    cur = getattr(cur, attr, False)
                    if not cur:
                        break
                if cur and getattr(cur, '_name', '') == 'res.partner' \
                        and getattr(cur, 'id', 0):
                    linked_partner = cur
                    break
            if linked_partner and (
                    not app_ref
                    or app_ref[0] != 'res.partner'
                    or app_ref[1] != linked_partner.id):
                # Post directly to the partner's chatter without
                # creating a separate log row (log stays against
                # the application/proposition).
                try:
                    linked_partner.sudo().message_post(body=(
                        'Membership proposition read on the floor at '
                        '<a href="%s">%s</a> on %s.'
                    ) % (
                        meeting_url,
                        meeting_label,
                        self.meeting_date or '',
                    ))
                except Exception:
                    _logger.warning(
                        "Could not post chatter to partner %s",
                        linked_partner.id)

        # ── Community events ────────────────────────────────────
        # NOTE: events are NOT logged as "reported" — per the user's
        # rule, upcoming events keep appearing on every agenda until
        # their event date has passed.  The date-window filter in
        # _populate_events (x_event_date >= meeting_date) naturally
        # drops past events, so no reported-tracking is needed for
        # this category.

    def _populate_bills_from_purchase(self):
        """Sum purchase.order amounts sitting in Floor-Vote approval
        state — those are exactly the bills the members vote on at the
        meeting ("Motion to pay the bills…").  Populates bills_amount
        AND records the linked POs on bill_ids so the Secretary can
        see which bills the total was built from.  Skipped cleanly
        if elkspurchase / x_approval_state aren't installed."""
        try:
            PO = self.env['purchase.order']
            if 'x_approval_state' not in PO._fields:
                return
        except (KeyError, ValueError):
            return
        floor_orders = PO.search([
            ('x_approval_state', '=', 'floor'),
            ('state', 'not in', ('cancel', 'done')),
        ])
        if floor_orders:
            self.bills_amount = sum(
                floor_orders.mapped('amount_total')
            )
            self.bill_ids = [(6, 0, floor_orders.ids)]

    def _populate_events(self):
        """Pull upcoming lodge events (from elksevent) into the
        Community Activities section.

        Matches elksevent's own 'Event Bookings' action exactly:
          ('x_is_event', '=', True), ('parent_id', '=', False)

        Sort: ASCENDING by x_event_date — nearest upcoming event
        first (Secretary reads the soonest-happening items to the
        floor first, then works down the calendar).  Formatted as
        bullet points.  Window: today (meeting_date) through +60
        days."""
        try:
            Task = self.env['project.task']
            if 'x_is_event' not in Task._fields:
                return
        except (KeyError, ValueError):
            return
        upcoming = Task.search([
            ('x_is_event', '=', True),
            ('parent_id', '=', False),
            ('x_event_date', '>=', self.meeting_date),
            ('x_event_date', '<=',
                self.meeting_date + timedelta(days=60)),
        ], order='x_event_date asc', limit=50)
        if upcoming:
            lines = []
            for ev in upcoming:
                ed = getattr(ev, 'x_event_date', '')
                # Format the date nicely (e.g. "Oct 24") if possible
                try:
                    ed_str = ed.strftime("%b %d") if ed else ''
                except AttributeError:
                    ed_str = str(ed)
                lines.append("• %s — %s" % (ed_str, ev.name))
            self.community_activities = "\n".join(lines)
        else:
            # Log so the Secretary can see the query returned nothing
            # rather than silently getting a blank section.
            _logger.info(
                "elkssecretary.lodge.meeting: no upcoming events "
                "found for %s (looked 60 days ahead of %s)",
                self.name, self.meeting_date,
            )

    # ── Re-run auto-populate (on-demand refresh) ─────────────────
    def action_repopulate_from_system(self):
        """Wipe the auto-populated sections and re-pull everything.

        Use this after fixing a data issue (renamed candidate, added
        an event) instead of deleting and recreating the meeting.
        Preserves manual fields the user has typed (Sickness &
        Distress, Business, etc.) but replaces the O2m tables that
        the system fills in — Officers, Committees, Propositions,
        floor-vote Bills.

        Text sections (Community Activities, Deaths) get overwritten
        only if their current value looks system-generated (starts
        with a bullet).  Free-form notes you typed stay put."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_(
                "Can't repopulate a finalized meeting.  Re-open first."))
        # Wipe the O2m tables we'll refill
        self.officer_ids.unlink()
        self.committee_ids.unlink()
        self.proposition_ids.unlink()
        # Clear bill_ids (M2m) — repopulate will re-search
        self.bill_ids = [(5, 0, 0)]
        # Clear community/deaths ONLY if they look auto-generated
        if self.community_activities and \
                self.community_activities.strip().startswith("•"):
            self.community_activities = False
        if self.deaths_of_members and \
                self.deaths_of_members.strip().startswith("•"):
            self.deaths_of_members = False
        # Re-run everything
        self._populate_from_system()
        self.message_post(body=_(
            "Auto-populated sections refreshed from the system."))
        return True

    # ── State transitions ────────────────────────────────────────
    def action_finalize(self):
        """Mark agenda finalized AND sync the Meeting Money record
        (creates one if it doesn't exist, updates it if it does)."""
        for rec in self:
            rec._sync_meeting_money_record()
            rec.state = 'finalized'
            rec.message_post(body=_("Meeting agenda finalized."))

    def _sync_meeting_money_record(self):
        """Push Project Dollars + Fines back to a matching
        elks.meeting.money record.  Creates one if none exists on
        this date."""
        try:
            Meeting = self.env['elks.meeting.money']
        except (KeyError, ValueError):
            return
        vals = {
            'meeting_date': self.meeting_date,
            'project_dollars_amount': self.project_dollars_amount,
            'fines_amount': self.fines_amount,
        }
        if self.meeting_money_id:
            self.meeting_money_id.write(vals)
        else:
            existing = Meeting.search(
                [('meeting_date', '=', self.meeting_date)],
                limit=1,
            )
            if existing:
                existing.write(vals)
                self.meeting_money_id = existing.id
            else:
                self.meeting_money_id = Meeting.create(vals).id

    def action_reopen(self):
        for rec in self:
            rec.state = 'draft'

    # ── DOCX generation ──────────────────────────────────────────
    def action_generate_minutes(self):
        """Generate the meeting MINUTES.

        Same underlying template + fill logic as the agenda download,
        but stored separately on minutes_docx (so the agenda draft and
        the finalized minutes don't overwrite each other).  Meant to
        be pressed AFTER the meeting when officer statuses,
        substitutions, motion names, close time, etc. have been
        entered.  Records who/when for the audit trail and posts to
        chatter.  Also flips the state to finalized so downstream
        reports know the minutes are locked."""
        self.ensure_one()
        if not HAS_DOCX:
            raise UserError(_(
                "python-docx is not installed on the server.  Ask "
                "the administrator to run `pip install python-docx` "
                "and restart Odoo."))
        buf = self._fill_template()
        filename = "Lodge_Meeting_Minutes_%s.docx" % (
            self.meeting_date.isoformat()
            if self.meeting_date else 'draft'
        )
        self.write({
            'minutes_docx': base64.b64encode(buf.getvalue()),
            'minutes_docx_filename': filename,
            'minutes_generated_date': fields.Datetime.now(),
            'minutes_generated_by': self.env.user.id,
            'state': 'finalized',
        })
        # Sync Meeting Money at the same time so YTD totals stay right
        self._sync_meeting_money_record()
        # Stamp every record that appeared on this agenda as
        # "reported at meeting X" — future meetings' auto-populate
        # skips anything already logged so nothing gets read twice.
        self._log_reported_records()
        self.message_post(body=_(
            "Meeting minutes generated by %s.") % self.env.user.name)
        return {
            'type': 'ir.actions.act_url',
            'url': "/web/content/?model=elks.lodge.meeting"
                   "&id=%d&field=minutes_docx&filename_field="
                   "minutes_docx_filename&download=true" % self.id,
            'target': 'self',
        }

    def action_download_docx(self):
        """Fill the Word template with this meeting's data and store
        the result on agenda_docx.  Returns an act_url that triggers
        the download."""
        self.ensure_one()
        if not HAS_DOCX:
            raise UserError(_(
                "The python-docx library is not installed on the "
                "Odoo server.  Ask the administrator to run:\n\n"
                "    pip install python-docx\n\n"
                "…and then restart Odoo."
            ))
        buf = self._fill_template()
        filename = "Lodge_Meeting_Agenda_%s.docx" % (
            self.meeting_date.isoformat() if self.meeting_date else 'draft'
        )
        self.write({
            'agenda_docx':          base64.b64encode(buf.getvalue()),
            'agenda_docx_filename': filename,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': "/web/content/?model=elks.lodge.meeting"
                   "&id=%d&field=agenda_docx&filename_field="
                   "agenda_docx_filename&download=true" % self.id,
            'target': 'self',
        }

    def _format_substitutions(self):
        """Format substitution_ids for the docx template.

        Returns an inline string like:
            'Esquire — John Doe; Chaplain — Jane Smith'
        Empty string if no substitutions on this meeting, so the
        template's blank underscores are preserved for hand-writing
        at the meeting."""
        if not self.substitution_ids:
            return ''
        parts = []
        for sub in self.substitution_ids:
            pos = sub.officer_id.position or ''
            name = sub.substitute_id.name if sub.substitute_id else ''
            if pos and name:
                parts.append("%s — %s" % (pos, name))
            elif pos:
                parts.append("%s — ?" % pos)
            elif name:
                parts.append(name)
        return "; ".join(parts)

    def _format_motions_block(self, section_key):
        """Return a formatted multi-line string of all motions for a
        given section ('unfinished', 'new', 'good_of_order').

        Each motion prints as up to 3 lines:
            Motion: <text>
              Moved by: X   Seconded by: Y
              Result: Passed (5-2)

        Empty string if no motions in that section — so we can append
        this block to the existing section text without dumping a
        stray header when there's nothing to say."""
        motions = [m for m in self.motion_ids if m.section == section_key]
        if not motions:
            return ''
        lines = []
        for m in motions:
            desc = (m.description or '').strip()
            if desc:
                lines.append("Motion: %s" % desc)
            mover = m.motion_by_id.name if m.motion_by_id else ''
            seconder = m.seconded_by_id.name if m.seconded_by_id else ''
            if mover or seconder:
                lines.append("  Moved by: %s   Seconded by: %s" % (
                    mover or '________________',
                    seconder or '________________',
                ))
            if m.result:
                result_label = dict(
                    self.env['elks.lodge.meeting.motion']
                        ._fields['result'].selection
                ).get(m.result, m.result)
                if m.vote_notes:
                    lines.append("  Result: %s (%s)" %
                                 (result_label, m.vote_notes))
                else:
                    lines.append("  Result: %s" % result_label)
            elif m.vote_notes:
                lines.append("  Vote: %s" % m.vote_notes)
        return "\n".join(lines)

    def _effective_officer_name(self, position_keywords):
        """Return the name of the person actually sitting in a chair
        for this meeting.

        Walks officer_ids for a row whose position contains ANY of the
        given keywords (case-insensitive).  If that officer's status is
        'excused' or 'absent', looks in substitution_ids for a fill-in
        against the SAME officer row and returns the substitute's name.
        Otherwise returns the officer's own name.

        Used for the OPENING placeholder (Exalted Ruler) so the printed
        agenda auto-fills with whoever will actually gavel the meeting
        open — the ER if present, else their pre-arranged substitute.

        Returns '' if nothing resolves, letting the template's
        underscore blank stay put for hand-fill at the meeting."""
        kws = [k.lower() for k in position_keywords]
        officer = False
        for o in self.officer_ids:
            pos = (o.position or '').lower()
            if any(k in pos for k in kws):
                officer = o
                break
        if not officer:
            # No matching officer row — fall back to the free-text
            # Exalted Ruler default the Secretary keyed in.
            return ''
        if officer.status in ('excused', 'absent'):
            # Look for a substitution row against this officer.
            for sub in self.substitution_ids:
                if sub.officer_id == officer and sub.substitute_id:
                    return sub.substitute_id.name or ''
            # Absent / excused but no sub filed — leave blank so the
            # Secretary sees an obvious hole to fill by hand.
            return ''
        return officer.officer_name or ''

    def _get_template_path(self):
        import os
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'static', 'src', 'lodge_meeting', 'template.docx',
        )

    def _fill_template(self):
        """Open the template and replace placeholders.

        Strategy:
          • Simple text replacements for [DATE], [YEAR], and the
            money-blank patterns.
          • Table-row population: identify tables by their header text
            (Position/Officer/Status → officer roll call, etc.) and
            fill body rows from the meeting's O2m lines.
        """
        doc = _DocxDocument(self._get_template_path())
        subs = self._get_substitutions()

        # (a) Paragraph text replacement — including runs inside cells
        for para in doc.paragraphs:
            self._replace_in_paragraph(para, subs)
        for tbl in doc.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        self._replace_in_paragraph(para, subs)

        # (a2) Regex-based fills for placeholders whose blank width
        # varies between template revisions.  These are matched by
        # phrase + trailing underscore run, so a template with 20 vs.
        # 30 underscores still fills correctly.  Runs are joined
        # temporarily so a placeholder split across XML runs matches.
        self._regex_fill_placeholders(doc)

        # (b) Table-row population by header signature
        self._populate_tables(doc)

        # (c) Text-section bullets — Deaths, Sickness, Community, etc.
        self._populate_text_sections(doc)

        # (d) Reorder Treasurer's Report so Motion to Pay Bills sits
        # right below Bills (closes out the vote before the balance-
        # sheet items are read).  Applied in memory per generation
        # so the source template on disk stays untouched.
        self._reorder_treasurer_section(doc)

        # (e) Itemize bills — inserts a bullet list under "Bills of:"
        # showing Vendor / PO# / Amount for each line.
        self._insert_itemized_bills(doc)

        # (f) Move "Project Dollars / Fines" (and the follow-up
        # "All birthday dollars go Project funds..." note) to sit
        # directly above the CLOSING: block, where the Secretary
        # naturally reads them out at the end of the meeting.
        self._reorder_project_dollars_before_closing(doc)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return buf

    def _get_substitutions(self):
        """Return the {placeholder: replacement} dict.

        Uses the template's actual placeholder strings — [DATE], [YEAR]
        — plus targeted phrasing swaps for the money and time blanks."""
        md = self.meeting_date or fields.Date.context_today(self)
        try:
            date_label = md.strftime("%B %d")
        except AttributeError:
            date_label = str(md)
        year_label = str(md.year) if hasattr(md, 'year') else ''

        # Prior meeting date, for "minutes of the regular meeting of ___"
        prior_date = (
            self.prior_meeting_id.meeting_date.strftime("%B %d, %Y")
            if (self.prior_meeting_id
                and self.prior_meeting_id.meeting_date)
            else ""
        )

        # When a Monetary field is 0 (unset), print the original blank
        # underscores instead of "$0.00" so the Secretary can pen in
        # the amount at the meeting.  Any non-zero value prints with
        # its formatted dollar amount.
        def _money(v, blank="$__________"):
            if not v:
                return blank
            # format() supports comma thousands separator; % doesn't.
            return "$" + format(v, ",.2f")

        return {
            "[DATE]":  date_label,
            "[YEAR]":  year_label,
            # Time / person / count blanks — target the exact phrase
            # so we don't clobber unrelated underscores in the template.
            "called to order at _______ pm":
                "called to order at %s" % (self.open_time or "_______ pm"),
            "with ______ members present":
                "with %s members present" %
                (str(self.members_present_open)
                 if self.members_present_open else "______"),
            # Exalted Ruler name in OPENING sentence — prefer the
            # actual ER officer's name from the roll call, falling back
            # to their substitute if excused/absent, then to the
            # free-text default, then to underscores.
            "Exalted Ruler ________________________":
                "Exalted Ruler %s" % (
                    self._effective_officer_name(['exalted ruler'])
                    or self.exalted_ruler_name
                    or "________________________"
                ),
            "The minutes of the regular meeting of _____________________, "
            "were read by":
                "The minutes of the regular meeting of %s were read by" %
                (prior_date or "_____________________"),
            "were read by _______________________________":
                "were read by %s" % (
                    self.minutes_read_by or "_______________________________"
                ),
            # Substitutions line — format the O2m rows as
            # "Position — Substitute; Position — Substitute; ..."
            # or preserve the underscore blank if there are no rows.
            "Substitutions / fill-ins: _______________________________________________":
                "Substitutions / fill-ins: %s" % (
                    self._format_substitutions() or
                    "_______________________________________________"
                ),
            "Project Dollars: $__________":
                "Project Dollars: %s" % _money(self.project_dollars_amount),
            "Fines: $__________":
                "Fines: %s" % _money(self.fines_amount),
            "Bills of: $__________":
                "Bills of: %s" % _money(self.bills_amount),
            "Assets: $__________":
                "Assets: %s" % _money(self.assets_amount),
            "Liability: $__________":
                "Liability: %s" % _money(self.liabilities_amount),
            "Equity: $__________":
                "Equity: %s" % _money(self.equity_amount),
            "Total Liabilities & Equity: $__________":
                "Total Liabilities & Equity: %s" %
                _money(self.total_le_amount),
            "Motion to pay the bills was made by ________ and seconded by _________.":
                "Motion to pay the bills was made by %s and seconded by %s." % (
                    self.bills_motion_by or "________",
                    self.bills_seconded_by or "_________",
                ),
            "closed in due form at _________ p.m.":
                "closed in due form at %s p.m." %
                (self.close_time or "_________"),
            "with ______ members present.":
                "with %s members present." %
                (str(self.members_present_close)
                 if self.members_present_close else "______"),
        }

    @staticmethod
    def _replace_in_paragraph(para, subs):
        """Replace substrings inside a Word paragraph.

        Runs can split a placeholder across multiple XML nodes.  We
        join → replace → re-emit into the first run to preserve at
        least one run's formatting."""
        if not para.runs:
            return
        text = "".join(r.text for r in para.runs)
        changed = False
        for old, new in subs.items():
            if old in text:
                text = text.replace(old, new)
                changed = True
        if changed:
            para.runs[0].text = text
            for r in para.runs[1:]:
                r.text = ""

    def _regex_fill_placeholders(self, doc):
        """Regex-based replacement pass for placeholders where the
        blank width varies between template revisions.

        Currently handles: "Exalted Ruler ______" — the OPENING
        sentence's ER blank.  Match any run of 2+ underscores directly
        following the phrase, so a template with 15, 20, or 40
        underscores all fill in the same way.

        Priority: substitution filed against the ER position →
        Exalted Ruler officer's own name (regardless of Present status
        as long as no sub is filed) → the free-text default
        exalted_ruler_name field.  Falls through (no change) if none
        of the above resolves — the underscore blank stays visible
        for hand-fill at the meeting.
        """
        import re

        # Resolve the name once — same priority order as
        # _effective_officer_name but with an override so a filed
        # substitution wins regardless of status (matching the
        # user's request: "fill in when present unless there is a
        # substitution").
        er_officer = False
        for o in self.officer_ids:
            if 'exalted ruler' in (o.position or '').lower():
                er_officer = o
                break

        er_name = ''
        if er_officer:
            # Substitution takes precedence — if anyone is filling
            # the ER chair for THIS meeting, they gavel it open.
            for sub in self.substitution_ids:
                if sub.officer_id == er_officer and sub.substitute_id:
                    er_name = sub.substitute_id.name or ''
                    break
            if not er_name:
                # No sub — use the ER's own name (present or blank).
                # If the Secretary has explicitly marked them
                # Absent/Excused with no sub filed, don't lie —
                # leave the blank for hand-fill.
                if er_officer.status in ('absent', 'excused'):
                    er_name = ''
                else:
                    er_name = er_officer.officer_name or ''

        if not er_name:
            er_name = self.exalted_ruler_name or ''

        if not er_name:
            return  # Nothing to fill — leave underscores in place.

        # Match "Exalted Ruler" (optional colon) followed by whitespace
        # and 2+ underscores.  Replace the whole underscore run with
        # the resolved name.
        pattern = re.compile(
            r'(Exalted Ruler:?\s+)_{2,}',
            re.IGNORECASE,
        )
        replacement = r'\g<1>' + er_name

        def _apply(para):
            if not para.runs:
                return
            text = "".join(r.text for r in para.runs)
            if not pattern.search(text):
                return
            new_text = pattern.sub(replacement, text)
            if new_text != text:
                para.runs[0].text = new_text
                for r in para.runs[1:]:
                    r.text = ""

        for para in doc.paragraphs:
            _apply(para)
        for tbl in doc.tables:
            for row in tbl.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        _apply(para)

    def _populate_tables(self, doc):
        """Identify each expandable table by its header cells and add
        body rows from the meeting's O2m data."""
        # First pass: figure out which table (if any) is the
        # 'Members to be Inducted' table.  It has an empty header
        # row so header-text matching won't find it — we identify it
        # by the "Members to be Inducted:" paragraph that precedes it
        # in document order.
        induction_tbl = self._find_induction_table(doc)
        if induction_tbl is not None:
            self._fill_induction_table(induction_tbl)

        for tbl in doc.tables:
            if not tbl.rows:
                continue
            # Skip if this is the induction table (already filled)
            if induction_tbl is not None and tbl is induction_tbl:
                continue
            header_text = " ".join(
                cell.text.strip().lower()
                for cell in tbl.rows[0].cells
            )

            # Officer roll call: Position / Officer / Status
            if ("position" in header_text and "officer" in header_text
                    and "status" in header_text):
                # Officer rows are pre-filled with names + blank status.
                # We just overwrite the Status column for present/absent.
                positions_map = {
                    o.position.lower(): o for o in self.officer_ids
                }
                for row in tbl.rows[1:]:
                    key = row.cells[0].text.strip().lower()
                    officer = positions_map.get(key)
                    if officer:
                        # Overwrite status cell
                        self._set_cell_text(
                            row.cells[2], officer._status_label()
                        )
                continue

            # Committee reports: Committee / Chairperson / Report
            if ("committee" in header_text
                    and "chairperson" in header_text
                    and "report" in header_text):
                committees_map = {
                    c.committee_name.lower(): c for c in self.committee_ids
                }
                for row in tbl.rows[1:]:
                    key = row.cells[0].text.strip().lower()
                    committee = committees_map.get(key)
                    if not committee:
                        continue
                    # Overwrite Chairperson cell with the actual
                    # linked member's name (or fall back to the char
                    # cache).  Only overwrites when we have something.
                    chair_name = (
                        committee.chairperson_id.name
                        if committee.chairperson_id
                        else committee.chairperson
                    )
                    if chair_name and len(row.cells) >= 2:
                        self._set_cell_text(row.cells[1], chair_name)
                    if committee.report_text and len(row.cells) >= 3:
                        self._set_cell_text(
                            row.cells[2], committee.report_text
                        )
                continue

            # Propositions For Membership: # / Name / City-State / Employer / Sponsor
            if ("name" in header_text
                    and "employer" in header_text
                    and "sponsor" in header_text):
                self._fill_proposition_table(tbl, 'membership')
                continue

            # Balloting on Candidates: # / Name / City-State / Occupation / Sponsor
            if ("name" in header_text
                    and "occupation" in header_text
                    and "sponsor" in header_text):
                self._fill_proposition_table(tbl, 'balloting')
                continue

            # Propositions For Affiliation / Reinstatement: # / Name / City-State
            if (len(tbl.rows[0].cells) == 3
                    and "name" in header_text
                    and "city" in header_text):
                self._fill_proposition_table(tbl, 'affiliation')
                continue

    def _insert_itemized_bills(self, doc):
        """Insert a Word TABLE under 'Bills of:' listing each bill:
        Vendor / PO# / Amount, with a total row at the bottom.

        Bills come from two sources:
          • bill_ids (Many2many purchase.order): Floor-Vote queue POs.
              Vendor = partner_id.name, PO# = name, amount_total.
          • manual_bill_ids (O2m elks.lodge.meeting.bill): ad-hoc
              entries typed by the Secretary.  No PO#.

        Removes the template's placeholder bullet paragraph (if any)
        that sits directly below 'Bills of:' so the table takes its
        place cleanly.  If no bills exist, nothing is inserted."""
        # Build the list of (vendor, po_number, amount) rows
        rows = []
        for po in self.bill_ids:
            rows.append((
                (po.partner_id.name if po.partner_id else ''),
                (po.name or ''),
                po.amount_total or 0.0,
            ))
        for mb in self.manual_bill_ids:
            rows.append((
                mb.vendor or '',
                '',  # no PO number for manual entries
                mb.amount or 0.0,
            ))
        if not rows:
            return

        # Find "Bills of:" and the placeholder bullet paragraph
        # directly below it.  We remove the bullet placeholder and
        # insert a table in its place.
        bills_idx = None
        bullet_idx = None
        paras = doc.paragraphs
        for i, p in enumerate(paras):
            if bills_idx is None and "Bills of:" in p.text:
                bills_idx = i
                continue
            if bills_idx is not None and bullet_idx is None:
                if p.text.strip() in ('', '-', '•', '·'):
                    bullet_idx = i
                    break
                if any(k in p.text for k in (
                        "Motion", "Assets:", "Liability:", "Equity:",
                        "Total Liabilities")):
                    break
        if bills_idx is None:
            return

        anchor_para = paras[bills_idx]

        # Build the table at the end of the doc, then MOVE its XML
        # element to sit as a sibling right after "Bills of:".
        # (python-docx doesn't provide "add_table_after(paragraph)",
        # so this deepcopy+addnext trick is the standard pattern.)
        from copy import deepcopy
        from docx.oxml.ns import qn
        from docx.shared import Pt, Inches

        n_rows = len(rows) + 2  # header + one per bill + total
        tbl = doc.add_table(rows=n_rows, cols=3)
        try:
            tbl.style = 'Light Grid Accent 1'
        except (KeyError, ValueError):
            # Style not present in this template — fall back to
            # 'Table Grid' which is always available.
            try:
                tbl.style = 'Table Grid'
            except (KeyError, ValueError):
                pass

        # Set column widths so Vendor gets most of the row, PO# fits
        # its number, Amount is right-aligned money.
        widths = (Inches(3.4), Inches(1.2), Inches(1.4))
        for row in tbl.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = w

        # Header row
        hdr = tbl.rows[0].cells
        hdr[0].text = "Vendor"
        hdr[1].text = "PO #"
        hdr[2].text = "Amount"
        for c in hdr:
            for para in c.paragraphs:
                for r in para.runs:
                    r.bold = True

        # Data rows
        total = 0.0
        for i, (vendor, po_num, amount) in enumerate(rows, start=1):
            cells = tbl.rows[i].cells
            cells[0].text = vendor or ''
            cells[1].text = po_num or ''
            cells[2].text = "$" + format(amount or 0.0, ",.2f")
            total += (amount or 0.0)

        # Total row
        totals = tbl.rows[-1].cells
        totals[0].text = ""
        totals[1].text = "Total"
        totals[2].text = "$" + format(total, ",.2f")
        for c in (totals[1], totals[2]):
            for para in c.paragraphs:
                for r in para.runs:
                    r.bold = True

        # Move the table from end-of-doc to sit right after
        # "Bills of:".  We deepcopy so the reference at the end of
        # the doc can be removed cleanly.
        tbl_element = tbl._element
        new_tbl = deepcopy(tbl_element)
        tbl_element.getparent().remove(tbl_element)
        anchor_para._element.addnext(new_tbl)

        # Insert a blank spacer paragraph AFTER the table so the
        # Bills total doesn't butt directly against the "Motion to
        # pay the bills..." line (or whatever paragraph the reorder
        # step lands next to it).  Word paragraphs can't be built
        # in isolation, so we mint one by dropping a new <w:p> XML
        # element right after the table.
        from docx.oxml import OxmlElement
        spacer = OxmlElement('w:p')
        new_tbl.addnext(spacer)

        # Remove the placeholder bullet paragraph so the table
        # doesn't sit alongside a lingering empty bullet.
        if bullet_idx is not None:
            bullet_para = paras[bullet_idx]
            bullet_element = bullet_para._element
            if bullet_element.getparent() is not None:
                bullet_element.getparent().remove(bullet_element)

    @staticmethod
    def _reorder_treasurer_section(doc):
        """Move the 'Motion to pay the bills' paragraph so it sits
        directly BEFORE 'Assets:'.

        Final layout after both this reorder and the itemized-bills
        insertion run:

            Bills of: $Total
              • Vendor A — PO/2026/0042 — $823.14
              • Vendor B — PO/2026/0048 — $1,247.85
              ...
            Motion to pay the bills was made by ___ and seconded by ___.
            Assets: $______
            Liability: $______
            Equity: $______
            Total Liabilities & Equity: $______

        Anchoring on 'Assets:' instead of 'Bills of:' means the reorder
        keeps working regardless of how many bill lines get inserted
        by _insert_itemized_bills.  Silently skipped if either anchor
        can't be found."""
        assets_p = None
        motion_p = None
        for p in doc.paragraphs:
            if assets_p is None and p.text.strip().startswith("Assets:"):
                assets_p = p
            if motion_p is None and "Motion to pay the bills" in p.text:
                motion_p = p
            if assets_p is not None and motion_p is not None:
                break
        if assets_p is None or motion_p is None:
            return
        motion_el = motion_p._element
        parent = motion_el.getparent()
        if parent is not None:
            parent.remove(motion_el)
        # Insert right BEFORE Assets:
        assets_p._element.addprevious(motion_el)

    @staticmethod
    def _reorder_project_dollars_before_closing(doc):
        """Move the 'Project Dollars: / Fines:' paragraph — plus the
        follow-on 'All birthday dollars go Project funds...' note — to
        sit directly BEFORE the CLOSING: block.

        The Project Dollars line is announced at the *end* of the
        meeting (right before adjournment), so having it lead the
        Treasurer's Report is confusing.  Moving it next to CLOSING
        matches the actual meeting flow.

        Implementation note: python-docx's Paragraph objects are
        thrown-away wrappers around the underlying <w:p> XML — a
        second call to doc.paragraphs returns fresh wrappers that
        don't compare equal to the first pass's wrappers.  So we
        DO NOT re-fetch doc.paragraphs after we've captured
        references; we work with the wrapper we already have and
        move its _element directly.  Silently skipped if either
        anchor can't be found."""
        pd_p = None
        closing_p = None
        # Single pass — capture wrappers as we find them.
        paras_snapshot = list(doc.paragraphs)
        for p in paras_snapshot:
            txt = p.text.strip()
            if pd_p is None and txt.startswith("Project Dollars:"):
                pd_p = p
            if (closing_p is None
                    and "CLOSING:" in txt.upper()):
                closing_p = p
            if pd_p is not None and closing_p is not None:
                break
        if pd_p is None or closing_p is None:
            return

        # Sanity check: only move if PD currently sits BEFORE CLOSING
        # in document order.  Otherwise it's already positioned or
        # something's odd — leave it alone.
        pd_el = pd_p._element
        closing_el = closing_p._element
        body = pd_el.getparent()
        if body is None or body is not closing_el.getparent():
            # Different parents (e.g. one is in a cell) — bail out
            # so we don't scramble the doc.
            return
        # Determine order via element position among siblings.
        siblings = list(body)
        try:
            pd_idx = siblings.index(pd_el)
            closing_idx = siblings.index(closing_el)
        except ValueError:
            return
        if pd_idx >= closing_idx:
            return  # Already positioned before CLOSING.

        # Walk forward from Project Dollars in XML sibling order,
        # collecting the "block" to move:
        #   • Project Dollars line
        #   • Fines: line (rare — usually same paragraph)
        #   • "All birthday dollars go Project funds..." note
        # Stop at any structural line or the CLOSING paragraph so we
        # never yank a financial row across.
        from docx.oxml.ns import qn
        stop_keywords = (
            "Treasurer's Report", "Bills of:", "Assets:", "Liability:",
            "Equity:", "Total Liabilities", "Motion to pay",
            "UNFINISHED BUSINESS", "NEW BUSINESS", "GOOD OF THE ORDER",
        )
        block_elements = [pd_el]
        for j in range(pd_idx + 1, closing_idx):
            candidate = siblings[j]
            # Only consider paragraph elements — skip tables/etc.
            if candidate.tag != qn('w:p'):
                # A non-paragraph (like a table) is a hard stop —
                # don't drag it into CLOSING.
                break
            # Extract text from this paragraph element.
            texts = candidate.findall('.//' + qn('w:t'))
            ctxt = "".join(t.text or '' for t in texts).strip()
            if not ctxt:
                # Blank paragraph — skip but keep walking.
                continue
            if any(k in ctxt for k in stop_keywords):
                break
            if (ctxt.startswith("Project Dollars:")
                    or ctxt.startswith("All birthday dollars")
                    or ctxt.startswith("Fines:")):
                block_elements.append(candidate)
                continue
            # Anything else — stop collecting.
            break

        # Move each block element to sit right BEFORE the CLOSING
        # paragraph, preserving order.
        for el in block_elements:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            closing_el.addprevious(el)

        # Add a blank spacer paragraph AFTER the moved block so the
        # "All birthday dollars..." line has breathing room before
        # CLOSING starts.  A bare <w:p> renders as one blank line
        # in Word.
        from docx.oxml import OxmlElement
        spacer = OxmlElement('w:p')
        closing_el.addprevious(spacer)

    def _populate_text_sections(self, doc):
        """Fill each section-header's follow-on empty paragraph with
        the corresponding text-area field content.

        Handles multi-line content by breaking on newlines and using
        Word soft line-breaks so the whole block stays in one bulleted
        paragraph.  If the section header isn't found in the template,
        the section is silently skipped."""
        # Combine free-text section content with any motions filed
        # against that section, so both print together under the
        # header.  Motions block goes AFTER the free text.
        def _combine(text, motion_key):
            block = self._format_motions_block(motion_key)
            text = (text or '').strip()
            if text and block:
                return text + "\n" + block
            return text or block

        section_map = [
            ("SICKNESS AND DISTRESS",             self.sickness_distress),
            ("DEATHS OF MEMBERS",                 self.deaths_of_members),
            ("COMMUNITY ACTIVITIES",              self.community_activities),
            ("READING OF COMMUNICATIONS",         self.communications),
            ("UNFINISHED BUSINESS",
                _combine(self.unfinished_business, 'unfinished')),
            ("NEW BUSINESS",
                _combine(self.new_business, 'new')),
            ("GOOD OF THE ORDER",
                _combine(self.good_of_order, 'good_of_order')),
        ]
        paras = doc.paragraphs
        for header_key, content in section_map:
            if not content:
                continue
            content = str(content).strip()
            if not content:
                continue
            # Find the section header paragraph
            header_idx = -1
            for i, p in enumerate(paras):
                if header_key in p.text.upper():
                    header_idx = i
                    break
            if header_idx == -1:
                continue
            # Find the next empty (or bullet-only) paragraph after it
            for j in range(header_idx + 1,
                           min(header_idx + 5, len(paras))):
                candidate = paras[j]
                cur = candidate.text.strip()
                if cur == "" or cur == "-" or cur in ("•", "·"):
                    self._write_multiline_to_paragraph(candidate, content)
                    break

    @staticmethod
    def _write_multiline_to_paragraph(para, text):
        """Write multi-line text into a template placeholder paragraph.

        Each line becomes its own sibling paragraph, cloned from the
        placeholder so it inherits the bullet / indent / style.  Leading
        bullet characters in the source text are stripped so we don't
        end up with double bullets (template's bullet + typed one)."""
        from copy import deepcopy
        from docx.oxml.ns import qn

        # Normalize lines; strip leading bullet markers from each.
        lines = []
        for raw in str(text).split("\n"):
            s = raw.strip()
            if not s:
                continue
            for marker in ("•", "·", "●", "○", "-", "*"):
                if s.startswith(marker):
                    s = s[len(marker):].lstrip()
            if s:
                lines.append(s)
        if not lines:
            return

        def _set_para_text(p, txt):
            # Blank all existing runs, then set the first one.
            for r in p.runs:
                r.text = ""
            if p.runs:
                p.runs[0].text = txt
            else:
                p.add_run(txt)

        # First line into the existing placeholder paragraph
        _set_para_text(para, lines[0])

        # Each subsequent line: clone the paragraph XML and insert
        # right after the previous one.  This preserves the bullet
        # style perfectly since we're literally copying the same
        # element structure.
        anchor = para._element
        for line in lines[1:]:
            new_para = deepcopy(para._element)
            # Wipe text runs in the clone
            for t in new_para.iter(qn('w:t')):
                t.text = ""
            # Set the first <w:t> we find to the new line
            for t in new_para.iter(qn('w:t')):
                t.text = line
                break
            anchor.addnext(new_para)
            anchor = new_para

    @staticmethod
    def _find_induction_table(doc):
        """Locate the 'Members to be Inducted:' table.

        Walks the document body in order and returns the first Table
        that immediately follows a paragraph whose text contains
        'Members to be Inducted'.  Returns None if not found — some
        template variants may not include this section."""
        from docx.oxml.ns import qn
        body = doc.element.body
        seen_header = False
        for child in body.iterchildren():
            tag = child.tag
            if tag == qn('w:p'):
                # Get the paragraph text
                text = "".join(
                    t.text or '' for t in child.iter(qn('w:t'))
                )
                if 'Members to be Inducted' in text:
                    seen_header = True
                    continue
            elif tag == qn('w:tbl') and seen_header:
                # Find the matching Table object in doc.tables
                for tbl in doc.tables:
                    if tbl._element is child:
                        return tbl
                return None
        return None

    def _fill_induction_table(self, tbl):
        """Populate the Members to be Inducted table with any
        propositions where will_be_inducted=True (regardless of
        which section they're filed under).  Body rows in the
        template are pre-numbered (1., 2., 3.) placeholders — we
        fill them in order and leave any trailing rows blank."""
        induction_lines = [
            p for p in self.proposition_ids if p.will_be_inducted
        ]
        if not induction_lines:
            return
        # This template's induction table has no header row; every
        # row is a body row.  Fill from the top.
        body_rows = list(tbl.rows)
        for i, row in enumerate(body_rows):
            if i >= len(induction_lines):
                break
            p = induction_lines[i]
            cells = row.cells
            # Column layout: # / Name / (blank) / (blank) / (blank)
            # We fill Name at col[1]; the other columns of this
            # template's Induction table are intentionally blank.
            if len(cells) >= 2:
                self._set_cell_text(cells[1], p.candidate_name or "")

    def _fill_proposition_table(self, tbl, section):
        """Populate a proposition-style table.  Body rows in the
        template are numbered (1., 2., 3.) placeholders; we fill from
        top and grow the table when we have MORE propositions than the
        template pre-provides by cloning the last body row.  Cloning
        preserves cell shading / borders / fonts so appended rows blend
        in with the template."""
        from copy import deepcopy
        from docx.oxml.ns import qn

        props = [p for p in self.proposition_ids if p.section == section]
        body_rows = tbl.rows[1:]

        # Grow the table if we have more propositions than rows.
        # We clone the LAST body row (so all borders / shading match)
        # and clear its cells before filling.
        if props and len(body_rows) < len(props):
            template_row_el = body_rows[-1]._element if body_rows \
                else tbl.rows[0]._element
            tbl_el = tbl._element
            needed = len(props) - len(body_rows)
            last_row_el = template_row_el
            for _ in range(needed):
                new_row_el = deepcopy(template_row_el)
                # Blank out cell text so we don't inherit the "3." etc.
                for tc in new_row_el.findall(qn('w:tc')):
                    for para in tc.findall(qn('w:p')):
                        for run in para.findall(qn('w:r')):
                            for t in run.findall(qn('w:t')):
                                t.text = ''
                last_row_el.addnext(new_row_el)
                last_row_el = new_row_el
            # Refresh body_rows to include the new ones
            body_rows = tbl.rows[1:]

        # Number the rows sequentially so the "#" column reads 1., 2.,
        # 3., 4. even when we cloned rows that carried an old number.
        for i, row in enumerate(body_rows):
            if i >= len(props):
                break
            p = props[i]
            cells = row.cells
            # Column layout: # / Name / City-State / [Employer|Occupation] / Sponsor
            if len(cells) >= 1:
                self._set_cell_text(cells[0], "%d." % (i + 1))
            if len(cells) >= 2:
                self._set_cell_text(cells[1], p.candidate_name or "")
            if len(cells) >= 3:
                self._set_cell_text(cells[2], p.city_state or "")
            if len(cells) == 5:
                # Membership (Employer, Sponsor) OR Balloting (Occupation, Sponsor)
                if section == 'membership':
                    self._set_cell_text(cells[3], p.employer or "")
                elif section == 'balloting':
                    self._set_cell_text(cells[3], p.occupation or "")
                self._set_cell_text(cells[4], p.sponsor or "")

    @staticmethod
    def _set_cell_text(cell, text):
        """Set a cell's text, preserving formatting on the first run."""
        if not cell.paragraphs:
            cell.text = text
            return
        para = cell.paragraphs[0]
        if para.runs:
            para.runs[0].text = text
            for r in para.runs[1:]:
                r.text = ""
        else:
            para.add_run(text)


# ═══════════════════════════════════════════════════════════════════
# Officer roll call line
# ═══════════════════════════════════════════════════════════════════
class ElksLodgeMeetingOfficer(models.Model):
    _name = "elks.lodge.meeting.officer"
    _description = "Lodge Meeting Officer Roll Call"
    _order = "sequence, id"
    # Use position as the display name so M2o dropdowns (e.g. the
    # Substitutions "Position Being Filled" picker) render
    # "Two Year Trustee" instead of "elks.lodge.meeting.officer,57".
    _rec_name = "position"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    position = fields.Char(required=True)
    officer_name = fields.Char()
    status = fields.Selection([
        ('present',  'Present'),
        ('absent',   'Absent'),
        ('excused',  'Excused'),
    ], default=False,
       help="Leave blank when generating the agenda before the "
            "meeting — filled in during roll call.  When you "
            "re-download the docx after the meeting, the status "
            "column populates automatically.")

    def _status_label(self):
        """Return the human-readable status; empty string if not yet
        marked (so the printed Status column stays blank on the
        pre-meeting draft)."""
        self.ensure_one()
        if not self.status:
            return ""
        return dict(self._fields['status'].selection).get(self.status, "")


# ═══════════════════════════════════════════════════════════════════
# Committee report line
# ═══════════════════════════════════════════════════════════════════
class ElksLodgeMeetingCommitteeLine(models.Model):
    """Committee line on the Lodge Meeting Agenda.

    committee_id links back to the elks.committee master (if
    available) so pre-populating pulls the current chairperson.  The
    Char committee_name is kept alongside so the Secretary can add
    ad-hoc committees that don't yet exist in the master list.

    chairperson_id is a Many2one to res.partner scoped to Elks
    members (domain built via _get_member_domain so an installation
    without the x_is_member flag still works).  The Char chairperson
    is kept as a computed display cache so historical rows without a
    partner link still print cleanly."""
    _name = "elks.lodge.meeting.committee.line"
    _description = "Lodge Meeting Committee Report"
    _order = "sequence, id"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    committee_id = fields.Many2one(
        "elks.committee", string="Standing Committee",
        ondelete="set null",
        help="Link back to the standing-committees master list in "
             "elkscontacts.  Leave blank for ad-hoc committees.",
    )
    committee_name = fields.Char(required=True)
    chairperson_id = fields.Many2one(
        "res.partner", string="Chairperson",
        domain=lambda self: self._get_member_domain(),
        help="Pick from the lodge's Elks members.",
    )
    chairperson = fields.Char(
        compute="_compute_chairperson_display",
        store=True, readonly=False,
        help="Display cache — auto-fills from Chairperson (partner) "
             "name; can be manually overridden for ad-hoc entries.",
    )
    report_text = fields.Text("Report")

    @api.depends("chairperson_id")
    def _compute_chairperson_display(self):
        for rec in self:
            if rec.chairperson_id:
                rec.chairperson = rec.chairperson_id.name

    @api.model
    def _get_member_domain(self):
        """Domain filter for chairperson_id — Elks members only.

        Uses the x_is_member flag if it's present on res.partner
        (elkscontacts convention); falls back to an unfiltered
        dropdown if the flag isn't available yet."""
        try:
            if 'x_is_member' in self.env['res.partner']._fields:
                return [('x_is_member', '=', True)]
        except (KeyError, ValueError):
            pass
        return []


# ═══════════════════════════════════════════════════════════════════
# Proposition / candidate line
# ═══════════════════════════════════════════════════════════════════
class ElksLodgeMeetingSubstitution(models.Model):
    """Line-by-line officer substitution / fill-in.

    Each row: an absent officer's position (M2o to the meeting's own
    officer roll-call row) + the member filling in for them (M2o to
    res.partner, scoped to Elks members).  Multiple rows per meeting
    are common when several officers are absent.

    Format on the printed docx: 'Esquire — John Doe; Chaplain — Jane
    Smith' inline on the Substitutions line.  If no rows exist, the
    template's blank underscores stay put so the Secretary can
    hand-write substitutions during roll call."""
    _name = "elks.lodge.meeting.substitution"
    _description = "Lodge Meeting Officer Substitution"
    _order = "sequence, id"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    officer_id = fields.Many2one(
        "elks.lodge.meeting.officer",
        string="Position Being Filled",
        required=True, ondelete='cascade',
        domain="[('meeting_id', '=', parent.id)]",
        help="Which absent officer's chair is being filled.",
    )
    position_display = fields.Char(
        related="officer_id.position", readonly=True,
        string="Position",
    )
    absent_officer_name = fields.Char(
        related="officer_id.officer_name", readonly=True,
        string="Absent Officer",
    )
    substitute_id = fields.Many2one(
        "res.partner",
        string="Filled In By",
        domain=lambda self: self._get_sub_member_domain(),
        help="Search for the lodge member filling in.",
    )
    notes = fields.Char(help="Optional — e.g. 'first-time sub'")

    @api.model
    def _get_sub_member_domain(self):
        """Scope substitute dropdown to Elks members if possible."""
        try:
            if 'x_is_member' in self.env['res.partner']._fields:
                return [('x_is_member', '=', True)]
        except (KeyError, ValueError):
            pass
        return []


class ElksLodgeMeetingBill(models.Model):
    """Ad-hoc bill line on the Lodge Meeting Agenda.

    Not everything paid at a meeting flows through the Floor-Vote
    purchase order queue.  Utility bills, one-off invoices, and
    reimbursements often get read off a list at the meeting without
    ever hitting a PO.  This model lets the Secretary add those
    manually so they show up in the Bills total.
    """
    _name = "elks.lodge.meeting.bill"
    _description = "Lodge Meeting Manual Bill"
    _order = "sequence, id"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    description = fields.Char(required=True)
    vendor = fields.Char()
    amount = fields.Monetary(
        "Amount", required=True, currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        related="meeting_id.currency_id", store=True,
    )


class ElksLodgeMeetingMotion(models.Model):
    """A floor motion raised during a Lodge meeting requiring a
    vote.

    Each row captures: the wording of the motion, the section of the
    meeting it belongs to (Unfinished / New / Good of the Order), who
    made the motion (M2o to Elks member), who seconded it, and the
    result (passed / failed / tabled / withdrawn — or blank while
    voting is still to happen).  Optional vote_notes for tallies like
    '5-2' or 'unanimous voice vote'.

    Rendered in the finalized minutes docx and available on the
    Business tab of the meeting form."""
    _name = "elks.lodge.meeting.motion"
    _description = "Lodge Meeting Floor Motion"
    _order = "sequence, id"
    _rec_name = "description"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)
    section = fields.Selection([
        ('unfinished',    'Unfinished Business'),
        ('new',           'New Business'),
        ('good_of_order', 'Good of the Order'),
        ('other',         'Other / Uncategorized'),
    ], default='new', required=True,
       help="Which part of the meeting this motion came up in.")
    description = fields.Text(
        "Motion", required=True,
        help="The wording of the motion as it was moved on the floor.",
    )
    motion_by_id = fields.Many2one(
        "res.partner", string="Moved By",
        domain=lambda self: self._get_motion_member_domain(),
        help="Elks member who made the motion.",
    )
    seconded_by_id = fields.Many2one(
        "res.partner", string="Seconded By",
        domain=lambda self: self._get_motion_member_domain(),
        help="Elks member who seconded the motion.",
    )
    result = fields.Selection([
        ('passed',    'Passed'),
        ('failed',    'Failed'),
        ('tabled',    'Tabled'),
        ('withdrawn', 'Withdrawn'),
    ], string="Result",
       help="Leave blank until the vote is called.",
    )
    vote_notes = fields.Char(
        "Vote Tally / Notes",
        help="Optional — e.g. '5-2', 'unanimous voice vote', "
             "'tabled to next meeting'.",
    )

    @api.model
    def _get_motion_member_domain(self):
        """Scope Moved By / Seconded By dropdowns to Elks members
        when the x_is_member flag exists on res.partner."""
        try:
            if 'x_is_member' in self.env['res.partner']._fields:
                return [('x_is_member', '=', True)]
        except (KeyError, ValueError):
            pass
        return []


class ElksLodgeMeetingProposition(models.Model):
    _name = "elks.lodge.meeting.proposition"
    _description = "Lodge Meeting Membership Item"
    _order = "section, id"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
    )
    section = fields.Selection([
        ('membership',   'Propositions for Membership'),
        ('affiliation',  'Affiliation / Reinstatement'),
        ('balloting',    'Balloting on Candidates'),
        ('induction',    'Members to be Inducted'),
    ], required=True, default='membership')
    candidate_name = fields.Char(required=True)
    city_state = fields.Char()
    employer = fields.Char()
    occupation = fields.Char()
    sponsor = fields.Char()
    # Checkbox — when True, this candidate also appears in the
    # "Members to be Inducted" table on the printed agenda,
    # regardless of which section they're in.  Typically Secretary
    # ticks this on a Balloting candidate the day of the meeting once
    # they know an induction ceremony is happening.
    will_be_inducted = fields.Boolean(
        "Will be Inducted at This Meeting",
        default=False,
        help="Check this to have the candidate listed in the "
             "'Members to be Inducted' section on the docx.  "
             "Auto-checked for records already at Elected stage; "
             "leave blank for candidates being balloted whose fate "
             "is unknown until the vote.",
    )


# ═══════════════════════════════════════════════════════════════════
# Report log — audit trail of records read on the floor
# ═══════════════════════════════════════════════════════════════════
class ElksLodgeMeetingReportLog(models.Model):
    """One row per source record that appeared on a meeting's agenda
    / minutes.  Written on Generate Minutes.

    Purpose:
      • DEATHS: prevents the same partner from being read on the
        floor at a second meeting — future _populate_deaths() calls
        skip any partner already in this log.  Also mirrors the flag
        to res.partner.x_date_read_on_floor so the existing Deaths
        CLMS queue view stays accurate.
      • PROPOSITIONS: audit trail only.  Candidates legitimately
        re-appear at successive meetings as they progress from
        proposed → balloting → elected, so the log does NOT filter
        them out — it just records which meeting they showed up at.
      • EVENTS: not logged.  Upcoming events keep showing on every
        agenda until their event date has passed, at which point the
        date-window filter drops them naturally."""
    _name = "elks.lodge.meeting.report.log"
    _description = "Lodge Meeting Report Log"
    _order = "reported_date desc, id desc"
    _rec_name = "title"

    meeting_id = fields.Many2one(
        "elks.lodge.meeting", required=True,
        ondelete="cascade", index=True,
        string="Meeting",
    )
    reported_date = fields.Date(
        "Reported On", index=True,
        help="The date of the meeting where this record was read "
             "on the floor.",
    )
    category = fields.Selection([
        ('death',       'Death'),
        ('proposition', 'Membership Proposition'),
        ('event',       'Community Event'),
        ('other',       'Other'),
    ], required=True, index=True)
    res_model = fields.Char(
        "Source Model", required=True, index=True,
        help="Odoo model of the source record (e.g. 'res.partner', "
             "'elks.membership.application').",
    )
    res_id = fields.Integer(
        "Source Record ID", required=True, index=True,
        help="Database ID of the source record.",
    )
    title = fields.Char(
        "Record Title",
        help="Snapshot of the source record's name at the time it "
             "was read — kept even if the source record is later "
             "renamed or deleted.",
    )
    resource_ref = fields.Reference(
        selection=[
            ('res.partner', 'Contact'),
            ('elks.membership.application', 'Membership Application'),
            ('project.task', 'Event / Task'),
        ],
        string="Open Record", compute="_compute_resource_ref",
        help="Click through to the source record.",
    )

    _sql_constraints = [
        ('uniq_meeting_record',
         'unique(meeting_id, res_model, res_id)',
         "This record has already been logged for this meeting."),
    ]

    @api.depends("res_model", "res_id")
    def _compute_resource_ref(self):
        for rec in self:
            if rec.res_model and rec.res_id:
                rec.resource_ref = "%s,%d" % (rec.res_model, rec.res_id)
            else:
                rec.resource_ref = False
