import logging
import subprocess
import tempfile

import jinja2

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DeployTask(models.Model):
    _name = 'deploy.task'
    _description = 'Deploy Task'

    name = fields.Char(string='Name', required=True)
    instance_ids = fields.Many2many('deploy.instance', string='Instances')
    recipe_ids = fields.Many2many('deploy.recipe', string='Recipes')

    state = fields.Selection(string='State', selection=[('running', 'Running'),
                                                        ('stopped', 'Stopped')])

    @api.multi
    def action_start_deploy(self):
        """Run deploy tasks.

        This method save the recipe code in disk and run it with fabric.
        """

        for task in self:

            # with open('logfile-deploy.log', 'wb', 0) as output:

            for instance in task.instance_ids.sorted(key='sequence'):

                for recipe in task.recipe_ids.sorted(key='sequence'):

                    vals = {
                        'task_id': task.id,
                        'instance_id': instance.id,
                        'recipe_id': recipe.id,
                    }

                    # Create deploy event
                    event = self.env['deploy.event'].create(vals)

                    # Render template to find code
                    code = jinja2.Template(recipe.code).render(
                        {'instance': instance})

                    with tempfile.NamedTemporaryFile(suffix='.py', delete=True, mode='w') as tmp:
                        # Write recipe code to temporary file
                        tmp.write(code)
                        tmp.flush()

                        # Build the command
                        cmd = [
                            'fab',
                            '--hosts',
                            instance.host,
                            '--user',
                            'michell',
                            '--port',
                            str(instance.port),
                            '--fabfile',
                            tmp.name,
                            'deploy',
                        ]

                        try:
                            # Run subprocess
                            # process = subprocess.run(cmd, check=True)
                            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, encoding='UTF-8')
                            event.state = 'success'
                            event.output = output

                        except subprocess.CalledProcessError as exc:
                            event.state = 'error'

                    event.finish_datetime = fields.Datetime.now()
