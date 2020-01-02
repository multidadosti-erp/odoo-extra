import datetime
import logging
import os
import re
import signal
import subprocess
import time

import dateutil.parser
import requests
import simplejson

from odoo import SUPERUSER_ID, api, fields, models
from odoo.modules import get_module_resource
from odoo.modules.module import get_resource_path
from odoo.tools import config

from .runbot import fqdn, run, dt2time, decode_utf, mkdirs

_logger = logging.getLogger(__name__)


class RunbotRepo(models.Model):
    _name = "runbot.repo"
    _description = 'Runbot Repo'
    _order = 'sequence, name, id'

    MODULES_AUTO = [('none', 'None (only explicit modules list)'),
                    ('repo', 'Repository modules (excluding dependencies)'),
                    ('all', 'All modules (including dependencies)')]

    name = fields.Char('Repository', required=True)
    sequence = fields.Integer('Sequence', index=True)

    path = fields.Char(compute="_compute_directory_path",
                       string='Directory',
                       readonly=1)

    base = fields.Char(compute="_compute_base_url",
                       string='Base URL', readonly=1)

    nginx = fields.Boolean('Nginx')

    mode = fields.Selection([('disabled', 'Disabled'),
                             ('poll', 'Poll'),
                             ('hook', 'Hook')],
                            string="Mode",
                            default="poll",
                            required=True,
                            help="""hook: Wait for webhook on
                             /runbot/hook/<id> i.e. github push event""")

    hook_time = fields.Datetime('Last hook time')

    duplicate_id = fields.Many2one('runbot.repo',
                                   string='Duplicate repo',
                                   help="""Repository for finding
                                   duplicate builds""")

    modules = fields.Char(string="Modules to install",
                          help="""Comma-separated list of modules
                          to install and test.""")

    modules_auto = fields.Selection(MODULES_AUTO,
                                    string="Other modules to install",
                                    default="repo")

    dependency_ids = fields.Many2many('runbot.repo',
                                      'runbot_repo_dep_rel',
                                      'dependant_id',
                                      'dependency_id',
                                      string='Extra dependencies',
                                      help="""Community addon repos which need
                                      to be present to run tests.""")

    token = fields.Char("Github token")
    group_ids = fields.Many2many('res.groups', string='Limited to groups')

    @api.depends('name')
    def _compute_directory_path(self):
        root = self._root()
        for repo in self:
            name = repo.name
            for i in '@:/':
                name = name.replace(i, '_')
            repo.path = os.path.join(root, 'repo', name)

    @api.depends('name')
    def _compute_base_url(self):
        for repo in self:
            name = re.sub('.+@', '', repo.name)
            name = re.sub('.git$', '', name)
            name = name.replace(':', '/')
            repo.base = name

    @api.model
    def _domain(self):
        conf_obj = self.env['ir.config_parameter'].sudo()
        return conf_obj.get_param('runbot.domain', fqdn())

    @api.model
    def _root(self):
        """Return root directory of repository"""
        default = get_resource_path('runbot', 'static')
        return self.env['ir.config_parameter'].sudo().get_param('runbot.root',
                                                                default)

    @api.multi
    def _git(self, cmd):
        """Execute git command cmd"""
        for repo in self:
            cmd = ['git', '--git-dir=%s' % repo.path] + cmd
            _logger.info("git: %s", ' '.join(cmd))
            return subprocess.check_output(cmd)

    @api.multi
    def _git_export(self, treeish, dest):
        for repo in self:
            _logger.debug('checkout %s %s %s', repo.name, treeish, dest)

            proc = [
                'git',
                '--git-dir=%s' % repo.path,
                'archive',
                treeish,
            ]

            p1 = subprocess.Popen(proc, stdout=subprocess.PIPE)

            proc = [
                'tar',
                '-xmC',
                dest,
            ]

            p2 = subprocess.Popen(proc,
                                  stdin=p1.stdout,
                                  stdout=subprocess.PIPE)
            p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
            p2.communicate()[0]

    @api.multi
    def _github(self, url, payload=None, ignore_errors=False):
        """Return a http request to be sent to github"""
        for repo in self:
            if not repo.token:
                return
            try:
                match_object = re.search(
                    r'([^/]+)/([^/]+)/([^/.]+(.git)?)', repo.base)

                if match_object:
                    url = url.replace(':owner', match_object.group(2))
                    url = url.replace(':repo', match_object.group(3))
                    url = 'https://api.%s%s' % (match_object.group(1), url)
                    session = requests.Session()
                    session.auth = (repo.token, 'x-oauth-basic')
                    session.headers.update(
                        {
                            'Accept': 'application/vnd.github.she-hulk-preview+json'  # noqa: E501
                        })
                    if payload:
                        response = session.post(
                            url, data=simplejson.dumps(payload))
                    else:
                        response = session.get(url)
                    response.raise_for_status()
                    return response.json()
            except Exception:
                if ignore_errors:
                    _logger.exception(
                        'Ignored github error %s %r', url, payload)
                else:
                    raise

    @api.multi
    def _update(self):
        for repo in self:
            repo._update_git()

    @api.multi
    def _update_git(self):
        self.ensure_one()
        _logger.debug('repo %s updating branches', self.name)

        Build = self.env['runbot.build']
        Branch = self.env['runbot.branch']

        if not os.path.isdir(os.path.join(self.path)):
            os.makedirs(self.path)

        if not os.path.isdir(os.path.join(self.path, 'refs')):
            run(['git', 'clone', '--bare', self.name, self.path])

        # check for mode == hook
        fname_fetch_head = os.path.join(self.path, 'FETCH_HEAD')
        if os.path.isfile(fname_fetch_head):
            fetch_time = os.path.getmtime(fname_fetch_head)
            if (self.mode == 'hook' and self.hook_time
                    and dt2time(self.hook_time) < fetch_time):

                t0 = time.time()
                _logger.debug('repo %s skip hook fetch fetch_time: %ss ago hook_time: %ss ago',  # noqa: E501
                              self.name,
                              int(t0 - fetch_time),
                              int(t0 - dt2time(self.hook_time)))
                return

        self._git(['gc', '--auto', '--prune=all'])
        self._git(['fetch', '-p', 'origin', '+refs/heads/*:refs/heads/*'])
        self._git(['fetch', '-p', 'origin', '+refs/pull/*/head:refs/pull/*'])

        fields = [
            'refname',
            'objectname',
            'committerdate:iso8601',
            'authorname',
            'authoremail',
            'subject',
            'committername',
            'committeremail',
        ]

        fmt = "%00".join(["%(" + field + ")" for field in fields])

        refs = [
            'for-each-ref',
            '--format',
            fmt,
            '--sort=-committerdate',
            'refs/heads',
            'refs/pull',
        ]
        git_refs = self._git(refs)
        git_refs = git_refs.strip()

        refs = [[decode_utf(field) for field in line.split(b'\x00')]
                for line in git_refs.split(b'\n')]

        self.env.cr.execute("""
            WITH t (branch) AS (SELECT unnest(%s))
          SELECT t.branch, b.id
            FROM t LEFT JOIN runbot_branch b ON (b.name = t.branch)
           WHERE b.repo_id = %s;
        """, ([r[0] for r in refs], self.id))

        ref_branches = {r[0]: r[1] for r in self.env.cr.fetchall()}

        for name, sha, date, author, author_email, subject, committer, committer_email in refs:  # noqa: E501

            # create or get branch
            if ref_branches.get(name):
                branch = Branch.browse(ref_branches[name])
            else:
                _logger.debug('repo %s found new branch %s', self.name, name)
                branch = Branch.create({'repo_id': self.id, 'name': name})

            # skip build for old branches
            if dateutil.parser.parse(date[:19]) + datetime.timedelta(30) < datetime.datetime.now():  # noqa: E501
                continue

            # create build (and mark previous builds as skipped) if not found
            build = Build.search(
                [('branch_id', '=', branch.id), ('name', '=', sha)])

            if not build:
                _logger.debug('repo %s branch %s new build found revno %s',
                              branch.repo_id.name, branch.name, sha)

                build_info = {
                    'branch_id': branch.id,
                    'name': sha,
                    'author': author,
                    'author_email': author_email,
                    'committer': committer,
                    'committer_email': committer_email,
                    'subject': subject,
                    'date': dateutil.parser.parse(date[:19]),
                }

                if not branch.sticky:
                    builds_to_be_skip = Build.search([
                        ('branch_id', '=', branch.id),
                        ('state', '=', 'pending'),
                    ], order='sequence asc')

                    if builds_to_be_skip:
                        builds_to_be_skip._skip()
                        # new order keeps lowest skipped sequence
                        build_info['sequence'] = builds_to_be_skip[0].sequence

                Build.create(build_info)

        # skip old builds (if their sequence number is too low,
        # they will not ever be built)
        skippable_domain = [('repo_id', '=', self.id),
                            ('state', '=', 'pending')]

        get_param = self.env['ir.config_parameter'].get_param

        running_max = int(get_param('runbot.running_max', default=75))

        buils_to_be_skip = Build.search(
            skippable_domain, order='sequence desc', offset=running_max)

        buils_to_be_skip._skip()

    @api.multi
    def _scheduler(self):
        get_param = self.env['ir.config_parameter'].get_param
        workers = int(get_param('runbot.workers', default=6))
        running_max = int(get_param('runbot.running_max', default=75))
        host = fqdn()

        Build = self.env['runbot.build']
        domain = [('repo_id', 'in', self.ids)]
        domain_host = domain + [('host', '=', host)]

        # schedule jobs (transitions testing -> running, kill jobs, ...)
        states = [
            'testing',
            'running',
            'deathrow',
        ]

        build_to_schedule = Build.search(
            domain_host + [('state', 'in', states)])
        build_to_schedule._schedule()

        # launch new tests
        testing = Build.search_count(domain_host + [('state', '=', 'testing')])
        pending = Build.search_count(domain + [('state', '=', 'pending')])

        while testing < workers and pending > 0:

            # find sticky pending build if any, otherwise, last
            # pending (by id, not by sequence) will do the job
            pending_build = Build.search(
                domain + [('state', '=', 'pending'),
                          ('branch_id.sticky', '=', True)], limit=1)

            if not pending_build:
                pending_build = Build.search(
                    domain + [('state', '=', 'pending')], order="sequence",
                    limit=1)

            pending_build._schedule()

            # compute the number of testing and pending jobs again
            testing = Build.search_count(
                domain_host + [('state', '=', 'testing')])

            pending = Build.search_count(domain + [('state', '=', 'pending')])

        # terminate and reap doomed build
        builds = Build.search(domain_host + [('state', '=', 'running')])
        builds.sorted(lambda build: build.branch_id.sticky, reverse=True)
        # terminate extra running builds

        # sort builds: the last build of each sticky branch then the rest
        sticky = {}
        non_sticky = []

        for build in builds:
            if build.branch_id.sticky and build.branch_id.id not in sticky:
                sticky[build.branch_id.id] = build.id
            else:
                non_sticky.append(build.id)

        build_ids = list(sticky.values())
        build_ids += non_sticky

        # terminate extra running builds
        build = Build.browse(build_ids[running_max:])
        build._kill()
        build._reap()

    @api.model
    def _reload_nginx(self):

        builds = self.env['runbot.build'].search([
            ('repo_id', 'in', self.ids),
            ('state', '=', 'running'),
        ])

        settings = {
            'runbot_static': os.path.join(get_module_resource('runbot', 'static'), ''),
            'port': config['http_port'],
            'nginx_dir': os.path.join(self._root(), 'nginx'),
            're_escape': re.escape,
            'builds': builds,
        }

        repo = self.search([('nginx', '=', True)], order='id')

        if repo:

            nginx_config = self.env['ir.ui.view'].render_template(
                "runbot.nginx_config", settings)

            mkdirs([settings['nginx_dir']])

            open(os.path.join(settings['nginx_dir'], 'nginx.conf'), 'w').write(nginx_config)  # noqa: E501

            try:
                _logger.debug('reload nginx')
                nginx_f = os.path.join(settings['nginx_dir'], 'nginx.pid')
                pid = int(open(nginx_f).read().strip(' \n'))
                os.kill(pid, signal.SIGHUP)

            except Exception:
                _logger.debug('start nginx')
                if run(['/usr/sbin/nginx', '-p', settings['nginx_dir'], '-c', 'nginx.conf']):  # noqa: E501
                    # obscure nginx bug leaving orphan worker
                    # listening on nginx port
                    if not run(['pkill', '-f', '-P1', 'nginx: worker']):
                        _logger.debug(
                            """failed to start nginx - orphan
                            worker killed, retrying""")

                        run(['/usr/sbin/nginx',
                             '-p',
                             settings['nginx_dir'],
                             '-c', 'nginx.conf'])
                    else:
                        _logger.debug(
                            """failed to start nginx - failed to kill orphan
                            worker - oh well""")

    @api.multi
    def killall(self):
        return

    @api.multi
    def _cron(self):
        repo = self.search([('mode', '!=', 'disabled')])
        repo._update()
        repo._scheduler()
        repo._reload_nginx()

    # backwards compatibility
    @api.multi
    def cron(self):
        if self._uid == SUPERUSER_ID:
            return self._cron()
