{
    'name': 'Odoo Shopify Sync',
    'version': '17.0',
    'category': 'Sales',
    'summary': 'Synchronize products and inventory between Shopify stores and Odoo',
    'description': 'A module to sync products and inventory between multiple Shopify stores and Odoo, including support for product variants.',
    
    # Author
    'author': 'Synetal Solutions Pvt. Ltd.',
    'website': 'http://www.synetalsolutions.com/',
    'maintainer': 'Synetal Solutions Pvt. Ltd.',

    'depends': ['base', 'stock', 'sale', 'sale_management', 'product' , 'account'],
    'data': [
        'security/ir.model.access.csv', 
        'views/sync_log_views.xml',  
        'views/shopify_store_views.xml',
        'data/scheduled_actions.xml',
        'views/sale_menus.xml', 
        
    ],
    'installable': True,
    'price': 0,
    'currency': 'EUR',
    'license': 'OPL-1',
    'auto_install': False,
    'images': ['static/description/banner.gif'],
    'application': True,
    'post_init_hook': 'post_init_hook',
}