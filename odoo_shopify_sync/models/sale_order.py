from odoo import models, fields

class SaleOrder(models.Model):
    _inherit = "sale.order"

    shopify_order_id = fields.Char(string="Shopify Order ID", index=True, help="ID of the order in Shopify")
    shopify_store_id = fields.Many2one("shopify.store", string="Shopify Store", help="The store from which this order originated.")

    