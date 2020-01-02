{
    'name': 'Runbot',
    'category': 'Website',
    'summary': 'Odoo Continue Intergration Addon',
    'version': '12.0.1.0.0',
    'author': 'Odoo SA',
    'contributors': [
        'Michell Stuttgart <michellstut@gmail.com>',
    ],
    'depends': [
        'website',
    ],
    'external_dependencies': {
        'python': [
            'matplotlib',
        ],
    },
    'data': [
        'security/runbot_security.xml',
        'security/ir.model.access.csv',
        'security/ir.rule.csv',
        'data/ir_cron.xml',
        'views/runbot.xml',
        'views/runbot_branch.xml',
        'views/runbot_build.xml',
        'views/res_config_settings.xml',
        'views/runbot_repo.xml',
        'views/runbot_templates.xml',
    ],
    'installable': True,
}
