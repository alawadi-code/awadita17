# -*- coding: utf-8 -*-

import logging
import base64
import requests

from odoo import models, fields, api, Command


_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'


    @api.model
    def create_product_from_shopify(self, product, warehouse):

        data = {
            'name': product.get('title', ''),
            'detailed_type': 'product',
            'sale_ok': True,
            'purchase_ok': True,
            'list_price': 0,
        }

        tags = product.get('tags')
        if tags:
            data['product_tag_ids'] = self._prepare_product_tags(tags)

        variant_options = product.get('options', [])
        variant_id = inventory_id = False
        if any("Default Title" in option["values"] for option in variant_options):
            variants = product.get('variants', [{}])
            product_info = variants.get('nodes')[0]
            variant_id = product_info['legacyResourceId']
            variant_graphql_id = product_info['id']
            inventory_id = product_info['inventoryItem']
            
            data.update({
                'default_code': product_info['sku'],
                'barcode': product_info['barcode'],
                'list_price': float(product_info['price']),
            })

        product_variants = self._prepare_product_variants(variant_options)
        if product_variants.get('attribute_line_ids', []):
            data.update(product_variants)

        shopify_product = self.create(data)
        if shopify_product.product_variant_count <= 1:
            if variant_id:
                shopify_product.product_variant_id.write({
                    'product_product_id': variant_id,
                    
                })
        else:
            # Update product variants information such as shopify id, pricing, etc.
            prod_prod_variants = shopify_product.product_variant_ids
            data_variants = product.get('variants', [])

            self._update_product_variant_info(data_variants, prod_prod_variants)

        # Add an image
        image_info = product.get('image')
        if image_info:
            image_url = image_info.get('src')
            if image_url:
                image = self._get_binary_image(image_url)
                data['image_1920'] = image


        
        
        return shopify_product

    def _prepare_product_variants(self, options):
        product_attribute = self.env['product.attribute']
        product_attribute_value = self.env['product.attribute.value']

        product_variants = []

        for variant in options:
            variant_value_ids = []
            
            if 'Default Title' in variant['values']:
                continue

            # Create or search for the product attribute
            product_attribute_id = product_attribute.search([('name', '=', variant['name']),
                                                             ('create_variant', '=', 'always')], limit=1)

            

            # Auto update Graphql ID
            if not product_attribute_id:
                product_attribute_id = product_attribute.create({'name': variant['name'],
                                                                
                                                                 'create_variant': 'always'
                                                                 })

            for index, value in enumerate(variant.get('optionValues', [])):
                
                attrib_value_id = product_attribute_value.search([('name', '=', value.get('name')),
                                                                 
                                                                  ('attribute_id', '=', product_attribute_id.id)],
                                                                 limit=1)

                # Auto update Graphql ID
                

                if not attrib_value_id:
                    attrib_value_id = product_attribute_value.create({
                        'name': value.get('name'),
                        
                        'attribute_id': product_attribute_id.id,
                        'sequence': index
                    })

                variant_value_ids.append(attrib_value_id.id)

            product_variants.append(Command.create({
                    'attribute_id': product_attribute_id.id,
                    'value_ids': [Command.set(variant_value_ids)]
                })
            )

        return {'attribute_line_ids': product_variants}

    def _update_product_variant_info(self, variant_data, product_variants):

        for data in variant_data.get('nodes'):
            selected_options = [item["optionValue"]["id"].split('/')[-1] for item in data.get('selectedOptions')]

            match_variant = False
            for product_variant in product_variants:
                variant_values = product_variant.product_template_variant_value_ids.mapped('product_attribute_value_id.shopify_id')
                if sorted(selected_options) == sorted(variant_values):
                    match_variant = product_variant
                    break

            price = data.get('price') or 0.0
            
            inventory_item = data.get('inventoryItem')
            if match_variant:
                match_variant.write({
                    'product_product_id': data.get('legacyResourceId'),
                    'product_graphql_id': data.get('id'),
                    'compare_at_price': float(data.get('compareAtPrice')) if data.get('compareAtPrice') else 0,
                   
                    'default_code': data.get('sku'),
                    'barcode': data.get('barcode'),
                    'lst_price': float(price),
                    'standard_price': float(inventory_item['unitCost']['amount']) if inventory_item['unitCost']['amount'] else 0,
                })

    def _prepare_product_tags(self, tags):
        # Add a tag
        tag_cmd = []

        for tag in tags:
            check_tag = self.env['product.tag'].search([('name', '=', tag)])
            if check_tag:
                tag_cmd.append(Command.link(check_tag.id))
            else:
                tag_cmd.append(Command.create({'name': tag}))
        return tag_cmd

    # For Testing
    def _get_binary_image(self, image_url):
        binary_data = None
        try:
            with requests.get(image_url) as response:
                if response.status_code == 200:
                    # Encode the image content to base64
                    binary_data = base64.b64encode(response.content)
        except requests.RequestException as e:
            _logger.info("Request failed: %s", e)
        except Exception as e:
            _logger.error("An unexpected error occurred: %s", e)

        return binary_data
