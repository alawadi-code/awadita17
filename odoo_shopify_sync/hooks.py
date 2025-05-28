# shopify_sync/hooks.py
import logging

_logger = logging.getLogger(__name__)

def post_init_hook(env):
    """
    Enable product variants by adding the 'group_product_variant' group
    when the shopify_sync module is installed.
    """
    # Use env to access cr and registry if needed
    cr = env.cr
    registry = env.registry
    
    # Find the 'Product Variants' group
    variant_group = env.ref('product.group_product_variant', raise_if_not_found=False)
    if not variant_group:
        _logger.warning("Product Variants group not found. Ensure 'product' module is installed.")
        return

    # Enable the group globally by setting it as implied for the base 'User' group
    user_group = env.ref('base.group_user')
    if variant_group not in user_group.implied_ids:
        user_group.sudo().write({'implied_ids': [(4, variant_group.id)]})
        _logger.info("Enabled 'Product Variants' by adding group_product_variant to base.group_user.")

    _logger.info("Shopify Sync module installed: Product Variants feature enabled.")

    