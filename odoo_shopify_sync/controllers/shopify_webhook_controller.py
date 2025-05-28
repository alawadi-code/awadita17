from odoo import http ,fields
from odoo.http import request
import requests
import logging
from datetime import datetime
import pytz

_logger = logging.getLogger(__name__)

class ShopifyWebhookController(http.Controller):

    @http.route('/shopify_webhook', type='json', auth='none', methods=['POST'])
    def handle_shopify_webhook(self):
        data = request.httprequest.get_json()
        event = request.httprequest.headers.get('X-Shopify-Topic')
        shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain')
        reason = request.httprequest.headers.get('X-Shopify-Reason', '')

        store = request.env['shopify.store'].sudo().search([('shopify_url', 'ilike', shop_domain)], limit=1)
        if not store:
            print(f"❌ Store not found for domain: {shop_domain}")
            return {'status': 'failed', 'message': 'Store not found'}

        print(f"📩 Webhook received from {shop_domain} | Event: {event} | Data: {data}")

        if event == 'inventory_levels/update' and reason != 'odoo_update':  # Skip Odoo-initiated updates
            self.handle_inventory_update(data, store)

        return {'status': 'success'}
    def handle_inventory_update(self, data, store):
        inventory_item_id = data.get('inventory_item_id')
        new_quantity = data.get('available', 0)
        updated_at = data.get('updated_at')  # Shopify timestamp

        product_sku = self.get_sku_by_inventory_id(store, inventory_item_id)
        if not product_sku:
            print(f"❌ SKU not found for inventory_item_id {inventory_item_id} in store {store.shopify_url}")
            return

        odoo_product = request.env['product.product'].sudo().search([('default_code', '=', product_sku)], limit=1)
        if not odoo_product:
            print(f"❌ Odoo product not found for SKU {product_sku}")
            return

        # Convert Shopify timestamp to Odoo format
        shopify_updated_at = datetime.strptime(updated_at[:19], "%Y-%m-%dT%H:%M:%S")
        print(shopify_updated_at , "shopify_updated_at")
        print(odoo_product.last_updated_at , "odoo_product.last_updated_at")
        if odoo_product.last_updated_at and shopify_updated_at <= odoo_product.last_updated_at:
            print(f"Skipping sync for SKU {product_sku}: Shopify update is not newer than Odoo.")
            return

        print(f"🔄 Syncing SKU: {product_sku} | New Qty: {new_quantity}")
        self.sync_product_inventory(product_sku, new_quantity, store.warehouse_id)

        # Update product metadata after sync
        odoo_product.sudo().write({
            'last_update_source': 'synced',
            'last_updated_at': shopify_updated_at
        })

        # Sync to other stores if needed
        other_stores = request.env['shopify.store'].sudo().search([('id', '!=', store.id)])
        for other_store in other_stores:
            other_inventory_item_id = self.get_inventory_id_by_sku(other_store, product_sku)
            if other_inventory_item_id:
                self.update_inventory_in_shopify_store(other_store, other_inventory_item_id, new_quantity)
            else:
                print(f"⚠️ SKU {product_sku} not found in {other_store.shopify_url}")

    def sync_product_inventory(self, shopify_sku, qty, warehouse):
        """Syncs product inventory in Odoo."""
        odoo_product = request.env['product.product'].sudo().search([('default_code', '=', shopify_sku)], limit=1)
        print(odoo_product , "odoo_product")
        if odoo_product:
            qty_difference = qty - odoo_product.qty_available
            if qty_difference:
                self.create_inventory_adjustment(odoo_product, qty, warehouse)

    def create_inventory_adjustment(self, odoo_product, qty, warehouse):
        """Creates an inventory adjustment in Odoo using stock.quant."""
        location_id = warehouse.lot_stock_id.id
        stock_quant = request.env['stock.quant'].sudo().search([
            ('product_id', '=', odoo_product.id),
            ('location_id', '=', location_id)
        ], limit=1)

        if stock_quant:
            stock_quant.write({'quantity': qty})
            print(f"✅ Updated stock quant for {odoo_product.name} to {stock_quant.quantity}")
        else:
            request.env['stock.quant'].sudo().create({
                'product_id': odoo_product.id,
                'location_id': location_id,
                'quantity': qty,
                'company_id': warehouse.company_id.id
            })
            print(f"✅ Created new stock quant for {odoo_product.name} with quantity {qty}")

    def get_sku_by_inventory_id(self, store, inventory_item_id):
        """Fetches SKU from Odoo cache or Shopify API if missing."""
        mapping = request.env['shopify.product.mapping'].sudo().search([
            ('store_id', '=', store.id),
            ('inventory_item_id', '=', inventory_item_id)
        ], limit=1)

        if mapping:
            return mapping.sku

        # Fetch from Shopify if not in Odoo
        response = requests.get(
            f"https://{store.shopify_url}/admin/api/2025-01/inventory_items/{inventory_item_id}.json",
            headers={"X-Shopify-Access-Token": store.api_password}
        )

        if response.status_code == 200:
            shopify_product = response.json().get('inventory_item', {})
            product_sku = shopify_product.get('sku')
            if product_sku:
                request.env['shopify.product.mapping'].sudo().create({
                    'store_id': store.id,
                    'sku': product_sku,
                    'inventory_item_id': inventory_item_id
                })
                return product_sku

        return None

    def get_inventory_id_by_sku(self, store, sku):
        """Fetches inventory_item_id from Odoo cache or Shopify API if missing."""
        mapping = request.env['shopify.product.mapping'].sudo().search([
            ('store_id', '=', store.id),
            ('sku', '=', sku)
        ], limit=1)

        if mapping:
            return mapping.inventory_item_id

        # Fetch from Shopify if not in Odoo
        url = f"https://{store.shopify_url}/admin/api/2025-01/products.json?limit=250&fields=id,variants"
        headers = {"X-Shopify-Access-Token": store.api_password}

        while url:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                products = response.json().get('products', [])
                for product in products:
                    for variant in product.get('variants', []):
                        if variant.get('sku') == sku:
                            request.env['shopify.product.mapping'].sudo().create({
                                'store_id': store.id,
                                'sku': sku,
                                'inventory_item_id': variant.get('inventory_item_id')
                            })
                            return variant.get('inventory_item_id')

                url = response.links.get('next', {}).get('url')  # Shopify pagination
            else:
                print(f"❌ Error fetching products from {store.shopify_url}: {response.text}")
                return None

        return None

    def update_inventory_in_shopify_store(self, store, inventory_item_id, new_quantity):
        """Updates inventory level in Shopify."""
        url = f"https://{store.shopify_url}/admin/api/2025-01/inventory_levels/set.json"
        headers = {
            "X-Shopify-Access-Token": store.api_password,
            "Content-Type": "application/json",
            "X-Shopify-API-Version": "2025-01",
            "X-Shopify-Reason": "true"  # Prevent infinite loops
        }
        data = {
            "location_id": store.location_id,
            "inventory_item_id": inventory_item_id,
            "available": new_quantity
        }

        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 200:
            print(f"✅ Updated inventory level for {inventory_item_id} in {store.shopify_url}")
        else:
            print(f"❌ Error updating inventory: {response.text} Updating inventory level for {inventory_item_id} in {store.shopify_url}")


    @http.route('/shopify_webhook/sales_order', type='json', auth='none', methods=['POST'])
    def handle_shopify_sales_order_webhook(self):
        """Handles Shopify sales order webhook and processes orders in Odoo."""
        try:
            data = request.httprequest.get_json()
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain')
            event = request.httprequest.headers.get('X-Shopify-Topic')
            print(f"Webhook received from {shop_domain} | Event: {event} | Data: {data}")
            
            store = request.env['shopify.store'].sudo().search([('shopify_url', 'ilike', shop_domain)], limit=1)
            if not store:
                _logger.error(f"ERROR: Store not found for domain: {shop_domain}")
                return {'status': 'failed', 'message': 'Store not found'}

            _logger.info(f"Shopify Order Webhook received from {shop_domain} | Order ID: {data.get('id')} | Event: {event}")

            # Set an explicit user context to avoid singleton errors
            admin_user = request.env['res.users'].sudo().search([('login', '=', 'admin')], limit=1) or request.env.user
            with request.env(user=admin_user.id):
                if event == 'orders/cancelled':
                    self.cancel_order(data, store)
                else:
                    self.sync_order(data, store)
            return {'status': 'success'}

        except Exception as e:
            _logger.error(f"ERROR: Error processing Shopify sales order webhook: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def sync_order(self, order_data, store):
        """Syncs a Shopify order to Odoo with stock reversion after delivery."""
        shopify_order_id = order_data.get('id')
        odoo_order = request.env['sale.order'].sudo().search([('shopify_order_id', '=', shopify_order_id)], limit=1)

        if not odoo_order:
            if not isinstance(order_data, dict):
                _logger.error(f"ERROR: Invalid order data for Shopify Order ID {shopify_order_id}: {order_data}")
                return

            customer_data = order_data.get('customer')
            shopify_customer_id = False
            email = order_data.get('email')

            if customer_data and isinstance(customer_data, dict):
                shopify_customer_id = str(customer_data.get('id', '')) if customer_data.get('id') else False
                email = customer_data.get('email') or email
            else:
                _logger.warning(f"WARNING: No customer data found for Shopify Order ID {shopify_order_id}")

            customer = request.env['res.partner'].sudo().search([
                '|',
                ('shopify_customer_id', '=', shopify_customer_id),
                ('email', '=', email)
            ], limit=1)

            if not customer:
                customer = request.env['res.partner'].sudo().search([('name', '=', 'Guest Customer')], limit=1)
                if not customer:
                    customer = request.env['res.partner'].sudo().create({
                        'name': 'Guest Customer',
                        'email': 'guest@example.com',
                        'phone': '',
                    })

            shopify_date = order_data.get('created_at')
            if shopify_date:
                try:
                    dt = datetime.strptime(shopify_date, '%Y-%m-%dT%H:%M:%S%z')
                    dt_utc = dt.astimezone(pytz.UTC)
                    date_order = dt_utc.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError as e:
                    _logger.error(f"ERROR: Invalid date format for Shopify Order ID {shopify_order_id}: {shopify_date} - {str(e)}")
                    date_order = fields.Datetime.now()
            else:
                date_order = fields.Datetime.now()

            financial_status = order_data.get('financial_status', 'pending')
            fulfillment_status = order_data.get('fulfillment_status')
            state = 'draft'

            order_vals = {
                'partner_id': customer.id,
                'shopify_order_id': shopify_order_id,
                'date_order': date_order,
                'state': state,
                'origin': f"Shopify Order #{order_data.get('name', shopify_order_id)}",
                'warehouse_id': store.warehouse_id.id,
            }
            odoo_order = request.env['sale.order'].sudo().create(order_vals)

            line_items = order_data.get('line_items', [])
            product_quantities = {}

            for line in line_items:
                if not isinstance(line, dict):
                    _logger.warning(f"WARNING: Invalid line item for Shopify Order ID {shopify_order_id}: {line}")
                    continue

                sku = line.get('sku')
                if not sku:
                    _logger.warning(f"WARNING: Line item missing SKU for Shopify Order ID {shopify_order_id}: {line}")
                    continue

                quantity = line.get('quantity', 0)
                price = float(line.get('price', 0.0))

                if sku in product_quantities:
                    product_quantities[sku]['quantity'] += quantity
                    product_quantities[sku]['price'] = price
                else:
                    product_quantities[sku] = {'quantity': quantity, 'price': price}

            for sku, data in product_quantities.items():
                product = request.env['product.product'].sudo().search([('default_code', '=', sku)], limit=1)
                if product:
                    request.env['sale.order.line'].sudo().create({
                        'order_id': odoo_order.id,
                        'product_id': product.id,
                        'product_uom_qty': data['quantity'],
                        'price_unit': data['price'],
                        'tax_id': [(6, 0, [])],
                    })
                else:
                    _logger.warning(f"WARNING: Product with SKU {sku} not found for Shopify Order ID {shopify_order_id}")

            if fulfillment_status in ('fulfilled', 'partial') or financial_status in ('paid', 'partially_paid'):
                if odoo_order.state in ('draft', 'sent'):
                    odoo_order.action_confirm()
                
                if financial_status in ('paid', 'partially_paid'):
                    self._handle_invoicing(odoo_order, order_data, financial_status)
                
                if fulfillment_status in ('fulfilled', 'partial'):
                    self._handle_delivery(odoo_order, order_data, fulfillment_status)

            if financial_status in ('refunded', 'partially_refunded', 'voided') and odoo_order.state != 'cancel':
                odoo_order.action_cancel()

            _logger.info(f"OK: Created Sales Order {odoo_order.name} for Shopify Order {shopify_order_id}")
        else:
            financial_status = order_data.get('financial_status', 'pending')
            fulfillment_status = order_data.get('fulfillment_status')

            if financial_status in ('refunded', 'partially_refunded', 'voided') and odoo_order.state != 'cancel':
                odoo_order.action_cancel()
            elif (fulfillment_status in ('fulfilled', 'partial') or financial_status in ('paid', 'partially_paid')) and odoo_order.state in ('draft', 'sent'):
                odoo_order.action_confirm()
                if financial_status in ('paid', 'partially_paid'):
                    self._handle_invoicing(odoo_order, order_data, financial_status)
                if fulfillment_status in ('fulfilled', 'partial'):
                    self._handle_delivery(odoo_order, order_data, fulfillment_status)

            _logger.info(f"OK: Order {shopify_order_id} already synced as {odoo_order.name}, checked status updates")

    def cancel_order(self, order_data, store):
        """Cancels an Odoo order when Shopify sends an orders/cancelled event."""
        shopify_order_id = order_data.get('id')
        odoo_order = request.env['sale.order'].sudo().search([('shopify_order_id', '=', shopify_order_id)], limit=1)

        if odoo_order:
            if odoo_order.state != 'cancel':
                odoo_order.action_cancel()
                _logger.info(f"OK: Cancelled Sales Order {odoo_order.name} for Shopify Order {shopify_order_id}")
            else:
                _logger.info(f"OK: Order {shopify_order_id} already cancelled in Odoo as {odoo_order.name}")
        else:
            _logger.warning(f"WARNING: No Odoo order found for cancelled Shopify Order {shopify_order_id}")

    def _handle_invoicing(self, odoo_order, order_data, financial_status):
        """Handle invoice creation and payment based on Shopify financial status."""
        if odoo_order.state == 'sale' and not odoo_order.invoice_ids:
            invoice = odoo_order._create_invoices()
            invoice.action_post()

            if financial_status == 'paid':
                journal = request.env['account.journal'].sudo().search([('type', '=', 'cash')], limit=1)
                if not journal:
                    _logger.error(f"ERROR: No cash journal found for payment of Shopify Order ID {order_data.get('id')}")
                    return

                payment = request.env['account.payment'].sudo().create({
                    'partner_id': odoo_order.partner_id.id,
                    'amount': invoice.amount_total,
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'journal_id': journal.id,
                    'date': fields.Date.today(),
                })
                payment.action_post()

                invoice_line = invoice.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                payment_line = payment.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                if invoice_line and payment_line:
                    (invoice_line + payment_line).reconcile()
                    _logger.info(f"OK: Invoice created and paid for Shopify Order ID {order_data.get('id')}")
                else:
                    _logger.error(f"ERROR: Failed to reconcile payment for Shopify Order ID {order_data.get('id')}")
            else:
                _logger.info(f"OK: Invoice created but not paid for Shopify Order ID {order_data.get('id')} (status: {financial_status})")

    def _handle_delivery(self, odoo_order, order_data, fulfillment_status):
        """Handle delivery creation, validation, and stock reversion."""
        if odoo_order.state == 'sale' and not odoo_order.picking_ids.filtered(lambda p: p.state == 'done'):
            picking = odoo_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
            if not picking:
                odoo_order.action_confirm()
                picking = odoo_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))

            if picking:
                # Record original stock levels before validation
                original_stock = {}
                for move in picking.move_ids:
                    product = move.product_id
                    location = move.location_id
                    original_stock[product.id] = request.env['stock.quant'].sudo().search([
                        ('product_id', '=', product.id),
                        ('location_id', '=', location.id)
                    ], limit=1).quantity or 0
                    _logger.debug(f"Before delivery - {product.default_code}: {original_stock[product.id]}")

                # Validate delivery normally (this reduces stock)
                picking.with_context(skip_backorder=True).button_validate()

                # Revert stock to original levels after validation
                for move in picking.move_ids:
                    product = move.product_id
                    location = move.location_id
                    original_qty = original_stock.get(product.id, 0)
                    current_quant = request.env['stock.quant'].sudo().search([
                        ('product_id', '=', product.id),
                        ('location_id', '=', location.id)
                    ], limit=1)
                    if current_quant:
                        current_quant.sudo().write({'quantity': original_qty})
                    else:
                        request.env['stock.quant'].sudo().create({
                            'product_id': product.id,
                            'location_id': location.id,
                            'quantity': original_qty,
                        })
                    _logger.debug(f"After revert - {product.default_code}: {original_qty}")

                if fulfillment_status == 'fulfilled':
                    _logger.info(f"OK: Delivery validated and stock reverted for Shopify Order ID {order_data.get('id')}")
                elif fulfillment_status == 'partial':
                    _logger.info(f"OK: Partial delivery validated and stock reverted for Shopify Order ID {order_data.get('id')}")
            else:
                _logger.error(f"ERROR: No picking created for Shopify Order ID {order_data.get('id')} despite confirmation")

    def get_or_create_customer(self, customer_data, store):
        """Finds or creates a customer in Odoo based on Shopify customer data."""
        if not customer_data:
            return request.env['res.partner'].sudo().search([('name', '=', 'Guest Customer')], limit=1)

        shopify_customer_id = str(customer_data.get('id'))
        email = customer_data.get('email')
        customer_name = f"{customer_data.get('first_name', '')} {customer_data.get('last_name', '')}".strip() or "Unnamed Customer"

        existing_customer = request.env['res.partner'].sudo().search([
            '|',
            ('shopify_customer_id', '=', shopify_customer_id),
            ('email', '=', email)
        ], limit=1)

        customer_vals = {
            'name': customer_name,
            'email': email,
            'shopify_customer_id': shopify_customer_id,
            'phone': customer_data.get('phone'),
            'street': customer_data.get('default_address', {}).get('address1'),
            'street2': customer_data.get('default_address', {}).get('address2'),
            'city': customer_data.get('default_address', {}).get('city'),
            'state_id': self.get_state_id(customer_data.get('default_address', {}).get('province')),
            'country_id': self.get_country_id(customer_data.get('default_address', {}).get('country')),
            'zip': customer_data.get('default_address', {}).get('zip'),
        }

        if existing_customer:
            existing_customer.write(customer_vals)
            _logger.info(f"OK: Updated existing customer {shopify_customer_id}")
            return existing_customer
        else:
            partner = request.env['res.partner'].sudo().create(customer_vals)
            _logger.info(f"OK: Created new customer {shopify_customer_id}")
            return partner

    def get_order_lines(self, line_items, store):
        """Creates order lines for Odoo sales order based on Shopify line items."""
        order_lines = []
        for item in line_items:
            product = request.env['product.product'].sudo().search([('default_code', '=', item.get('sku'))], limit=1)
            if not product:
                _logger.warning(f"WARNING: No matching product found for SKU {item.get('sku')}, skipping...")
                continue

            order_lines.append((0, 0, {
                'product_id': product.id,
                'name': item.get('name'),
                'product_uom_qty': item.get('quantity'),
                'price_unit': float(item.get('price')),
                'tax_id': [(6, 0, [])],
            }))
        return order_lines

    def get_state_id(self, state_name):
        """Finds the state ID in Odoo based on name."""
        if not state_name:
            return False
        state = request.env['res.country.state'].sudo().search([('name', 'ilike', state_name)], limit=1)
        return state.id if state else False

    def get_country_id(self, country_name):
        """Finds the country ID in Odoo based on name."""
        if not country_name:
            return False
        country = request.env['res.country'].sudo().search([('name', 'ilike', country_name)], limit=1)
        return country.id if country else False
    # New customer webhook handler
    @http.route('/shopify_webhook/customer', type='json', auth='none', methods=['POST'])
    def handle_shopify_customer_webhook(self):
        """Handles Shopify customer create/update webhooks and syncs to Odoo."""
        try:
            data = request.httprequest.get_json()
            shop_domain = request.httprequest.headers.get('X-Shopify-Shop-Domain')
            event = request.httprequest.headers.get('X-Shopify-Topic')
            print(f"📩 Customer Webhook received from {shop_domain} | Event: {event} | Customer ID: {data.get('id')}")

            store = request.env['shopify.store'].sudo().search([('shopify_url', 'ilike', shop_domain)], limit=1)
            if not store:
                _logger.error(f"❌ Store not found for domain: {shop_domain}")
                return {'status': 'failed', 'message': 'Store not found'}

            _logger.info(f"📩 Shopify Customer Webhook ({event}) received from {shop_domain} | Customer ID: {data.get('id')}")

            # Sync the customer
            self.sync_customer(data, store)
            return {'status': 'success'}

        except Exception as e:
            _logger.error(f"❌ Error processing Shopify customer webhook: {str(e)}")
            return {'status': 'error', 'message': str(e)}

     # New method for customer webhook syncing
    def sync_customer(self, customer_data, store):
        """Syncs a Shopify customer from webhook data to Odoo."""
        self.get_or_create_customer(customer_data, store)  # Reuse the same logic

    