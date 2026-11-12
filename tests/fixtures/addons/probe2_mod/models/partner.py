from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_score = fields.Integer()
    x_parent_id = fields.Many2one("res.partner")
    x_city = fields.Char(related="x_parent_id.citty")  # seeded: bad related path
    x_label = fields.Char(compute="_compute_label")

    @api.depends("x_parent_id.nmae")  # seeded: bad depends path
    def _compute_label(self):
        for rec in self:
            rec.x_label = rec.name

    def action(self):
        self.env.ref("base.main_compani")  # seeded: unknown xml id
        self.mapped("x_parent_id.x_scroe")  # seeded: bad mapped path
        self.search([("x_score", "=>", 1)])  # seeded: unknown domain operator
        self.with_context(default_commnt="x").create({})  # seeded: typo in a default
        return self.x_parent_id.x_score.upper()  # seeded: int has no upper
