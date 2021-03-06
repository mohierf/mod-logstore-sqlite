#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2009-2010:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.


#
# This file is used to test host- and service-downtimes.
#

import os
import sys
import re
import datetime
import shutil
import time
import random
import copy

import pytest
import sqlite3

from shinken_modules import ShinkenModulesTest
from shinken_test import time_hacker, unittest

from shinken.objects.module import Module
from shinken.objects.service import Service
from shinken.modulesctx import modulesctx
from shinken.comment import Comment
from shinken.log import logger
from shinken.modulesmanager import ModulesManager
from shinken.misc.datamanager import datamgr


livestatus_broker = modulesctx.get_module('livestatus')
LiveStatus_broker = livestatus_broker.LiveStatus_broker
LiveStatus = livestatus_broker.LiveStatus
LiveStatusRegenerator = livestatus_broker.LiveStatusRegenerator
LiveStatusQueryCache = livestatus_broker.LiveStatusQueryCache
Logline = livestatus_broker.Logline
LiveStatusLogStoreSqlite = modulesctx.get_module('logstore-sqlite').LiveStatusLogStoreSqlite


from mock_livestatus import mock_livestatus_handle_request


sys.setcheckinterval(10000)


@mock_livestatus_handle_request
class TestConfig(ShinkenModulesTest):
    def contains_line(self, text, pattern):
        regex = re.compile(pattern)
        for line in text.splitlines():
            if re.search(regex, line):
                return True
        return False

    def update_broker(self, dodeepcopy=False):
        """Overloads the Shinken update_broker method because it does not handle
        the broks list as a list but as a dict !"""
        for brok in self.sched.brokers['Default-Broker']['broks']:
            if dodeepcopy:
                brok = copy.deepcopy(brok)
            brok.prepare()
            # print("Managing a brok, type: %s" % brok.type)
            self.livestatus_broker.manage_brok(brok)
        self.sched.brokers['Default-Broker']['broks'] = []

    def tearDown(self):
        self.livestatus_broker.db.commit()
        self.livestatus_broker.db.close()
        if os.path.exists(self.livelogs):
            os.remove(self.livelogs)
        if os.path.exists(self.livelogs + "-journal"):
            os.remove(self.livelogs + "-journal")
        if os.path.exists("tmp/archives"):
            for db in os.listdir("tmp/archives"):
                os.remove(os.path.join("tmp/archives", db))
        if os.path.exists('var/nagios.log'):
            os.remove('var/nagios.log')
        if os.path.exists('var/retention.dat'):
            os.remove('var/retention.dat')
        if os.path.exists('var/status.dat'):
            os.remove('var/status.dat')
        self.livestatus_broker = None


