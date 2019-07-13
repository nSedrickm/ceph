from mgr_module import MgrModule
import datetime
import errno
import json
import six
from collections import defaultdict
from prettytable import PrettyTable
from threading import Event


DATEFMT = '%Y-%m-%d %H:%M:%S.%f'


class Module(MgrModule):
    MODULE_OPTIONS = [
    ]

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        self.crashes = None
        self.run = True
        self.event = Event()

    def shutdown(self):
        self.run = False
        self.event.set()

    def serve(self):
        self.config_notify()
        while self.run:
            self.event.wait(self.warn_recent_interval / 100)
            self.event.clear()

    def config_notify(self):
        for opt in self.MODULE_OPTIONS:
            setattr(self,
                    opt['name'],
                    self.get_module_option(opt['name']) or opt['default'])
            self.log.debug(' mgr option %s = %s',
                           opt['name'], getattr(self, opt['name']))

    def _load_crashes(self):
        raw = self.get_store_prefix('crash/')
        self.crashes = {k[6:]: json.loads(m) for (k, m) in raw.items()}

    def handle_command(self, inbuf, command):
        if not self.crashes:
            self._load_crashes()
        for cmd in self.COMMANDS:
            if cmd['cmd'].startswith(command['prefix']):
                handler = cmd['handler']
                break
        if handler is None:
            return errno.EINVAL, '', 'unknown command %s' % command['prefix']

        return handler(self, command, inbuf)

    @staticmethod
    def validate_crash_metadata(inbuf):
        # raise any exceptions to caller
        metadata = json.loads(inbuf)
        if 'crash_id' not in metadata:
            raise AttributeError("missing 'crash_id' field")
        return metadata

    @staticmethod
    def time_from_string(timestr):
        # drop the 'Z' timezone indication, it's always UTC
        timestr = timestr.rstrip('Z')
        return datetime.datetime.strptime(timestr, DATEFMT)

    def timestamp_filter(self, f):
        """
        Filter crash reports by timestamp.

        :param f: f(time) return true to keep crash report
        :returns: crash reports for which f(time) returns true
        """
        def inner(pair):
            _, crash = pair
            time = self.time_from_string(crash["timestamp"])
            return f(time)
        return filter(inner, self.crashes.items())

    # command handlers

    def do_info(self, cmd, inbuf):
        crashid = cmd['id']
        crash = self.crashes.get(crashid)
        if not crash:
            return errno.EINVAL, '', 'crash info: %s not found' % crashid
        val = json.dumps(crash, indent=4)
        return 0, val, ''

    def do_post(self, cmd, inbuf):
        try:
            metadata = self.validate_crash_metadata(inbuf)
        except Exception as e:
            return errno.EINVAL, '', 'malformed crash metadata: %s' % e
        crashid = metadata['crash_id']

        if crashid not in self.crashes:
            self.crashes[crashid] = metadata
            key = 'crash/%s' % crashid
            self.set_store(key, json.dumps(metadata))
        return 0, '', ''

    def ls(self):
        if not self.crashes:
            self._load_crashes()
        return self.do_ls({'prefix': 'crash ls'}, '')

    def do_ls(self, cmd, inbuf):
        r = self.crashes.values()
        if cmd.get('format') == 'json' or cmd.get('format') == 'json-pretty':
            return 0, json.dumps(r, indent=4), ''
        else:
            table = PrettyTable(['ID', 'ENTITY', 'NEW'],
                                border=False)
            table.left_padding_width = 0
            table.right_padding_width = 1
            table.align['ID'] = 'l'
            table.align['ENTITY'] = 'l'
            for c in r:
                table.add_row([c.get('crash_id'),
                               c.get('entity_name','unknown'),
                               '' if 'archived' in c else '*'])
            return 0, table.get_string(), ''

    def do_rm(self, cmd, inbuf):
        crashid = cmd['id']
        if crashid in self.crashes:
            del self.crashes[crashid]
            key = 'crash/%s' % crashid
            self.set_store(key, None)       # removes key
        return 0, '', ''

    def do_prune(self, cmd, inbuf):
        keep = cmd['keep']
        try:
            keep = int(keep)
        except ValueError:
            return errno.EINVAL, '', 'keep argument must be integer'

        self._prune(keep * 60*60*24)
        return 0, '', ''

    def _prune(self, seconds):
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(seconds=seconds)
        removed_any = False
        # make a copy of the list, since we'll modify self.crashes below
        to_prune = list(self.timestamp_filter(lambda ts: ts <= cutoff))
        for crashid, crash in to_prune:
            del self.crashes[crashid]
            key = 'crash/%s' % crashid
            self.set_store(key, None)

    def do_archive(self, cmd, inbuf):
        crashid = cmd['id']
        crash = self.crashes.get(crashid)
        if not crash:
            return errno.EINVAL, '', 'crash info: %s not found' % crashid
        if not crash.get('archived'):
            crash['archived'] = str(datetime.datetime.utcnow())
            self.crashes[crashid] = crash
            key = 'crash/%s' % crashid
            self.set_store(key, json.dumps(crash))
        return 0, '', ''

    def do_archive_all(self, cmd, inbuf):
        for crashid, crash in self.crashes.items():
            if not crash.get('archived'):
                crash['archived'] = str(datetime.datetime.utcnow())
                self.crashes[crashid] = crash
                key = 'crash/%s' % crashid
                self.set_store(key, json.dumps(crash))
        return 0, '', ''

    def do_stat(self, cmd, inbuf):
        # age in days for reporting, ordered smallest first
        bins = [1, 3, 7]
        retlines = list()

        def binstr(bindict):
            binlines = list()
            count = len(bindict['idlist'])
            if count:
                binlines.append(
                    '%d older than %s days old:' % (count, bindict['age'])
                )
                for crashid in bindict['idlist']:
                    binlines.append(crashid)
            return '\n'.join(binlines)

        total = 0
        now = datetime.datetime.utcnow()
        for i, age in enumerate(bins):
            agelimit = now - datetime.timedelta(days=age)
            bins[i] = {
                'age': age,
                'agelimit': agelimit,
                'idlist': list()
            }

        for crashid, crash in self.crashes.items():
            total += 1
            stamp = self.time_from_string(crash['timestamp'])
            for i, bindict in enumerate(bins):
                if stamp <= bindict['agelimit']:
                    bindict['idlist'].append(crashid)
                    # don't count this one again
                    continue

        retlines.append('%d crashes recorded' % total)

        for bindict in bins:
            retlines.append(binstr(bindict))
        return 0, '\n'.join(retlines), ''

    def do_json_report(self, cmd, inbuf):
        """
        Return a machine readable summary of recent crashes.
        """
        try:
            hours = int(cmd['hours'])
        except ValueError:
            return errno.EINVAL, '', '<hours> argument must be integer'

        report = defaultdict(lambda: 0)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        for crashid, crash in self.crashes.items():
            pname = crash.get("process_name", "unknown")
            if not pname:
                pname = "unknown"
            report[pname] += 1

        return 0, '', json.dumps(report)

    def self_test(self):
        # test time conversion
        timestr = '2018-06-22 20:35:38.058818Z'
        dt = self.time_from_string(timestr)
        if dt != datetime.datetime(2018, 6, 22, 20, 35, 38, 58818):
            raise RuntimeError('time_from_string() failed')

    COMMANDS = [
        {
            'cmd': 'crash info name=id,type=CephString',
            'desc': 'show crash dump metadata',
            'perm': 'r',
            'handler': do_info,
        },
        {
            'cmd': 'crash ls',
            'desc': 'Show saved crash dumps',
            'perm': 'r',
            'handler': do_ls,
        },
        {
            'cmd': 'crash post',
            'desc': 'Add a crash dump (use -i <jsonfile>)',
            'perm': 'rw',
            'handler': do_post,
        },
        {
            'cmd': 'crash prune name=keep,type=CephString',
            'desc': 'Remove crashes older than <keep> days',
            'perm': 'rw',
            'handler': do_prune,
        },
        {
            'cmd': 'crash rm name=id,type=CephString',
            'desc': 'Remove a saved crash <id>',
            'perm': 'rw',
            'handler': do_rm,
        },
        {
            'cmd': 'crash stat',
            'desc': 'Summarize recorded crashes',
            'perm': 'r',
            'handler': do_stat,
        },
        {
            'cmd': 'crash json_report name=hours,type=CephString',
            'desc': 'Crashes in the last <hours> hours',
            'perm': 'r',
            'handler': do_json_report,
        },
        {
            'cmd': 'crash archive name=id,type=CephString',
            'desc': 'Acknowledge a crash and silence health warning(s)',
            'perm': 'w',
            'handler': do_archive,
        },
        {
            'cmd': 'crash archive-all',
            'desc': 'Acknowledge all new crashes and silence health warning(s)',
            'perm': 'w',
            'handler': do_archive_all,
        },
    ]
