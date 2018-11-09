from odoo import fields, models


class DeployRecipe(models.Model):
    _name = 'deploy.recipe'

    name = fields.Char(string='Name', required=True)
    code = fields.Text(string='Code', required=True)
    sequence = fields.Integer(string='Sequence',
                              help="Gives the sequence order when displaying a list of deploy recipes")
