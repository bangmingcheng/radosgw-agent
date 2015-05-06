from BaseHTTPServer import BaseHTTPRequestHandler
import argparse
import contextlib
import logging
import logging.handlers
import os.path
import yaml
import sys
import json
import boto
import datetime
import time
import base64
import hmac
import sha
import urllib2
from urllib2 import URLError, HTTPError

from radosgw_agent import client
from radosgw_agent import sync
from radosgw_agent import config
from radosgw_agent import worker
from radosgw_agent.util import string
from radosgw_agent.exceptions import SkipShard, SyncError, SyncTimedOut, SyncFailed, NotFound, BucketEmpty
from radosgw_agent.constants import DEFAULT_TIME

log = logging.getLogger(__name__)


def check_positive_int(string):
    value = int(string)
    if value < 1:
        msg = '%r is not a positive integer' % string
        raise argparse.ArgumentTypeError(msg)
    return value


def check_endpoint(endpoint):
    try:
        return client.parse_endpoint(endpoint)
    except client.InvalidProtocol as e:
        raise argparse.ArgumentTypeError(str(e))
    except client.InvalidHost as e:
        raise argparse.ArgumentTypeError(str(e))


def parse_args():
    conf_parser = argparse.ArgumentParser(add_help=False)
    conf_parser.add_argument(
        '-c', '--conf',
        type=file,
        help='configuration file'
        )
    args, remaining = conf_parser.parse_known_args()
    log_dir = '/var/log/ceph/radosgw-agent/'
    log_file = 'radosgw-agent.log'
    if args.conf is not None:
        log_file = os.path.basename(args.conf.name)
    defaults = dict(
        sync_scope='incremental',
        log_lock_time=20,
        log_file=os.path.join(log_dir, log_file),
        )
    if args.conf is not None:
        with contextlib.closing(args.conf):
            config = yaml.safe_load_all(args.conf)
            for new in config:
                defaults.update(new)

    parser = argparse.ArgumentParser(
        parents=[conf_parser],
        description='Synchronize radosgw installations',
        )
    parser.set_defaults(**defaults)
    verbosity = parser.add_mutually_exclusive_group(required=False)
    verbosity.add_argument(
        '-v', '--verbose',
        action='store_true', dest='verbose',
        help='be more verbose',
        )
    verbosity.add_argument(
        '-q', '--quiet',
        action='store_true', dest='quiet',
        help='be less verbose',
        )
    parser.add_argument(
        '--src-access-key',
        required='src_access_key' not in defaults,
        help='access key for source zone system user',
        )
    parser.add_argument(
        '--src-secret-key',
        required='src_secret_key' not in defaults,
        help='secret key for source zone system user',
        )
    parser.add_argument(
        '--dest-access-key',
        required='dest_access_key' not in defaults,
        help='access key for destination zone system user',
        )
    parser.add_argument(
        '--dest-secret-key',
        required='dest_secret_key' not in defaults,
        help='secret key for destination zone system user',
        )
    parser.add_argument(
        '--destination',
        type=check_endpoint,
        required='destination' not in defaults,
        help='radosgw endpoint to which to sync '
        '(e.g. http://zone2.example.org:8080)',
        )
    src_options = parser.add_mutually_exclusive_group(required=False)
    src_options.add_argument(
        '--source',
        type=check_endpoint,
        help='radosgw endpoint from which to sync '
        '(e.g. http://zone1.example.org:8080)',
        )
    src_options.add_argument(
        '--src-zone',
        help='radosgw zone from which to sync',
        )
    parser.add_argument(
        '--metadata-only',
        action='store_true',
        help='sync bucket and user metadata, but not bucket contents',
        )
    parser.add_argument(
        '--num-workers',
        default=1,
        type=check_positive_int,
        help='number of items to sync at once',
        )
    parser.add_argument(
        '--sync-scope',
        choices=['full', 'incremental'],
        default='incremental',
        help='synchronize everything (for a new region) or only things that '
             'have changed since the last run',
        )
    parser.add_argument(
        '--lock-timeout',
        type=check_positive_int,
        default=60,
        help='timeout in seconds after which a log segment lock will expire if '
             'not refreshed',
        )
    parser.add_argument(
        '--log-file',
        help='where to store log output',
        )
    parser.add_argument(
        '--max-entries',
        type=check_positive_int,
        default=1000,
        help='maximum number of log entries to process at once during '
        'continuous sync',
        )
    parser.add_argument(
        '--incremental-sync-delay',
        type=check_positive_int,
        default=30,
        help='seconds to wait between syncs',
        )
    parser.add_argument(
        '--object-sync-timeout',
        type=check_positive_int,
        default=60 * 60 * 60,
        help='seconds to wait for an individual object to sync before '
        'assuming failure',
        )
    parser.add_argument(
        '--prepare-error-delay',
        type=check_positive_int,
        default=10,
        help='seconds to wait before retrying when preparing '
        'an incremental sync fails',
        )
    parser.add_argument(
        '--rgw-data-log-window',
        type=check_positive_int,
        default=30,
        help='period until a data log entry is valid - '
        'must match radosgw configuration',
        )
    parser.add_argument(
        '--test-server-host',
        # host to run a simple http server for testing the sync agent on,
        help=argparse.SUPPRESS,
        )
    parser.add_argument(
        '--daemon_id',
        # port to run a simple http server for testing the sync agent on,
        default='radosgw-sync',
        )
    parser.add_argument(
        '--test-server-port',
        # port to run a simple http server for testing the sync agent on,
        type=check_positive_int,
        default=8080,
        help=argparse.SUPPRESS,
        )
    return parser.parse_known_args(remaining)


