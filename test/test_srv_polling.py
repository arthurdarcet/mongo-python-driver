# Copyright 2019-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run the SRV support tests."""

import sys

from time import sleep

sys.path[0:0] = [""]

import pymongo

from pymongo import common
from pymongo.srv_resolver import _HAVE_DNSPYTHON
from pymongo.mongo_client import MongoClient
from test import client_knobs, unittest
from test.utils import wait_until, FunctionCallCounter


WAIT_TIME = 0.1


class SRVPollingKnobs(object):
    def __init__(self, ttl_time=None, min_srv_rescan_interval=None,
                 dns_resolver_nodelist_response=None,
                 count_resolver_calls=False):
        self.ttl_time = ttl_time
        self.min_srv_rescan_interval = min_srv_rescan_interval
        self.dns_resolver_nodelist_response = dns_resolver_nodelist_response
        self.count_resolver_calls = count_resolver_calls

        self.old_min_srv_rescan_interval = None
        self.old_dns_resolver_response = None

    def enable(self):
        self.old_min_srv_rescan_interval = common.MIN_SRV_RESCAN_INTERVAL
        self.old_dns_resolver_response = \
            pymongo.srv_resolver._SrvResolver.get_hosts_and_min_ttl

        if self.min_srv_rescan_interval is not None:
            common.MIN_SRV_RESCAN_INTERVAL = self.min_srv_rescan_interval

        def mock_get_hosts_and_min_ttl(resolver, *args):
            nodes, ttl = self.old_dns_resolver_response(resolver)
            if self.dns_resolver_nodelist_response is not None:
                nodes = self.dns_resolver_nodelist_response()
            if self.ttl_time is not None:
                ttl = self.ttl_time
            return nodes, ttl

        if self.count_resolver_calls:
            patch_func = FunctionCallCounter(mock_get_hosts_and_min_ttl)
        else:
            patch_func = mock_get_hosts_and_min_ttl

        pymongo.srv_resolver._SrvResolver.get_hosts_and_min_ttl = patch_func

    def __enter__(self):
        self.enable()

    def disable(self):
        common.MIN_SRV_RESCAN_INTERVAL = self.old_min_srv_rescan_interval
        pymongo.srv_resolver._SrvResolver.get_hosts_and_min_ttl = \
            self.old_dns_resolver_response

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable()


class TestSRVPolling(unittest.TestCase):

    BASE_SRV_RESPONSE = [
        ("localhost.test.build.10gen.cc", 27017),
        ("localhost.test.build.10gen.cc", 27018)]

    CONNECTION_STRING = "mongodb+srv://test1.test.build.10gen.cc"

    def setUp(self):
        if not _HAVE_DNSPYTHON:
            raise unittest.SkipTest("SRV polling tests require the dnspython "
                                    "module")

    def get_nodelist(self, client):
        return client._topology.description.server_descriptions().keys()

    def assert_nodelist_change(self, expected_nodelist, client):
        """Check if the client._topology eventually sees all nodes in the
        expected_nodelist.
        """
        def predicate():
            nodelist = self.get_nodelist(client)
            if set(expected_nodelist) == set(nodelist):
                return True
            return False
        wait_until(predicate, "see expected nodelist", timeout=10*WAIT_TIME)

    def assert_nodelist_nochange(self, expected_nodelist, client):
        """Check if the client._topology ever deviates from seeing all nodes
        in the expected_nodelist. Consistency is checked after sleeping for
        (WAIT_TIME * 10) seconds. Also check that the resolver is called at
        least once.
        """
        sleep(WAIT_TIME*10)
        nodelist = self.get_nodelist(client)
        if set(expected_nodelist) != set(nodelist):
            msg = "Client nodelist %s changed unexpectedly (expected %s)"
            raise self.fail(msg % (nodelist, expected_nodelist))
        self.assertGreaterEqual(
            pymongo.srv_resolver._SrvResolver.get_hosts_and_min_ttl.call_count,
            1, "resolver was never called")
        return True

    def _run_scenario(self, dns_response, expect_change):
        if callable(dns_response):
            dns_resolver_response = dns_response
        else:
            def dns_resolver_response():
                return dns_response

        if expect_change:
            assertion_method = self.assert_nodelist_change
            count_resolver_calls = False
            expected_response = dns_response
        else:
            assertion_method = self.assert_nodelist_nochange
            count_resolver_calls = True
            expected_response = self.BASE_SRV_RESPONSE

        # Patch timeouts to ensure short test running times.
        with SRVPollingKnobs(
                ttl_time=WAIT_TIME, min_srv_rescan_interval=WAIT_TIME):
            mc = MongoClient(self.CONNECTION_STRING)
            self.assert_nodelist_change(self.BASE_SRV_RESPONSE, mc)
            # Patch list of hosts returned by DNS query.
            with SRVPollingKnobs(
                    dns_resolver_nodelist_response=dns_resolver_response,
                    count_resolver_calls=count_resolver_calls):
                assertion_method(expected_response, mc)

    def run_scenario(self, dns_response, expect_change):
        # Patch timeouts to ensure short rescan SRV interval.
        with client_knobs(heartbeat_frequency=WAIT_TIME,
                          min_heartbeat_interval=WAIT_TIME,
                          events_queue_frequency=WAIT_TIME):
            self._run_scenario(dns_response, expect_change)

    def test_addition(self):
        response = self.BASE_SRV_RESPONSE[:]
        response.append(
            ("localhost.test.build.10gen.cc", 27019))
        self.run_scenario(response, True)

    def test_removal(self):
        response = self.BASE_SRV_RESPONSE[:]
        response.remove(
            ("localhost.test.build.10gen.cc", 27018))
        self.run_scenario(response, True)

    def test_replace_one(self):
        response = self.BASE_SRV_RESPONSE[:]
        response.remove(
            ("localhost.test.build.10gen.cc", 27018))
        response.append(
            ("localhost.test.build.10gen.cc", 27019))
        self.run_scenario(response, True)

    def test_replace_both_with_one(self):
        response = [("localhost.test.build.10gen.cc", 27019)]
        self.run_scenario(response, True)

    def test_replace_both_with_two(self):
        response = [("localhost.test.build.10gen.cc", 27019),
                    ("localhost.test.build.10gen.cc", 27020)]
        self.run_scenario(response, True)

    def test_dns_failures(self):
        from dns import exception
        for exc in (exception.FormError, exception.TooBig, exception.Timeout):
            def response_callback(*args):
                raise exc("DNS Failure!")
            self.run_scenario(response_callback, False)

    def test_dns_record_lookup_empty(self):
        response = []
        self.run_scenario(response, False)


if __name__ == '__main__':
    unittest.main()