@mock_livestatus_handle_request
class TestConfigSmall(TestConfig):
    def setUp(self):
        setup_state_time = time.time()
        self.setup_with_file('etc/shinken_1r_1h_1s.cfg')
        self.testid = str(os.getpid() + random.randint(1, 1000))

        self.init_livestatus()

        print("Requesting initial status broks...")
        self.sched.conf.skip_initial_broks = False
        self.sched.brokers['Default-Broker'] = {'broks': [], 'has_full_broks': False}
        self.sched.fill_initial_broks('Default-Broker')
        print("My initial broks: %d broks" % (len(self.sched.brokers['Default-Broker'])))

        self.update_broker()
        print("Initial setup duration:", time.time() - setup_state_time)

        self.nagios_path = None
        self.livestatus_path = None
        self.nagios_config = None
        # add use_aggressive_host_checking so we can mix exit codes 1 and 2
        # but still get DOWN state
        host = self.sched.hosts.find_by_name("test_host_0")
        host.__class__.use_aggressive_host_checking = 1

        # Some cleanings
        for file in os.listdir('tmp'):
            if os.path.isfile(file):
                os.remove(os.path.join('tmp', file))
        if os.path.exists("tmp/archives"):
            for db in os.listdir("tmp/archives"):
                if os.path.isfile(db):
                    os.remove(os.path.join("tmp/archives", db))

    def write_logs(self, host, loops=0):
        for loop in range(0, loops):
            host.state = 'DOWN'
            host.state_type = 'SOFT'
            host.attempt = 1
            host.output = "I am DOWN"
            host.raise_alert_log_entry()
            host.state = 'UP'
            host.state_type = 'HARD'
            host.attempt = 1
            host.output = "I am UP"
            host.raise_alert_log_entry()
            self.update_broker()

    def save_and_query_db(self, logs_count=0, archives=False):
        self.livestatus_broker.db.commit()

        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs")
        print("Log count (module): %s" % numlogs)
        self.assertEqual(numlogs[0][0], logs_count)

        # Save the database for offline analysis
        if os.path.exists(self.livelogs):
            print("Livelogs saving: %s" % os.path.abspath(self.livelogs))
            shutil.copyfile(self.livelogs, "/tmp/livelogs")
        if os.path.exists(self.livelogs + "-journal"):
            print("Livelogs journal saving: %s" % os.path.abspath(self.livelogs + "-journal"))
            shutil.copyfile(self.livelogs + "-journal", "/tmp/livelogs-journal")

        # Direct DB connection and query
        con = sqlite3.connect("/tmp/livelogs")
        cursor = con.cursor()
        cursor.execute("select count(*) from logs")
        records = cursor.fetchall()
        cursor.close()
        # May be different from the module count!
        print("Log count (sqlite): %s" % len(records))
        # self.assertEqual(records[0][0], logs_count)

        if not archives:
            return

        self.assertTrue(os.path.exists("tmp/archives"))

        print("DB archive files:")
        dbs = []
        for d in os.listdir("tmp/archives"):
            print("- %s" % d)
            if not d.endswith("journal"):
                dbs.append(d)
        # tempres = [d for d in os.listdir("tmp/archives") if not d.endswith("journal")]
        self.assertEqual(4, len(dbs))
        lengths = []
        for db in sorted(dbs):
            dbmodconf = Module({
                'module_name': 'LogStore',
                'module_type': 'logstore_sqlite',
                'use_aggressive_sql': '0',
                'database_file': "tmp/archives/" + db,
                'archive_path': "tmp/archives/",
                'max_logs_age': '0',
            })
            tmpconn = LiveStatusLogStoreSqlite(dbmodconf)
            tmpconn.open()
            numlogs = tmpconn.execute("SELECT COUNT(*) FROM logs")
            lengths.append(numlogs[0][0])
            print("DB daily file: %s (%d logs)" % (db, numlogs[0][0]))
            tmpconn.close()

        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs")
        print("Log count (module): %s" % numlogs)
        lengths.append(numlogs[0][0])
        self.assertEqual(numlogs[0][0], logs_count)

        print("lengths is: %s" % lengths)
        self.assertEqual([6, 14, 22, 30, 8], lengths)

    def test_hostsbygroup(self):
        self.print_header()
        now = time.time()
        objlist = []
        print "-------------------------------------------"
        print "Service.lsm_host_name", Service.lsm_host_name
        print "Logline.lsm_current_host_name", Logline.lsm_current_host_name
        print "-------------------------------------------"
        for host in self.sched.hosts:
            objlist.append([host, 0, 'UP'])
        for service in self.sched.services:
            objlist.append([service, 0, 'OK'])
        self.scheduler_loop(1, objlist)
        self.update_broker()
        request = """GET hostsbygroup
ColumnHeaders: on
Columns: host_name hostgroup_name
Filter: groups >= allhosts
OutputFormat: csv
KeepAlive: on
ResponseHeader: fixed16
"""

        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print response

    def test_one_log(self):
        self.print_header()
        host = self.sched.hosts.find_by_name("test_host_0")
        now = time.time()
        time_hacker.time_warp(-3600)
        num_logs = 0
        host.state = 'DOWN'
        host.state_type = 'SOFT'
        host.attempt = 1
        host.output = "i am down"
        host.raise_alert_log_entry()
        time.sleep(3600)
        host.state = 'UP'
        host.state_type = 'HARD'
        host.attempt = 1
        host.output = "i am up"
        host.raise_alert_log_entry()
        time.sleep(3600)
        self.update_broker()
        print "-------------------------------------------"
        print "Service.lsm_host_name", Service.lsm_host_name
        print "Logline.lsm_current_host_name", Logline.lsm_current_host_name
        print "-------------------------------------------"

        self.livestatus_broker.db.log_db_do_archive()
        print "request logs from", int(now - 3600), int(now + 3600)
        print "request logs from", time.asctime(time.localtime(int(now - 3600))), time.asctime(time.localtime(int(now + 3600)))
        request = """GET log
        Filter: time >= """ + str(int(now - 3600)) + """
        Filter: time <= """ + str(int(now + 3600)) + """
        Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print response
        print "next next_log_db_rotate", time.asctime(time.localtime(self.livestatus_broker.db.next_log_db_rotate))
        result = self.livestatus_broker.db.log_db_historic_contents()
        for day in result:
            print "file is", day[0]
            print "start is", time.asctime(time.localtime(day[3]))
            print "stop  is", time.asctime(time.localtime(day[4]))
            print "archive is", day[2]
            print "handle is", day[1]
        print self.livestatus_broker.db.log_db_relevant_files(now - 3600, now + 3600)

    def test_num_logs(self):
        self.print_header()
        host = self.sched.hosts.find_by_name("test_host_0")
        now = time.time()
        time_hacker.time_warp(-1 * 3600 * 24 * 7)
        num_logs = 0
        while time.time() < now:
            host.state = 'DOWN'
            host.state_type = 'SOFT'
            host.attempt = 1
            host.output = "i am down"
            host.raise_alert_log_entry()
            num_logs += 1
            time.sleep(3600)
            host.state = 'UP'
            host.state_type = 'HARD'
            host.attempt = 1
            host.output = "i am up"
            host.raise_alert_log_entry()
            num_logs += 1
            time.sleep(3600)
        self.update_broker()

        self.livestatus_broker.db.log_db_do_archive()
        print "request logs from", int(now - 3600 * 24 * 5), int(now - 3600 * 24 * 3)
        print "request logs from", time.asctime(time.localtime(int(now - 3600 * 24 * 5))), time.asctime(time.localtime(int(now - 3600 * 24 * 3)))
        request = """GET log