def sign_string(
        secret_key,
        verb="GET",
        content_md5="",
        content_type="",
        date=None,
        canonical_amz_headers="",
        canonical_resource="/?versions"
        ):

    date = date or time.asctime(time.gmtime())
    to_sign = string.concatenate(verb, content_md5, content_type, date)
    to_sign = string.concatenate(
        canonical_amz_headers,
        canonical_resource,
        newline=False
    )
    return base64.b64encode(hmac.new(secret_key, to_sign, sha).digest())


def check_versioning(endpoint):
    date = time.asctime(time.gmtime())
    signed_string = sign_string(endpoint.secret_key, date=date)

    url = str(endpoint) + '/?versions'
    headers = {
        'Authorization': 'AWS ' + endpoint.access_key + ':' + signed_string,
        'Date': date
    }

    data = None
    req = urllib2.Request(url, data, headers)
    try:
        response = urllib2.urlopen(req)
        response.read()
        log.debug('%s endpoint supports versioning' % endpoint)
        return True
    except HTTPError as error:
        if error.code == 403:
            log.info('%s endpoint does not support versioning' % endpoint)
        log.warning('encountered issues reaching to endpoint %s' % endpoint)
        log.warning(error)
    except URLError as error:
        log.error("was unable to connect to %s" % url)
        log.error(error)
    return False


def set_args_to_config(args):
    """
    Ensure that the arguments passed in to the CLI are slapped onto the config
    object so that it can be referenced throghout the agent
    """
    if 'args' not in config:
        config['args'] = args.__dict__


class TestHandler(BaseHTTPRequestHandler):
    """HTTP handler for testing radosgw-agent.

    This should never be used outside of testing.
    """
    num_workers = None
    lock_timeout = None
    max_entries = None
    rgw_data_log_window = 30
    src = None
    dest = None

    def do_POST(self):
        log = logging.getLogger(__name__)
        status = 200
        resp = ''
        sync_cls = None
        if self.path.startswith('/metadata/full'):
            sync_cls = sync.MetaSyncerFull
        elif self.path.startswith('/metadata/incremental'):
            sync_cls = sync.MetaSyncerInc
        elif self.path.startswith('/data/full'):
            sync_cls = sync.DataSyncerFull
        elif self.path.startswith('/data/incremental'):
            sync_cls = sync.DataSyncerInc
        else:
            log.warn('invalid request, ignoring')
            status = 400
            resp = 'bad path'

        try:
            if sync_cls is not None:
                syncer = sync_cls(TestHandler.src, TestHandler.dest,
                                  TestHandler.max_entries,
                                  rgw_data_log_window=TestHandler.rgw_data_log_window,
                                  object_sync_timeout=TestHandler.object_sync_timeout)
                syncer.prepare()
                syncer.sync(
                    TestHandler.num_workers,
                    TestHandler.lock_timeout,
                    )
        except Exception as e:
            log.exception('error during sync')
            status = 500
            resp = str(e)

        self.log_request(status, len(resp))
        if status >= 400:
            self.send_error(status, resp)
        else:
            self.send_response(status)
            self.end_headers()


