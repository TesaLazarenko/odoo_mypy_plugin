"""Every check of the plugin on correct code: it must report nothing here."""

from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_score = fields.Integer()
    x_parent_id = fields.Many2one("res.partner")
    x_parent_city = fields.Char(related="x_parent_id.city")
    x_country_id = fields.Many2one(related="x_parent_id.country_id")
    x_total = fields.Float(compute="_compute_total", store=True)
    x_kind = fields.Selection([("a", "A"), ("b", "B")], default="a")
    x_child_ids = fields.One2many("res.partner", "x_parent_id", domain=[("active", "=", True)])

    @api.depends("x_parent_id.x_score", "x_score")
    def _compute_total(self) -> None:
        for rec in self:
            rec.x_total = rec.x_score + (rec.x_parent_id.x_score or 0)

    @api.constrains("x_score")
    def _check_score(self) -> None:
        pass

    @api.onchange("x_parent_id")
    def _onchange_parent(self) -> None:
        self.x_kind = "b"

    def action(self) -> int:
        company = self.env.ref("base.main_company")
        partners = self.search([("x_kind", "in", ["a", "b"]), ("x_parent_id.name", "ilike", "x")])
        names: list[str | bool] = list(partners.mapped("name"))
        rows = partners.read(["name", "x_score"])
        self.with_context(default_x_kind="a").create({"name": "New", "x_score": 1})
        self.write({"x_parent_id": company.partner_id.id, "x_kind": "b"})
        if "sale.order" in self.env:
            self.env["sale.order"].search([])
        return len(names) + sum(row["x_score"] for row in rows)
