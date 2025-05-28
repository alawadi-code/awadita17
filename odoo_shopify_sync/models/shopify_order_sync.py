from odoo import models, fields, api
import requests
import logging
from datetime import datetime

_logger = logging.getLogger(__name__)

class ShopifySalesOrderSync(models.Model):
    _name = 'shopify.order.sync'
    _description = 'Sync Sales Orders from Shopify to Odoo'

    store_id = fields.Many2one('shopify.store', string='Shopify Store', required=True)
    shopify_order_id = fields.Char(string='Shopify Order ID', required=True, index=True)
    odoo_order_id = fields.Many2one('sale.order', string='Odoo Order', ondelete='cascade')
    order_status = fields.Selection([
        ('draft', 'Draft'),
        ('confirmed', 'Confirmed'),
        ('done', 'Done')
    ], string='Order Status', default='draft')

    @api.model
    def sync_orders_from_shopify(self):
        """ Fetch new sales orders from Shopify and create them in Odoo """
        stores = self.env['shopify.store'].search([])
        for store in stores:
            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2024-01/orders.json?status=open"
            response = requests.get(url)
            
            if response.status_code == 200:
                orders = response.json().get('orders', [])
                for order in orders:
                    self.create_odoo_sales_order(order, store)
            else:
                _logger.error(f"Failed to fetch orders from {store.name}: {response.text}")

    def create_odoo_sales_order(self, shopify_order, store):
        """ Convert Shopify Order to Odoo Sales Order """
        shopify_order_id = str(shopify_order.get('id'))
        existing_mapping = self.search([('shopify_order_id', '=', shopify_order_id), ('store_id', '=', store.id)], limit=1)
        
        if existing_mapping:
            _logger.info(f"Skipping existing Shopify Order {shopify_order_id} in store {store.name}")
            return existing_mapping.odoo_order_id

        # Get or create the customer
        customer = self.get_or_create_customer(shopify_order.get('customer', {}))
        
        # Create sales order in Odoo
        order_vals = {
            'partner_id': customer.id,
            'date_order': shopify_order.get('created_at', fields.Datetime.now()),
            'shopify_order_id': shopify_order_id,
            'order_line': self.get_order_lines(shopify_order, store),
        }
        
        odoo_order = self.env['sale.order'].create(order_vals)
        
        # Map Shopify Order with Odoo
        self.create({
            'store_id': store.id,
            'shopify_order_id': shopify_order_id,
            'odoo_order_id': odoo_order.id,
            'order_status': 'confirmed'
        })

        return odoo_order

    def get_or_create_customer(self, shopify_customer):
        """ Fetch or create customer in Odoo based on Shopify customer details """
        if not shopify_customer:
            return self.env['res.partner'].browse(self.env['res.partner'].search([], limit=1).id)
        
        email = shopify_customer.get('email')
        existing_customer = self.env['res.partner'].search([('email', '=', email)], limit=1)
        
        if existing_customer:
            return existing_customer

        return self.env['res.partner'].create({
            'name': shopify_customer.get('first_name', '') + ' ' + shopify_customer.get('last_name', ''),
            'email': email,
            'phone': shopify_customer.get('phone'),
        })

    def get_order_lines(self, shopify_order, store):
        """ Map Shopify order items to Odoo order lines """
        order_lines = []
        for item in shopify_order.get('line_items', []):
            product = self.env['product.product'].search([('default_code', '=', item.get('sku'))], limit=1)
            if not product:
                _logger.warning(f"Product with SKU {item.get('sku')} not found in Odoo. Skipping line item.")
                continue
            
            order_lines.append((0, 0, {
                'product_id': product.id,
                'name': product.name,
                'product_uom_qty': item.get('quantity', 1),
                'price_unit': float(item.get('price', 0.0)),
            }))
        return order_lines