def append_attr_value(d, attr, attrv):
    if attrv and len(str(attrv)) > 0:
        d[attr] = attrv


def append_attr(d, k, attr):
    try:
        attrv = getattr(k, attr)
    except:
        return
    append_attr_value(d, attr, attrv)


def get_attrs(k, attrs):
    d = {}
    for a in attrs:
        append_attr(d, k, a)

    return d


class BucketShardBounds(object):
    def __init__(self, marker, timestamp, retries):
        self.marker = marker
        self.timestamp = timestamp
        self.retries = retries


class BucketBounds(object):
    def __init__(self):
        self.bounds = {}

    def add(self, shard_id, marker, timestamp, retries):
        self.bounds[shard_id] = BucketShardBounds(marker, timestamp, retries)


class BucketShardBoundsJSONEncoder(BucketBounds):
    @staticmethod
    def default(k):
        attrs = ['marker', 'timestamp', 'retries']
        return get_attrs(k, attrs)


class BucketBoundsJSONEncoder(BucketBounds):
    @staticmethod
    def default(k):
        attrs = ['bounds']
        return get_attrs(k, attrs)


class OpStateEntry(object):
    def __init__(self, entry):
        self.timestamp = entry['timestamp']
        self.op_id = entry['op_id']
        self.object = entry['object']
        self.state = entry['state']


class OpStateEntryJSONEncoder(OpStateEntry):
    @staticmethod
    def default(k):
        attrs = ['timestamp', 'op_id', 'object', 'state']
        return get_attrs(k, attrs)


class SyncToolJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, BucketShardBounds):
            return BucketShardBoundsJSONEncoder.default(obj)
        if isinstance(obj, BucketBounds):
            return BucketBoundsJSONEncoder.default(obj)
        if isinstance(obj, OpStateEntry):
            return OpStateEntryJSONEncoder.default(obj)
        return json.JSONEncoder.default(self, obj)


def dump_json(o, cls=SyncToolJSONEncoder):
    return json.dumps(o, cls=cls, indent=4)


class SyncToolDataSync(object):
    def __init__(self, dest_conn, src_conn, worker):
        self.dest_conn = dest_conn
        self.src_conn = src_conn
        self.worker = worker

    def sync_object(self, bucket, obj):
        if not self.worker.sync_object(bucket, obj):
            log.info('failed to sync {b}/{o}'.format(b=bucket, o=obj))

    def delete_object(self, bucket, obj, mtime):
        if not self.worker.delete_object(bucket, obj, mtime):
            log.info('failed to delete {b}/{o}'.format(b=bucket, o=obj))

    def get_bucket_instance(self, bucket):
        return self.worker.get_bucket_instance(bucket)


class Shard(object):
    def __init__(self, sync_work, bucket, shard_id, shard_instance):
        self.sync_work = sync_work
        self.bucket = bucket
        self.shard_id = shard_id
        self.shard_instance = shard_instance
        try:
            self.marker = client.get_log_info(self.sync_work.src_conn, 'bucket-index',
                                              self.shard_instance)['max_marker']
        except:
            self.marker = None

        self.bound = self.get_bound()

    def get_bound(self):
        return client.get_worker_bound(
                        self.sync_work.dest_conn,
                        'bucket-index',
                        self.shard_instance,
                        init_if_not_found=False)

    def set_bound(self, marker, retries, timestamp):
        try:
            data = [dict(name=item, time=timestamp) for item in retries]
            client.set_worker_bound(self.sync_work.dest_conn,
                                    'bucket-index',
                                    marker,
                                    timestamp,
                                    self.sync_work.worker.daemon_id,
                                    self.shard_instance,
                                    data=data)
        except:
            log.warn('error setting worker bound for key "%s",'
                     ' may duplicate some work later. Traceback:', self.shard_instance,
                      exc_info=True)
            return False

        return True

