# -*- coding: utf-8 -*-
import argparse
import copy
import datetime
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
import timeit
import traceback
from email.mime.text import MIMEText
from smtplib import SMTP
from smtplib import SMTPException
from socket import error
import statsd

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from croniter import croniter
from elasticsearch.exceptions import ConnectionError
from elasticsearch.exceptions import ElasticsearchException
from elasticsearch.exceptions import NotFoundError
from elasticsearch.exceptions import TransportError

from elastalert.alerters.debug import DebugAlerter
from elastalert.config import load_conf
from elastalert.enhancements import DropMatchException
from elastalert.kibana_discover import generate_kibana_discover_url
from elastalert.kibana_external_url_formatter import create_kibana_external_url_formatter
from elastalert.opensearch_discover import generate_opensearch_discover_url
from elastalert.opensearch_external_url_formatter import create_opensearch_external_url_formatter
from elastalert.prometheus_wrapper import PrometheusWrapper
from elastalert.ruletypes import FlatlineRule
from elastalert.ruletypes import RuleType
from elastalert.util import (add_keyword_postfix, cronite_datetime_to_timestamp, dt_to_ts, dt_to_unix, EAException,
                             elastalert_logger, elasticsearch_client, format_index, lookup_es_key, parse_deadline,
                             parse_duration, pretty_ts, replace_dots_in_field_names, seconds, set_es_key,
                             should_scrolling_continue, total_seconds, ts_add, ts_now, ts_to_dt, unix_to_dt,
                             ts_utc_to_tz)


