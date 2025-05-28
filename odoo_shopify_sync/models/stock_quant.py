from odoo import models, api, fields
import requests
import logging
from datetime import datetime
import pytz

_logger = logging.getLogger(__name__)

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def create(self, vals):
        record = super(StockQuant, self).create(vals)
        if 'quantity' in vals and not self._should_skip_shopify_sync():
            product = record.product_id  # This is the variant (product.product)
            actual_quantity = product.with_context(warehouse=record.location_id.warehouse_id.id).qty_available  # Total available qty for the variant
            
            # Set last_updated_at to current US Eastern Time
            us_eastern = pytz.timezone('America/New_York')
            current_time_us = fields.Datetime.to_string(datetime.now(us_eastern))
            
            product.sudo().write({
                'last_update_source': 'odoo',
                'last_updated_at': current_time_us  # Updated to US Eastern Time
            })
            _logger.info(f"[SYNC] Created stock quant for variant {product.default_code}. Total available qty: {actual_quantity}")
            self.env['shopify.store'].sync_quantity_to_shopify(product, actual_quantity)
        else:
            _logger.debug(f"Skipping Shopify sync on create for variant {record.product_id.default_code or 'unknown'}")
        return record

    def write(self, vals):
        res = super(StockQuant, self).write(vals)
        if 'quantity' in vals and not self._should_skip_shopify_sync():
            for quant in self:
                product = quant.product_id  # This is the variant (product.product)
                actual_quantity = product.with_context(warehouse=quant.location_id.warehouse_id.id).qty_available  # Total available qty for the variant
                
                # Set last_updated_at to current US Eastern Time
                us_eastern = pytz.timezone('America/New_York')
                current_time_us = fields.Datetime.to_string(datetime.now(us_eastern))
                
                product.sudo().write({
                    'last_update_source': 'odoo',
                    'last_updated_at': current_time_us  # Updated to US Eastern Time
                })
                _logger.info(f"[SYNC] Updated stock quant for variant {product.default_code}. Total available qty: {actual_quantity}")
                self.env['shopify.store'].sync_quantity_to_shopify(product, actual_quantity)
        else:
            for quant in self:
                _logger.debug(f"Skipping Shopify sync on write for variant {quant.product_id.default_code or 'unknown'}")
        return res

    def _should_skip_shopify_sync(self):
        """
        Determine if Shopify sync should be skipped.
        Returns True to skip sync, False to proceed.
        """
        if self.env.context.get('from_shopify', False):
            _logger.debug("Skipping Shopify sync: Update originated from Shopify.")
            return True

        if self.env.context.get('move_line_nosuggest', False) or self.env.context.get('from_stock_move', False):
            _logger.debug("Skipping Shopify sync: Update from order processing or stock move.")
            return True

        product = self.product_id
        if product and product.last_update_source == 'shopify':
            _logger.debug(f"Skipping Shopify sync: Last update for {product.default_code} was from Shopify.")
            return True

        return False