class BucketMeta:
    def init(self, worker, bucket):
        self.worker = worker
        self.bucket_instance = self.get_bucket_instance(bucket)
        self.metadata = client.get_metadata(self.src_conn, 'bucket.instance', bucket_instance)

    def get_bucket_metadata(self):
        bucket_instance = self.get_bucket_instance(bucket)
        metadata = client.get_metadata(self.src_conn, 'bucket.instance', bucket_instance)
        return bucket_instance, metadata

def meta_ver(meta):
    try:
        return meta['ver']['tag'] + ':' + meta['ver']['ver']
    except:
        return ''

class Bucket(object):
    def __init__(self, bucket, shard_id, sync_work):
        self.sync_work = sync_work
        self.bucket = bucket
        self.bucket_instance =  self.get_bucket_instance(bucket)
        self.meta = self.get_bucket_instance_metadata(self.sync_work.src_conn, self.bucket_instance)
        self.shard_id = shard_id

        try:
            target_meta = self.get_bucket_instance_metadata(self.sync_work.dest_conn, self.bucket_instance)
            self.need_sync_meta = meta_ver(self.meta) != meta_ver(target_meta)
        except NotFound:
            self.need_sync_meta = True

        self.num_shards = 0
        try:
            self.num_shards = self.meta['data']['bucket_info']['num_shards']
        except:
            pass

        log.debug('num_shards={n}'.format(n=self.num_shards))

    def get_bucket_instance(self, bucket):
        metadata = client.get_metadata(self.sync_work.src_conn, 'bucket', bucket)
        return bucket + ':' + metadata['data']['bucket']['bucket_id']

    def get_bucket_instance_metadata(self, conn, bucket_instance):
        metadata = client.get_metadata(conn, 'bucket.instance', bucket_instance)
        return metadata

    def sync_meta(self):
        client.update_metadata(self.sync_work.dest_conn, 'bucket.instance', self.bucket_instance, self.meta)

    def init_sync(self):
        if self.need_sync_meta:
            self.sync_meta()

    def iterate_shards(self):
        if self.num_shards <= 0:
            yield Shard(self.sync_work, self.bucket, -1, self.bucket_instance)
        elif self.shard_id >= 0:
            yield Shard(self.sync_work, self.bucket, self.shard_id, self.bucket_instance + ':' + str(self.shard_id))
        else:
            for x in xrange(self.num_shards):
                yield Shard(self.sync_work, self.bucket, x, self.bucket_instance + ':' + str(x))

    def get_bound(self, instance):
        return client.get_worker_bound(
                        self.sync_work.dest_conn,
                        'bucket-index',
                        instance,
                        init_if_not_found=False)

    def get_bucket_bounds(self):
        bounds = BucketBounds()

        for shard in self.iterate_shards():
            result = shard.get_bound()

            if result:
                bounds.add(shard.shard_id, result['marker'], result['oldest_time'], result['retries'])

        return bounds

    def get_source_markers(self):
        markers = {}
        for shard in self.iterate_shards():
            markers[shard.shard_id] = shard.marker

        return markers


class ObjectEntry(object):
    def __init__(self, key, mtime, tag):
        self.key = key
        if not hasattr(self.key, 'VersionedEpoch'):
            self.key.VersionedEpoch = 0
        self.mtime = mtime
        self.tag = tag

    def __str__(self):
        return self.key.__str__() + ',' + str(self.key.VersionedEpoch) + ',' + self.mtime + ',' + self.tag


