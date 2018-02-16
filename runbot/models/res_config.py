from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    workers = fields.Integer('Total Number of Workers')
    running_max = fields.Integer('Maximum Number of Running Builds')
    timeout = fields.Integer('Default Timeout (in seconds)')
    starting_port = fields.Integer('Starting Port for Running Builds')
    domain = fields.Char('Runbot Domain')

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()

        get_param = self.env['ir.config_parameter'].get_param

        workers = get_param('runbot.workers', default=6)
        running_max = get_param('runbot.running_max', default=75)
        timeout = get_param('runbot.timeout', default=1800)
        starting_port = get_param('runbot.starting_port', default=2000)
        runbot_domain = get_param('runbot.domain', default='runbot.odoo.com')

        res.update({
            'workers': int(workers),
            'running_max': int(running_max),
            'timeout': int(timeout),
            'starting_port': int(starting_port),
            'domain': runbot_domain,
        })

        return res

    @api.multi
    def set_values(self):
        super(ResConfigSettings, self).set_values()

        set_param = self.env['ir.config_parameter'].set_param

        for config in self:
            set_param('runbot.workers', repr(config.workers))
            set_param('runbot.running_max', repr(config.running_max))
            set_param('runbot.timeout', repr(config.timeout))
            set_param('runbot.starting_port', repr(config.starting_port))
            set_param('runbot.domain', config.domain)
