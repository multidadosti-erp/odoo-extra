from unittest import mock

from odoo.tests.common import TransactionCase


class TestDeployTask(TransactionCase):

    def setUp(self):
        super(TestDeployTask, self).setUp()

        instance = {
            'name': 'Test Instance',
            'host': 'multierp.multidadosti.com.br',
            'port': 22,
        }

        recipe_1 = {
            'name': 'Test Recipe',
            'code': 'import this'
        }

        recipe_2 = {
            'name': 'Test Recipe',
            'code': 'import this'
        }

        vals = {
            'name': 'Test Task',
            'instance_ids': [(0, 0, instance)],
            'recipe_ids': [(0, 0, recipe_1), (0, 0, recipe_2)],
        }

        self.task = self.env['deploy.task'].create(vals)

    @mock.patch('subprocess.check_output')
    def test_action_start_deploy(self, mk):

        mk.return_value = 'command output'

        self.task.action_start_deploy()

        events = self.env['deploy.event'].search([('task_id', '=', self.task.id)])

        self.assertEqual(len(events), len(self.task.recipe_ids))

        for event in events:
            self.assertIn(event.instance_id, self.task.instance_ids)
            self.assertIn(event.recipe_id, self.task.recipe_ids)
