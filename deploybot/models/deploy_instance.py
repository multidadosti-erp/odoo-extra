from odoo import fields, models


class DeployInstance(models.Model):
    _name = 'deploy.instance'
    _description = 'Deploy Instance'

    name = fields.Char(string='Name', required=True)
    user = fields.Char(string='User')
    host = fields.Char(string='Host', required=True)
    port = fields.Integer(string='Port', default=22, required=True)
    home = fields.Char(string='Home Path')
    database = fields.Char(string='Database')
    service = fields.Char(string='Service Name')
    sequence = fields.Integer(string='Sequence',
                              help="Gives the sequence order when displaying a list of deploy instances")

    category = fields.Selection(string='Category',
                                selection=[('development', 'Development'),
                                           ('test', 'Test'),
                                           ('production', 'Production')])