class BILogIter(object):
    def __init__(self, shard, marker):
        self.shard = shard
        self.marker = marker
        self.sync_work = shard.sync_work

    @staticmethod
    def entry_to_obj(e):
        versioned_epoch = 0
        try:
            if e['versioned']:
                versioned_epoch = e['ver']['epoch']
        except:
            pass

        k = boto.s3.key.Key()

        k.name = e['object']
        k.version_id = e['instance']
        k.VersionedEpoch = versioned_epoch

        return ObjectEntry(k, e['timestamp'], e['op_tag'])

    def iterate(self, squash=True):
        max_entries = 1000

        marker = self.marker or ''

        log_entries = client.get_log(self.sync_work.src_conn, 'bucket-index',
                                     marker, max_entries, self.shard.shard_instance)

        if not squash:
            for e in log_entries:
                self.marker = e['object']
                if e['state'] == 'complete':
                    yield (BILogIter.entry_to_obj(e), e.get('op_id'))

            return

        keys = {}
        l = []
        for e in reversed(log_entries):
            self.marker = e['object']
            if e['state'] == 'complete':
                instance = e['instance'] or None
                key = (e['object'], instance)
                if keys.get(key) is None:
                    l.insert(0, e)
                    keys[key] = True

        for e in l:
            yield (BILogIter.entry_to_obj(e), e.get('op_id'), e.get('op'))


def get_value_map(s):
    attrs = s.split(',')

    m = {}

    for attr in attrs:
        tv = attr.split('=', 1)
        if len(tv) == 1:
            m[tv[0]] = None
        else:
            m[tv[0]] = tv[1]

    return m


def get_list_marker(list_pos, inc_pos):
    return '.' + str({'list': list_pos, 'inc': inc_pos})


def parse_bound_marker(start_marker, bound):
    list_pos = None
    inc_pos = None
    if bound:
        s = bound.get('marker', '')
        if s == '':
            list_pos = ''
            inc_pos = start_marker
        elif s[0] == '.':
            d = get_value_map(s[1:])
            list_pos = d.get('list')
            inc_pos = d.get('inc')
        else:
            inc_pos = s
    else:
        list_pos = ''
        inc_pos = start_marker

    return (list_pos, inc_pos)


class ShardIter(object):
    def __init__(self, shard):
        self.shard = shard

    def iterate_diff_objects(self):
        start_marker = self.shard.marker

        (list_pos, inc_pos) = parse_bound_marker(start_marker, self.shard.bound)

        print 'marker=', get_list_marker(list_pos, inc_pos)

        if list_pos is not None:
            # no bound existing, list all objects
            for obj in client.list_objects_in_bucket(self.shard.sync_work.src_conn, self.shard.bucket, shard_id=self.shard.shard_id, marker=list_pos):
                marker = get_list_marker(obj.name, inc_pos)
                print 'marker=', marker
                yield (ObjectEntry(obj, obj.last_modified, obj.RgwxTag), marker, 'write')

        # now continue from where we last stopped
        li = BILogIter(self.shard, inc_pos)
        for (e, marker, op) in li.iterate():
            print 'marker=', marker, 'op=', op
            yield (e, marker, op)

    def sync_objs(self, sync, bucket, max_entries):
        retries = []
        need_to_set_bound = False
        marker = ''

        count = 0

        for (obj, marker, op) in self.iterate_diff_objects():
            obj = Object(bucket, obj, sync)
            entry = obj.obj_entry
            log.info('sync bucket={b} object={o}'.format(b=bucket, o=entry.key))

            log.info('sync obj={o}, marker={m} last-modified={l}'.format(o=entry.key, m=marker, l=entry.mtime))

            if op == 'del':
                ret = obj.delete()
            else:
                ret = obj.sync()
            if ret is False:
                log.info('sync bucket={b} object={o} failed'.format(b=bucket, o=entry.key))
                retries.append(entry.key)

            need_to_set_bound = True
            count += 1
            if max_entries > 0 and count == max_entries:
                break
            yield

        if need_to_set_bound:
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
            print 'set_bound marker=', marker, 'timestamp=', timestamp
            ret = self.shard.set_bound(marker, retries, timestamp)
            if not ret:
                log.info('failed to store state information for bucket {0}'.format(bucket))


