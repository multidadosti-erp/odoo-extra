import glob
import logging
import operator
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time

import psutil

import odoo
from odoo import api, fields, models
from odoo.tools import appdirs, config

from .runbot import (dashes,
                     dt2time,
                     fqdn,
                     grep,
                     local_pgadmin_cursor,
                     mkdirs,
                     now,
                     rfind,
                     run,
                     uniq_list)

_logger = logging.getLogger(__name__)

# ----------------------------------------------------------
# Runbot Const
# ----------------------------------------------------------

_re_error = r'^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ (?:ERROR|CRITICAL) )|(?:Traceback \(most recent call last\):)$'  # noqa: E501
_re_warning = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \d+ WARNING '
_re_job = re.compile(r'_job_\d')
_re_coverage = re.compile(r'\bcoverage\b')

# increase cron frequency from 0.016 Hz to 0.1 Hz to
# reduce starvation and improve throughput with many workers
# TODO: find a nicer way than monkey patch to accomplish this
# odoo.service.server.SLEEP_INTERVAL = 10
# odoo.addons.base.ir.ir_cron._intervalTypes['minutes'] = lambda interval: relativedelta(seconds=interval * 10)   # noqa: E501


class RunbotBuild(models.Model):
    _name = "runbot.build"
    _order = 'id desc'

    SERVER_MATCH_SELECT = [
        ('builtin', 'This branch includes Odoo server'),
        ('exact', 'branch/PR exact name'),
        ('prefix', 'branch whose name is a prefix of current one'),
        ('fuzzy', 'Fuzzy - common ancestor found'),
        ('default', 'No match found - defaults to master'),
    ]

    branch_id = fields.Many2one('runbot.branch',
                                string='Branch',
                                required=True,
                                ondelete='cascade',
                                index=True)

    repo_id = fields.Many2one(related="branch_id.repo_id",
                              string="Repository",
                              store=True,
                              readonly=True,
                              ondelete='cascade',
                              index=True)

    name = fields.Char(string='Revno', required=True, index=True)
    host = fields.Char()
    port = fields.Integer()
    dest = fields.Char(compute="_compute_dest", readonly=1, store=True)
    domain = fields.Char(compute="_compute_domain", string='URL')
    date = fields.Datetime(string='Commit date')
    author = fields.Char()
    author_email = fields.Char()
    committer = fields.Char()
    committer_email = fields.Char()
    subject = fields.Text()
    sequence = fields.Integer(index=True)
    modules = fields.Char(string="Modules to Install")

    # ok, ko, warn, skipped, killed, manually_killed
    result = fields.Char(default="")

    guess_result = fields.Char(compute="_compute_guess_result")
    pid = fields.Integer()

    # pending, testing, running, done, duplicate, deathrow
    state = fields.Char('Status', default="pending")
    job = fields.Char()  # job_*
    job_start = fields.Datetime()
    job_end = fields.Datetime()
    job_time = fields.Integer(compute="_compute_job_time")
    job_age = fields.Integer(compute="_compute_job_age")

    duplicate_id = fields.Many2one('runbot.build',
                                   string='Corresponding Build')

    server_match = fields.Selection(selection=SERVER_MATCH_SELECT,
                                    string='Server branch matching')

    @api.depends('branch_id', 'name')
    def _compute_dest(self):
        for build in self:
            nickname = dashes(build.branch_id.name.split('/')[2])[:32]
            build.dest = "%05d-%s-%s" % (build.id, nickname, build.name[:6])

    @api.depends('repo_id', 'host', 'port')
    def _compute_domain(self):
        domain = self.env['runbot.repo']._domain()
        for build in self:
            if build.repo_id.nginx:
                build.domain = "%s.%s" % (build.dest, build.host)
            else:
                build.domain = "%s:%s" % (domain, build.port)

    @api.depends('job_start', 'job_end')
    def _compute_job_time(self):
        """Return the time taken by the tests"""
        for build in self:
            build.job_time = 0
            if build.job_end:
                build.job_time = int(
                    dt2time(build.job_end) - dt2time(build.job_start))
            elif build.job_start:
                build.job_time = int(time.time() - dt2time(build.job_start))

    @api.depends('job_start')
    def _compute_job_age(self):
        """Return the time between job start and now"""
        for build in self:
            build.job_age = 0
            if build.job_start:
                build.job_age = int(time.time() - dt2time(build.job_start))

    @api.multi
    @api.depends('state')
    def _compute_guess_result(self):
        self.env.cr.execute("""
            SELECT b.id,
                   CASE WHEN b.state != 'testing' THEN b.result
                        WHEN array_agg(l.level)::text[] && ARRAY['ERROR', 'CRITICAL'] THEN 'ko'
                        WHEN array_agg(l.level)::text[] && ARRAY['WARNING'] THEN 'warn'
                        ELSE 'ok'
                    END
              FROM runbot_build b
         LEFT JOIN ir_logging l ON (l.build_id = b.id AND l.level != 'INFO')
             WHERE b.id IN %s
          GROUP BY b.id
        """, [tuple(self.ids)])  # noqa: E501
        return self.env.cr.dictfetchall()

    @api.model
    def create(self, values):
        build = super(RunbotBuild, self).create(values)
        extra_info = {'sequence': build.id}

        # detect duplicate
        duplicate_build = None

        domain = [
            ('repo_id', '=', build.repo_id.duplicate_id.id),
            ('name', '=', build.name),
            ('duplicate_id', '=', False),
            '|', ('result', '=', False), ('result', '!=', 'skipped')
        ]
        duplicate_builds = self.search(domain)

        for duplicate in duplicate_builds:
            duplicate_build = duplicate
            # Consider the duplicate if its closest branches are the
            # same than the current build closest branches.
            for extra_repo in build.repo_id.dependency_ids:
                build_closest_name = build._get_closest_branch_name(
                    extra_repo.id)
                duplicate_closest_name = duplicate._get_closest_branch_name(
                    extra_repo.id)

                if build_closest_name != duplicate_closest_name:
                    duplicate_build = None

        if duplicate_build:
            extra_info.update({
                'state': 'duplicate',
                'duplicate_id': duplicate_build.id,
            })

            duplicate_build.write({'duplicate_id': build.id})

        build.write(extra_info)
        return build

    @api.multi
    def _reset(self):
        self.write({'state': 'pending'})

    @api.multi
    def _logger(self, *l, **kw):
        la = list(l)
        for build in self:
            la[0] = "%s %s" % (build.dest, la[0])
            _logger.debug(*la)

    def _list_jobs(self):
        return sorted(job[1:] for job in dir(self) if _re_job.match(job))

    @api.model
    def _find_port(self):
        # currently used port
        builds = self.search([('state', 'not in', ['pending', 'done'])])
        ports = builds.mapped('port')

        # starting port
        get_param = self.env['ir.config_parameter'].get_param
        port = int(get_param('runbot.starting_port', default=2000))

        # find next free port
        while port in ports:
            port += 2

        return port

    @api.multi
    def _get_closest_branch_name(self, target_repo_id):
        """Return (repo, branch name) of the closest common branch
        between build's branch and any branch of target_repo or its
        duplicated repos.

        Rules priority for choosing the branch from the other repo is:
        1. Same branch name
        2. A PR whose head name match
        3. Match a branch which is the dashed-prefix of current branch name
        4. Common ancestors (git merge-base)
        Note that PR numbers are replaced by the branch name of the PR target
        to prevent the above rules to mistakenly link PR of different repos
        together.
        """
        self.ensure_one()
        Branch = self.env['runbot.branch']

        branch, repo = self.branch_id, self.repo_id
        pi = branch._get_pull_info()
        name = pi['base']['ref'] if pi else branch.branch_name

        target_repo = self.env['runbot.repo'].browse(target_repo_id)

        target_repo_ids = [target_repo.id]
        r = target_repo.duplicate_id
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        _logger.debug('Search closest of %s (%s) in repos %r',
                      name, repo.name, target_repo_ids)

        def sort_by_repo(d):
            return (not d['sticky'],
                    target_repo_ids.index(d['repo_id'][0]),
                    -1 * len(d.get('branch_name', '')),
                    -1 * d['id'])

        def result_for(d, match='exact'):
            return (d['repo_id'][0], d['name'], match)

        def branch_exists(d):
            return Branch.browse([d['id']])._is_on_remote()

        fields = [
            'name',
            'repo_id',
            'sticky',
        ]

        # 1. same name, not a PR
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('branch_name', '=', name),
            ('name', '=like', 'refs/heads/%'),
        ]

        targets = Branch.search_read(domain, fields, order='id DESC')
        targets = sorted(targets, key=sort_by_repo)

        if targets and branch_exists(targets[0]):
            return result_for(targets[0])

        # 2. PR with head name equals
        domain = [
            ('repo_id', 'in', target_repo_ids),
            ('pull_head_name', '=', name),
            ('name', '=like', 'refs/pull/%'),
        ]

        pulls = Branch.search_read(domain, fields, order='id DESC')
        pulls = sorted(pulls, key=sort_by_repo)

        for pull in pulls:
            pi = Branch.browse([pull['id']])._get_pull_info()

            if pi.get('state') == 'open':
                return result_for(pull)

        # 3. Match a branch which is the dashed-prefix of current branch name
        branches = Branch.search_read(
            [('repo_id', 'in', target_repo_ids),
             ('name', '=like', 'refs/heads/%')],
            fields + ['branch_name'], order='id DESC')

        branches = sorted(branches, key=sort_by_repo)

        for branch in branches:
            if (name.startswith(branch['branch_name'] + '-')
                    and branch_exists(branch)):
                return result_for(branch, 'prefix')

        # 4. Common ancestors (git merge-base)
        for target_id in target_repo_ids:
            common_refs = {}

            self.env.cr.execute("""
                SELECT b.name
                  FROM runbot_branch b,
                       runbot_branch t
                 WHERE b.repo_id = %s
                   AND t.repo_id = %s
                   AND b.name = t.name
                   AND b.name LIKE 'refs/heads/%%'
            """, [repo.id, target_id])

            for common_name, in self.env.cr.fetchall():
                try:
                    commit = repo._git(
                        ['merge-base', branch['name'], common_name]).strip()
                    cmd = ['log', '-1', '--format=%cd', '--date=iso', commit]
                    common_refs[common_name] = repo._git(cmd).strip()
                except subprocess.CalledProcessError:
                    # If merge-base doesn't find any common ancestor,
                    # the command exits with a non-zero return code,
                    # resulting in subprocess.check_output raising this
                    # exception. We ignore this branch as there is no
                    # common ref between us.
                    continue
            if common_refs:
                b = sorted(iter(common_refs.items()),
                           key=operator.itemgetter(1), reverse=True)[0][0]
                return target_id, b, 'fuzzy'

        # 5. last-resort value
        return target_repo_id, 'master', 'default'

    @api.multi
    def _path(self, *l, **kw):
        self.ensure_one()
        root = self.env['runbot.repo']._root()
        return os.path.join(root, 'build', self.dest, *l)

    @api.multi
    def _server(self, *l, **kw):
        self.ensure_one()
        if os.path.exists(self._path('odoo')):
            return self._path('odoo', *l)
        return self._path('openerp', *l)

    @api.model
    def _filter_modules(self, modules, available_modules, explicit_modules):
        blacklist_modules = set([
            'auth_ldap',
            'document_ftp',
            'base_gengo',
            'website_gengo',
            'website_instantclick',
            'test_assetsbundle',
            'pad',
            'pad_project',
            'note_pad',
            'pos_cache',
            'pos_blackbox_be',
            'web_favicon',
            'test_pylint',
            'pi_open_in_new_tab',
            'web_responsive_multidados',
        ])

        def mod_filter(m): return (
            m in available_modules and
            (m in explicit_modules or (not m.startswith(
                ('hw_', 'theme_', 'l10n_')) and m not in blacklist_modules))
        )
        return uniq_list(list(filter(mod_filter, modules)))

    @api.multi
    def _checkout(self):
        for build in self:
            # starts from scratch
            if os.path.isdir(build._path()):
                shutil.rmtree(build._path())

            # runbot log path
            mkdirs([build._path("logs"), build._server('addons')])

            # checkout branch
            build.branch_id.repo_id._git_export(build.name, build._path())

            # v6 rename bin -> openerp
            if os.path.isdir(build._path('bin/addons')):
                shutil.move(build._path('bin'), build._server())

            has_server = os.path.isfile(build._server('__init__.py'))
            server_match = 'builtin'

            # build complete set of modules to install
            modules_to_move = []
            modules_to_test = ((build.branch_id.modules or '') + ',' +
                               (build.repo_id.modules or ''))

            modules_to_test = [_f for _f in modules_to_test.split(',') if _f]
            explicit_modules = set(modules_to_test)

            _logger.debug("manual modules_to_test for build %s: %s",
                          build.dest, modules_to_test)

            if not has_server:
                if build.repo_id.modules_auto == 'repo':
                    modules_to_test += [
                        os.path.basename(os.path.dirname(a))
                        for a in (glob.glob(build._path('*/__openerp__.py')) +
                                  glob.glob(build._path('*/__manifest__.py')))
                    ]
                    _logger.debug(
                        "local modules_to_test for build %s: %s", build.dest,
                        modules_to_test)

                for extra_repo in build.repo_id.dependency_ids:
                    repo_id, closest_name, server_match = \
                        build._get_closest_branch_name(extra_repo.id)

                    repo = self.env['runbot.repo'].browse(repo_id)

                    _logger.debug('branch %s of %s: %s match branch %s of %s',
                                  build.branch_id.name, build.repo_id.name,
                                  server_match, closest_name, repo.name)
                    build._log(
                        'Building environment',
                        '%s match branch %s of %s' % (
                            server_match, closest_name, repo.name)
                    )

                    repo._git_export(closest_name, build._path())

                # Finally mark all addons to move to openerp/addons
                modules_to_move += [
                    os.path.dirname(module)
                    for module in (glob.glob(build._path('*/__openerp__.py')) +
                                   glob.glob(build._path('*/__manifest__.py')))
                ]

            # move all addons to server addons path
            addons_list = glob.glob(build._path('addons/*')) + modules_to_move

            for module in uniq_list(addons_list):
                basename = os.path.basename(module)
                addon_path = build._server('addons', basename)

                if os.path.exists(addon_path):
                    build._log(
                        'Building environment',
                        'You have duplicate modules'
                        'in your branches "%s"' % basename
                    )
                    if (os.path.islink(addon_path) or
                            os.path.isfile(addon_path)):
                        os.remove(addon_path)
                    else:
                        shutil.rmtree(addon_path)
                shutil.move(module, build._server('addons'))

            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            if (build.repo_id.modules_auto == 'all'or
                    (build.repo_id.modules_auto != 'none' and has_server)):
                modules_to_test += available_modules

            modules_to_test = self._filter_modules(modules_to_test,
                                                   set(available_modules),
                                                   explicit_modules)

            _logger.debug("modules_to_test for build %s: %s",
                          build.dest, modules_to_test)

            build.write({'server_match': server_match,
                         'modules': ','.join(modules_to_test)})

    @api.model
    def _local_pg_dropdb(self, dbname):
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute('DROP DATABASE IF EXISTS "%s"' % dbname)
        # cleanup filestore
        datadir = appdirs.user_data_dir()
        paths = [os.path.join(datadir, pn, 'filestore', dbname)
                 for pn in 'OpenERP Odoo'.split()]
        run(['rm', '-rf'] + paths)

    @api.model
    def _local_pg_createdb(self, dbname):
        self._local_pg_dropdb(dbname)
        _logger.debug("createdb %s", dbname)
        with local_pgadmin_cursor() as local_cr:
            local_cr.execute(
                """CREATE DATABASE "%s" TEMPLATE template0 LC_COLLATE 'C' ENCODING 'unicode'""" % dbname)  # noqa: E501

    def _cmd(self):
        """Return a list describing the command to start the build"""
        for build in self:
            bins = [
                'odoo-bin',                 # >= 10.0
                'openerp-server',           # 9.0, 8.0
                'openerp-server.py',        # 7.0
                'bin/openerp-server.py',    # < 7.0
            ]

            for server_path in map(build._path, bins):
                if os.path.isfile(server_path):
                    break

            # commandline
            cmd = [
                sys.executable,
                build._path(server_path),
                "--http-port=%d" % build.port,
            ]
            # options
            if grep(build._server("tools/config.py"), "no-xmlrpcs"):
                cmd.append("--no-http")

            if grep(build._server("tools/config.py"), "no-netrpc"):
                cmd.append("--no-netrpc")

            if grep(build._server("tools/config.py"), "log-db"):
                logdb = self.env.cr.dbname

                if config['db_host'] and grep(build._server('sql_db.py'), 'allow_uri'):  # noqa: E501
                    logdb = 'postgres://{cfg[db_user]}:{cfg[db_password]}@{cfg[db_host]}/{db}'.format(  # noqa: E501
                        cfg=config, db=self.env.cr.dbname)

                cmd += ["--log-db=%s" % logdb]

                if grep(build._server('tools/config.py'), 'log-db-level'):
                    cmd += ["--log-db-level", '25']

            if grep(build._server("tools/config.py"), "data-dir"):
                datadir = build._path('datadir')

                if not os.path.exists(datadir):
                    os.mkdir(datadir)

                cmd += ["--data-dir", datadir]

        return cmd, build.modules

    def _spawn(self, cmd, log_path, cpu_limit=None, shell=False,
               env=None):

        def preexec_fn():
            os.setsid()
            if cpu_limit:
                # set soft cpulimit
                _, hard = resource.getrlimit(resource.RLIMIT_CPU)
                r = resource.getrusage(resource.RUSAGE_SELF)
                cpu_time = r.ru_utime + r.ru_stime
                resource.setrlimit(resource.RLIMIT_CPU,
                                   (cpu_time + cpu_limit, hard))
            # close parent files
            os.closerange(3, os.sysconf("SC_OPEN_MAX"))

        out = open(log_path, "w")
        _logger.debug("spawn: %s stdout: %s", ' '.join(cmd), log_path)

        p = subprocess.Popen(cmd,
                             stdout=out,
                             stderr=out,
                             preexec_fn=preexec_fn,
                             shell=shell,
                             env=env)
        return p.pid

    @api.multi
    def _github_status(self):
        """Notify github of failed/successful builds"""
        runbot_domain = self.env['runbot.repo']._domain()

        for build in self:
            desc = "runbot build %s" % (build.dest,)

            if build.state == 'testing':
                state = 'pending'

            elif build.state in ('running', 'done'):
                state = 'error'
                if build.result == 'ok':
                    state = 'success'

                if build.result == 'ko':
                    state = 'failure'

                desc += " (runtime %ss)" % (build.job_time,)
            else:
                continue

            status = {
                "state": state,
                "target_url": "http://%s/runbot/build/%s" % (runbot_domain,
                                                             build.id),
                "description": desc,
                "context": "ci/runbot"
            }

            _logger.debug("github updating status %s to %s", build.name, state)
            build.repo_id._github('/repos/:owner/:repo/statuses/%s' %
                                  build.name, status, ignore_errors=True)

    def _job_00_init(self, build, log_path):
        build._log('init', 'Init build environment')
        # notify pending build - avoid confusing users by saying nothing
        build._github_status()
        build._checkout()
        return -2

    def _job_10_test_base(self, build, log_path):
        build._log('test_base', 'Start test base module')
        # run base test
        self._local_pg_createdb("%s-base" % build.dest)
        cmd, _ = build._cmd()

        if grep(build._server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")

        cmd += [
            '-d',
            '%s-base' % build.dest,
            '-i',
            'base',
            '--stop-after-init',
            '--log-level=test',
            '--max-cron-threads=0',
        ]

        return self._spawn(cmd, log_path, cpu_limit=300)

    def _job_20_test_all(self, build, log_path):
        build._log('test_all', 'Start test all modules')
        self._local_pg_createdb("%s-all" % build.dest)
        cmd, mods = build._cmd()

        if grep(build._server("tools/config.py"), "test-enable"):
            cmd.append("--test-enable")

        cmd += [
            '-d',
            '%s-all' % build.dest,
            '-i',
            odoo.tools.ustr(mods),
            '--stop-after-init',
            '--log-level=test',
            '--max-cron-threads=0',
        ]

        env = None

        if build.branch_id.coverage:
            env = self._coverage_env(build)
            available_modules = [
                os.path.basename(os.path.dirname(a))
                for a in (glob.glob(build._server('addons/*/__openerp__.py')) +
                          glob.glob(build._server('addons/*/__manifest__.py')))
            ]
            bad_modules = set(available_modules) - set((mods or '').split(','))
            omit = [
                '--omit',
                ','.join(build._server('addons', m) for m in bad_modules)
            ] if bad_modules else []

            cmd = [
                'coverage',
                'run',
                '--branch',
                '--source',
                build._server(),
            ] + omit + cmd[:]

        # reset job_start to an accurate job_20 job_time
        build.write({
            'job_start': now(),
        })
        return self._spawn(cmd, log_path, cpu_limit=2100, env=env)

    def _coverage_env(self, build):
        return dict(os.environ, COVERAGE_FILE=build._path('.coverage'))

    def _job_21_coverage(self, build, log_path):
        if not build.branch_id.coverage:
            return -2

        cov_path = build._path('coverage')
        mkdirs([cov_path])
        cmd = ["coverage", "html", "-d", cov_path, "--ignore-errors"]
        return self._spawn(cmd, log_path, env=self._coverage_env(build))

    def _job_30_run(self, build, log_path):
        # adjust job_end to record an accurate job_20 job_time
        build._log('run', 'Start running build %s' % build.dest)
        log_all = build._path('logs', 'job_20_test_all.txt')
        log_time = time.localtime(os.path.getmtime(log_all))
        v = {
            'job_end': time.strftime(odoo.tools.DEFAULT_SERVER_DATETIME_FORMAT,
                                     log_time),
        }
        if grep(log_all, ".modules.loading: Modules loaded."):
            if rfind(log_all, _re_error):
                v['result'] = "ko"
            elif rfind(log_all, _re_warning):
                v['result'] = "warn"
            elif (not grep(build._server("test/common.py"), "post_install")
                  or grep(log_all, "Initiating shutdown.")):
                v['result'] = "ok"
        else:
            v['result'] = "ko"

        build.write(v)
        build._github_status()

        # run server
        cmd, _ = build._cmd()
        if os.path.exists(build._server('addons/im_livechat')):
            cmd += ["--workers", "2"]
            cmd += ["--longpolling-port", "%d" % (build.port + 1)]
            cmd += ["--max-cron-threads", "1"]
        else:
            # not sure, to avoid old server to check other dbs
            cmd += ["--max-cron-threads", "0"]

        cmd += ['-d', "%s-all" % build.dest]

        if grep(build._server("tools/config.py"), "db-filter"):
            if build.repo_id.nginx:
                cmd += ['--db-filter', '%d.*$']
            else:
                cmd += ['--db-filter', '%s.*$' % build.dest]

        return self._spawn(cmd, log_path, cpu_limit=None)

    @api.multi
    def _force(self):
        """Force a rebuild"""
        for build in self:
            domain = [('state', '=', 'pending')]
            pending_build = build.search(domain, order='id', limit=1)

            if not pending_build:
                pending_build = build.search([], order='id desc', limit=1)

            # Force it now
            rebuild = True
            if build.state == 'done' and build.result == 'skipped':
                values = {
                    'state': 'pending',
                    'sequence': pending_build.sequence,
                    'result': '',
                }

                build.sudo().write(values)

            # or duplicate it
            elif build.state in ['running', 'done', 'duplicate', 'deathrow']:
                new_build = {
                    'sequence': pending_build.sequence,
                    'branch_id': build.branch_id.id,
                    'name': build.name,
                    'author': build.author,
                    'author_email': build.author_email,
                    'committer': build.committer,
                    'committer_email': build.committer_email,
                    'subject': build.subject,
                    'modules': build.modules,
                }
                build = self.sudo().create(new_build)
            else:
                rebuild = False
            if rebuild:
                build._log('rebuild', 'Rebuild initiated by %s' %
                           self.env.user.name)
            return build.repo_id.id

    @api.multi
    def _schedule(self):
        jobs = self._list_jobs()

        get_param = self.env['ir.config_parameter'].get_param
        # For retro-compatibility, keep this parameter in seconds
        default_timeout = int(get_param('runbot.timeout', default=1800)) // 60

        for build in self:
            if build.state == 'deathrow':
                build._kill(result='manually_killed')
            elif build.state == 'pending':
                # allocate port and schedule first job
                port = self._find_port()

                build.write({
                    'host': fqdn(),
                    'port': port,
                    'state': 'testing',
                    'job': jobs[0],
                    'job_start': now(),
                    'job_end': False,
                })
            else:
                # check if current job is finished
                if build.pid and psutil.pid_exists(build.pid):

                    # kill if overpassed
                    timeout = (
                        build.branch_id.job_timeout or default_timeout) * 60

                    if build.job != jobs[-1] and build.job_time > timeout:
                        build._logger('%s time exceded (%ss)',
                                      build.job, build.job_time)
                        build.write({'job_end': now()})
                        build._kill(result='killed')

                    continue

                build._logger('%s finished', build.job)
                # schedule
                v = {}
                # testing -> running
                if build.job == jobs[-2]:
                    v['state'] = 'running'
                    v['job'] = jobs[-1]
                    v['job_end'] = now(),
                # running -> done
                elif build.job == jobs[-1]:
                    v['state'] = 'done'
                    v['job'] = ''
                # testing
                else:
                    v['job'] = jobs[jobs.index(build.job) + 1]
                build.write(v)
            build.refresh()

            # run job
            pid = None
            if build.state != 'done':
                build._logger('running %s', build.job)
                job_method = getattr(self, '_' + build.job)
                mkdirs([build._path('logs')])
                log_path = build._path('logs', '%s.txt' % build.job)

                try:
                    pid = job_method(build, log_path)
                    build.write({
                        'pid': pid,
                    })
                except Exception:
                    _logger.exception('%s failed running method %s',
                                      build.dest, build.job)
                    build._log(build.job,
                               "failed running job method, see runbot log")
                    build._kill(result='ko')
                    continue
            # needed to prevent losing pids if multiple jobs
            # are started and one them raise an exception
            self.env.cr.commit()

            if pid == -2:
                # no process to wait, directly call next job
                # FIXME find a better way that this recursive call
                build._schedule()

            # cleanup only needed if it was not killed
            if build.state == 'done':
                build._local_cleanup()

    @api.multi
    def _skip(self):
        self.write({'state': 'done', 'result': 'skipped'})
        to_unduplicate = self.search(
            [('id', 'in', self.ids), ('duplicate_id', '!=', False)])
        to_unduplicate._force()

    @api.multi
    def _local_cleanup(self):
        for build in self:
            # Cleanup the *local* cluster
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname LIKE %s
                """, [build.dest + '%'])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

        # cleanup: find any build older than 7 days.
        root = self.env['runbot.repo']._root()
        build_dir = os.path.join(root, 'build')
        builds = os.listdir(build_dir)
        self.env.cr.execute("""
            SELECT dest
              FROM runbot_build
             WHERE dest IN %s
               AND (state != 'done' OR job_end > (now() - interval '7 days'))
        """, [tuple(builds)])
        actives = set(b[0] for b in self.env.cr.fetchall())

        for b in builds:
            path = os.path.join(build_dir, b)
            if b not in actives and os.path.isdir(path):
                shutil.rmtree(path)

        # cleanup old unused databases
        self.env.cr.execute(
            "select id from runbot_build where state in ('testing', 'running')")  # noqa: E501

        db_ids = [id[0] for id in self.env.cr.fetchall()]

        if db_ids:
            with local_pgadmin_cursor() as local_cr:
                local_cr.execute("""
                    SELECT datname
                      FROM pg_database
                     WHERE pg_get_userbyid(datdba) = current_user
                       AND datname ~ '^[0-9]+-.*'
                       AND SUBSTRING(datname, '^([0-9]+)-.*')::int not in %s

                """, [tuple(db_ids)])
                to_delete = local_cr.fetchall()
            for db, in to_delete:
                self._local_pg_dropdb(db)

    def _kill(self, result=None):
        host = fqdn()
        for build in self:
            if build.host != host:
                continue
            build._log('kill', 'Kill build %s' % build.dest)
            if build.pid:
                build._logger('killing %s', build.pid)
                try:
                    os.killpg(build.pid, signal.SIGKILL)
                except OSError:
                    pass
            v = {'state': 'done', 'job': False}
            if result:
                v['result'] = result
            build.write(v)
            self.env.cr.commit()
            build._github_status()
            build._local_cleanup()

    def _ask_kill(self):
        for build in self:
            if build.state == 'pending':
                build._skip()
                build._log('_ask_kill',
                           'Skipping build %s, requested by %s (user #%s)' % (
                               build.dest, self.env.user.name, self.env.uid))

            elif build.state in ['testing', 'running']:
                build.write({'state': 'deathrow'})
                build._log('_ask_kill',
                           'Killing build %s, requested by %s (user #%s)' % (
                               build.dest, self.env.user.name, self.env.uid))

    def _reap(self):
        while True:
            try:
                pid, status, _ = os.wait3(os.WNOHANG)
            except OSError:
                break
            if pid == 0:
                break
            _logger.debug('reaping: pid: %s status: %s', pid, status)

    @api.multi
    def _log(self, func, message, context=None):
        self.ensure_one()
        _logger.debug("Build %s %s %s", self.id, func, message)
        self.env['ir.logging'].create({
            'build_id': self.id,
            'level': 'INFO',
            'type': 'runbot',
            'name': 'odoo.runbot',
            'message': message,
            'path': 'runbot',
            'func': func,
            'line': '0',
        })
