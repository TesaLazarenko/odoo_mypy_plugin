from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_score = fields.Integer()
    x_parent_id = fields.Many2one("res.partnr")  # seeded: typo in comodel
    x_total = fields.Float(compute="_compute_totl")  # seeded: compute method does not exist
    x_city = fields.Char(related="x_parent_id.citty")  # seeded: bad related path

    @api.depends("x_parent_id.nmae")  # seeded: bad depends path
    def _compute_total(self):
        for rec in self:
            rec.x_total = rec.x_score + rec.name  # seeded: int + str|False

    def action(self):
        self.env["res.partnr"].search([])  # seeded: unknown model
        self.search([("x_scroe", "=", 1)])  # seeded: unknown field in domain
        self.create({"x_score": "oops", "nope": 1})  # seeded: wrong type + unknown field
        self.write({"x_parent_id": "abc"})  # seeded: wrong m2o value
        return self.x_parent_id.x_score.upper()  # seeded: int has no upper