class Object(object):
    def __init__(self, bucket, obj_entry, sync_work):
        self.sync_work = sync_work
        self.bucket = bucket
        self.obj_entry = obj_entry

    def sync(self):
        try:
            self.sync_work.sync_object(self.bucket, self.obj_entry.key)
            return True
        except:
            return False

    def delete(self):
        try:
            self.sync_work.delete_object(self.bucket, self.obj_entry.key, self.obj_entry.mtime)
            return True
        except:
            return False

    def status(self):
        opstate_ret = client.get_op_state(self.sync_work.dest_conn, '', '', self.bucket, self.obj_entry)
        entries = [OpStateEntry(entry) for entry in opstate_ret]

        print dump_json(entries)

class BucketState(object):
    def __init__(self, bucket, shard, marker, bound):
        self.bucket = bucket
        self.shard = shard
        self.marker = marker
        self.bound = bound

class ZoneFullSyncState(object):
    def __init__(self, sync, zone):
        self.sync = sync
        self.zone = zone
        self.num_shards = 128
        self.marker = { 'num_shards': self.num_shards }
        self.status = self.get_full_sync_status()


    def get_full_sync_status(self):
        cur_bound = client.get_worker_bound(
                        self.sync.dest_conn,
                        'zone.full_data_sync.meta',
                        None,
                        init_if_not_found=False)

        if not cur_bound:
            self.set_state('init')
            return self.marker

        self.marker = json.loads(cur_bound['marker'])
        return self.marker

    def set_state(self, state):
            self.marker['state'] = state
            print json.dumps(self.marker)
            client.set_worker_bound(self.sync.dest_conn, 'zone.full_data_sync.meta',
                                    json.dumps(self.marker),
                                    DEFAULT_TIME, 
                                    self.sync.worker.daemon_id,
                                    None)

    def shard_num_for_key(self, key):
        key = key.encode('utf8')
        hash_val = 0
        for char in key:
            c = ord(char)
            hash_val = ((hash_val + (c << 4) + (c >> 4)) * 11) % 7877
        return hash_val % self.num_shards


    def iterate_full_sync_buckets(self):
        for shard_id in xrange(self.num_shards):
            info = client.list_worker_bound(self.sync.dest_conn, 'zone.full_data_sync', shard_id)
            if info is not None:
                for k in info['keys']:
                    yield k



class BucketsIterator(object):
    def __init__(self, sync, zone, bucket_name):
        self.sync = sync
        self.zone = zone
        self.bucket_name = bucket_name

        self.explicit_bucket = bucket_name is not None

        self.fs_state_manager = ZoneFullSyncState(sync, zone)
        self.fs_state = self.fs_state_manager.get_full_sync_status()

    def get_full_sync_state(self):
        return self.fs_state

    def build_full_sync_work(self):
        log.info('building full data sync work')
        for bucket_name in client.get_bucket_list(self.sync.src_conn):
                marker = ''
                shard_id = self.fs_state_manager.shard_num_for_key(bucket_name)
                client.set_worker_bound(self.sync.dest_conn,
                                    'zone.full_data_sync',
                                    marker,
                                    DEFAULT_TIME,
                                    self.sync.worker.daemon_id,
                                    shard_id,
                                    data=None,
                                    key=bucket_name)
                log.info('adding bucket to full sync work: {b}'.format(b=bucket_name))

    def iterate_dirty_buckets(self):
        if not self.explicit_bucket:
            cur_state = self.fs_state['state']
            if cur_state == 'init':
                self.build_full_sync_work()
                self.fs_state_manager.set_state('full-sync')
                cur_state = 'full-sync'

            if cur_state == 'full-sync':
                src_buckets = self.fs_state_manager.iterate_full_sync_buckets()
            else:
                assert False
        else:
            src_buckets = [self.bucket_name]

        for b in src_buckets:
            buck = Bucket(b, -1, self.sync)

            markers = buck.get_source_markers()

            for shard in buck.iterate_shards():
                try:
                    bound = shard.get_bound()['marker']
                except:
                    bound = None

                try:
                    marker = markers[shard.shard_id]
                except:
                    marker = None

                if not marker or marker == '':
                    # an empty bucket
                    continue

                if marker != bound:
                    yield BucketState(buck, shard, marker, bound)

