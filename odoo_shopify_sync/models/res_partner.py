from odoo import models, fields

class ResPartner(models.Model):
    _inherit = "res.partner"

    shopify_customer_id = fields.Char(string="Shopify Order ID", index=True, help="ID of the order in Shopify")
    shopify_store_id = fields.Many2one("shopify.store", string="Shopify Store", help="The store from which this order originated.")
