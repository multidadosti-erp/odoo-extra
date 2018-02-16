import contextlib
import logging
import os
import re
import signal
import socket
import time
from collections import OrderedDict

from dateutil.relativedelta import relativedelta

import odoo
import psycopg2
from odoo import fields

_logger = logging.getLogger(__name__)

# increase cron frequency from 0.016 Hz to 0.1 Hz to reduce starvation and
# improve throughput with many workers
# TODO: find a nicer way than monkey patch to accomplish this
odoo.service.server.SLEEP_INTERVAL = 10
odoo.addons.base.ir.ir_cron._intervalTypes['minutes'] = (lambda interval: relativedelta(seconds=interval * 10))

# ----------------------------------------------------------
# RunBot helpers
# ----------------------------------------------------------


def log(*l, **kw):
    out = [i if isinstance(i, str) else repr(i) for i in l] + \
          ["%s=%r" % (k, v) for k, v in list(kw.items())]
    _logger.debug(' '.join(out))


def dashes(string):
    """Sanitize the input string"""
    for i in '~":\'':
        string = string.replace(i, "")
    for i in '/_. ':
        string = string.replace(i, "-")
    return string


def mkdirs(dirs):
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d)


def grep(filename, string):
    if os.path.isfile(filename):
        return open(filename).read().find(string) != -1
    return False


def rfind(filename, pattern):
    """Determine in something in filename matches the pattern"""
    if os.path.isfile(filename):
        regexp = re.compile(pattern, re.M)
        with open(filename, 'r') as f:
            if regexp.findall(f.read()):
                return True
    return False


def nowait():
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)


def run(l, env=None):
    """Run a command described by l in environment env"""
    log("run", l)
    env = dict(os.environ, **env) if env else None
    if isinstance(l, list):
        if env:
            rc = os.spawnvpe(os.P_WAIT, l[0], l, env)
        else:
            rc = os.spawnvp(os.P_WAIT, l[0], l)
    elif isinstance(l, str):
        tmp = ['sh', '-c', l]
        if env:
            rc = os.spawnvpe(os.P_WAIT, tmp[0], tmp, env)
        else:
            rc = os.spawnvp(os.P_WAIT, tmp[0], tmp)
    log("run", rc=rc)
    return rc


def now():
    return fields.Datetime.now()


def dt2time(datetime):
    """Convert datetime to time"""
    return time.mktime(time.strptime(
        datetime, odoo.tools.DEFAULT_SERVER_DATETIME_FORMAT))


def decode_utf(field):
    try:
        return field.decode('utf-8')
    except UnicodeDecodeError:
        return ''


def uniq_list(l):
    return list(OrderedDict.fromkeys(l).keys())


def fqdn():
    # return 'localhost'
    return socket.getfqdn()


@contextlib.contextmanager
def local_pgadmin_cursor():
    cnx = None
    try:
        cnx = psycopg2.connect("dbname=postgres")
        cnx.autocommit = True  # required for admin commands
        yield cnx.cursor()
    finally:
        if cnx:
            cnx.close()