class Zone(object):
    def __init__(self, sync):
        self.sync = sync

    def iterate_diff(self, bi):
        for bs in bi.iterate_dirty_buckets():
            yield bs

    def sync_data(self, bi, restart):
        gens = {}

        if restart:
            bi.fs_state_manager.set_state('init')


        objs_per_bucket = 10
        concurrent_buckets = 2

        while True:
            for bs in self.iterate_diff(bi):
                bucket = bs.bucket
                shard = bs.shard
                marker = bs.marker
                bound = bs.bound

                print dump_json({'bucket': bucket.bucket, 'bucket_instance': bucket.bucket_instance, 'shard_id': shard.shard_id, 'marker': marker, 'bound': bound})

                si = ShardIter(shard)

                bucket_shard_id = bucket.bucket_instance + ':' + str(shard.shard_id)

                bucket.sync_meta()

                if bucket_shard_id not in gens:
                    gens[bucket_shard_id] = si.sync_objs(self.sync, bucket.bucket, objs_per_bucket)

                print 'shard_id', shard.shard_id, 'len(gens)', len(gens)

                while True:
                    rm = []
                    for (s, gen) in gens.iteritems():
                        try:
                            gen.next()
                        except StopIteration:
                            print 'StopIteration:', s
                            rm.append(s)

                    for s in rm:
                        del gens[s]

                    if len(gens) < concurrent_buckets:
                        break

    def iterate_diff_objects(self, bucket, shard_id, marker, bound):
        if not bound:
            for obj in client.list_objects_in_bucket(self.sync.src_conn, bucket, shard_id=shard_id):
                yield obj


