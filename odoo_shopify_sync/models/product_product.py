from odoo import models, fields

class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_product_id = fields.Char("Shopify Product ID")
    shopify_store_ids = fields.Many2many('shopify.store', string="Shopify Stores Synced")
    last_update_source = fields.Selection(
        [('odoo', 'Odoo'), ('shopify', 'Shopify'), ('synced', 'Synced')],
        string='Last Update Source',
        default='synced',
        help='Tracks the source of the last inventory update to prevent sync loops.'
    )
    last_updated_at = fields.Datetime(
        string='Last Updated At',
        default=fields.Datetime.now,
        help='Timestamp of the last inventory update.'
    )
