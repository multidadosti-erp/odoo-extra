import logging
import re
import subprocess

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_re_coverage = re.compile(r'\bcoverage\b')


class RunbotBranch(models.Model):
    _name = "runbot.branch"
    _order = 'name'

    repo_id = fields.Many2one('runbot.repo',
                              string='Repository',
                              required=True,
                              ondelete='cascade',
                              index=True)

    name = fields.Char(string='Ref Name', required=True)

    branch_name = fields.Char(compute="_compute_branch_name",
                              string='Branch',
                              readonly=1,
                              store=True)

    branch_url = fields.Char(compute="_compute_branch_url",
                             string='Branch url',
                             readonly=1)

    pull_head_name = fields.Char(compute="_compute_pull_head_name",
                                 string='PR HEAD name',
                                 readonly=1,
                                 store=True)

    sticky = fields.Boolean('Sticky', index=True)

    coverage = fields.Boolean('Coverage')

    state = fields.Char('Status')

    modules = fields.Char(string="Modules to Install",
                          help="""Comma-separated list of modules
                          to install and test.""")

    job_timeout = fields.Integer(
        'Job Timeout (minutes)', help='For default timeout: Mark it zero')

    @api.depends('name')
    def _compute_branch_name(self):
        for branch in self:
            branch.branch_name = branch.name and branch.name.split('/')[-1] or ''

    @api.depends('repo_id', 'branch_name')
    def _compute_branch_url(self):
        for branch in self:
            if re.match('^[0-9]+$', branch.branch_name):
                branch.branch_url = "https://%s/pull/%s" % (
                    branch.repo_id.base, branch.branch_name)
            else:
                branch.branch_url = "https://%s/tree/%s" % (
                    branch.repo_id.base, branch.branch_name)

    @api.depends('name')
    def _compute_pull_head_name(self):
        for branch in self:
            pi = branch.sudo()._get_pull_info()
            if pi:
                branch.pull_head_name = pi['head']['ref']
            else:
                branch.pull_head_name = False

    @api.multi
    def _get_branch_quickconnect_url(self, fqdn, dest):
        r = {}
        for branch in self:
            if branch.branch_name.startswith('7'):
                r[branch.id] = "http://%s/login?db=%s-all&login=admin&key=admin" % (fqdn, dest)  # noqa: E501
            elif branch.name.startswith('8'):
                r[branch.id] = "http://%s/login?db=%s-all&login=admin&key=admin&redirect=/web?debug=1" % (fqdn, dest)  # noqa: E501
            else:
                r[branch.id] = "http://%s/web/login?db=%s-all&login=admin&redirect=/web?debug=1" % (fqdn, dest)  # noqa: E501
        return r

    @api.multi
    def _get_pull_info(self):
        self.ensure_one()
        repo = self.repo_id
        if repo.token and self.name.startswith('refs/pull/'):
            pull_number = self.name[len('refs/pull/'):]
            repo_url = '/repos/:owner/:repo/pulls/%s' % pull_number
            return repo._github(repo_url, ignore_errors=True) or {}
        return {}

    @api.multi
    def _is_on_remote(self):
        # check that a branch still exists on remote
        self.ensure_one()
        repo = self.repo_id
        try:
            repo._git(['ls-remote', '-q', '--exit-code', repo.name, self.name])
        except subprocess.CalledProcessError:
            return False
        return True

    @api.model
    def create(self, values):
        values.setdefault('coverage', _re_coverage.search(
            values.get('name') or '') is not None)
        return super(RunbotBranch, self).create(values)
