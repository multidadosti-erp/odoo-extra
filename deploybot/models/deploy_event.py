from odoo import api, fields, models


class DeployEvent(models.Model):
    _name = 'deploy.event'
    _description = 'Deploy Event'

    name = fields.Char(string='Name',
                       copy=False)

    task_id = fields.Many2one(comodel_name='deploy.task',
                              readonly=True,
                              string='Task')

    instance_id = fields.Many2one(comodel_name='deploy.instance',
                                  readonly=True,
                                  string='Instance')

    recipe_id = fields.Many2one(comodel_name='deploy.recipe',
                                readonly=True,
                                string='Recipe')

    output = fields.Text(string='Output', readonly=True,)

    start_datetime = fields.Datetime(string='Start DateTime',
                                     change_default=True,
                                     readonly=True,
                                     store=True,
                                     default=lambda x: fields.datetime.now())

    finish_datetime = fields.Datetime(string='Finish DateTime', readonly=True,)

    state = fields.Selection(string='State',
                             readonly=True,
                             defualt='running',
                             selection=[
                                 ('error', 'Error'),
                                 ('success', 'Success'),
                             ])

    @api.model
    def create(self, values):
        seq_obj = self.env.ref('deploybot.ir_sequence_deploy_event')
        values['name'] = seq_obj.next_by_id()
        return super(DeployEvent, self).create(values)
