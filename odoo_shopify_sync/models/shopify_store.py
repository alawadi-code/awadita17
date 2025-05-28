from odoo import models, fields, api
import requests
import base64
from datetime import datetime, timedelta
from pytz import timezone
import pytz
import logging
import psycopg2
import psycopg2.extensions
import time
from functools import wraps
import warnings

# Suppress deprecation warning for invalid escape sequence
warnings.filterwarnings("ignore", category=DeprecationWarning, message="invalid escape sequence")

_logger = logging.getLogger(__name__)

# Enhanced retry decorator with exponential backoff
def retry_on_db_errors(max_attempts=5, base_delay=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempts = 0
            while attempts < max_attempts:
                cr = None
                env = args[0].env if args and hasattr(args[0], 'env') else kwargs.get('env')
                try:
                    _logger.debug(f"Creating new cursor for {func.__name__}, attempt {attempts + 1}")
                    cr = env.registry.cursor()
                    new_env = env.__class__(cr, env.uid, env.context.copy())
                    if args and hasattr(args[0], 'env'):
                        args = list(args)
                        args[0] = args[0].with_env(new_env)
                    else:
                        kwargs['env'] = new_env
                    
                    _logger.debug(f"Executing {func.__name__} with env type: {type(new_env)}, cursor active: {not cr.closed}")
                    result = func(*args, **kwargs)
                    cr.commit()
                    return result
                except (psycopg2.errors.SerializationFailure, psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                    attempts += 1
                    if cr and not cr.closed:
                        cr.rollback()
                        cr.close()
                    if attempts == max_attempts:
                        _logger.error(f"Failed {func.__name__} after {max_attempts} attempts: {str(e)}")
                        raise
                    delay = base_delay * (2 ** (attempts - 1))  # Exponential backoff
                    _logger.info(f"Retrying {func.__name__} after DB error (attempt {attempts + 1}, delay {delay}s): {str(e)}")
                    time.sleep(delay)
                except Exception as e:
                    _logger.error(f"Unexpected error in {func.__name__}: {str(e)}")
                    if cr and not cr.closed:
                        cr.rollback()
                        cr.close()
                    raise
                finally:
                    if cr and not cr.closed:
                        cr.close()
                        _logger.debug(f"Closed cursor for {func.__name__}")
        return wrapper
    return decorator

class ShopifyStore(models.Model):
    _name = 'shopify.store'
    _description = 'Shopify Store'
    
    name = fields.Char('Store Name', required=True)
    shopify_url = fields.Char('Shopify URL', required=True)
    api_key = fields.Char('API Key', required=True)
    api_password = fields.Char('API Password', required=True)
    location_id = fields.Char('Shopify Location ID', help='Used for inventory synchronization')
    warehouse_id = fields.Many2one(
        'stock.warehouse', 
        string='Warehouse', 
        required=True, 
        help='Each Shopify store maps to a different warehouse'
    )
    product_last_fetch_date = fields.Datetime('Last Product Fetch Date')
    order_last_fetch_date = fields.Datetime('Last Order Fetch Date')
    customer_last_fetch_date = fields.Datetime('Last Customer Fetch Date')
    webhook_url = fields.Char('Webhook URL', compute='_compute_webhook_url')
    state = fields.Selection([('draft', 'Draft'), ('active', 'Active')], default='active', tracking=True)
    current_page_info = fields.Char(string="Pagination Cursor")
    is_full_sync = fields.Boolean(string="Full Sync Completed", default=False)
    log_count = fields.Integer('Sync Count', compute='_compute_log_count')
    lock_cron = fields.Boolean(string="Cron Lock", default=False)
    
    def _valid_field_parameter(self, field, name):
        if name == 'tracking':
            return True
        return super()._valid_field_parameter(field, name)

    def _compute_log_count(self):
        for store in self:
            store.log_count = self.env['shopify.sync.log'].search_count([
                ('store_id', '=', store.id)
            ])

    @retry_on_db_errors()
    def fetch_shopify_customers(self):
        """Fetch and sync customers from Shopify."""
        for store in self:
            last_fetch_date = store.customer_last_fetch_date or datetime(1970, 1, 1)
            updated_at_min = last_fetch_date.strftime('%Y-%m-%dT%H:%M:%S')

            base_url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/customers.json"
            params = {
                'updated_at_min': updated_at_min,
                'limit': 250,
            }

            total_customers = 0
            while True:
                response = requests.get(base_url, params=params)
                if response.status_code == 200:
                    customers = response.json().get('customers', [])
                    for customer in customers:
                        self.sync_customer(customer, store)
                        self.env.cr.commit()
                        total_customers += 1
                        _logger.info(f"Synced customer {customer.get('id')} (Total synced: {total_customers})")

                    link_header = response.headers.get('Link')
                    if not link_header or 'rel="next"' not in link_header:
                        break
                    
                    next_link = [link for link in link_header.split(',') if 'rel="next"' in link][0]
                    page_info = next_link.split('page_info=')[1].split('>')[0]
                    params = {'page_info': page_info, 'limit': 250}
                else:
                    _logger.error(f"Error fetching customers: {response.status_code} - {response.text}")
                    break

            store.customer_last_fetch_date = fields.Datetime.now()
            _logger.info(f"Updated customer_last_fetch_date to {store.customer_last_fetch_date}")
            _logger.info(f"Total customers synced: {total_customers}")

    @retry_on_db_errors()
    def sync_customer(self, customer, store):
        """Sync a single Shopify customer to Odoo res.partner."""
        shopify_customer_id = str(customer.get('id'))
        email = customer.get('email')
        first_name = customer.get('first_name', '')
        last_name = customer.get('last_name', '')
        customer_name = f"{first_name} {last_name}".strip() or "Unnamed Customer"

        odoo_customer = self.env['res.partner'].search([
            '|',
            ('shopify_customer_id', '=', shopify_customer_id),
            ('email', '=', email)
        ], limit=1)

        customer_vals = {
            'name': customer_name,
            'email': email,
            'shopify_customer_id': shopify_customer_id,
            'phone': customer.get('phone'),
            'street': customer.get('default_address', {}).get('address1'),
            'street2': customer.get('default_address', {}).get('address2'),
            'city': customer.get('default_address', {}).get('city'),
            'zip': customer.get('default_address', {}).get('zip'),
            'country_id': self.env['res.country'].search([('code', '=', customer.get('default_address', {}).get('country_code'))], limit=1).id,
            'state_id': self.env['res.country.state'].search([
                ('code', '=', customer.get('default_address', {}).get('province_code')),
                ('country_id.code', '=', customer.get('default_address', {}).get('country_code'))
            ], limit=1).id,
        }

        if odoo_customer:
            odoo_customer.with_context(commit_transaction=True).write(customer_vals)
            _logger.info(f"Updated existing customer {shopify_customer_id}")
        else:
            odoo_customer = self.env['res.partner'].create(customer_vals)
            self.env.cr.commit()
            _logger.info(f"Created new customer {shopify_customer_id}")

    def sync_inventory_cron(self):
        """Periodic reconciliation of Shopify inventory, orders, and customers."""
        stores = self.search([('lock_cron', '=', False)])
        
        _logger.info("Starting sync for all stores")
        for store in stores:
            # Acquire lock
            store.with_context(commit_transaction=True).write({'lock_cron': True})
            try:
                _logger.info(f"Syncing store {store.id}")
                store.update_shopify_location_id()
                store.fetch_shopify_inventory()
                _logger.info(f"Completed inventory sync for store {store.id}")
                
                store.fetch_shopify_customers()
                _logger.info(f"Completed customer sync for store {store.id}")
                
                store.fetch_shopify_orders()
                _logger.info(f"Completed order sync for store {store.id}")
            finally:
                # Release lock
                store.with_context(commit_transaction=True).write({'lock_cron': False})
        
        _logger.info("Sync process fully completed")
    
    def _compute_webhook_url(self):
        """Generates the webhook URL dynamically based on Odoo base URL."""
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        if base_url.startswith("http://"):
            base_url = base_url.replace("http://", "https://")
        for record in self:
            record.webhook_url = f"{base_url}/shopify_webhook"

    def register_shopify_webhooks(self):
        """Registers multiple webhooks in Shopify."""
        for store in self:
            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2024-07/webhooks.json"
            headers = {"Content-Type": "application/json"}

            webhook_topics = [
                {"topic": "inventory_levels/update", "endpoint": f"{store.webhook_url}/inventory"},
                {"topic": "orders/create", "endpoint": f"{store.webhook_url}/sales_order"},
                {"topic": "orders/cancelled", "endpoint": f"{store.webhook_url}/sales_order"},
                {"topic": "products/create", "endpoint": f"{store.webhook_url}/product"},
                {"topic": "products/update", "endpoint": f"{store.webhook_url}/product"},
                {"topic": "customers/create", "endpoint": f"{store.webhook_url}/customer"},
                {"topic": "customers/update", "endpoint": f"{store.webhook_url}/customer"}
            ]

            for webhook in webhook_topics:
                payload = {
                    "webhook": {
                        "topic": webhook["topic"],
                        "address": webhook["endpoint"],
                        "format": "json"
                    }
                }

                response = requests.post(url, json=payload, headers=headers)

                if response.status_code == 201:
                    _logger.info(f"✅ Webhook registered for {webhook['topic']} at {webhook['endpoint']}")
                else:
                    _logger.error(f"❌ Failed to register webhook for {webhook['topic']} - {response.text}")

    @retry_on_db_errors()
    def sync_quantity_to_shopify(self, odoo_product, new_quantity):
        """Syncs the provided quantity to Shopify for the specific variant."""
        if not odoo_product or odoo_product.last_update_source != 'odoo':
            _logger.info(f"Skipping sync for {odoo_product.default_code}: Not an Odoo-initiated update.")
            return

        shopify_mappings = self.env['shopify.product.mapping'].sudo().search([('sku', '=', odoo_product.default_code)])
        if not shopify_mappings:
            _logger.error(f"[ERROR] No Shopify mapping found for variant {odoo_product.default_code}")
            return

        for mapping in shopify_mappings:
            store = mapping.store_id
            if not store.location_id:
                store.update_shopify_location_id()

            if store.location_id:
                inventory_item_url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2025-01/inventory_items/{mapping.inventory_item_id}.json"
                response = requests.get(inventory_item_url)
                if response.status_code == 200 and not response.json().get('inventory_item', {}).get('tracked', False):
                    _logger.info(f"Skipping inventory sync for {odoo_product.default_code}: Inventory tracking disabled.")
                    continue

                url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2025-01/inventory_levels/set.json"
                payload = {
                    "location_id": store.location_id,
                    "inventory_item_id": mapping.inventory_item_id,
                    "available": int(new_quantity)
                }
                headers = {"Content-Type": "application/json", "X-Shopify-Reason": "odoo_update"}
                _logger.debug(f"Syncing to Shopify: URL={url}, Payload={payload}")
                try:
                    response = requests.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    _logger.info(f"[SUCCESS] Synced {new_quantity} for variant {odoo_product.default_code} to {store.name}")
                except requests.exceptions.RequestException as e:
                    _logger.error(f"[ERROR] Failed to sync to {store.name}: {e} - {response.text if 'response' in locals() else 'No response'}")
                    self.env['shopify.sync.log'].create({
                        'sync_type': 'product',
                        'store_id': store.id,
                        'status': 'failed',
                        'error_message': f"Failed to sync SKU {odoo_product.default_code}: {str(e)}"
                    })
            else:
                _logger.error(f"[ERROR] No location_id for {store.name}. Sync skipped.")

    @api.model
    def create(self, vals):
        """Registers webhook when a store is added."""
        store = super().create(vals)
        store.register_shopify_webhooks()
        return store
    
    def write(self, vals):
        """Re-registers webhooks when store credentials are updated."""
        for store in self:
            if any(field in vals for field in ['shopify_url', 'api_key', 'api_password']):
                webhook_id = store.get_shopify_webhook_id()
                if webhook_id:
                    store.delete_shopify_webhook(webhook_id)

        result = super().write(vals)

        for store in self:
            if any(field in vals for field in ['shopify_url', 'api_key', 'api_password']):
                store.register_shopify_webhooks()

        return result

    def unlink(self):
        """Deletes related records and webhooks before deleting a store."""
        for store in self:
            related_records = self.env['shopify.product.mapping'].sudo().search([('store_id', '=', store.id)])
            related_records.unlink()
            
            webhook_id = store.get_shopify_webhook_id()
            if webhook_id:
                store.delete_shopify_webhook(webhook_id)

        return super().unlink()

    def get_shopify_webhook_id(self):
        """Fetch the registered webhook ID from Shopify."""
        self.ensure_one()
        url = f"https://{self.api_key}:{self.api_password}@{self.shopify_url}/admin/api/2025-01/webhooks.json"
        response = requests.get(url)
        if response.status_code == 200:
            for webhook in response.json().get("webhooks", []):
                if webhook["address"].startswith(self.webhook_url):
                    return webhook["id"]
        return None

    def delete_shopify_webhook(self, webhook_id):
        """Deletes a webhook from Shopify."""
        url = f"https://{self.api_key}:{self.api_password}@{self.shopify_url}/admin/api/2024-07/webhooks/{webhook_id}.json"
        response = requests.delete(url)
        if response.status_code == 200:
            _logger.info(f"✅ Webhook {webhook_id} deleted for {self.name}")
        else:
            _logger.error(f"❌ Failed to delete webhook {webhook_id} for {self.name} - {response.text}")

    def update_shopify_location_id(self):
        """Fetch and update Shopify Location ID."""
        for store in self.filtered(lambda s: not s.location_id):
            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/locations.json"
            response = requests.get(url)
            if response.status_code == 200:
                locations = response.json().get('locations', [])
                if locations:
                    store.location_id = locations[0].get('id')
                    _logger.info(f"Updated location ID for {store.name}: {store.location_id}")

    @retry_on_db_errors()
    def fetch_shopify_inventory(self):
        for store in self:
            params = {}
            base_url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/products.json"
            log = self.env['shopify.sync.log'].create({
                'sync_type': 'product',
                'store_id': store.id,
                'status': 'in_progress'
            })
            if store.current_page_info:
                params = {'page_info': store.current_page_info, 'limit': 25}
            else:
                last_fetch_date = store.product_last_fetch_date or datetime(1970, 1, 1)
                updated_at_min = last_fetch_date.strftime('%Y-%m-%dT%H:%M:%S')
                params = {
                    'updated_at_min': updated_at_min,
                    'order': 'updated_at asc',
                    'limit': 25,
                }

            while True:
                response = requests.get(base_url, params=params)
                if response.status_code != 200:
                    log.write({
                        'status': 'failed',
                        'error_message': f"API Error: {response.status_code} - {response.text}"
                    })
                    self.env.cr.commit()
                    _logger.error(f"Failed to fetch products: {response.status_code} - {response.text}")
                    break

                products = response.json().get('products', [])
                if not products:
                    break

                max_updated = store.product_last_fetch_date or datetime(1970, 1, 1)
                total_fetched = 0
                total_skipped = 0
                for product in products:
                    cr = self.env.registry.cursor()
                    try:
                        new_env = self.env.__class__(cr, self.env.uid, self.env.context.copy())
                        store_with_new_env = store.with_env(new_env)
                        store_with_new_env.sync_product_inventory(product, store)
                        total_fetched += 1
                        product_updated = product.get('updated_at')
                        if product_updated:
                            product_dt = datetime.strptime(product_updated, '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=None)
                            if product_dt > max_updated:
                                max_updated = product_dt
                        cr.commit()
                    except Exception as e:
                        total_skipped += 1
                        log.write({'error_message': f"Skipped product {product.get('id')}: {str(e)}"})
                        _logger.warning(f"Skipped product {product.get('id')}: {str(e)}")
                        cr.rollback()
                    finally:
                        if not cr.closed:
                            cr.close()

                    log.write({
                        'total_fetched': total_fetched,
                        'total_skipped': total_skipped
                    })
                    self.env.cr.commit()

                log.write({
                    'status': 'completed',
                    'total_fetched': total_fetched,
                    'total_skipped': total_skipped,
                    'total_remaining': 0
                })

                link_header = response.headers.get('Link', '')
                next_link = next((link for link in link_header.split(', ') if 'rel="next"' in link), None)
                if next_link:
                    page_info = next_link.split('page_info=')[1].split('>')[0]
                    store.with_context(commit_transaction=True).write({
                        'current_page_info': page_info,
                        'product_last_fetch_date': max_updated,
                    })
                    self.env.cr.commit()
                    params = {'page_info': page_info, 'limit': 25}
                else:
                    store.with_context(commit_transaction=True).write({
                        'current_page_info': False,
                        'product_last_fetch_date': datetime.now(),
                        'is_full_sync': True,
                    })
                    self.env.cr.commit()
                    break

    @retry_on_db_errors()
    def sync_product_inventory(self, shopify_product, store):
        warehouse = store.warehouse_id
        odoo_template = self.env["product.template"].search([("name", "=", shopify_product["title"])], limit=1)
        has_sku = True
        for variant in shopify_product.get("variants", []):
            shopify_sku = variant.get("sku")
            if not shopify_sku:
                has_sku = False
                break

        if not has_sku:
            _logger.warning(f"Skipping product without SKU: {shopify_product['title']}")
            return

        if not odoo_template:
            odoo_template = self.env["product.template"].create({
                "name": shopify_product["title"],
                "type": "product",
            })
            self.env.cr.commit()

        # Cache attributes and values
        attribute_cache = {}
        value_cache = {}
        attribute_map = {}
        for option in shopify_product.get("options", []):
            attribute_name = option.get("name")
            if attribute_name == "Title" and option.get("values") == ["Default Title"]:
                continue
            attribute = attribute_cache.get(attribute_name)
            if not attribute:
                attribute = self.env["product.attribute"].search([("name", "=", attribute_name)], limit=1)
                if not attribute:
                    attribute = self.env["product.attribute"].create({"name": attribute_name})
                    self.env.cr.commit()
                attribute_cache[attribute_name] = attribute

            attribute_values = []
            for value in option.get("values", []):
                cache_key = (attribute.id, value)
                attr_value = value_cache.get(cache_key)
                if not attr_value:
                    attr_value = self.env["product.attribute.value"].search(
                        [("name", "=", value), ("attribute_id", "=", attribute.id)], limit=1
                    )
                    if not attr_value:
                        attr_value = self.env["product.attribute.value"].create(
                            {"name": value, "attribute_id": attribute.id}
                        )
                        self.env.cr.commit()
                    value_cache[cache_key] = attr_value
                attribute_values.append(attr_value.id)

            attribute_line = self.env["product.template.attribute.line"].search(
                [("attribute_id", "=", attribute.id), ("product_tmpl_id", "=", odoo_template.id)], limit=1
            )
            if not attribute_line:
                attribute_line = self.env["product.template.attribute.line"].create({
                    "product_tmpl_id": odoo_template.id,
                    "attribute_id": attribute.id,
                    "value_ids": [(6, 0, attribute_values)],
                })
                self.env.cr.commit()
            attribute_map[attribute_name] = attribute_line

        # Check if variants already exist
        existing_variants = self.env["product.product"].search_count([("product_tmpl_id", "=", odoo_template.id)])
        if not existing_variants:
            odoo_template._create_variant_ids()
            self.env.cr.commit()

        for variant in shopify_product.get("variants", []):
            shopify_sku = variant.get("sku") or f"{shopify_product['id']}-{variant['id']}"
            attribute_values = []
            attribute_combination = []

            for i, option in enumerate(shopify_product.get("options", [])):
                attribute_name = option.get("name")
                attribute_value_name = variant.get(f"option{i + 1}")
                if attribute_name == "Title" and option.get("values") == ["Default Title"]:
                    continue
                if attribute_name and attribute_value_name:
                    attribute = attribute_cache.get(attribute_name)
                    cache_key = (attribute.id, attribute_value_name)
                    attr_value = value_cache.get(cache_key)
                    if not attr_value:
                        attr_value = self.env["product.attribute.value"].search(
                            [("name", "=", attribute_value_name), ("attribute_id", "=", attribute.id)], limit=1
                        )
                        if not attr_value:
                            attr_value = self.env["product.attribute.value"].create(
                                {"name": attribute_value_name, "attribute_id": attribute.id}
                            )
                            self.env.cr.commit()
                        value_cache[cache_key] = attr_value
                    attribute_line = attribute_map[attribute_name]
                    if attr_value.id not in attribute_line.value_ids.ids:
                        attribute_line.with_context(commit_transaction=True).write({"value_ids": [(4, attr_value.id)]})
                        self.env.cr.commit()

                    template_attr_value = self.env["product.template.attribute.value"].search(
                        [
                            ("product_tmpl_id", "=", odoo_template.id),
                            ("attribute_id", "=", attribute.id),
                            ("product_attribute_value_id", "=", attr_value.id),
                        ], limit=1
                    )
                    if not template_attr_value:
                        template_attr_value = self.env["product.template.attribute.value"].create({
                            "product_tmpl_id": odoo_template.id,
                            "attribute_id": attribute.id,
                            "product_attribute_value_id": attr_value.id,
                            "attribute_line_id": attribute_line.id,
                        })
                        self.env.cr.commit()
                    attribute_values.append(template_attr_value.id)
                    attribute_combination.append(template_attr_value.product_attribute_value_id.id)

            prods = self.env["product.product"].search([("product_tmpl_id", "=", odoo_template.id)])
            odoo_product = None
            for prod in prods:
                variant_attribute_ids = set(prod.product_template_variant_value_ids.mapped("product_attribute_value_id.id"))
                if variant_attribute_ids == set(attribute_combination):
                    odoo_product = prod
                    break

            if not odoo_product:
                _logger.warning(f"No exact variant match for SKU {shopify_sku}, attributes {attribute_combination}. Skipping sync.")
                continue

            self.create_product_mapping(store, variant)
            self.env.cr.commit()

            if odoo_product.default_code != shopify_sku:
                odoo_product.with_context(commit_transaction=True).write({"default_code": shopify_sku})
                self.env.cr.commit()
            if odoo_product.list_price != variant.get("price", 0.0):
                odoo_product.with_context(commit_transaction=True).write({"list_price": variant.get("price", 0.0)})
                self.env.cr.commit()

            inventory_quantity = variant.get("inventory_quantity", 0)
            self.update_inventory_quantity(odoo_product, inventory_quantity, warehouse)
            self.env.cr.commit()

        if "image" in shopify_product and shopify_product["image"] and "src" in shopify_product["image"]:
            self.sync_product_image(odoo_template, shopify_product["image"]["src"])
            self.env.cr.commit()

    @retry_on_db_errors()
    def fetch_shopify_orders(self):
        for store in self:
            last_fetch_date = store.order_last_fetch_date or datetime(1970, 1, 1)
            updated_at_min = last_fetch_date.strftime('%Y-%m-%dT%H:%M:%S')

            base_url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/orders.json"
            params = {
                'updated_at_min': updated_at_min,
                'limit': 250,
                'status': 'any',
            }

            total_orders = 0
            skipped_orders = 0
            while True:
                response = requests.get(base_url, params=params)

                if response.status_code == 200:
                    orders = response.json().get('orders', [])
                    for order in orders:
                        if self._all_products_exist_in_odoo(order, store):
                            self.sync_order(order, store)
                            self.env.cr.commit()
                            total_orders += 1
                            _logger.info(f"Synced order {order.get('id')} (Total synced: {total_orders})")
                        else:
                            skipped_orders += 1
                            _logger.warning(f"Skipped order {order.get('id')} due to missing products")

                    link_header = response.headers.get('Link')
                    if not link_header or 'rel="next"' not in link_header:
                        break
                    
                    next_link = [link for link in link_header.split(',') if 'rel="next"' in link][0]
                    page_info = next_link.split('page_info=')[1].split('>')[0]
                    params = {'page_info': page_info, 'limit': 250}
                else:
                    _logger.error(f"Error fetching orders: {response.status_code} - {response.text}")
                    break

            store.order_last_fetch_date = fields.Datetime.from_string(
                datetime.now(timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
            )
            _logger.info(f"Updated order_last_fetch_date to {store.order_last_fetch_date}")
            _logger.info(f"Total orders synced: {total_orders}, Skipped: {skipped_orders}")

    def _all_products_exist_in_odoo(self, order, store):
        for line_item in order.get('line_items', []):
            product = self.env['product.product'].search([
                '|',
                ('default_code', '=', line_item.get('sku')),
                ('shopify_product_id', '=', str(line_item.get('product_id')))
            ], limit=1)
            if not product:
                _logger.warning(f"Product not found in Odoo: SKU={line_item.get('sku')}, Shopify ID={line_item.get('product_id')}")
                return False
        return True

    @retry_on_db_errors()
    def sync_order(self, order, store):
        shopify_order_id = order.get('id')
        odoo_order = self.env['sale.order'].search([('shopify_order_id', '=', shopify_order_id)], limit=1)

        if not odoo_order:
            if not isinstance(order, dict):
                _logger.error(f"Invalid order data for Shopify Order ID {shopify_order_id}: {order}")
                return

            customer_data = order.get('customer')
            shopify_customer_id = False
            email = order.get('email')

            if customer_data and isinstance(customer_data, dict):
                shopify_customer_id = str(customer_data.get('id', '')) if customer_data.get('id') else False
                email = customer_data.get('email') or email
            else:
                _logger.warning(f"No customer data found for Shopify Order ID {shopify_order_id}")

            customer = self.env['res.partner'].search([
                '|',
                ('shopify_customer_id', '=', shopify_customer_id),
                ('email', '=', email)
            ], limit=1)

            if not customer:
                customer = self.env['res.partner'].sudo().search([('name', '=', 'Guest Customer')], limit=1)
                if not customer:
                    customer = self.env['res.partner'].sudo().create({
                        'name': 'Guest Customer',
                        'email': 'guest@example.com',
                        'phone': '',
                    })
                    self.env.cr.commit()

            shopify_date = order.get('created_at')
            if shopify_date:
                try:
                    dt = datetime.strptime(shopify_date, '%Y-%m-%dT%H:%M:%S%z')
                    dt_utc = dt.astimezone(pytz.UTC)
                    date_order = dt_utc.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError as e:
                    _logger.error(f"Invalid date format for Shopify Order ID {shopify_order_id}: {shopify_date} - {str(e)}")
                    date_order = fields.Datetime.now()
            else:
                date_order = fields.Datetime.now()

            financial_status = order.get('financial_status', 'pending')
            fulfillment_status = order.get('fulfillment_status')
            state = 'draft'

            order_vals = {
                'partner_id': customer.id,
                'shopify_order_id': shopify_order_id,
                'date_order': date_order,
                'state': state,
                'origin': f"Shopify Order #{order.get('name', shopify_order_id)}",
                'warehouse_id': store.warehouse_id.id,
            }
            odoo_order = self.env['sale.order'].create(order_vals)
            self.env.cr.commit()

            line_items = order.get('line_items', [])
            product_quantities = {}

            for line in line_items:
                if not isinstance(line, dict):
                    _logger.warning(f"Invalid line item for Shopify Order ID {shopify_order_id}: {line}")
                    continue

                sku = line.get('sku')
                if not sku:
                    _logger.warning(f"Line item missing SKU for Shopify Order ID {shopify_order_id}: {line}")
                    continue

                quantity = line.get('quantity', 0)
                price = float(line.get('price', 0.0))

                if sku in product_quantities:
                    product_quantities[sku]['quantity'] += quantity
                    product_quantities[sku]['price'] = price
                else:
                    product_quantities[sku] = {'quantity': quantity, 'price': price}

            for sku, data in product_quantities.items():
                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
                if product:
                    self.env['sale.order.line'].create({
                        'order_id': odoo_order.id,
                        'product_id': product.id,
                        'product_uom_qty': data['quantity'],
                        'price_unit': data['price'],
                        'tax_id': [(6, 0, [])],
                    })
                    self.env.cr.commit()
                else:
                    _logger.warning(f"Product with SKU {sku} not found for Shopify Order ID {shopify_order_id}")

            if fulfillment_status in ('fulfilled', 'partial') or financial_status in ('paid', 'partially_paid'):
                if odoo_order.state in ('draft', 'sent'):
                    odoo_order.action_confirm()
                    self.env.cr.commit()
                
                if financial_status in ('paid', 'partially_paid'):
                    self._handle_invoicing(odoo_order, order, financial_status)
                
                if fulfillment_status in ('fulfilled', 'partial'):
                    self._handle_delivery(odoo_order, order, fulfillment_status)

            if financial_status in ('refunded', 'partially_refunded', 'voided') and odoo_order.state != 'cancel':
                odoo_order.action_cancel()
                self.env.cr.commit()

        else:
            financial_status = order.get('financial_status', 'pending')
            fulfillment_status = order.get('fulfillment_status')

            if financial_status in ('refunded', 'partially_refunded', 'voided') and odoo_order.state != 'cancel':
                odoo_order.action_cancel()
                self.env.cr.commit()
            elif (fulfillment_status in ('fulfilled', 'partial') or financial_status in ('paid', 'partially_paid')) and odoo_order.state in ('draft', 'sent'):
                odoo_order.action_confirm()
                self.env.cr.commit()
                if financial_status in ('paid', 'partially_paid'):
                    self._handle_invoicing(odoo_order, order, financial_status)
                if fulfillment_status in ('fulfilled', 'partial'):
                    self._handle_delivery(odoo_order, order, fulfillment_status)

            _logger.info(f"Order {shopify_order_id} already synced as {odoo_order.name}, checked status updates")

        _logger.info(f"Processed order {shopify_order_id}")

    def _handle_invoicing(self, odoo_order, order, financial_status):
        """Handle invoice creation and payment based on Shopify financial status."""
        if odoo_order.state == 'sale' and not odoo_order.invoice_ids:
            invoice = odoo_order._create_invoices()
            invoice.action_post()
            self.env.cr.commit()

            if financial_status == 'paid':
                journal = self.env['account.journal'].search([('type', '=', 'cash')], limit=1)
                if not journal:
                    _logger.error(f"No cash journal found for payment of Shopify Order ID {order.get('id')}")
                    return

                payment = self.env['account.payment'].create({
                    'partner_id': odoo_order.partner_id.id,
                    'amount': invoice.amount_total,
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'journal_id': journal.id,
                    'date': fields.Date.today(),
                })
                payment.action_post()
                self.env.cr.commit()

                invoice_line = invoice.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                payment_line = payment.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                if invoice_line and payment_line:
                    (invoice_line + payment_line).reconcile()
                    self.env.cr.commit()
                    _logger.info(f"Invoice created and paid for Shopify Order ID {order.get('id')}")
                else:
                    _logger.error(f"Failed to reconcile payment for Shopify Order ID {order.get('id')}")
            else:
                _logger.info(f"Invoice created but not paid for Shopify Order ID {order.get('id')} (status: {financial_status})")

    def _handle_delivery(self, odoo_order, order, fulfillment_status):
        """Handle delivery creation, validation, and stock reversion."""
        if odoo_order.state == 'sale' and not odoo_order.picking_ids.filtered(lambda p: p.state == 'done'):
            picking = odoo_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
            if not picking:
                odoo_order.action_confirm()
                picking = odoo_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
                self.env.cr.commit()

            if picking:
                original_stock = {}
                for move in picking.move_ids:
                    product = move.product_id
                    location = move.location_id
                    original_stock[product.id] = self.env['stock.quant'].search([
                        ('product_id', '=', product.id),
                        ('location_id', '=', location.id)
                    ], limit=1).quantity or 0
                    _logger.debug(f"Before delivery - {product.default_code}: {original_stock[product.id]}")

                picking.with_context(skip_backorder=True).button_validate()
                self.env.cr.commit()

                for move in picking.move_ids:
                    product = move.product_id
                    location = move.location_id
                    original_qty = original_stock.get(product.id, 0)
                    current_quant = self.env['stock.quant'].search([
                        ('product_id', '=', product.id),
                        ('location_id', '=', location.id)
                    ], limit=1)
                    if current_quant:
                        current_quant.sudo().with_context(commit_transaction=True).write({'quantity': original_qty})
                    else:
                        self.env['stock.quant'].sudo().create({
                            'product_id': product.id,
                            'location_id': location.id,
                            'quantity': original_qty,
                        })
                    self.env.cr.commit()
                    _logger.debug(f"After revert - {product.default_code}: {original_qty}")

                if fulfillment_status == 'fulfilled':
                    _logger.info(f"Delivery validated and stock reverted for Shopify Order ID {order.get('id')}")
                elif fulfillment_status == 'partial':
                    _logger.info(f"Partial delivery validated and stock reverted for Shopify Order ID {order.get('id')}")
            else:
                _logger.error(f"No picking created for Shopify Order ID {order.get('id')} despite confirmation")

    def create_product_mapping(self, store, product):
        """Create or update a mapping for the Shopify product in Odoo"""
        product_sku = product.get('sku')
        inventory_item_id = product.get('inventory_item_id')
        if product_sku and inventory_item_id:
            existing_mapping = self.env['shopify.product.mapping'].sudo().search([
                ('store_id', '=', store.id),
                ('sku', '=', product_sku),
            ], limit=1)
            if existing_mapping:
                existing_mapping.with_context(commit_transaction=True).write({'inventory_item_id': inventory_item_id})
                _logger.info(f"Updated product mapping for SKU {product_sku} for store {store.name}")
            else:
                self.env['shopify.product.mapping'].sudo().create({
                    'store_id': store.id,
                    'sku': product_sku,
                    'inventory_item_id': inventory_item_id
                })
                self.env.cr.commit()
                _logger.info(f"Created product mapping for SKU {product_sku} for store {store.name}")

    @retry_on_db_errors()
    def update_inventory_quantity(self, odoo_product, new_quantity, warehouse):
        """Updates the inventory correctly using stock.quant to reflect the new Shopify quantity."""
        if not odoo_product:
            _logger.error("No valid Odoo product found for inventory update.")
            return

        location_id = warehouse.lot_stock_id.id
        stock_quant = self.env["stock.quant"].search(
            [("product_id", "=", odoo_product.id), ("location_id", "=", location_id)], limit=1
        )
        if stock_quant:
            stock_quant.sudo().with_context(commit_transaction=True).write({"quantity": new_quantity})
        else:
            self.env["stock.quant"].sudo().create({
                "product_id": odoo_product.id,
                "location_id": location_id,
                "quantity": new_quantity,
                "company_id": warehouse.company_id.id,
            })
        self.env.cr.commit()

    def create_inventory_adjustment(self, odoo_product, qty_difference, warehouse):
        """Adjusts inventory in Odoo based on Shopify stock levels."""
        location_id = warehouse.lot_stock_id.id
        stock_quant = self.env['stock.quant'].search([
            ('product_id', '=', odoo_product.id),
            ('location_id', '=', location_id)
        ], limit=1)

        if stock_quant:
            stock_quant.with_context(commit_transaction=True).write({'quantity': stock_quant.quantity + qty_difference})
        else:
            self.env['stock.quant'].create({
                'product_id': odoo_product.id,
                'location_id': location_id,
                'quantity': qty_difference,
                'company_id': warehouse.company_id.id
            })
        self.env.cr.commit()

    def sync_product_image(self, odoo_template, image_url):
        """Syncs Shopify product images to Odoo."""
        try:
            image_data = requests.get(image_url).content
            odoo_template.with_context(commit_transaction=True).write({'image_1920': base64.b64encode(image_data)})
            self.env.cr.commit()
        except Exception as e:
            _logger.error(f"Error syncing image for product {odoo_template.name}: {str(e)}")

    def shopify_api_post(self, endpoint, payload):
        """Send POST request to Shopify API using API key and password."""
        shopify_url = f"https://{self.api_key}:{self.api_password}@{self.shopify_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        try:
            response = requests.post(shopify_url, json=payload, headers=headers)
            if response.status_code in [200, 201]:
                _logger.info(f"✅ Successfully synced to Shopify Store: {self.name}")
            else:
                _logger.error(f"❌ Failed to sync to Shopify Store {self.name}: {response.status_code} - {response.text}")
            return response
        except requests.exceptions.RequestException as e:
            _logger.error(f"❌ Shopify API request failed for {self.name}: {str(e)}")
            return None

class ShopifyProductMapping(models.Model):
    _name = "shopify.product.mapping"
    _description = "Shopify SKU to Inventory Mapping"

    store_id = fields.Many2one('shopify.store', ondelete='cascade', string="Shopify Store", required=True)
    sku = fields.Char(string="SKU", required=True, index=True)
    inventory_item_id = fields.Char(string="Inventory Item ID", required=True, index=True)

class ShopifySyncLog(models.Model):
    _name = 'shopify.sync.log'
    _description = 'Shopify Synchronization Log'
    _order = 'sync_date desc'

    sync_type = fields.Selection([
        ('product', 'Products'),
        ('customer', 'Customers'),
        ('order', 'Orders')
    ], string='Sync Type', required=True)
    
    sync_date = fields.Datetime('Sync Date', default=fields.Datetime.now)
    store_id = fields.Many2one('shopify.store', string='Store', required=True)
    
    total_fetched = fields.Integer('Fetched Items')
    total_skipped = fields.Integer('Skipped Items')
    total_remaining = fields.Integer('Remaining Items')
    
    status = fields.Selection([
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ], string='Status', default='in_progress')
    
    error_message = fields.Text('Error Details')