class SyncToolCommand(object):

    def __init__(self):
        args, self.remaining = parse_args()

        self.args = args

        log = logging.getLogger()
        log_level = logging.INFO
        lib_log_level = logging.WARN
        if args.verbose:
            log_level = logging.DEBUG
            lib_log_level = logging.DEBUG
        elif args.quiet:
            log_level = logging.WARN

        logging.basicConfig(level=log_level)
        logging.getLogger('boto').setLevel(lib_log_level)

        if args.log_file is not None:
            handler = logging.handlers.WatchedFileHandler(
                filename=args.log_file,
                )
            formatter = logging.Formatter(
                fmt='%(asctime)s.%(msecs)03d %(process)d:%(levelname)s:%(name)s:%(message)s',
                datefmt='%Y-%m-%dT%H:%M:%S',
                )
            handler.setFormatter(formatter)
            logging.getLogger().addHandler(handler)

        set_args_to_config(args)

        self.dest = args.destination
        self.dest.access_key = args.dest_access_key
        self.dest.secret_key = args.dest_secret_key
        self.src = args.source or client.Endpoint(None, None, None)
        if args.src_zone:
            self.src.zone = args.src_zone
        self.dest_conn = client.connection(self.dest)

        try:
            self.region_map = client.get_region_map(self.dest_conn)
        except Exception:
            log.exception('Could not retrieve region map from destination: ' % self.dest)
            sys.exit(1)

        try:
            client.configure_endpoints(self.region_map, self.dest, self.src, args.metadata_only)
        except client.ClientException as e:
            log.error(e)
            sys.exit(1)

        self.src.access_key = args.src_access_key
        self.src.secret_key = args.src_secret_key

        self.src_conn = client.connection(self.src)

        self.log = log

        if config['args'].get('versioned'):
            log.debug('versioned flag enabled, overriding versioning check')
            config['use_versioning'] = True
        else:
            config['use_versioning'] = check_versioning(self.src)

    def _parse(self):
        parser = argparse.ArgumentParser(
            description='radosgw sync tool',
            usage='''radosgw-sync <command> [<args>]

The commands are:
    data_sync               sync data of bucket / object
''')
        parser.add_argument('command', help='Subcommand to run')
        # parse_args defaults to [1:] for args, but you need to
        # exclude the rest of the args too, or validation will fail

        self.sync = SyncToolDataSync(self.dest_conn, self.src_conn,
                        worker.DataWorker(None,
                           None,
                           20, # log lock timeout
                           self.src,
                           self.dest,
                           daemon_id=self.args.daemon_id,
                           max_entries=self.args.max_entries,
                           object_sync_timeout=self.args.object_sync_timeout))

        self.zone = Zone(self.sync)

        cmd_args = parser.parse_args(self.remaining[0:1])
        if not hasattr(self, cmd_args.command) or cmd_args.command[0] == '_':
            print 'Unrecognized command:', cmd_args.command
            parser.print_help()
            exit(1)
        # use dispatch pattern to invoke method with same name
        ret = getattr(self, cmd_args.command)
        return ret

    def status(self):
        parser = argparse.ArgumentParser(
            description='Get sync status of bucket or object',
            usage='radosgw-sync status <bucket_name>/<key> [<args>]')
        parser.add_argument('--shard-id', type=int, default=-1)
        parser.add_argument('source', nargs='?')
        args = parser.parse_args(self.remaining[1:])

        target = ''
        if args.source is not None:
            target = args.source.split('/', 1)

        if len(target) == 0:
            bi = BucketsIterator(self.sync, self.zone, None)
            print dump_json({'full-sync-state': bi.get_full_sync_state()})
        elif len(target) == 1:
            bucket = target[0]
            b = Bucket(bucket, args.shard_id, self.sync)
            log.info('status bucket={b}'.format(b=bucket))

            bounds = b.get_bucket_bounds()
            markers = b.get_source_markers()
            print dump_json({'source': markers, 'dest': bounds})
        else:
            bucket = target[0]
            b = Bucket(bucket, args.shard_id, self.sync)
            obj_name = target[1]
            obj = Object(bucket, obj_name, self.sync)
            log.info('sync bucket={b} object={o}'.format(b=bucket, o=obj_name))

            obj.status()

    def data_sync(self):
        parser = argparse.ArgumentParser(
            description='Sync bucket / object',
            usage='radosgw-sync data_sync <bucket_name>/<key> [<args>]')
        parser.add_argument('source', nargs='?')
        parser.add_argument('--restart', action='store_true')
        args = parser.parse_args(self.remaining[1:])

        if args.source:
            target = args.source.split('/', 1)
        else:
            target = ''

        if len(target) == 0:
            self.zone.sync_data(BucketsIterator(self.sync, self.zone, None), args.restart) # client.get_bucket_list(self.src_conn))
        elif len(target) == 1:
            bucket = target[0]
            self.zone.sync_data(BucketsIterator(self.sync, self.zone, bucket), args.restart)
            log.info('sync bucket={b}'.format(b=bucket))
        else:
            bucket = target[0]
            obj_name = target[1]
            obj = Object(bucket, obj_name, self.sync)
            log.info('sync bucket={b} object={o}'.format(b=bucket, o=obj_name))

            ret = obj.sync()
            if ret is False:
                log.info('sync bucket={b} object={o} failed'.format(b=bucket, o=obj_name))

    def diff(self):
        parser = argparse.ArgumentParser(
            description='Show buckets with pending modified data',
            usage='radosgw-sync diff')
        parser.add_argument('bucket_name', nargs='?')
        args = parser.parse_args(self.remaining[1:])

        bi = BucketsIterator(self.sync, self.zone, args.bucket_name)

        for bs in bi.iterate_dirty_buckets():
            bucket = bs.bucket

            print dump_json({'bucket': bucket.bucket, 'bucket_instance': bucket.bucket_instance, 'shard_id': bs.shard.shard_id, 'marker': bs.marker, 'bound': bs.bound})

            si = ShardIter(bs.shard)

            for (obj, marker, op) in si.iterate_diff_objects():
                print obj, marker

    def get_bucket_bounds(self, bucket):
        print dump_json({'src': src_buckets, 'dest': dest_buckets})


def main():

    cmd = SyncToolCommand()._parse()
    cmd()