Filter: time >= """ + str(int(now - 3600 * 24 * 5)) + """
Filter: time <= """ + str(int(now - 3600 * 24 * 3)) + """
Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print response
        print "next next_log_db_rotate", time.asctime(time.localtime(self.livestatus_broker.db.next_log_db_rotate))
        result = self.livestatus_broker.db.log_db_historic_contents()
        for day in result:
            print "file is", day[0]
            print "start is", time.asctime(time.localtime(day[3]))
            print "stop  is", time.asctime(time.localtime(day[4]))
            print "archive is", day[2]
            print "handle is", day[1]
        print self.livestatus_broker.db.log_db_relevant_files(now - 3 * 24 * 3600, now)

    def test_split_database(self):
        #
        # after daylight-saving time has begun or ended,
        # this test may fail for some days
        #
        # os.removedirs("var/archives")
        self.print_header()
        host = self.sched.hosts.find_by_name("test_host_0")
        save_now = time.time()
        today = datetime.datetime.fromtimestamp(time.time())
        today_noon = datetime.datetime(today.year, today.month, today.day, 12, 0, 0)
        today_morning = datetime.datetime(today.year, today.month, today.day, 0, 0, 0)
        back2days_noon = today_noon - datetime.timedelta(days=2)
        back2days_morning = today_morning - datetime.timedelta(days=2)
        back4days_noon = today_noon - datetime.timedelta(days=4)
        back4days_morning = today_morning - datetime.timedelta(days=4)
        today_noon = int(time.mktime(today_noon.timetuple()))
        today_morning = int(time.mktime(today_morning.timetuple()))
        back2days_noon = int(time.mktime(back2days_noon.timetuple()))
        back2days_morning = int(time.mktime(back2days_morning.timetuple()))
        back4days_noon = int(time.mktime(back4days_noon.timetuple()))
        back4days_morning = int(time.mktime(back4days_morning.timetuple()))
        now = time.time()
        time_hacker.time_warp(-1 * (now - back4days_noon))
        now = time.time()
        print "4t is", time.asctime(time.localtime(int(now)))
        logs_count = 0
        for day in range(1, 5):
            print "day", day
            # at 12:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(3600)
            # at 13:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(36000)
            # at 23:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(3600)
            # at 00:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(43200)
        # day 1: 1 * (2 + 2 + 2)
        # day 2: 2 * (2 + 2 + 2) + 1 * 2 (from last loop)
        # day 3: 3 * (2 + 2 + 2) + 2 * 2 (from last loop)
        # day 4: 4 * (2 + 2 + 2) + 3 * 2 (from last loop)
        # today: 4 * 2 (from last loop)
        logs_today = 8
        # 6 + 14 + 22 + 30  + 8 = 80
        now = time.time()
        print "0t is", time.asctime(time.localtime(int(now)))

        self.save_and_query_db(logs_count=logs_count)

        request = """GET log
        OutputFormat: python
        Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        pyresponse = eval(response)
        # ignore the internal logs (if some exist...)
        pyresponse = [l for l in pyresponse if l[1].strip() not in ["Warning", "Info", "Debug"]]
        print "pyresponse", len(pyresponse)
        print "expect", logs_count
        self.assertEqual(len(pyresponse), logs_count)

        # fixme: if not close / open the DB module, the next query returns nothing!
        # because the DB connection looks like lost !
        # fixme: find out if it is important and why
        self.livestatus_broker.db.close()
        self.livestatus_broker.db.open()

        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs")
        print("+++ Log count: %s" % numlogs)
        self.assertEqual(numlogs[0][0], logs_count)

        self.save_and_query_db(logs_count=logs_count)

        # Without close / open of the DB module, the 4 expected files are not
        # created... there is no logical reason to this!
        self.livestatus_broker.db.log_db_do_archive()

        # Only today's logs
        self.save_and_query_db(logs_count=logs_today, archives=True)

        request = """GET log
        Filter: time >= """ + str(int(back4days_morning)) + """
        Filter: time <= """ + str(int(back2days_noon)) + """
        OutputFormat: python
        Columns: time type options state host_name"""
        print("Request: %s" % request)
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        pyresponse = eval(response)
        self.assertEqual(30, len(pyresponse))

        self.livestatus_broker.db.log_db_do_archive()

        request = """GET log
        Filter: time >= """ + str(int(back4days_morning)) + """
        Filter: time <= """ + str(int(back2days_noon)) + """
        OutputFormat: python
        Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        pyresponse = eval(response)
        self.assertEqual(30, len(pyresponse))

        self.livestatus_broker.db.log_db_do_archive()

        request = """GET log
        Filter: time >= """ + str(int(back4days_morning)) + """
        Filter: time <= """ + str(int(back2days_noon) - 1) + """
        OutputFormat: python
        Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        pyresponse = eval(response)
        self.assertEqual(24, len(pyresponse))

        # now warp to the time when we entered this test
        time_hacker.time_warp(-1 * (time.time() - save_now))
        # and now start the same logging
        today = datetime.datetime.fromtimestamp(time.time())
        today_noon = datetime.datetime(today.year, today.month, today.day, 12, 0, 0)
        today_morning = datetime.datetime(today.year, today.month, today.day, 0, 0, 0)
        back2days_noon = today_noon - datetime.timedelta(days=2)
        back2days_morning = today_morning - datetime.timedelta(days=2)
        back4days_noon = today_noon - datetime.timedelta(days=4)
        back4days_morning = today_morning - datetime.timedelta(days=4)
        today_noon = int(time.mktime(today_noon.timetuple()))
        today_morning = int(time.mktime(today_morning.timetuple()))
        back2days_noon = int(time.mktime(back2days_noon.timetuple()))
        back2days_morning = int(time.mktime(back2days_morning.timetuple()))
        back4days_noon = int(time.mktime(back4days_noon.timetuple()))
        back4days_morning = int(time.mktime(back4days_morning.timetuple()))
        now = time.time()
        time_hacker.time_warp(-1 * (now - back4days_noon))
        now = time.time()
        time.sleep(5)
        print "4t is", time.asctime(time.localtime(int(now)))
        # logs_count = 0
        for day in range(1, 5):
            print "day", day
            # at 12:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(3600)
            # at 13:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(36000)
            # at 23:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(3600)
            # at 00:00
            now = time.time()
            print "it is", time.asctime(time.localtime(int(now)))
            self.write_logs(host, day)
            logs_count += 2 * day
            time.sleep(43200)
        # day 1: 1 * (2 + 2 + 2)
        # day 2: 2 * (2 + 2 + 2) + 1 * 2 (from last loop)
        # day 3: 3 * (2 + 2 + 2) + 2 * 2 (from last loop)
        # day 4: 4 * (2 + 2 + 2) + 3 * 2 (from last loop)
        # today: 4 * 2 (from last loop)
        # 6 + 14 + 22 + 30  + 8 = 80

        request = """GET log
        OutputFormat: python
        Columns: time type options state host_name"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        pyresponse = eval(response)
        # ignore the internal logs (if some exist...)
        pyresponse = [l for l in pyresponse if l[1].strip() not in ["Warning", "Info", "Debug"]]
        print "pyresponse", len(pyresponse)
        print "expect", logs_count
        self.assertEqual(len(pyresponse), logs_count)

        # fixme: if not close / open the DB module, the next query returns nothing!
        # because the DB connection looks like lost !
        # fixme: find out if it is important and why
        self.livestatus_broker.db.close()
        self.livestatus_broker.db.open()

        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs")
        print("+++ Log count: %s" % numlogs)
        self.assertEqual(numlogs[0][0], 88)
        # !!! 80 new records + 8 today's records. Other records were archived...

        self.livestatus_broker.db.log_db_do_archive()
        self.assertTrue(os.path.exists("tmp/archives"))
        self.assertTrue(len([d for d in os.listdir("tmp/archives") if not d.endswith("journal")]) == 4)
        lengths = []
        for db in sorted([d for d in os.listdir("tmp/archives") if not d.endswith("journal")]):
            dbmodconf = Module({
                'module_name': 'LogStore',
                'module_type': 'logstore_sqlite',
                'use_aggressive_sql': '0',
                'database_file': "tmp/archives/" + db,
                'max_logs_age': '0',
            })
            tmpconn = LiveStatusLogStoreSqlite(dbmodconf)
            tmpconn.open()
            numlogs = tmpconn.execute("SELECT COUNT(*) FROM logs")
            lengths.append(numlogs[0][0])
            print("DB daily file: %s (%d logs)" % (db, numlogs[0][0]))
            tmpconn.close()
        print("lengths is: %s" % lengths)
        self.assertEqual([12, 28, 44, 60], lengths)

    def test_archives_path(self):
        # os.removedirs("var/archives")
        self.print_header()
        lengths = []
        database_file = "dotlivestatus.db"
        archives_path = os.path.join(os.path.dirname(database_file), 'archives')
        print "archive is", archives_path

    def test_sven(self):
        self.print_header()
        host = self.sched.hosts.find_by_name("test_host_0")
        now = time.time()
        num_logs = 0
        host.state = 'DOWN'
        host.state_type = 'SOFT'
        host.attempt = 1
        host.output = "i am down"
        host.raise_alert_log_entry()
        time.sleep(60)
        host.state = 'UP'
        host.state_type = 'HARD'
        host.attempt = 1
        host.output = "i am up"
        host.raise_alert_log_entry()
        time.sleep(60)
        self.show_logs()
        self.update_broker()
        self.livestatus_broker.db.log_db_do_archive()
        query_end = time.time() + 3600
        query_start = query_end - 3600 * 24 * 21
        request = """GET log
Columns: class time type state host_name service_description plugin_output message options contact_name command_name state_type current_host_groups current_service_groups
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
And: 2
Filter: host_name = test_host_0
Filter: type = HOST ALERT
Filter: options ~ ;HARD;
Filter: type = INITIAL HOST STATE
Filter: options ~ ;HARD;
Filter: type = CURRENT HOST STATE
Filter: options ~ ;HARD;
Filter: type = HOST DOWNTIME ALERT
Or: 7
And: 2
Filter: host_name = test_host_0
Filter: type = SERVICE ALERT
Filter: options ~ ;HARD;
Filter: type = INITIAL SERVICE STATE
Filter: options ~ ;HARD;
Filter: type = CURRENT SERVICE STATE
Filter: options ~ ;HARD;
Filter: type = SERVICE DOWNTIME ALERT
Or: 7
And: 2
Filter: class = 2
Filter: type ~~ TIMEPERIOD TRANSITION
Or: 4
OutputFormat: json
ResponseHeader: fixed16
"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print request
        print response
        pyresponse = eval(response.splitlines()[1])
        pyresponse = [l for l in pyresponse if l[2].strip() not in ["Warning", "Info", "Debug"]]
        print pyresponse
        self.assertTrue(len(pyresponse) == 2)

    def test_max_logs_age(self):
        # 0 - unset
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': None,
            'archive_path': None,
            'logs_table': None,
            'max_logs_age': None
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual("/tmp/livelogs.db", livestatus_broker.database_file)
        self.assertEqual("/tmp/archives", livestatus_broker.archive_path)
        self.assertEqual(7, livestatus_broker.max_logs_age)

        # 1 - default
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': '7'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual(7, livestatus_broker.max_logs_age)

        # 2 - days
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': '7d'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual(7, livestatus_broker.max_logs_age)

        # 3 - weeks
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': '1w'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual(7, livestatus_broker.max_logs_age)

        # 4 - months
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': '3m'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual(3*31, livestatus_broker.max_logs_age)

        # 5 - years
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': '7y'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual(7*365, livestatus_broker.max_logs_age)

        # 6 - wrong format
        db_module_conf = Module({
            'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'database_file': 'livelogs',
            'archive_path': 'archives',
            'logs_table': 'ls-logs',
            'max_logs_age': 'XxX'
        })

        livestatus_broker = LiveStatusLogStoreSqlite(db_module_conf)
        self.assertEqual('XxX', livestatus_broker.max_logs_age)
        # A warning log is raised!


@mock_livestatus_handle_request
class TestConfigBig(TestConfig):
    def setUp(self):
        setup_state_time = time.time()
        print("%s - starting setup..." % time.strftime("%H:%M:%S"))

        # self.setup_with_file('etc/shinken_1r_1h_1s.cfg')
        self.setup_with_file('etc/shinken_5r_100h_2000s.cfg')

        self.testid = str(os.getpid() + random.randint(1, 1000))
        print("%s - Initial setup duration: %.2f seconds" % (time.strftime("%H:%M:%S"),
                                                             time.time() - setup_state_time))

        # self.cfg_database = 'test' + self.testid
        #
        # dbmodconf = Module({
        #     'module_name': 'LogStore',
        #     'module_type': 'logstore_sqlite',
        #     'database_file': self.cfg_database,
        #     'max_logs_age': '3m'
        # })
        #
        # self.init_livestatus(dbmodconf=dbmodconf)
        self.init_livestatus()
        print("%s - Initialized livestatus: %.2f seconds" % (time.strftime("%H:%M:%S"),
                                                             time.time() - setup_state_time))

        print("Requesting initial status broks...")
        self.sched.conf.skip_initial_broks = False
        self.sched.brokers['Default-Broker'] = {'broks': [], 'has_full_broks': False}
        self.sched.fill_initial_broks('Default-Broker')
        self.update_broker()
        print("%s - Initial setup duration: %.2f seconds" % (time.strftime("%H:%M:%S"),
                                                             time.time() - setup_state_time))

        # add use_aggressive_host_checking so we can mix exit codes 1 and 2
        # but still get DOWN state
        host = self.sched.hosts.find_by_name("test_host_000")
        # host = self.sched.hosts.find_by_name("test_host_0")
        host.__class__.use_aggressive_host_checking = 1

    # @pytest.mark.skip("Temp ...")
    def test_a_long_history(self):
        test_host_005 = self.sched.hosts.find_by_name("test_host_005")
        test_host_099 = self.sched.hosts.find_by_name("test_host_099")
        test_ok_00 = self.sched.services.find_srv_by_name_and_hostname("test_host_005", "test_ok_00")
        test_ok_01 = self.sched.services.find_srv_by_name_and_hostname("test_host_005", "test_ok_01")
        test_ok_04 = self.sched.services.find_srv_by_name_and_hostname("test_host_005", "test_ok_04")
        test_ok_16 = self.sched.services.find_srv_by_name_and_hostname("test_host_005", "test_ok_16")
        test_ok_99 = self.sched.services.find_srv_by_name_and_hostname("test_host_099", "test_ok_01")

        days = 4
        etime = time.time()
        print "now it is", time.ctime(etime)
        print "now it is", time.gmtime(etime)
        etime_midnight = (etime - (etime % 86400)) + time.altzone
        print "midnight was", time.ctime(etime_midnight)
        print "midnight was", time.gmtime(etime_midnight)
        query_start = etime_midnight - (days - 1) * 86400
        query_end = etime_midnight
        print "query_start", time.ctime(query_start)
        print "query_end ", time.ctime(query_end)

        # |----------|----------|----------|----------|----------|---x
        #                                                            etime
        #                                                        etime_midnight
        #             ---x------
        #                etime -  4 days
        #                       |---
        #                       query_start
        #
        #                ............................................
        #                events in the log database ranging till now
        #
        #                       |________________________________|
        #                       events which will be read from db
        #
        loops = int(86400 / 192)
        time_hacker.time_warp(-1 * days * 86400)
        print "warp back to", time.ctime(time.time())
        # run silently
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        should_be = 0
        for day in xrange(days):
            sys.stderr.write("day %d now it is %s i run %d loops\n" % (day, time.ctime(time.time()), loops))
            self.scheduler_loop(2, [
                [test_ok_00, 0, "OK"],
                [test_ok_01, 0, "OK"],
                [test_ok_04, 0, "OK"],
                [test_ok_16, 0, "OK"],
                [test_ok_99, 0, "OK"],
            ])
            self.update_broker()
            #for i in xrange(3600 * 24 * 7):
            for i in xrange(loops):
                if i % 10000 == 0:
                    sys.stderr.write(str(i))
                if i % 399 == 0:
                    self.scheduler_loop(3, [
                        [test_ok_00, 1, "WARN"],
                        [test_ok_01, 2, "CRIT"],
                        [test_ok_04, 3, "UNKN"],
                        [test_ok_16, 1, "WARN"],
                        [test_ok_99, 2, "CRIT"],
                    ])
                    if int(time.time()) >= query_start and int(time.time()) <= query_end:
                        should_be += 3
                        sys.stderr.write("now it should be %s\n" % should_be)
                time.sleep(62)
                if i % 399 == 0:
                    self.scheduler_loop(1, [
                        [test_ok_00, 0, "OK"],
                        [test_ok_01, 0, "OK"],
                        [test_ok_04, 0, "OK"],
                        [test_ok_16, 0, "OK"],
                        [test_ok_99, 0, "OK"],
                    ])
                    if int(time.time()) >= query_start and int(time.time()) <= query_end:
                        should_be += 1
                        sys.stderr.write("now it should be %s\n" % should_be)
                time.sleep(2)
                if i % 17 == 0:
                    self.scheduler_loop(3, [
                        [test_ok_00, 1, "WARN"],
                        [test_ok_01, 2, "CRIT"],
                    ])

                time.sleep(62)
                if i % 17 == 0:
                    self.scheduler_loop(1, [
                        [test_ok_00, 0, "OK"],
                        [test_ok_01, 0, "OK"],
                    ])
                time.sleep(2)
                if i % 14 == 0:
                    self.scheduler_loop(3, [
                        [test_host_005, 2, "DOWN"],
                    ])
                if i % 12 == 0:
                    self.scheduler_loop(3, [
                        [test_host_099, 2, "DOWN"],
                    ])
                time.sleep(62)
                if i % 14 == 0:
                    self.scheduler_loop(3, [
                        [test_host_005, 0, "UP"],
                    ])
                if i % 12 == 0:
                    self.scheduler_loop(3, [
                        [test_host_099, 0, "UP"],
                    ])
                time.sleep(2)
                self.update_broker()
                if i % 1000 == 0:
                    self.livestatus_broker.db.commit()
            endtime = time.time()
            self.livestatus_broker.db.commit()
            sys.stderr.write("day %d end it is %s\n" % (day, time.ctime(time.time())))
        sys.stdout.close()
        sys.stdout = old_stdout
        self.livestatus_broker.db.commit_and_rotate_log_db()
        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs")
        print "numlogs is", numlogs

        # now we have a lot of events
        # find type = HOST ALERT for test_host_005
        request = """GET log
