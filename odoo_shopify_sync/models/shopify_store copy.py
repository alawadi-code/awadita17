from odoo import models, fields, api
import requests
import base64
from datetime import datetime

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
    webhook_url = fields.Char('Webhook URL', compute='_compute_webhook_url')

    def _compute_webhook_url(self):
        """Generates the webhook URL dynamically based on Odoo base URL."""
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        if base_url.startswith("http://"):
            base_url = base_url.replace("http://", "https://")
        for record in self:
            record.webhook_url = f"{base_url}/shopify_webhook"

    def register_shopify_webhooks(self):
        """Registers a webhook in Shopify to track inventory updates."""
        for store in self:
            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2024-07/webhooks.json"
            headers = {"Content-Type": "application/json"}
            payload = {
                "webhook": {
                    "topic": "inventory_levels/update",
                    "address": store.webhook_url,
                    "format": "json"
                }
            }
            response = requests.post(url, json=payload, headers=headers)
            if response.status_code == 201:
                print(f"✅ Webhook registered for {store.name}")
            else:
                print(f"❌ Failed to register webhook for {store.name} - {response.text}")

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
                if webhook["address"] == self.webhook_url:
                    return webhook["id"]
        return None

    def delete_shopify_webhook(self, webhook_id):
        """Deletes a webhook from Shopify."""
        url = f"https://{self.api_key}:{self.api_password}@{self.shopify_url}/admin/api/2025-01/webhooks/{webhook_id}.json"
        response = requests.delete(url)
        if response.status_code == 200:
            print(f"✅ Webhook {webhook_id} deleted for {self.name}")
        else:
            print(f"❌ Failed to delete webhook {webhook_id} for {self.name} - {response.text}")

    def update_shopify_location_id(self):
        """Fetch and update Shopify Location ID."""
        for store in self.filtered(lambda s: not s.location_id):
            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/locations.json"
            response = requests.get(url)
            if response.status_code == 200:
                locations = response.json().get('locations', [])
                if locations:
                    store.location_id = locations[0].get('id')
                    print(f"Updated location ID for {store.name}: {store.location_id}")

    @api.model
    def sync_inventory_cron(self):
        for store in self.search([]):
            store.update_shopify_location_id()
            store.fetch_shopify_inventory()

    def fetch_shopify_inventory(self):
        for store in self:
            last_fetch_date = store.product_last_fetch_date or datetime(1970, 1, 1)
            last_fetch_date =  datetime(1970, 1, 1)
            updated_at_min = last_fetch_date.strftime('%Y-%m-%dT%H:%M:%S')

            url = f"https://{store.api_key}:{store.api_password}@{store.shopify_url}/admin/api/2023-01/products.json?updated_at_min={updated_at_min}"
            response = requests.get(url)
            if response.status_code == 200:
                for product in response.json().get('products', []):
                    self.sync_product_inventory(product, store)
                    self.create_product_mapping(store, product)
                store.product_last_fetch_date = fields.Datetime.now()
                
    def create_product_mapping(self, store, product):
        """Create a mapping for the Shopify product in Odoo"""
        product_sku = product['variants'][0].get('sku')
        inventory_item_id = product['variants'][0].get('inventory_item_id')

        if product_sku and inventory_item_id:
            existing_mapping = self.env['shopify.product.mapping'].sudo().search([
                ('store_id', '=', store.id),
                ('sku', '=', product_sku),
                ('inventory_item_id', '=', inventory_item_id)
            ], limit=1)

            if not existing_mapping:
                self.env['shopify.product.mapping'].sudo().create({
                    'store_id': store.id,
                    'sku': product_sku,
                    'inventory_item_id': inventory_item_id
                })
                print(f"Created product mapping for SKU {product_sku} and Inventory Item ID {inventory_item_id} for store {store.name}")
            else:
                print(f"Mapping already exists for SKU {product_sku} in store {store.name}")

        
    def sync_product_inventory(self, shopify_product, store):
        """Sync Shopify products and variants with Odoo, ensuring attributes and variants are created correctly."""

        warehouse = store.warehouse_id

        # Search for existing product template
        odoo_template = self.env["product.template"].search(
            [("name", "=", shopify_product["title"])], limit=1
        )

        if not odoo_template:
            # Create the main product template
            odoo_template = self.env["product.template"].create(
                {
                    "name": shopify_product["title"],
                    "type": "product",
                }
            )

        # Ensure attributes exist and are linked to the product template
        attribute_map = {}  # Dictionary to store attribute lines for the product template

        for option in shopify_product.get("options", []):
            attribute_name = option.get("name")

            # Find or create the attribute
            attribute = self.env["product.attribute"].search(
                [("name", "=", attribute_name)], limit=1
            )
            if not attribute:
                attribute = self.env["product.attribute"].create({"name": attribute_name})

            # Ensure at least one value exists for the attribute
            attribute_values = []
            for value in option.get("values", []):
                attr_value = self.env["product.attribute.value"].search(
                    [("name", "=", value), ("attribute_id", "=", attribute.id)], limit=1
                )

                if not attr_value:
                    attr_value = self.env["product.attribute.value"].create(
                        {"name": value, "attribute_id": attribute.id}
                    )

                attribute_values.append(attr_value.id)

            # Ensure attribute line exists in product template
            attribute_line = self.env["product.template.attribute.line"].search(
                [
                    ("attribute_id", "=", attribute.id),
                    ("product_tmpl_id", "=", odoo_template.id),
                ],
                limit=1,
            )

            if not attribute_line:
                attribute_line = self.env["product.template.attribute.line"].create(
                    {
                        "product_tmpl_id": odoo_template.id,
                        "attribute_id": attribute.id,
                        "value_ids": [
                            (6, 0, attribute_values)
                        ],  # Ensure values are assigned to attribute line
                    }
                )

            # Store the attribute line ID for later use
            attribute_map[attribute_name] = attribute_line

        # Iterate through Shopify variants
        for variant in shopify_product.get("variants", []):
            shopify_sku = variant.get("sku")
            
            # Generate SKU if missing
            if not shopify_sku:
                shopify_sku = f"{shopify_product['id']}-{variant['id']}"

            # Search for product variant in Odoo by SKU
            odoo_product = self.env["product.product"].search(
                [("default_code", "=", shopify_sku)], limit=1
            )

            attribute_values = []
            attribute_combination = []

            for i, option in enumerate(shopify_product.get("options", [])):
                attribute_name = option.get("name")
                attribute_value_name = variant.get(f"option{i + 1}")

                if attribute_name and attribute_value_name:
                    # Find or create attribute value
                    attribute = self.env["product.attribute"].search(
                        [("name", "=", attribute_name)], limit=1
                    )
                    attr_value = self.env["product.attribute.value"].search(
                        [
                            ("name", "=", attribute_value_name),
                            ("attribute_id", "=", attribute.id),
                        ],
                        limit=1,
                    )

                    if not attr_value:
                        attr_value = self.env["product.attribute.value"].create(
                            {"name": attribute_value_name, "attribute_id": attribute.id}
                        )

                    # Ensure value is assigned to the template attribute line
                    attribute_line = attribute_map[attribute_name]

                    if attr_value.id not in attribute_line.value_ids.ids:
                        attribute_line.write({"value_ids": [(4, attr_value.id)]})

                    # Ensure the attribute value exists in the product template
                    template_attr_value = self.env[
                        "product.template.attribute.value"
                    ].search(
                        [
                            ("product_tmpl_id", "=", odoo_template.id),
                            ("attribute_id", "=", attribute.id),
                            ("product_attribute_value_id", "=", attr_value.id),
                        ],
                        limit=1,
                    )

                    if not template_attr_value:
                        template_attr_value = self.env[
                            "product.template.attribute.value"
                        ].create(
                            {
                                "product_tmpl_id": odoo_template.id,
                                "attribute_id": attribute.id,
                                "product_attribute_value_id": attr_value.id,
                                "attribute_line_id": attribute_line.id,
                            }
                        )

                    attribute_values.append(template_attr_value.id)
                    attribute_combination.append(
                        template_attr_value.product_attribute_value_id.id
                    )

            # Check if a product with this combination already exists
            existing_variant = self.env["product.product"].search(
                [
                    ("product_tmpl_id", "=", odoo_template.id),
                    (
                        "product_template_variant_value_ids.product_attribute_value_id",
                        "in",
                        attribute_combination,
                    ),
                ],
                limit=1,
            )
          
            if not odoo_product and not existing_variant:
                # Create the product variant
                odoo_product = self.env["product.product"].create(
                    {
                        "product_tmpl_id": odoo_template.id,
                        "default_code": shopify_sku,
                        "list_price": variant.get("price", 0.0),
                        "product_template_variant_value_ids": [(6, 0, attribute_values)],
                    }
                )
            elif existing_variant:
                odoo_product = existing_variant
                existing_variant.default_code = shopify_sku
                existing_variant.list_price = variant.get("price", 0.0)

            # Sync inventory quantity
            inventory_quantity = variant.get("inventory_quantity", 0)
            current_qty = odoo_product.with_context(warehouse=warehouse.id).qty_available
            qty_difference = inventory_quantity - current_qty

            if qty_difference != 0:
                self.create_inventory_adjustment(odoo_product, qty_difference, warehouse)

            # Update stock quantity
            odoo_product.with_context(
                warehouse=warehouse.id
            ).qty_available = inventory_quantity

            # Update price if different
            if odoo_product.list_price != variant.get("price", 0.0):
                odoo_product.list_price = variant.get("price", 0.0)

        # Sync main product image
        if (
            "image" in shopify_product
            and shopify_product["image"]
            and "src" in shopify_product["image"]
        ):
            self.sync_product_image(odoo_template, shopify_product["image"]["src"])

    def create_inventory_adjustment(self, odoo_product, qty_difference, warehouse):
        """ Adjusts inventory in Odoo based on Shopify stock levels. """
        
        location_id = warehouse.lot_stock_id.id
        stock_quant = self.env['stock.quant'].search([
            ('product_id', '=', odoo_product.id),
            ('location_id', '=', location_id)
        ], limit=1)

        if stock_quant:
            stock_quant.write({'quantity': stock_quant.quantity + qty_difference})
        else:
            self.env['stock.quant'].create({
                'product_id': odoo_product.id,
                'location_id': location_id,
                'quantity': qty_difference,
                'company_id': warehouse.company_id.id
            })

    def sync_product_image(self, odoo_template, image_url):
        """ Syncs Shopify product images to Odoo. """
        
        try:
            image_data = requests.get(image_url).content
            odoo_template.write({'image_1920': base64.b64encode(image_data)})
        except Exception as e:
            print(f"Error syncing image for product {odoo_template.name}: {str(e)}")

class ShopifyProductMapping(models.Model):
    _name = "shopify.product.mapping"
    _description = "Shopify SKU to Inventory Mapping"

    store_id = fields.Many2one('shopify.store', ondelete='cascade', string="Shopify Store", required=True)
    sku = fields.Char(string="SKU", required=True, index=True)
    inventory_item_id = fields.Char(string="Inventory Item ID", required=True, index=True)
