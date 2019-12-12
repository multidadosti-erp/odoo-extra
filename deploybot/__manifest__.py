{
    'name': 'Deploy Bot',
    'version': '11.0.1.0.0',
    'summary': 'Deploy Bot to update Odoo instancies',
    'category': 'Extra Tools',
    'author': 'MultidadosTI',
    'maintainer': 'MultidadosTI',
    'website': 'www.multidadosti.com.br',
    'license': 'LGPL-3',
    'contributors': [
        'Michell Stuttgart <michellstut@gmail.com>',
    ],
    'depends': [
        'base',
    ],
    'external_dependencies': {
        'python': [
            'fabric',
        ],
    },
    'data': [
        'views/deploy_menu.xml',
        'views/deploy_task.xml',
        'views/deploy_instance.xml',
        'views/deploy_recipe.xml',
        'views/deploy_event.xml',
        'data/ir_sequence.xml',
        'security/ir.model.access.csv',
    ],
    'installable': False,
    'auto_install': False,
    'application': True,
}
