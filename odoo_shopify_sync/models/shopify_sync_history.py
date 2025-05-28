from odoo import models, fields

class ShopifySyncHistory(models.Model):
    _name = 'shopify.sync.history'
    _description = 'Shopify Sync History'

    product_id = fields.Many2one('product.product', string='Odoo Product', required=True)
    shopify_product_id = fields.Char(string='Shopify Product ID', required=True)
    sync_time = fields.Datetime(string='Sync Time', default=fields.Datetime.now, required=True)
    source = fields.Selection([('odoo', 'Odoo'), ('shopify', 'Shopify')], default='odoo', string='Sync Source')


    def clean_old_sync_history(self):
        """Deletes old sync history records."""
        expiry_date = fields.Datetime.now() - fields.timedelta(days=30)
        old_records = self.env['shopify.sync.history'].sudo().search([('sync_time', '<', expiry_date)])
        old_records.unlink()
        