class ElastAlerter(object):
    """ The main ElastAlert runner. This class holds all state about active rules,
    controls when queries are run, and passes information between rules and alerts.

    :param args: An argparse arguments instance. Should contain debug and start"""

    thread_data = threading.local()

    def parse_args(self, args):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            '--config',
            action='store',
            dest='config',
            default="config.yaml",
            help='Global config file (default: config.yaml)')
        parser.add_argument('--debug', action='store_true', dest='debug', help='Suppresses alerts and prints information instead. '
                                                                               'Not compatible with `--verbose`')
        parser.add_argument('--rule', dest='rule', help='Run only a specific rule (by filename, must still be in rules folder)')
        parser.add_argument('--silence', dest='silence', help='Silence rule for a time period. Must be used with --rule. Usage: '
                                                              '--silence <units>=<number>, eg. --silence hours=2')
        parser.add_argument(
            "--silence_qk_value",
            dest="silence_qk_value",
            help="Silence the rule only for this specific query key value.",
        )
        parser.add_argument('--start', dest='start', help='YYYY-MM-DDTHH:MM:SS Start querying from this timestamp. '
                                                          'Use "NOW" to start from current time. (Default: present)')
        parser.add_argument('--end', dest='end', help='YYYY-MM-DDTHH:MM:SS Query to this timestamp. (Default: present)')
        parser.add_argument('--verbose', action='store_true', dest='verbose', help='Increase verbosity without suppressing alerts. '
                                                                                   'Not compatible with `--debug`')
        parser.add_argument('--patience', action='store', dest='timeout',
                            type=parse_duration,
                            default=datetime.timedelta(),
                            help='Maximum time to wait for ElasticSearch to become responsive.  Usage: '
                            '--patience <units>=<number>. e.g. --patience minutes=5')
        parser.add_argument(
            '--pin_rules',
            action='store_true',
            dest='pin_rules',
            help='Stop ElastAlert from monitoring config file changes')
        parser.add_argument('--es_debug', action='store_true', dest='es_debug', help='Enable verbose logging from Elasticsearch queries')
        parser.add_argument(
            '--es_debug_trace',
            action='store',
            dest='es_debug_trace',
            help='Enable logging from Elasticsearch queries as curl command. Queries will be logged to file. Note that '
                 'this will incorrectly display localhost:9200 as the host/port')
        parser.add_argument('--prometheus_port', type=int, dest='prometheus_port', help='Enables Prometheus metrics on specified port.')

        parser.add_argument(
            '--prometheus_addr',
            type=str,
            dest='prometheus_addr',
            default=None,
            help='Address to bind the Prometheus metrics server on.')

        self.args = parser.parse_args(args)

    def __init__(self, args):
        self.es_clients = {}
        self.parse_args(args)
        self.debug = self.args.debug
        self.verbose = self.args.verbose

        if self.verbose and self.debug:
            elastalert_logger.info(
                "Note: --debug and --verbose flags are set. --debug takes precedent."
            )

        if self.verbose or self.debug:
            elastalert_logger.setLevel(logging.INFO)

        if self.debug:
            elastalert_logger.info(
                """Note: In debug mode, alerts will be logged to console but NOT actually sent.
                To send them but remain verbose, use --verbose instead."""
            )

        if not self.args.es_debug:
            logging.getLogger('elasticsearch').setLevel(logging.WARNING)

        if self.args.es_debug_trace:
            tracer = logging.getLogger('elasticsearch.trace')
            tracer.setLevel(logging.INFO)
            tracer.addHandler(logging.FileHandler(self.args.es_debug_trace))

        self.conf = load_conf(self.args)
        self.rules_loader = self.conf['rules_loader']
        self.rules = self.rules_loader.load(self.conf, self.args)

        elastalert_logger.info(f'{len(self.rules)} rules loaded')

        self.max_query_size = self.conf['max_query_size']
        self.scroll_keepalive = self.conf['scroll_keepalive']
        self.writeback_index = self.conf['writeback_index']
        self.run_every = self.conf['run_every']
        self.alert_time_limit = self.conf['alert_time_limit']
        self.old_query_limit = self.conf['old_query_limit']
        self.disable_rules_on_error = self.conf['disable_rules_on_error']
        self.notify_email = self.conf.get('notify_email', [])
        self.notify_all_errors = self.conf.get('notify_all_errors', False)
        self.notify_alert = self.conf.get('notify_alert', [])
        alert_conf_obj = self.conf.copy()
        alert_conf_obj['name'] = 'ElastAlert 2 System Error Notification'
        alert_conf_obj['alert'] = self.notify_alert
        alert_conf_obj['type'] = RuleType({})
        self.notify_alerters = self.rules_loader.load_alerts(alert_conf_obj, self.notify_alert)
        self.from_addr = self.conf.get('from_addr', 'ElastAlert')
        self.smtp_host = self.conf.get('smtp_host', 'localhost')
        self.max_aggregation = self.conf.get('max_aggregation', 10000)
        self.buffer_time = self.conf['buffer_time']
        self.silence_cache = {}
        self.rule_hashes = self.rules_loader.get_hashes(self.conf, self.args.rule)
        self.starttime = self.args.start
        self.disabled_rules = []
        self.replace_dots_in_field_names = self.conf.get('replace_dots_in_field_names', False)
        self.thread_data.alerts_sent = 0
        self.thread_data.num_hits = 0
        self.thread_data.num_dupes = 0
        executors = {
            'default': ThreadPoolExecutor(max_workers=self.conf.get('max_threads', 10)),
        }
        job_defaults = {
            'misfire_grace_time': self.conf.get('misfire_grace_time', 5),
            'coalesce': True,
            'max_instances': 1
        }
        self.scheduler = BackgroundScheduler(executors=executors, job_defaults=job_defaults)
        self.string_multi_field_name = self.conf.get('string_multi_field_name', False)
        self.statsd_instance_tag = self.conf.get('statsd_instance_tag', '')
        self.statsd_host = self.conf.get('statsd_host', '')
        if self.statsd_host and len(self.statsd_host) > 0:
            self.statsd = statsd.StatsClient(host=self.statsd_host, port=8125)
        else:
            self.statsd = None
        self.add_metadata_alert = self.conf.get('add_metadata_alert', False)
        self.prometheus_port = self.args.prometheus_port
        self.prometheus_addr = self.args.prometheus_addr
        self.show_disabled_rules = self.conf.get('show_disabled_rules', True)
        self.pretty_ts_format = self.conf.get('custom_pretty_ts_format')

        self.writeback_es = elasticsearch_client(self.conf)

        remove = []
        for rule in self.rules:
            if 'is_enabled' in rule and not rule['is_enabled']:
                self.disabled_rules.append(rule)
                remove.append(rule)
            elif not self.init_rule(rule):
                remove.append(rule)
        list(map(self.rules.remove, remove))

        if self.args.silence:
            self.silence()

        self.alert_lock = threading.Lock()

    @staticmethod
    def get_index(rule, starttime=None, endtime=None):
        """ Gets the index for a rule. If strftime is set and starttime and endtime
        are provided, it will return a comma seperated list of indices. If strftime
        is set but starttime and endtime are not provided, it will replace all format
        tokens with a wildcard. """
        index = rule['index']
        add_extra = rule.get('search_extra_index', False)
        if rule.get('use_strftime_index'):
            if starttime and endtime:
                return format_index(index, starttime, endtime, add_extra)
            else:
                # Replace the substring containing format characters with a *
                format_start = index.find('%')
                format_end = index.rfind('%') + 2
                return index[:format_start] + '*' + index[format_end:]
        else:
            return index

    @staticmethod
    def get_query(filters, starttime=None, endtime=None, sort=True, timestamp_field='@timestamp', to_ts_func=dt_to_ts, desc=False):
        """ Returns a query dict that will apply a list of filters, filter by
        start and end time, and sort results by timestamp.

        :param filters: A list of Elasticsearch filters to use.
        :param starttime: A timestamp to use as the start time of the query.
        :param endtime: A timestamp to use as the end time of the query.
        :param sort: If true, sort results by timestamp. (Default True)
        :return: A query dictionary to pass to Elasticsearch.
        """
        starttime = to_ts_func(starttime)
        endtime = to_ts_func(endtime)
        filters = copy.copy(filters)

        # ElastAlert documentation still specifies an old way of writing filters
        # This snippet of code converts it into the new standard
        new_filters = []
        for es_filter in filters:
            if es_filter.get('query'):
                new_filters.append(es_filter['query'])
            else:
                new_filters.append(es_filter)

        es_filters = {'filter': {'bool': {'must': new_filters}}}
        if starttime and endtime:
            es_filters['filter']['bool']['must'].insert(0, {'range': {timestamp_field: {'gt': starttime,
                                                                                        'lte': endtime}}})
        query = {'query': {'bool': es_filters}}
        if sort:
            query['sort'] = [{timestamp_field: {'order': 'desc' if desc else 'asc'}}]
        return query

    def get_terms_query(self, query, rule, size, field):
        """ Takes a query generated by get_query and outputs a aggregation query """
        query_element = query['query']
        if 'sort' in query_element:
            query_element.pop('sort')
        aggs_query = query
        aggs_query['aggs'] = {'counts': {'terms': {'field': field,
                                                   'size': size,
                                                   'min_doc_count': rule.get('min_doc_count', 1)}}}
        return aggs_query

    def get_aggregation_query(self, query, rule, query_key, terms_size, timestamp_field='@timestamp'):
        """ Takes a query generated by get_query and outputs a aggregation query """
        query_element = query['query']
        if 'sort' in query_element:
            query_element.pop('sort')
        metric_agg_element = rule['aggregation_query_element']

        bucket_interval_period = rule.get('bucket_interval_period')
        if bucket_interval_period is not None:
            aggs_element = {
                'interval_aggs': {
                    'date_histogram': {
                        'field': timestamp_field,
                        'fixed_interval': bucket_interval_period},
                    'aggs': metric_agg_element
                }
            }
            if rule.get('bucket_offset_delta'):
                aggs_element['interval_aggs']['date_histogram']['offset'] = '+%ss' % (rule['bucket_offset_delta'])
        else:
            aggs_element = metric_agg_element

        if query_key is not None:
            for idx, key in reversed(list(enumerate(query_key.split(',')))):
                aggs_element = {'bucket_aggs': {'terms': {'field': key, 'size': terms_size,
                                                          'min_doc_count': rule.get('min_doc_count', 1)},
                                                'aggs': aggs_element}}

        aggs_query = query
        aggs_query['aggs'] = aggs_element
        return aggs_query

    def get_index_start(self, index, timestamp_field='@timestamp'):
        """ Query for one result sorted by timestamp to find the beginning of the index.

        :param index: The index of which to find the earliest event.
        :return: Timestamp of the earliest event.
        """
        query = {'sort': {timestamp_field: {'order': 'asc'}}}
        try:
            res = self.thread_data.current_es.search(index=index, size=1, body=query,
                                                         _source_includes=[timestamp_field], ignore_unavailable=True)
        except ElasticsearchException as e:
            self.handle_error("Elasticsearch query error: %s" % (e), {'index': index, 'query': query})
            return '1969-12-30T00:00:00Z'
        if len(res['hits']['hits']) == 0:
            # Index is completely empty, return a date before the epoch
            return '1969-12-30T00:00:00Z'
        return res['hits']['hits'][0][timestamp_field]

    @staticmethod
    def process_hits(rule, hits):
        """ Update the _source field for each hit received from ES based on the rule configuration.

        This replaces timestamps with datetime objects,
        folds important fields into _source and creates compound query_keys.

        :return: A list of processed _source dictionaries.
        """

        processed_hits = []
        for hit in hits:
            # Merge fields and _source
            hit.setdefault('_source', {})
            for key, value in list(hit.get('fields', {}).items()):
                # Fields are returned as lists, assume any with length 1 are not arrays in _source
                # Except sometimes they aren't lists. This is dependent on ES version
                hit['_source'].setdefault(key, value[0] if type(value) is list and len(value) == 1 else value)

            # Convert the timestamp to a datetime
            ts = lookup_es_key(hit['_source'], rule['timestamp_field'])
            if not ts and not rule["_source_enabled"]:
                raise EAException(
                    "Error: No timestamp was found for hit. '_source_enabled' is set to false, check your mappings for stored fields"
                )

            set_es_key(hit['_source'], rule['timestamp_field'], rule['ts_to_dt'](ts))
            set_es_key(hit, rule['timestamp_field'], lookup_es_key(hit['_source'], rule['timestamp_field']))

            # Tack metadata fields into _source
            for field in ['_id', '_index', '_type']:
                if field in hit:
                    hit['_source'][field] = hit[field]

            if rule.get('compound_query_key'):
                values = [lookup_es_key(hit['_source'], key) for key in rule['compound_query_key']]
                hit['_source'][rule['query_key']] = ', '.join([str(value) for value in values])

            if rule.get('compound_aggregation_key'):
                values = [lookup_es_key(hit['_source'], key) for key in rule['compound_aggregation_key']]
                hit['_source'][rule['aggregation_key']] = ', '.join([str(value) for value in values])

            processed_hits.append(hit['_source'])

        return processed_hits

    def get_hits(self, rule, starttime, endtime, index, scroll=False):
        """ Query Elasticsearch for the given rule and return the results.
        :param rule: The rule configuration.
        :param starttime: The earliest time to query.
        :param endtime: The latest time to query.
        :return: A list of hits, bounded by rule['max_query_size'] (or self.max_query_size).
        """

        query = self.get_query(
            rule['filter'],
            starttime,
            endtime,
            timestamp_field=rule['timestamp_field'],
            to_ts_func=rule['dt_to_ts'],
        )
        extra_args = {'_source_includes': rule['include']}
        scroll_keepalive = rule.get('scroll_keepalive', self.scroll_keepalive)
        if not rule.get('_source_enabled'):
            query['stored_fields'] = rule['include']
            extra_args = {}

        if rule.get('include_fields', None) is not None:
            query['fields'] = rule['include_fields']

        try:
            if scroll:
                res = self.thread_data.current_es.scroll(scroll_id=rule['scroll_id'], scroll=scroll_keepalive)
            else:
                res = self.thread_data.current_es.search(
                    scroll=scroll_keepalive,
                    index=index,
                    size=rule.get('max_query_size', self.max_query_size),
                    body=query,
                    ignore_unavailable=True,
                    **extra_args
                )
                if '_scroll_id' in res:
                    rule['scroll_id'] = res['_scroll_id']

                self.thread_data.total_hits = int(res['hits']['total']['value'])

            if len(res.get('_shards', {}).get('failures', [])) > 0:
                try:
                    errs = [e['reason']['reason'] for e in res['_shards']['failures'] if 'Failed to parse' in e['reason']['reason']]
                    if len(errs):
                        raise ElasticsearchException(errs)
                except (TypeError, KeyError):
                    # Different versions of ES have this formatted in different ways. Fallback to str-ing the whole thing
                    raise ElasticsearchException(str(res['_shards']['failures']))

            elastalert_logger.debug(str(res))
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running query: %s' % (e), {'rule': rule['name'], 'query': query})
            return None
        hits = res['hits']['hits']
        self.thread_data.num_hits += len(hits)
        lt = rule.get('use_local_time')
        status_log = "Queried rule %s from %s to %s: %s / %s hits" % (
            rule['name'],
            pretty_ts(starttime, lt, self.pretty_ts_format),
            pretty_ts(endtime, lt, self.pretty_ts_format),
            self.thread_data.num_hits,
            len(hits)
        )
        if self.thread_data.total_hits > rule.get('max_query_size', self.max_query_size):
            elastalert_logger.info("%s (scrolling..)" % status_log)
        else:
            elastalert_logger.info(status_log)

        hits = self.process_hits(rule, hits)

        return hits

    def get_hits_count(self, rule, starttime, endtime, index):
        """ Query Elasticsearch for the count of results and returns a list of timestamps
        equal to the endtime. This allows the results to be passed to rules which expect
        an object for each hit.

        :param rule: The rule configuration dictionary.
        :param starttime: The earliest time to query.
        :param endtime: The latest time to query.
        :return: A dictionary mapping timestamps to number of hits for that time period.
        """
        query = self.get_query(
            rule['filter'],
            starttime,
            endtime,
            timestamp_field=rule['timestamp_field'],
            sort=False,
            to_ts_func=rule['dt_to_ts'],
        )

        es_client = self.thread_data.current_es
        try:
            res = es_client.count(
                index=index,
                body=query,
                ignore_unavailable=True
            )
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running count query: %s' % (e), {'rule': rule['name'], 'query': query})
            return None

        self.thread_data.num_hits += res['count']
        lt = rule.get('use_local_time')
        elastalert_logger.info(
            "Queried rule %s from %s to %s: %s hits" % (rule['name'], pretty_ts(starttime, lt, self.pretty_ts_format),
                                                        pretty_ts(endtime, lt, self.pretty_ts_format), res['count'])
        )
        return {endtime: res['count']}

    @staticmethod
    def query_key_filters(rule: dict, qk_value_csv: str) -> dict:
        if qk_value_csv is None:
            return

        # Split on comma followed by zero or more whitespace characters. It's
        # expected to be commaspace separated. However 76ab593 suggests there
        # are cases when it is only comma and not commaspace
        qk_values = re.split(r',\s*',qk_value_csv)
        end = '.keyword'

        query_keys = []
        try:
            query_keys = rule['compound_query_key']
        except KeyError:
            query_key = rule.get('query_key')
            if query_key is not None:
                query_keys.append(query_key)

        if len(qk_values) != len(query_keys):
            msg = ( f"Received {len(qk_values)} value(s) for {len(query_keys)} key(s)."
                    f" Did '{qk_value_csv}' have a value with a comma?"
                    " See https://github.com/jertel/elastalert2/pull/1330#issuecomment-1849962106" )
            elastalert_logger.warning( msg )

        for key, value in zip(query_keys, qk_values):
            if rule.get('raw_count_keys', True):
                if not key.endswith(end):
                    key += end
            yield {'term': {key: value}}

    def get_hits_terms(self, rule, starttime, endtime, index, key, qk=None, size=None):
        rule_filter = copy.copy(rule['filter'])

        for filter in self.query_key_filters(rule=rule, qk_value_csv=qk):
            rule_filter.append(filter)

        base_query = self.get_query(
            rule_filter,
            starttime,
            endtime,
            timestamp_field=rule['timestamp_field'],
            sort=False,
            to_ts_func=rule['dt_to_ts'],
        )
        if size is None:
            size = rule.get('terms_size', 50)
        query = self.get_terms_query(base_query, rule, size, key)

        try:
            res = self.thread_data.current_es.search(index=index, body=query, size=0, ignore_unavailable=True)
        except ElasticsearchException as e:
            # Elasticsearch sometimes gives us GIGANTIC error messages
            # (so big that they will fill the entire terminal buffer)
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running terms query: %s' % (e), {'rule': rule['name'], 'query': query})
            return None

        if 'aggregations' not in res:
            return {}
        buckets = res['aggregations']['counts']['buckets']
        self.thread_data.num_hits += len(buckets)
        lt = rule.get('use_local_time')
        elastalert_logger.info(
            'Queried rule %s from %s to %s: %s buckets' % (
            rule['name'], pretty_ts(starttime, lt, self.pretty_ts_format),
            pretty_ts(endtime, lt, self.pretty_ts_format), len(buckets))
        )
        return {endtime: buckets}

    def get_hits_aggregation(self, rule, starttime, endtime, index, query_key, term_size=None):
        rule_filter = copy.copy(rule['filter'])
        base_query = self.get_query(
            rule_filter,
            starttime,
            endtime,
            timestamp_field=rule['timestamp_field'],
            sort=False,
            to_ts_func=rule['dt_to_ts'],
        )
        if term_size is None:
            term_size = rule.get('terms_size', 50)
        query = self.get_aggregation_query(base_query, rule, query_key, term_size, rule['timestamp_field'])
        try:
            res = self.thread_data.current_es.search(index=index, body=query, size=0, ignore_unavailable=True)
        except ElasticsearchException as e:
            if len(str(e)) > 1024:
                e = str(e)[:1024] + '... (%d characters removed)' % (len(str(e)) - 1024)
            self.handle_error('Error running query: %s' % (e), {'rule': rule['name']})
            return None
        if 'aggregations' not in res:
            return {}
        payload = res['aggregations']

        self.thread_data.num_hits += res['hits']['total']['value']

        return {endtime: payload}

    def remove_duplicate_events(self, data, rule):
        new_events = []
        for event in data:
            if event['_id'] in rule['processed_hits']:
                continue

            # Remember the new data's IDs
            rule['processed_hits'][event['_id']] = lookup_es_key(event, rule['timestamp_field'])
            new_events.append(event)

        return new_events

    def remove_old_events(self, rule):
        # Anything older than the buffer time we can forget
        now = ts_now()
        remove = []
        buffer_time = rule.get('buffer_time', self.buffer_time)
        if rule.get('query_delay'):
            try:
                buffer_time += rule['query_delay']
            except Exception as e:
                self.handle_error("[remove_old_events]Error parsing query_delay send time format %s" % e)
        for _id, timestamp in rule['processed_hits'].items():
            if now - timestamp > buffer_time:
                remove.append(_id)
        list(map(rule['processed_hits'].pop, remove))

    def run_query(self, rule, start=None, end=None, scroll=False):
        """ Query for the rule and pass all of the results to the RuleType instance.

        :param rule: The rule configuration.
        :param start: The earliest time to query.
        :param end: The latest time to query.
        Returns True on success and False on failure.
        """
        if start is None:
            start = self.get_index_start(rule['index'])
        if end is None:
            end = ts_now()

        if rule.get('query_timezone'):
            elastalert_logger.info("Query start and end time converting UTC to query_timezone : {}".format(rule.get('query_timezone')))
            start = ts_utc_to_tz(start, rule.get('query_timezone'))
            end = ts_utc_to_tz(end, rule.get('query_timezone'))

        # Reset hit counter and query
        rule_inst = rule['type']
        rule['scrolling_cycle'] = rule.get('scrolling_cycle', 0) + 1
        index = self.get_index(rule, start, end)
        if rule.get('use_count_query'):
            data = self.get_hits_count(rule, start, end, index)
        elif rule.get('use_terms_query'):
            data = self.get_hits_terms(rule, start, end, index, rule['query_key'])
        elif rule.get('aggregation_query_element'):
            data = self.get_hits_aggregation(rule, start, end, index, rule.get('query_key', None))
        else:
            data = self.get_hits(rule, start, end, index, scroll)
            if data:
                old_len = len(data)
                data = self.remove_duplicate_events(data, rule)
                self.thread_data.num_dupes += old_len - len(data)

        # There was an exception while querying
        if data is None:
            return False
        elif data:
            if rule.get('use_count_query'):
                rule_inst.add_count_data(data)
            elif rule.get('use_terms_query'):
                rule_inst.add_terms_data(data)
            elif rule.get('aggregation_query_element'):
                rule_inst.add_aggregation_data(data)
            else:
                rule_inst.add_data(data)

        try:
            if rule.get('scroll_id') and self.thread_data.num_hits < self.thread_data.total_hits and should_scrolling_continue(rule):
                if not self.run_query(rule, start, end, scroll=True):
                    self.cleanup_scroll_id(rule)
                    return False
        except RuntimeError:
            # It's possible to scroll far enough to hit max recursive depth
            pass
        finally:
            # Ensure scroll ID is always cleaned up
            self.cleanup_scroll_id(rule)

        return True

    def get_starttime(self, rule):
        """ Query ES for the last time we ran this rule.

        :param rule: The rule configuration.
        :return: A timestamp or None.
        """
        sort = {'sort': {'@timestamp': {'order': 'desc'}}}
        query = {'query': {'bool': {'filter': {'term': {'rule_name': '%s' % (rule['name'])}}}}}
        query.update(sort)

        try:
            doc_type = 'elastalert_status'
            index = self.writeback_es.resolve_writeback_index(self.writeback_index, doc_type)
            res = self.writeback_es.search(index=index, size=1, body=query,
                                           _source_includes=['endtime', 'rule_name'])
            if res['hits']['hits']:
                endtime = ts_to_dt(res['hits']['hits'][0]['_source']['endtime'])

                if ts_now() - endtime < self.old_query_limit:
                    return endtime
                else:
                    elastalert_logger.info("Found expired previous run for %s at %s" % (rule['name'], endtime))
                    return None
        except (ElasticsearchException, KeyError) as e:
            self.handle_error('Error querying for last run: %s' % (e), {'rule': rule['name']})

    def set_starttime(self, rule, endtime):
        """ Given a rule and an endtime, sets the appropriate starttime for it. """
        # This means we are starting fresh
        if 'starttime' not in rule:
            if not rule.get('scan_entire_timeframe'):
                # Try to get the last run from Elasticsearch
                last_run_end = self.get_starttime(rule)
                if last_run_end:
                    rule['starttime'] = last_run_end
                    self.adjust_start_time_for_overlapping_agg_query(rule)
                    self.adjust_start_time_for_interval_sync(rule, endtime)
                    rule['minimum_starttime'] = rule['starttime']
                    return None

        # Use buffer for normal queries, or run_every increments otherwise
        # or, if scan_entire_timeframe, use timeframe

        if not rule.get('use_count_query') and not rule.get('use_terms_query'):
            if not rule.get('scan_entire_timeframe'):
                buffer_time = rule.get('buffer_time', self.buffer_time)
                buffer_delta = endtime - buffer_time
            else:
                buffer_delta = endtime - rule['timeframe']
            # If we started using a previous run, don't go past that
            if 'minimum_starttime' in rule and rule['minimum_starttime'] > buffer_delta:
                rule['starttime'] = rule['minimum_starttime']
            # If buffer_time doesn't bring us past the previous endtime, use that instead
            elif 'previous_endtime' in rule and rule['previous_endtime'] < buffer_delta:
                rule['starttime'] = rule['previous_endtime']
                self.adjust_start_time_for_overlapping_agg_query(rule)
            else:
                rule['starttime'] = buffer_delta

            self.adjust_start_time_for_interval_sync(rule, endtime)

        else:
            if not rule.get('scan_entire_timeframe'):
                # Query from the end of the last run, if it exists, otherwise a run_every sized window
                rule['starttime'] = rule.get('previous_endtime', endtime - self.run_every)
            else:
                #Based on PR 3141 old Yelp/elastalert - rschirin
                rule['starttime'] = endtime - rule['timeframe']

    def adjust_start_time_for_overlapping_agg_query(self, rule):
        if rule.get('aggregation_query_element'):
            if rule.get('allow_buffer_time_overlap') and not rule.get('use_run_every_query_size') and (
                    rule['buffer_time'] > rule['run_every']):
                rule['starttime'] = rule['starttime'] - (rule['buffer_time'] - rule['run_every'])
                rule['original_starttime'] = rule['starttime']

    def adjust_start_time_for_interval_sync(self, rule, endtime):
        # If aggregation query adjust bucket offset
        if rule.get('aggregation_query_element'):

            if rule.get('bucket_interval'):
                es_interval_delta = rule.get('bucket_interval_timedelta')
                unix_starttime = dt_to_unix(rule['starttime'])
                es_interval_delta_in_sec = total_seconds(es_interval_delta)
                offset = int(unix_starttime % es_interval_delta_in_sec)

                if rule.get('sync_bucket_interval'):
                    rule['starttime'] = unix_to_dt(unix_starttime - offset)
                    endtime = unix_to_dt(dt_to_unix(endtime) - offset)
                else:
                    rule['bucket_offset_delta'] = offset

    def get_segment_size(self, rule):
        """ The segment size is either buffer_size for queries which can overlap or run_every for queries
        which must be strictly separate. This mimicks the query size for when ElastAlert is running continuously. """
        if not rule.get('use_count_query') and not rule.get('use_terms_query') and not rule.get('aggregation_query_element'):
            return rule.get('buffer_time', self.buffer_time)
        elif rule.get('aggregation_query_element'):
            if rule.get('use_run_every_query_size'):
                return self.run_every
            else:
                return rule.get('buffer_time', self.buffer_time)
        else:
            return self.run_every

    def get_query_key_value(self, rule, match):
        # get the value for the match's query_key (or none) to form the key used for the silence_cache.
        # Flatline ruletype sets "key" instead of the actual query_key
        if isinstance(rule['type'], FlatlineRule) and 'key' in match:
            return str(match['key'])
        return self.get_named_key_value(rule, match, 'query_key')

    def get_aggregation_key_value(self, rule, match):
        # get the value for the match's aggregation_key (or none) to form the key used for grouped aggregates.
        return self.get_named_key_value(rule, match, 'aggregation_key')

    def get_named_key_value(self, rule, match, key_name):
        # search the match for the key specified in the rule to get the value
        if key_name in rule:
            try:
                key_value = lookup_es_key(match, rule[key_name])
                if key_value is not None:
                    # Only do the unicode conversion if we actually found something)
                    # otherwise we might transform None --> 'None'
                    key_value = str(key_value)
            except KeyError:
                # Some matches may not have the specified key
                # use a special token for these
                key_value = '_missing'
        else:
            key_value = None

        return key_value

    def enhance_filter(self, rule):
        """ If there is a blacklist or whitelist in rule then we add it to the filter.
        It adds it as a query_string. If there is already an query string its is appended
        with blacklist or whitelist.

        :param rule:
        :return:
        """
        if not rule.get('filter_by_list', True):
            return
        if 'blacklist' in rule:
            listname = 'blacklist'
        elif 'whitelist' in rule:
            listname = 'whitelist'
        else:
            return

        filters = rule['filter']
        additional_terms = []
        for term in rule[listname]:
            if not term.startswith('/') or not term.endswith('/'):
                additional_terms.append(rule['compare_key'] + ':"' + term + '"')
            else:
                # These are regular expressions and won't work if they are quoted
                additional_terms.append(rule['compare_key'] + ':' + term)
        if listname == 'whitelist':
            query = "NOT " + " AND NOT ".join(additional_terms)
        else:
            query = " OR ".join(additional_terms)
        query_str_filter = {'query_string': {'query': query}}
        filters.append(query_str_filter)
        elastalert_logger.debug("Enhanced filter with {} terms: {}".format(listname, str(query_str_filter)))

    def get_elasticsearch_client(self, rule):
        key = rule['name']
        es_client = self.es_clients.get(key)
        if es_client is None:
            es_client = elasticsearch_client(rule)
            self.es_clients[key] = es_client
        return es_client

    def run_rule(self, rule, endtime, starttime=None):
        """ Run a rule for a given time period, including querying and alerting on results.

        :param rule: The rule configuration.
        :param starttime: The earliest timestamp to query.
        :param endtime: The latest timestamp to query.
        :return: The number of matches that the rule produced.
        """
        run_start = time.time()
        self.thread_data.current_es = self.get_elasticsearch_client(rule)

        # If there are pending aggregate matches, try processing them
        for x in range(len(rule['agg_matches'])):
            match = rule['agg_matches'].pop()
            self.add_aggregated_alert(match, rule)

        # Start from provided time if it's given
        if starttime:
            rule['starttime'] = starttime
        else:
            self.set_starttime(rule, endtime)

        rule['original_starttime'] = rule['starttime']
        rule['scrolling_cycle'] = 0

        self.thread_data.num_hits = 0
        self.thread_data.num_dupes = 0
        self.thread_data.cumulative_hits = 0

        # Don't run if starttime was set to the future
        if ts_now() <= rule['starttime']:
            elastalert_logger.warning("Attempted to use query start time in the future (%s), sleeping instead" % (starttime))
            return 0

        # Run the rule. If querying over a large time period, split it up into segments
        segment_size = self.get_segment_size(rule)

        tmp_endtime = rule['starttime']

        while endtime - rule['starttime'] > segment_size:
            tmp_endtime = tmp_endtime + segment_size
            if not self.run_query(rule, rule['starttime'], tmp_endtime):
                return 0
            self.thread_data.cumulative_hits += self.thread_data.num_hits
            self.thread_data.num_hits = 0
            rule['starttime'] = tmp_endtime
            rule['type'].garbage_collect(tmp_endtime)

        if rule.get('aggregation_query_element'):
            if endtime - tmp_endtime == segment_size:
                if not self.run_query(rule, tmp_endtime, endtime):
                    return 0
                self.thread_data.cumulative_hits += self.thread_data.num_hits
            elif total_seconds(rule['original_starttime'] - tmp_endtime) == 0:
                rule['starttime'] = rule['original_starttime']
                return 0
            else:
                endtime = tmp_endtime
        else:
            if not self.run_query(rule, rule['starttime'], endtime):
                return 0
            self.thread_data.cumulative_hits += self.thread_data.num_hits
            rule['type'].garbage_collect(endtime)

        # Process any new matches
        num_matches = len(rule['type'].matches)
        while rule['type'].matches:
            match = rule['type'].matches.pop(0)
            match['num_hits'] = self.thread_data.cumulative_hits
            match['num_matches'] = num_matches

            # If realert is set, silence the rule for that duration
            # Silence is cached by query_key, if it exists
            # Default realert time is 0 seconds
            silence_cache_key = rule['realert_key']
            query_key_value = self.get_query_key_value(rule, match)
            if query_key_value is not None:
                silence_cache_key += '.' + query_key_value

            if self.is_silenced(rule['name'] + "._silence") or self.is_silenced(silence_cache_key):
                elastalert_logger.info('Ignoring match for silenced rule %s' % (silence_cache_key,))
                continue

            if rule['realert']:
                next_alert, exponent = self.next_alert_time(rule, silence_cache_key, ts_now())
                self.set_realert(silence_cache_key, next_alert, exponent)

            if rule.get('run_enhancements_first'):
                try:
                    for enhancement in rule['match_enhancements']:
                        try:
                            enhancement.process(match)
                        except EAException as e:
                            self.handle_error("Error running match enhancement: %s" % (e), {'rule': rule['name']})
                except DropMatchException:
                    continue

            # If no aggregation, alert immediately
            if not rule['aggregation']:
                self.alert([match], rule)
                continue

            # Add it as an aggregated match
            self.add_aggregated_alert(match, rule)

        # Mark this endtime for next run's start
        rule['previous_endtime'] = endtime

        time_taken = time.time() - run_start

        # Write to ES that we've run this rule against this time period
        body = {'rule_name': rule['name'],
                'endtime': endtime,
                'starttime': rule['original_starttime'],
                'matches': num_matches,
                'hits': max(self.thread_data.num_hits, self.thread_data.cumulative_hits),
                '@timestamp': ts_now(),
                'time_taken': time_taken}
        self.writeback('elastalert_status', body)

        # Write metrics about the run to statsd
        if self.statsd:
            try:
                self.statsd.gauge(
                    'rule.time_taken', time_taken,
                    tags={"elastalert_instance": self.statsd_instance_tag, "rule_name": rule['name']})
                self.statsd.gauge(
                    'query.hits', self.thread_data.num_hits,
                    tags={"elastalert_instance": self.statsd_instance_tag, "rule_name": rule['name']})
                self.statsd.gauge(
                    'already_seen.hits', self.thread_data.num_dupes,
                    tags={"elastalert_instance": self.statsd_instance_tag, "rule_name": rule['name']})
                self.statsd.gauge(
                    'query.matches', num_matches,
                    tags={"elastalert_instance": self.statsd_instance_tag, "rule_name": rule['name']})
                self.statsd.gauge(
                    'query.alerts_sent', self.thread_data.alerts_sent,
                    tags={"elastalert_instance": self.statsd_instance_tag, "rule_name": rule['name']})
            except BaseException as e:
                elastalert_logger.error("unable to send metrics:\n%s" % str(e))

        return num_matches

    def init_rule(self, new_rule, new=True):
        ''' Copies some necessary non-config state from an exiting rule to a new rule. '''
        if not new and self.scheduler.get_job(job_id=new_rule['name']):
            self.scheduler.remove_job(job_id=new_rule['name'])

        self.enhance_filter(new_rule)

        # Change top_count_keys to .raw
        if 'top_count_keys' in new_rule and new_rule.get('raw_count_keys', True):
            if self.string_multi_field_name:
                string_multi_field_name = self.string_multi_field_name
            else:
                string_multi_field_name = '.keyword'

            for i, key in enumerate(new_rule['top_count_keys']):
                if not key.endswith(string_multi_field_name):
                    new_rule['top_count_keys'][i] += string_multi_field_name

        blank_rule = {'agg_matches': [],
                      'aggregate_alert_time': {},
                      'current_aggregate_id': {},
                      'processed_hits': {},
                      'run_every': self.run_every,
                      'has_run_once': False}
        rule = blank_rule

        # Set rule to either a blank template or existing rule with same name
        if not new:
            for rule in self.rules:
                if rule['name'] == new_rule['name']:
                    break
            else:
                rule = blank_rule

        copy_properties = ['agg_matches',
                           'current_aggregate_id',
                           'aggregate_alert_time',
                           'processed_hits',
                           'starttime',
                           'minimum_starttime',
                           'has_run_once']
        for prop in copy_properties:
            if prop not in rule:
                continue
            new_rule[prop] = rule[prop]

        job = self.scheduler.add_job(self.handle_rule_execution, 'interval',
                                     args=[new_rule],
                                     seconds=new_rule['run_every'].total_seconds(),
                                     id=new_rule['name'],
                                     name="Rule: %s" % (new_rule['name']),
                                     max_instances=1,
                                     jitter=5)
        job.modify(next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=random.randint(0, 15)))

        return new_rule

    def load_rule_changes(self):
        """ Using the modification times of rule config files, syncs the running rules
            to match the files in rules_folder by removing, adding or reloading rules. """
        new_rule_hashes = self.rules_loader.get_hashes(self.conf, self.args.rule)

        # Check each current rule for changes
        for rule_file, hash_value in self.rule_hashes.items():
            if rule_file not in new_rule_hashes:
                # Rule file was deleted
                elastalert_logger.info('Rule file %s not found, stopping rule execution' % (rule_file))
                for rule in self.rules:
                    if rule['rule_file'] == rule_file:
                        break
                else:
                    continue
                self.scheduler.remove_job(job_id=rule['name'])
                # Clean up memory for deleted rule
                self.cleanup_rule_memory(rule)
                self.rules.remove(rule)
                continue
            if hash_value != new_rule_hashes[rule_file]:
                # Rule file was changed, reload rule
                try:
                    new_rule = self.rules_loader.load_configuration(rule_file, self.conf)
                    if not new_rule:
                        elastalert_logger.error('Invalid rule file skipped: %s' % rule_file)
                        continue
                    if 'is_enabled' in new_rule and not new_rule['is_enabled']:
                        elastalert_logger.info('Rule file %s is now disabled.' % (rule_file))
                        # Find and clean up the existing rule
                        existing_rule = None
                        for rule in self.rules:
                            if rule['rule_file'] == rule_file:
                                existing_rule = rule
                                break
                        
                        # Remove this rule if it's been disabled
                        self.rules = [rule for rule in self.rules if rule['rule_file'] != rule_file]
                        # Stop job if is running
                        if self.scheduler.get_job(job_id=new_rule['name']):
                            self.scheduler.remove_job(job_id=new_rule['name'])
                        # Clean up memory for disabled rule
                        if existing_rule:
                            self.cleanup_rule_memory(existing_rule)
                        # Append to disabled_rule
                        for disabled_rule in self.disabled_rules:
                            if disabled_rule['name'] == new_rule['name']:
                                break
                        else:
                            self.disabled_rules.append(new_rule)
                        continue
                except EAException as e:
                    message = 'Could not load rule %s: %s' % (rule_file, e)
                    self.handle_error(message)
                    # Want to send email to address specified in the rule. Try and load the YAML to find it.
                    try:
                        rule_yaml = self.rules_loader.load_yaml(rule_file)
                    except EAException:
                        self.send_notification_email(exception=e)
                        continue

                    self.send_notification_email(exception=e, rule=rule_yaml)
                    continue
                elastalert_logger.info("Reloading configuration for rule %s" % (rule_file))

                # Re-enable if rule had been disabled
                for disabled_rule in self.disabled_rules:
                    if disabled_rule['name'] == new_rule['name']:
                        self.disabled_rules.remove(disabled_rule)
                        break

                # Initialize the rule that matches rule_file
                new_rule = self.init_rule(new_rule, False)
                self.rules = [rule for rule in self.rules if rule['rule_file'] != rule_file]
                if new_rule:
                    self.rules.append(new_rule)

        # Load new rules
        if not self.args.rule:
            for rule_file in set(new_rule_hashes.keys()) - set(self.rule_hashes.keys()):
                try:
                    new_rule = self.rules_loader.load_configuration(rule_file, self.conf)
                    if not new_rule:
                        elastalert_logger.error('Invalid rule file skipped: %s' % rule_file)
                        continue
                    if 'is_enabled' in new_rule and not new_rule['is_enabled']:
                        continue
                    if new_rule['name'] in [rule['name'] for rule in self.rules]:
                        raise EAException("A rule with the name %s already exists" % (new_rule['name']))
                except EAException as e:
                    self.handle_error('Could not load rule %s: %s' % (rule_file, e))
                    self.send_notification_email(exception=e, rule_file=rule_file)
                    continue
                if self.init_rule(new_rule):
                    elastalert_logger.info('Loaded new rule %s' % (rule_file))
                    if new_rule['name'] in self.es_clients:
                        self.es_clients.pop(new_rule['name'])
                    self.rules.append(new_rule)

        self.rule_hashes = new_rule_hashes

    def start(self):
        """ Periodically go through each rule and run it """
        if self.starttime:
            if self.starttime == 'NOW':
                self.starttime = ts_now()
            else:
                try:
                    self.starttime = ts_to_dt(self.starttime)
                except (TypeError, ValueError):
                    self.handle_error("%s is not a valid ISO8601 timestamp (YYYY-MM-DDTHH:MM:SS+XX:00)" % (self.starttime))
                    exit(1)

        for rule in self.rules:
            rule['initial_starttime'] = self.starttime
        self.wait_until_responsive(timeout=self.args.timeout)
        self.running = True
        elastalert_logger.info("Starting up")
        self.scheduler.add_job(self.handle_pending_alerts, 'interval',
                               seconds=self.run_every.total_seconds(),
                               id='_internal_handle_pending_alerts',
                               name='Internal: Handle Pending Alerts')
        self.scheduler.add_job(self.handle_config_change, 'interval',
                               seconds=self.run_every.total_seconds(),
                               id='_internal_handle_config_change',
                               name='Internal: Handle Config Change')
        # Add memory cleanup job to run every 10 minutes
        self.scheduler.add_job(self.cleanup_memory_caches, 'interval',
                               seconds=600,  # 10 minutes
                               id='_internal_memory_cleanup',
                               name='Internal: Memory Cache Cleanup')
        self.scheduler.start()
        while self.running:
            next_run = datetime.datetime.now(tz=datetime.UTC) + self.run_every

            # Quit after end_time has been reached
            if self.args.end:
                endtime = ts_to_dt(self.args.end)

                next_run_dt = next_run.replace(tzinfo=datetime.timezone.utc)
                if next_run_dt > endtime:
                    elastalert_logger.info("End time '%s' falls before the next run time '%s', exiting." % (endtime, next_run_dt))
                    exit(0)

            if next_run < datetime.datetime.now(tz=datetime.UTC):
                continue

            # Show disabled rules
            if self.show_disabled_rules:
                elastalert_logger.info("Disabled rules are: %s" % (str(self.get_disabled_rules())))

            # Wait before querying again
            sleep_duration = total_seconds(next_run - datetime.datetime.now(tz=datetime.UTC))
            self.sleep_for(sleep_duration)

    def wait_until_responsive(self, timeout, clock=timeit.default_timer):
        """Wait until ElasticSearch becomes responsive (or too much time passes)."""

        # Elapsed time is a floating point number of seconds.
        timeout = timeout.total_seconds()

        # Don't poll unless we're asked to.
        if timeout <= 0.0:
            return

        # Periodically poll ElasticSearch.  Keep going until ElasticSearch is
        # responsive *and* the writeback index exists.
        ref = clock()
        while (clock() - ref) < timeout:
            try:
                if self.writeback_es.indices.exists(self.writeback_index):
                    return
            except ConnectionError:
                pass
            time.sleep(1.0)

        if self.writeback_es.ping():
            elastalert_logger.error(
                'Writeback index "%s" does not exist, did you run `elastalert-create-index`?',
                self.writeback_index,
            )
        else:
            elastalert_logger.error(
                'Could not reach ElasticSearch at "%s:%d".',
                self.conf['es_host'],
                self.conf['es_port'],
            )
        exit(1)

    def run_all_rules(self):
        """ Run each rule one time """
        self.handle_pending_alerts()

        for rule in self.rules:
            self.handle_rule_execution(rule)

        self.handle_config_change()

    def handle_pending_alerts(self):
        self.thread_data.alerts_sent = 0
        self.send_pending_alerts()
        # Perform periodic memory cleanup
        self.cleanup_silence_cache()
        self.cleanup_es_clients_cache()
        elastalert_logger.info("Background alerts thread %s pending alerts sent at %s" % (
            self.thread_data.alerts_sent, pretty_ts(ts_now(), ts_format=self.pretty_ts_format)))

    def handle_config_change(self):
        if not self.args.pin_rules:
            self.load_rule_changes()
            elastalert_logger.info(
                "Background configuration change check run at %s" % (pretty_ts(ts_now(), ts_format=self.pretty_ts_format)))

    def handle_rule_execution(self, rule):
        self.thread_data.alerts_sent = 0
        next_run = datetime.datetime.now(tz=datetime.UTC) + rule['run_every']
        # Set endtime based on the rule's delay
        delay = rule.get('query_delay')
        if hasattr(self.args, 'end') and self.args.end:
            endtime = ts_to_dt(self.args.end)
        elif delay:
            try:
                endtime = ts_now() - delay
            except Exception as e:
                self.handle_error("[handle_rule_execution]Error parsing query_delay send time format %s" % e)
        else:
            endtime = ts_now()

        # Apply rules based on execution time limits
        if rule.get('limit_execution'):
            rule['next_starttime'] = None
            rule['next_min_starttime'] = None
            exec_next = next(croniter(rule['limit_execution']))
            endtime_epoch = dt_to_unix(endtime)
            # If the estimated next endtime (end + run_every) isn't at least a minute past the next exec time
            # That means that we need to pause execution after this run
            if endtime_epoch + rule['run_every'].total_seconds() < exec_next - 59:
                # apscheduler requires pytz tzinfos, so don't use unix_to_dt here!
                rule['next_starttime'] = datetime.datetime.fromtimestamp(exec_next, tz=datetime.UTC).replace(tzinfo=pytz.utc)
                if rule.get('limit_execution_coverage'):
                    rule['next_min_starttime'] = rule['next_starttime']
                if not rule['has_run_once']:
                    self.reset_rule_schedule(rule)
                    return

        rule['has_run_once'] = True
        try:
            num_matches = self.run_rule(rule, endtime, rule.get('initial_starttime'))
        except EAException as e:
            self.handle_error("Error running rule %s: %s" % (rule['name'], e), {'rule': rule['name']})
        except Exception as e:
            self.handle_uncaught_exception(e, rule)
        else:
            old_starttime = pretty_ts(rule.get('original_starttime'), rule.get('use_local_time'), self.pretty_ts_format)
            elastalert_logger.info("Ran %s from %s to %s: %s query hits (%s already seen), %s matches,"
                                   " %s alerts sent" % (rule['name'], old_starttime,
                                                        pretty_ts(endtime, rule.get('use_local_time'),
                                                                  self.pretty_ts_format),
                                                        self.thread_data.num_hits, self.thread_data.num_dupes,
                                                        num_matches,
                                                        self.thread_data.alerts_sent))
            rule_duration = seconds(endtime - rule.get('original_starttime'))
            elastalert_logger.info("%s range %s" % (rule['name'], rule_duration))

            self.thread_data.alerts_sent = 0

            if next_run < datetime.datetime.now(tz=datetime.UTC):
                # We were processing for longer than our refresh interval
                # This can happen if --start was specified with a large time period
                # or if we are running too slow to process events in real time.
                elastalert_logger.warning(
                    "Querying from %s to %s took longer than %s!" % (
                        old_starttime,
                        pretty_ts(endtime, rule.get('use_local_time'), self.pretty_ts_format),
                        self.run_every
                    )
                )

        rule['initial_starttime'] = None

        self.remove_old_events(rule)

        self.reset_rule_schedule(rule)

    def reset_rule_schedule(self, rule):
        # We hit the end of a execution schedule, pause ourselves until next run
        if rule.get('limit_execution') and rule['next_starttime']:
            self.scheduler.modify_job(job_id=rule['name'], next_run_time=rule['next_starttime'])
            # If we are preventing covering non-scheduled time periods, reset min_starttime and previous_endtime
            if rule['next_min_starttime']:
                rule['minimum_starttime'] = rule['next_min_starttime']
                rule['previous_endtime'] = rule['next_min_starttime']
            elastalert_logger.info('Pausing %s until next run at %s' % (
            rule['name'], pretty_ts(rule['next_starttime'], ts_format=self.pretty_ts_format)))

    def stop(self):
        """ Stop an ElastAlert runner that's been started """
        self.running = False

    def get_disabled_rules(self):
        """ Return disabled rules """
        return [rule['name'] for rule in self.disabled_rules]

    def sleep_for(self, duration):
        """ Sleep for a set duration """
        elastalert_logger.info("Sleeping for %s seconds" % (duration))
        time.sleep(duration)

    def alert(self, matches, rule, alert_time=None, retried=False):
        """ Wraps alerting, Kibana linking and enhancements in an exception handler """
        try:
            return self.send_alert(matches, rule, alert_time=alert_time, retried=retried)
        except Exception as e:
            self.handle_uncaught_exception(e, rule)

    def include_rule_params_in_matches(self, matches, rule):
        if len(rule.get('include_rule_params_in_matches',[])) > 0:
            tmp_matches = matches
            if rule.get('include_rule_params_in_first_match_only', False):
                tmp_matches = [matches[0]]
            for match in tmp_matches:
                for param in rule.get('include_rule_params_in_matches'):
                    match['rule_param_' + param] = rule.get(param)

    def send_alert(self, matches, rule, alert_time=None, retried=False):
        """ Send out an alert.

        :param matches: A list of matches.
        :param rule: A rule configuration.
        """
        if not matches:
            return

        if alert_time is None:
            alert_time = ts_now()

        # Compute top count keys
        if rule.get('top_count_keys'):
            for match in matches:
                if 'query_key' in rule:
                    qk = lookup_es_key(match, rule['query_key'])
                else:
                    qk = None

                if isinstance(rule['type'], FlatlineRule):
                    # flatline rule triggers when there have been no events from now()-timeframe to now(),
                    # so using now()-timeframe will return no results. for now we can just mutliple the timeframe
                    # by 2, but this could probably be timeframe+run_every to prevent too large of a lookup?
                    timeframe = datetime.timedelta(seconds=2 * rule.get('timeframe').total_seconds())
                else:
                    timeframe = rule.get('timeframe', datetime.timedelta(minutes=10))

                start = ts_to_dt(lookup_es_key(match, rule['timestamp_field'])) - timeframe
                end = ts_to_dt(lookup_es_key(match, rule['timestamp_field'])) + datetime.timedelta(minutes=10)
                keys = rule.get('top_count_keys')
                counts = self.get_top_counts(rule, start, end, keys, qk=qk)
                match.update(counts)

        if rule.get('generate_kibana_discover_url'):
            kb_link = generate_kibana_discover_url(rule, matches[0])
            if kb_link:
                kb_link_formatter = self.get_kibana_discover_external_url_formatter(rule)
                matches[0]['kibana_discover_url'] =  kb_link_formatter.format(kb_link)

        if rule.get('generate_opensearch_discover_url'):
            opsh_link = generate_opensearch_discover_url(rule, matches[0])
            if opsh_link:
                opsh_link_formatter = self.get_opensearch_discover_external_url_formatter(rule)
                matches[0]['opensearch_discover_url'] =  opsh_link_formatter.format(opsh_link)

        self.include_rule_params_in_matches(matches, rule)
        
        # Enhancements were already run at match time if
        # run_enhancements_first is set or
        # retried==True, which means this is a retry of a failed alert
        if not rule.get('run_enhancements_first') and not retried:
            for enhancement in rule['match_enhancements']:
                valid_matches = []
                for match in matches:
                    try:
                        enhancement.process(match)
                        valid_matches.append(match)
                    except DropMatchException:
                        pass
                    except EAException as e:
                        self.handle_error("Error running match enhancement: %s" % (e), {'rule': rule['name']})
                matches = valid_matches
                if not matches:
                    return None

        # Don't send real alerts in debug mode
        if self.debug:
            alerter = DebugAlerter(rule)
            alerter.alert(matches)
            return None

        # Run the alerts
        alert_sent = False
        alert_exception = None
        # Alert.pipeline is a single object shared between every alerter
        # This allows alerters to pass objects and data between themselves
        alert_pipeline = {"alert_time": alert_time}
        for alert in rule['alert']:
            alert.pipeline = alert_pipeline
            try:
                alert.alert(matches)
            except EAException as e:
                self.handle_error('Error while running alert %s: %s' % (alert.get_info()['type'], e), {'rule': rule['name']})
                alert_exception = str(e)
            else:
                self.thread_data.alerts_sent += 1
                alert_sent = True

        # Write the alert(s) to ES
        agg_id = None
        for match in matches:
            alert_body = self.get_alert_body(match, rule, alert_sent, alert_time, alert_exception)
            # Set all matches to aggregate together
            if agg_id:
                alert_body['aggregate_id'] = agg_id
            res = self.writeback('elastalert', alert_body, rule)
            if res and not agg_id:
                agg_id = res['_id']

    def get_alert_body(self, match, rule, alert_sent, alert_time, alert_exception=None):
        body = {
            'match_body': match,
            'rule_name': rule['name'],
            'alert_info': rule['alert'][0].get_info() if not self.debug else {},
            'alert_sent': alert_sent,
            'alert_time': alert_time
        }

        if rule.get('include_match_in_root'):
            body.update({k: v for k, v in match.items() if not k.startswith('_')})

        if self.add_metadata_alert:
            body['category'] = rule['category']
            body['description'] = rule['description']
            body['owner'] = rule['owner']
            body['priority'] = rule['priority']

        match_time = lookup_es_key(match, rule['timestamp_field'])
        if match_time is not None:
            body['match_time'] = match_time

        # TODO record info about multiple alerts

        # If the alert failed to send, record the exception
        if not alert_sent:
            body['alert_exception'] = alert_exception
        return body

    def get_kibana_discover_external_url_formatter(self, rule):
        """ Gets or create the external url formatter for kibana discover links """
        key = '__kibana_discover_external_url_formatter__'
        formatter = rule.get(key)
        if formatter is None:
            shorten = rule.get('shorten_kibana_discover_url')
            security_tenant = rule.get('kibana_discover_security_tenant')
            formatter = create_kibana_external_url_formatter(rule, shorten, security_tenant)
            rule[key] = formatter
        return formatter


    def get_opensearch_discover_external_url_formatter(self, rule):
        """ Gets or create the external url formatter for Opensearch discover links """
        key = '__opensearch_discover_external_url_formatter__'
        formatter = rule.get(key)
        if formatter is None:
            formatter = create_opensearch_external_url_formatter(rule)
            rule[key] = formatter
        return formatter

    def writeback(self, doc_type, body, rule=None, match_body=None):
        # ES 2.0 - 2.3 does not support dots in field names.
        if self.replace_dots_in_field_names:
            writeback_body = replace_dots_in_field_names(body)
        else:
            writeback_body = body

        for key in list(writeback_body.keys()):
            # Convert any datetime objects to timestamps
            if isinstance(writeback_body[key], datetime.datetime):
                writeback_body[key] = dt_to_ts(writeback_body[key])

        if self.debug:
            elastalert_logger.info("Skipping writing to ES: %s" % (writeback_body))
            return None

        if '@timestamp' not in writeback_body:
            writeback_body['@timestamp'] = dt_to_ts(ts_now())

        try:
            index = self.writeback_es.resolve_writeback_index(self.writeback_index, doc_type)
            res = self.writeback_es.index(index=index, body=body)
            return res
        except ElasticsearchException as e:
            elastalert_logger.exception("Error writing alert info to Elasticsearch: %s" % (e))
            if self.notify_all_errors:
                self.handle_notify_error(e, None)

    def find_recent_pending_alerts(self, time_limit):
        """ Queries writeback_es to find alerts that did not send
        and are newer than time_limit """

        # XXX only fetches 1000 results. If limit is reached, next loop will catch them
        # unless there is constantly more than 1000 alerts to send.

        # Fetch recent, unsent alerts that aren't part of an aggregate, earlier alerts first.
        inner_query = {'query_string': {'query': '!_exists_:aggregate_id AND alert_sent:false'}}
        time_filter = {'range': {'alert_time': {'gte': dt_to_ts(ts_now() - time_limit),
                                                'lte': dt_to_ts(ts_now())}}}
        sort = {'sort': {'alert_time': {'order': 'asc'}}}
        query = {'query': {'bool': {'must': inner_query, 'filter': time_filter}}}
        query.update(sort)
        try:
            res = self.writeback_es.search(index=self.writeback_index, body=query, size=1000)
            if res['hits']['hits']:
                return res['hits']['hits']
        except ElasticsearchException as e:
            elastalert_logger.exception("Error finding recent pending alerts: %s %s" % (e, query))
            if self.notify_all_errors:
                self.handle_notify_error(e, None)
        return []

    def send_pending_alerts(self):
        with self.alert_lock:
            pending_alerts = self.find_recent_pending_alerts(self.alert_time_limit)
            for alert in pending_alerts:
                _id = alert['_id']
                alert = alert['_source']
                try:
                    rule_name = alert.pop('rule_name')
                    alert_time = alert.pop('alert_time')
                    match_body = alert.pop('match_body')
                except KeyError:
                    # Malformed alert, drop it
                    continue

                # Find original rule
                for rule in self.rules:
                    if rule['name'] == rule_name:
                        break
                else:
                    # Original rule is missing, keep alert for later if rule reappears
                    continue

                # Set current_es for top_count_keys query
                self.thread_data.current_es = elasticsearch_client(rule)

                # Send the alert unless it's a future alert
                if ts_now() > ts_to_dt(alert_time):
                    aggregated_matches = self.get_aggregated_matches(_id)
                    if aggregated_matches:
                        matches = [match_body] + [agg_match['match_body'] for agg_match in aggregated_matches]
                        self.alert(matches, rule, alert_time=alert_time)
                    else:
                        # If this rule isn't using aggregation, this must be a retry of a failed alert
                        retried = False
                        if not rule.get('aggregation'):
                            retried = True
                        self.alert([match_body], rule, alert_time=alert_time, retried=retried)

                    if rule['current_aggregate_id']:
                        for qk, agg_id in rule['current_aggregate_id'].items():
                            if agg_id == _id:
                                rule['current_aggregate_id'].pop(qk)
                                break

                    # Delete it from the index
                    try:
                        self.writeback_es.delete(index=self.writeback_index, id=_id)
                    except ElasticsearchException:  # TODO: Give this a more relevant exception, try:except: is evil.
                        self.handle_error("Failed to delete alert %s at %s" % (_id, alert_time))

            # Send in memory aggregated alerts
            for rule in self.rules:
                if rule['agg_matches']:
                    for aggregation_key_value, aggregate_alert_time in rule['aggregate_alert_time'].items():
                        if ts_now() > aggregate_alert_time:
                            alertable_matches = [
                                agg_match
                                for agg_match
                                in rule['agg_matches']
                                if self.get_aggregation_key_value(rule, agg_match) == aggregation_key_value
                            ]
                            self.alert(alertable_matches, rule)
                            rule['agg_matches'] = [
                                agg_match
                                for agg_match
                                in rule['agg_matches']
                                if self.get_aggregation_key_value(rule, agg_match) != aggregation_key_value
                            ]

    def get_aggregated_matches(self, _id):
        """ Removes and returns all matches from writeback_es that have aggregate_id == _id """

        # XXX if there are more than self.max_aggregation matches, you have big alerts and we will leave entries in ES.
        query = {'query': {'query_string': {'query': 'aggregate_id:"%s"' % (_id)}}, 'sort': {'@timestamp': 'asc'}}
        matches = []
        try:
            res = self.writeback_es.search(index=self.writeback_index, body=query,
                                           size=self.max_aggregation)
            for match in res['hits']['hits']:
                matches.append(match['_source'])
                self.writeback_es.delete(index=self.writeback_index, id=match['_id'])
        except (KeyError, ElasticsearchException) as e:
            self.handle_error("Error fetching aggregated matches: %s" % (e), {'id': _id})
        return matches

    def find_pending_aggregate_alert(self, rule, aggregation_key_value=None):
        query = {'filter': {'bool': {'must': [{'term': {'rule_name': rule['name']}},
                                              {'range': {'alert_time': {'gt': ts_now()}}},
                                              {'term': {'alert_sent': 'false'}}],
                                     'must_not': [{'exists': {'field': 'aggregate_id'}}]}}}
        if aggregation_key_value:
            query['filter']['bool']['must'].append({'term': {'aggregation_key': aggregation_key_value}})
        query = {'query': {'bool': query}}
        query['sort'] = {'alert_time': {'order': 'desc'}}
        try:
            res = self.writeback_es.search(index=self.writeback_index, body=query, size=1)
            if len(res['hits']['hits']) == 0:
                return None
        except (KeyError, ElasticsearchException) as e:
            self.handle_error("Error searching for pending aggregated matches: %s" % (e), {'rule_name': rule['name']})
            return None

        return res['hits']['hits'][0]

    def add_aggregated_alert(self, match, rule):
        """ Save a match as a pending aggregate alert to Elasticsearch. """

        with self.alert_lock:
            # Optionally include the 'aggregation_key' as a dimension for aggregations
            aggregation_key_value = self.get_aggregation_key_value(rule, match)

            # This is a fallback option in case this change to using ts_now() interferes with the behavior current
            # users are accustomed to. It is not documented because it likely won't be needed. If no one reports 
            # a problem we can remove this fallback option in a future release.
            if rule.get('aggregation_alert_time_compared_with_timestamp_field', False):
                compare_dt = lookup_es_key(match, rule['timestamp_field'])
            else:
                compare_dt = ts_now()

            if (not rule['current_aggregate_id'].get(aggregation_key_value) or
                    ('aggregate_alert_time' in rule and aggregation_key_value in rule['aggregate_alert_time'] and rule[
                        'aggregate_alert_time'].get(aggregation_key_value) < ts_to_dt(compare_dt))):

                # ElastAlert may have restarted while pending alerts exist
                pending_alert = self.find_pending_aggregate_alert(rule, aggregation_key_value)
                if pending_alert:
                    alert_time = ts_to_dt(pending_alert['_source']['alert_time'])
                    rule['aggregate_alert_time'][aggregation_key_value] = alert_time
                    agg_id = pending_alert['_id']
                    rule['current_aggregate_id'] = {aggregation_key_value: agg_id}
                    elastalert_logger.info(
                        'Adding alert for %s to aggregation(id: %s, aggregation_key: %s), next alert at %s' % (
                            rule['name'],
                            agg_id,
                            aggregation_key_value,
                            alert_time
                        )
                    )
                else:
                    # First match, set alert_time
                    alert_time = ''
                    if isinstance(rule['aggregation'], dict) and rule['aggregation'].get('schedule'):
                        croniter._datetime_to_timestamp = cronite_datetime_to_timestamp  # For Python 2.6 compatibility
                        try:
                            iter = croniter(rule['aggregation']['schedule'], ts_now())
                            alert_time = unix_to_dt(iter.get_next())
                        except Exception as e:
                            self.handle_error("Error parsing aggregate send time Cron format %s" % (e), rule['aggregation']['schedule'])
                    else:
                        try:
                            if rule.get('aggregate_by_match_time', False):
                                match_time = ts_to_dt(lookup_es_key(match, rule['timestamp_field']))
                               
                                alert_time = match_time + rule['aggregation']
                            else:
                                alert_time = ts_now() + rule['aggregation']
                        except Exception as e:
                            self.handle_error("[add_aggregated_alert]Error parsing aggregate send time format %s" % (e), rule['aggregation'])

                    rule['aggregate_alert_time'][aggregation_key_value] = alert_time
                    agg_id = None
                    elastalert_logger.info(
                        'New aggregation for %s, aggregation_key: %s. next alert at %s.' % (rule['name'], aggregation_key_value, alert_time)
                    )
            else:
                # Already pending aggregation, use existing alert_time
                alert_time = rule['aggregate_alert_time'].get(aggregation_key_value)
                agg_id = rule['current_aggregate_id'].get(aggregation_key_value)
                elastalert_logger.info(
                    'Adding alert for %s to aggregation(id: %s, aggregation_key: %s), next alert at %s' % (
                        rule['name'],
                        agg_id,
                        aggregation_key_value,
                        alert_time
                    )
                )

            alert_body = self.get_alert_body(match, rule, False, alert_time)
            if agg_id:
                alert_body['aggregate_id'] = agg_id
            if aggregation_key_value:
                alert_body['aggregation_key'] = aggregation_key_value
            res = self.writeback('elastalert', alert_body, rule)

            # If new aggregation, save _id
            if res and not agg_id:
                rule['current_aggregate_id'][aggregation_key_value] = res['_id']

            # Couldn't write the match to ES, save it in memory for now
            if not res:
                rule['agg_matches'].append(match)

            return res

    def silence(self, silence_cache_key=None):
        """ Silence an alert for a period of time. --silence and --rule must be passed as args. """
        if self.debug:
            elastalert_logger.error('--silence not compatible with --debug')
            exit(1)

        if not self.args.rule:
            elastalert_logger.error('--silence must be used with --rule')
            exit(1)

        # With --rule, self.rules will only contain that specific rule
        if not silence_cache_key:
            if self.args.silence_qk_value:
                silence_cache_key = self.rules[0]['realert_key'] + "." + self.args.silence_qk_value
            else:
                silence_cache_key = self.rules[0]['name'] + "._silence"

        try:
            silence_ts = parse_deadline(self.args.silence)
        except (ValueError, TypeError):
            elastalert_logger.error('%s is not a valid time period' % (self.args.silence))
            exit(1)

        if not self.set_realert(silence_cache_key, silence_ts, 0):
            elastalert_logger.error('Failed to save silence command to Elasticsearch')
            exit(1)

        elastalert_logger.info('Success. %s will be silenced until %s' % (silence_cache_key, silence_ts))

    def set_realert(self, silence_cache_key, timestamp, exponent):
        """ Write a silence to Elasticsearch for silence_cache_key until timestamp. """
        body = {'exponent': exponent,
                'rule_name': silence_cache_key,
                '@timestamp': ts_now(),
                'until': timestamp}

        self.silence_cache[silence_cache_key] = (timestamp, exponent)
        return self.writeback('silence', body)

    def is_silenced(self, rule_name):
        """ Checks if rule_name is currently silenced. Returns false on exception. """
        if rule_name in self.silence_cache:
            if ts_now() < self.silence_cache[rule_name][0]:
                return True
            else:
                # Remove expired silence cache entry
                self.silence_cache.pop(rule_name, None)

        if self.debug:
            return False
        query = {'term': {'rule_name': rule_name}}
        sort = {'sort': {'until': {'order': 'desc'}}}
        query = {'query': query}
        query.update(sort)

        try:
            doc_type = 'silence'
            index = self.writeback_es.resolve_writeback_index(self.writeback_index, doc_type)
            res = self.writeback_es.search(index=index, size=1, body=query,
                                           _source_includes=['until', 'exponent'])
        except ElasticsearchException as e:
            self.handle_error("Error while querying for alert silence status: %s" % (e), {'rule': rule_name})

            return False
        if res['hits']['hits']:
            until_ts = res['hits']['hits'][0]['_source']['until']
            exponent = res['hits']['hits'][0]['_source'].get('exponent', 0)
            if rule_name not in list(self.silence_cache.keys()):
                self.silence_cache[rule_name] = (ts_to_dt(until_ts), exponent)
            else:
                self.silence_cache[rule_name] = (ts_to_dt(until_ts), self.silence_cache[rule_name][1])
            if ts_now() < ts_to_dt(until_ts):
                return True
        return False

    def cleanup_silence_cache(self):
        """ Remove expired entries from silence cache to prevent memory leaks """
        now = ts_now()
        expired_keys = []
        for rule_name, (until_time, exponent) in self.silence_cache.items():
            if now >= until_time:
                expired_keys.append(rule_name)
        
        for key in expired_keys:
            self.silence_cache.pop(key, None)
        
        if expired_keys:
            elastalert_logger.debug(f"Cleaned up {len(expired_keys)} expired silence cache entries")

    def cleanup_es_clients_cache(self):
        """ Remove elasticsearch clients for rules that no longer exist """
        active_rule_names = {rule['name'] for rule in self.rules}
        active_rule_names.update({rule['name'] for rule in self.disabled_rules})
        
        stale_clients = []
        for client_key in self.es_clients.keys():
            if client_key not in active_rule_names:
                stale_clients.append(client_key)
        
        for key in stale_clients:
            self.es_clients.pop(key, None)
        
        if stale_clients:
            elastalert_logger.debug(f"Cleaned up {len(stale_clients)} stale elasticsearch client entries")

    def cleanup_rule_memory(self, rule):
        """ Clean up rule-specific memory allocations to prevent memory leaks """
        try:
            # Clean up processed_hits that are older than buffer_time
            self.remove_old_events(rule)
            
            # Clean up expired aggregate alert times
            if 'aggregate_alert_time' in rule:
                now = ts_now()
                expired_keys = []
                for key, alert_time in rule['aggregate_alert_time'].items():
                    if now > alert_time:
                        expired_keys.append(key)
                
                for key in expired_keys:
                    rule['aggregate_alert_time'].pop(key, None)
                    # Also clean up corresponding current_aggregate_id entries
                    rule['current_aggregate_id'].pop(key, None)
                
                if expired_keys:
                    elastalert_logger.debug(f"Cleaned up {len(expired_keys)} expired aggregate alert times for rule {rule['name']}")
            
            # Clean up rule type specific garbage collection
            if hasattr(rule.get('type'), 'garbage_collect'):
                rule['type'].garbage_collect(now)
                
        except Exception as e:
            elastalert_logger.error(f"Error cleaning up memory for rule {rule.get('name', 'unknown')}: {e}")

    def cleanup_expired_aggregates(self):
        """ Clean up expired aggregate alert times from rules """
        now = ts_now()
        cleaned_count = 0
        
        for rule in self.rules:
            if 'aggregate_alert_time' in rule:
                expired_keys = []
                for agg_key, alert_time in rule['aggregate_alert_time'].items():
                    if now > alert_time:
                        expired_keys.append(agg_key)
                
                for key in expired_keys:
                    rule['aggregate_alert_time'].pop(key, None)
                    rule['current_aggregate_id'].pop(key, None)
                    cleaned_count += 1
        
        if cleaned_count > 0:
            elastalert_logger.debug(f"Cleaned up {cleaned_count} expired aggregate alert time entries")

    def cleanup_memory_caches(self):
        """ Perform periodic cleanup of memory caches to prevent memory leaks """
        self.cleanup_silence_cache()
        self.cleanup_es_clients_cache()
        self.cleanup_expired_aggregates()
        # Clean up old processed hits for all rules to prevent unbounded growth
        for rule in self.rules:
            self.remove_old_events(rule)

    def cleanup_scroll_id(self, rule):
        """ Safely clean up scroll ID from rule and elasticsearch """
        if 'scroll_id' in rule:
            scroll_id = rule.pop('scroll_id')
            try:
                if hasattr(self.thread_data, 'current_es') and self.thread_data.current_es:
                    self.thread_data.current_es.clear_scroll(scroll_id=scroll_id)
            except (NotFoundError, AttributeError, ElasticsearchException):
                # Scroll might have already expired or client might not be available
                pass

    def handle_notify_error(self, message, rule, exception=None):
        if self.notify_email:
            self.send_notification_email(exception=exception, rule=rule)
        if self.notify_alerters:
            alert_pipeline = {"alert_time": ts_now()}
            details = [{
                'timestamp': ts_now(),
                'message': message,
                'rule': rule,
            }]
            for alerter in self.notify_alerters:
                alerter.pipeline = alert_pipeline
                try:
                    alerter.alert(details)
                except Exception as e:
                    elastalert_logger.error('Error while running notify alert %s: %s' % (alerter.get_info()['type'], e))

    def handle_error(self, message, data=None):
        ''' Logs message at error level and writes message, data and traceback to Elasticsearch. '''
        elastalert_logger.error(message)
        body = {'message': message}
        tb = traceback.format_exc()
        body['traceback'] = tb.strip().split('\n')
        if data:
            body['data'] = data
        self.writeback('elastalert_error', body)
        if self.notify_all_errors:
            self.handle_notify_error(message, None)

    def handle_uncaught_exception(self, exception, rule):
        """ Disables a rule and sends a notification. """
        elastalert_logger.error(traceback.format_exc())
        msg = 'Uncaught exception running rule %s: %s' % (rule['name'], exception)
        self.handle_error(msg, {'rule': rule['name']})
        if self.disable_rules_on_error:
            self.rules = [running_rule for running_rule in self.rules if running_rule['name'] != rule['name']]
            self.disabled_rules.append(rule)
            self.scheduler.pause_job(job_id=rule['name'])
            # Clean up rule memory when disabling
            self.cleanup_rule_memory(rule)
            elastalert_logger.info('Rule %s disabled', rule['name'])
        self.handle_notify_error(msg, rule, exception=exception)

    def send_notification_email(self, text='', exception=None, rule=None, subject=None, rule_file=None):
        email_body = text
        rule_name = None
        if rule:
            rule_name = rule['name']
        elif rule_file:
            rule_name = rule_file
        if exception and rule_name:
            if not subject:
                subject = 'Uncaught exception in ElastAlert - %s' % (rule_name)
            email_body += '\n\n'
            email_body += 'The rule %s has raised an uncaught exception.\n\n' % (rule_name)
            if self.disable_rules_on_error:
                modified = ' or if the rule config file has been modified' if not self.args.pin_rules else ''
                email_body += 'It has been disabled and will be re-enabled when ElastAlert restarts%s.\n\n' % (modified)
            tb = traceback.format_exc()
            email_body += tb

        if isinstance(self.notify_email, str):
            self.notify_email = [self.notify_email]
        email = MIMEText(email_body)
        email['Subject'] = subject if subject else 'ElastAlert notification'
        recipients = self.notify_email
        if rule and rule.get('notify_email'):
            if isinstance(rule['notify_email'], str):
                rule['notify_email'] = [rule['notify_email']]
            recipients = recipients + rule['notify_email']
        recipients = list(set(recipients))
        email['To'] = ', '.join(recipients)
        email['From'] = self.from_addr
        email['Reply-To'] = self.conf.get('email_reply_to', email['To'])

        try:
            smtp = SMTP(self.smtp_host)
            smtp.sendmail(self.from_addr, recipients, email.as_string())
        except (SMTPException, error) as e:
            elastalert_logger.error('Error connecting to SMTP host: %s' % (e), {'email_body': email_body})

    def get_top_counts(self, rule, starttime, endtime, keys, number=None, qk=None):
        """ Counts the number of events for each unique value for each key field.
        Returns a dictionary with top_events_<key> mapped to the top 5 counts for each key. """
        all_counts = {}
        if not number:
            number = rule.get('top_count_number', 5)
        for key in keys:
            index = self.get_index(rule, starttime, endtime)

            hits_terms = self.get_hits_terms(rule, starttime, endtime, index, key, qk, number)
            if hits_terms is None:
                top_events_count = {}
            else:
                buckets = list(hits_terms.values())[0]

                # get_hits_terms adds to num_hits, but we don't want to count these
                self.thread_data.num_hits -= len(buckets)
                terms = {}
                for bucket in buckets:
                    terms[bucket['key']] = bucket['doc_count']
                counts = list(terms.items())
                counts.sort(key=lambda x: x[1], reverse=True)
                top_events_count = dict(counts[:number])

            # Save a dict with the top 5 events by key
            all_counts['top_events_%s' % (key)] = top_events_count

        return all_counts

    def next_alert_time(self, rule, name, timestamp):
        """ Calculate an 'until' time and exponent based on how much past the last 'until' we are. """
        if name in self.silence_cache:
            last_until, exponent = self.silence_cache[name]
        else:
            # If this isn't cached, this is the first alert or writeback_es is down, normal realert
            return timestamp + rule['realert'], 0

        if not rule.get('exponential_realert'):
            return timestamp + rule['realert'], 0
        diff = seconds(timestamp - last_until)
        # Increase exponent if we've alerted recently
        if diff < seconds(rule['realert']) * 2 ** exponent:
            exponent += 1
        else:
            # Continue decreasing exponent the longer it's been since the last alert
            while diff > seconds(rule['realert']) * 2 ** exponent and exponent > 0:
                diff -= seconds(rule['realert']) * 2 ** exponent
                exponent -= 1

        wait = datetime.timedelta(seconds=seconds(rule['realert']) * 2 ** exponent)
        if wait >= rule['exponential_realert']:
            return timestamp + rule['exponential_realert'], exponent - 1
        return timestamp + wait, exponent


def handle_signal(signal, frame):
    elastalert_logger.info('SIGINT received, stopping ElastAlert...')
    # use os._exit to exit immediately and avoid someone catching SystemExit
    os._exit(0)


def main(args=None):
    signal.signal(signal.SIGINT, handle_signal)
    if not args:
        args = sys.argv[1:]
    client = ElastAlerter(args)

    if client.prometheus_port and not client.debug:
        p = PrometheusWrapper(client)
        p.start()

    if not client.args.silence:
        client.start()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))