Columns: class time type state host_name service_description plugin_output message options contact_name command_name state_type current_host_groups current_service_groups
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
Filter: host_name = test_host_099
Filter: service_description = test_ok_01
And: 5
OutputFormat: json"""
        # switch back to realtime. we want to know how long it takes
        time_hacker.set_real_time()

        print request
        print "query 1 --------------------------------------------------"
        tic = time.time()
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        tac = time.time()
        pyresponse = eval(response)
        print "number of records with test_ok_01", len(pyresponse)
        self.assertEqual(should_be, len(pyresponse))

        # and now test Negate:
        request = """GET log
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
Filter: host_name = test_host_099
Filter: service_description = test_ok_01
And: 2
Negate:
And: 2
OutputFormat: json"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        print "got response with true instead of negate"
        notpyresponse = eval(response)
        print "number of records without test_ok_01", len(notpyresponse)

        request = """GET log
Filter: time >= """ + str(int(query_start)) + """
Filter: time <= """ + str(int(query_end)) + """
Filter: type = SERVICE ALERT
And: 1
Filter: type = HOST ALERT
And: 1
Filter: type = SERVICE FLAPPING ALERT
Filter: type = HOST FLAPPING ALERT
Filter: type = SERVICE DOWNTIME ALERT
Filter: type = HOST DOWNTIME ALERT
Filter: type ~ starting...
Filter: type ~ shutting down...
Or: 8
OutputFormat: json"""
        response, keepalive = self.livestatus_broker.livestatus.handle_request(request)
        allpyresponse = eval(response)
        print "all records", len(allpyresponse)
        self.assertTrue(len(allpyresponse) == len(notpyresponse) + len(pyresponse))
        # the numlogs above only counts records in the currently attached db
        numlogs = self.livestatus_broker.db.execute("SELECT COUNT(*) FROM logs WHERE time >= %d AND time <= %d" % (int(query_start), int(query_end)))
        print "numlogs is", numlogs
        time_hacker.set_my_time()


