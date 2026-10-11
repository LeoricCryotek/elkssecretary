# -*- coding: utf-8 -*-
from . import models
from . import wizard


def post_init_hook(env):
    """Alphabetize all top-level app menus after this module is
    installed or upgraded.

    Runs the same code the "Tools > Alphabetize App Menus" server
    action runs, so new modules added to the Odoo instance show up
    in alphabetical order the moment elkssecretary is upgraded.

    Idempotent — safe to run on every upgrade.  Any menu whose
    sequence already matches its alphabetical position is left
    untouched.
    """
    import logging
    _logger = logging.getLogger(__name__)
    Menu = env['ir.ui.menu'].sudo()
    top_menus = Menu.search([('parent_id', '=', False)])
    sorted_menus = top_menus.sorted(key=lambda m: (m.name or '').lower())
    changed = 0
    for idx, m in enumerate(sorted_menus, start=1):
        new_seq = idx * 10
        if m.sequence != new_seq:
            m.write({'sequence': new_seq})
            changed += 1
    _logger.info(
        "elkssecretary post_init_hook: alphabetized %d of %d "
        "top-level app menus.", changed, len(sorted_menus),
    )