@mock_livestatus_handle_request
class TestConfigNoLogstore(TestConfig):
    def setUp(self):
        setup_state_time = time.time()
        self.setup_with_file('etc/shinken_1r_1h_1s.cfg')
        self.testid = str(os.getpid() + random.randint(1, 1000))
        self.init_livestatus()

        print("Requesting initial status broks...")
        self.sched.conf.skip_initial_broks = False
        self.sched.brokers['Default-Broker'] = {'broks': [], 'has_full_broks': False}
        self.sched.fill_initial_broks('Default-Broker')
        print("My initial broks: %d broks" % (len(self.sched.brokers['Default-Broker'])))

        self.update_broker()
        print("%s - Initial setup duration: %.2f seconds" % (time.strftime("%H:%M:%S"),
                                                             time.time() - setup_state_time))

        self.nagios_path = None
        self.livestatus_path = None
        self.nagios_config = None
        # add use_aggressive_host_checking so we can mix exit codes 1 and 2
        # but still get DOWN state
        host = self.sched.hosts.find_by_name("test_host_0")
        host.__class__.use_aggressive_host_checking = 1

        # Some cleanings
        for file in os.listdir('tmp'):
            if os.path.isfile(file):
                os.remove(os.path.join('tmp', file))
        if os.path.exists("tmp/archives"):
            for db in os.listdir("tmp/archives"):
                if os.path.isfile(db):
                    os.remove(os.path.join("tmp/archives", db))

    def tearDown(self):
        self.livestatus_broker.db.commit()
        self.livestatus_broker.db.close()
        if os.path.exists(self.livelogs):
            os.remove(self.livelogs)
        if os.path.exists(self.livelogs + "-journal"):
            os.remove(self.livelogs + "-journal")
        if os.path.exists(self.livestatus_broker.pnp_path):
            shutil.rmtree(self.livestatus_broker.pnp_path)
        if os.path.exists('var/nagios.log'):
            os.remove('var/nagios.log')
        if os.path.exists('var/retention.dat'):
            os.remove('var/retention.dat')
        if os.path.exists('var/status.dat'):
            os.remove('var/status.dat')
        self.livestatus_broker = None

    def init_livestatus(self):
        self.livelogs = 'tmp/livelogs.db' + self.testid
        modconf = Module({'module_name': 'LiveStatus',
            'module_type': 'livestatus',
            'port': str(50000 + os.getpid()),
            'pnp_path': 'tmp/pnp4nagios_test' + self.testid,
            'host': '127.0.0.1',
            'socket': 'live',
            'name': 'test', #?
            'database_file': self.livelogs,
        })

        dbmodconf = Module({'module_name': 'LogStore',
            'module_type': 'logstore_sqlite',
            'use_aggressive_sql': "0",
            'database_file': self.livelogs,
            'archive_path': os.path.join(os.path.dirname(self.livelogs), 'archives'),
        })
        ####################################
        # !NOT! modconf.modules = [dbmodconf]
        ####################################
        self.livestatus_broker = LiveStatus_broker(modconf)
        self.livestatus_broker.create_queues()

        self.livestatus_broker.init()

        # --- livestatus_broker.main
        self.livestatus_broker.log = logger
        # this seems to damage the logger so that the scheduler can't use it
        # self.livestatus_broker.log.load_obj(self.livestatus_broker)
        self.livestatus_broker.debug_output = []
        self.livestatus_broker.modules_manager = ModulesManager('livestatus', modulesctx.get_modulesdir(), [])
        self.livestatus_broker.modules_manager.set_modules(self.livestatus_broker.modules)
        # We can now output some previouly silented debug ouput
        self.livestatus_broker.do_load_modules()
        for inst in self.livestatus_broker.modules_manager.instances:
            if inst.properties["type"].startswith('logstore'):
                f = getattr(inst, 'load', None)
                if f and callable(f):
                    f(self.livestatus_broker)  # !!! NOT self here !!!!
                break
        for s in self.livestatus_broker.debug_output:
            print "errors during load", s
        del self.livestatus_broker.debug_output
        self.livestatus_broker.add_compatibility_sqlite_module()
        self.livestatus_broker.rg = LiveStatusRegenerator()
        self.livestatus_broker.datamgr = datamgr
        datamgr.load(self.livestatus_broker.rg)
        self.livestatus_broker.query_cache = LiveStatusQueryCache()
        self.livestatus_broker.query_cache.disable()
        self.livestatus_broker.rg.register_cache(self.livestatus_broker.query_cache)
        # --- livestatus_broker.main

        # --- livestatus_broker.do_main
        self.livestatus_broker.db = self.livestatus_broker.modules_manager.instances[0]
        self.livestatus_broker.db.open()
        # --- livestatus_broker.do_main

        # --- livestatus_broker.manage_lql_thread
        self.livestatus_broker.livestatus = LiveStatus(self.livestatus_broker.datamgr, self.livestatus_broker.query_cache, self.livestatus_broker.db, self.livestatus_broker.pnp_path, self.livestatus_broker.from_q)
        # --- livestatus_broker.manage_lql_thread

    def test_has_implicit_module(self):
        module = self.livestatus_broker.modules_manager.instances[0]
        self.assertTrue(
            module.properties['type'] == 'logstore_sqlite')
        self.assertTrue(
            module.__class__.__name__ == 'LiveStatusLogStoreSqlite')

        self.assertTrue(self.livestatus_broker.db.database_file == self.livelogs)
