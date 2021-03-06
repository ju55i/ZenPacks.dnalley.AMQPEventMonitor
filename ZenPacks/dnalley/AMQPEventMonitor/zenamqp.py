#!/usr/bin/env python
###########################################################################
#
# Copyright (C) 2007, David Nalley
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published by
# the Free Software Foundation.
#
###########################################################################

import json
import os
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "lib"))

import logging

import Globals
import zope.component
import zope.interface
from Products.ZenCollector.daemon import CollectorDaemon
from Products.ZenCollector.interfaces import ICollectorPreferences,\
                                             IEventService,\
                                             IScheduledTask,\
                                             IStatisticsService
from Products.ZenCollector.tasks import SimpleTaskFactory,\
                                        SimpleTaskSplitter,\
                                        TaskStates
from Products.ZenEvents.ZenEventClasses import Clear, Debug, Info, Warning, Error, Critical
from Products.ZenUtils.observable import ObservableMixin
from Products.ZenUtils.Utils import unused
from Products.ZenCollector.services.config import DeviceProxy
unused(DeviceProxy)

from twisted.internet.defer import inlineCallbacks
from twisted.internet import reactor
from twisted.internet.protocol import ClientCreator
from txamqp.protocol import AMQClient
from txamqp.client import TwistedDelegate
import txamqp.spec

SEVERITY_MAP   = (Clear, Debug, Info, Info, Warning, Warning, Error, Error, Critical, Critical)
COLLECTOR_NAME = 'zenamqp'
Status_Amqp    = '/Status/Amqp'

log = logging.getLogger(COLLECTOR_NAME)

class AMQPEventPreferences(object):
    zope.interface.implements(ICollectorPreferences)

    def __init__(self):
        self.collectorName           = COLLECTOR_NAME
        self.configCycleInterval     = 20 # minutes
        self.configurationService    = 'ZenPacks.dnalley.AMQPEventMonitor.services.EventsConfig'
        self.cycleInterval           = 5 * 60 # seconds
        self.defaultRRDCreateCommand = None
        self.options                 = None

    def buildOptions(self, parser):
        unused(self, parser)
        pass

    def postStartup(self):
        unused(self)
        pass


class AMQPEventsTask(ObservableMixin):
    zope.interface.implements(IScheduledTask)

    def __init__(self, dev, name, ival, config):
        super(AMQPEventsTask, self).__init__()

        self.name      = name
        self.configId  = dev
        self.interval  = ival
        self.state     = TaskStates.STATE_IDLE
        self._config   = config
        self._devId    = dev
        self._manageIp = self._config.manageIp

        self._eventService = zope.component.queryUtility(IEventService)

    @inlineCallbacks
    def _onConnSucc(self, conn, queue, username, password):
        yield conn.authenticate(username, password)
        chan = yield conn.channel(1)
        yield chan.channel_open()
        yield chan.queue_declare(queue=queue, durable=True, exclusive=False, auto_delete=False)
        yield chan.basic_consume(queue=queue, no_ack=True, consumer_tag="zenoss")
        queue = yield conn.queue("zenoss")
        self._eventService.sendEvent(dict(
                            summary='Successfully connected to AMQP queue %s' % self._config.zAMQPQueue,
                            component='amqp',
                            eventClass=Status_Amqp,
                            device=self._devId,
                            severity=Clear,
                            agent=COLLECTOR_NAME))

        while True:
            msg = yield queue.get()

            base_evt = {"device":self._devId,"summary":"No summary","message":"No message","agent":COLLECTOR_NAME,
                        "eventClassKey":"amqp","eventGroup":"amqp","component":"amqp", "severity":1,
                        "user":username}

            if msg.content.properties == {'content type':'application/x-zenoss'}:
                amqp_evt = json.loads(msg.content.body, encoding="latin1")
                evt = dict(base_evt, **amqp_evt)
            elif msg.content.properties == {'content type':'text/plain'}:
                evt = dict(base_evt, summary=msg.content.body)
            else:
                evt = base_evt
            
            #import pdb; pdb.set_trace()
            self._eventService.sendEvent(evt)

        # Never get here
        yield chan.basic_cancel("zenoss")
        yield chan.channel_close()
        chan0 = yield conn.channel(0)
        yield chan0.connection_close()
        reactor.stop()

    def _onConnFail(self, result):
        error = "Unable to connect to amqp queue %s on %s: %s" % (
                                              self._config.zAMQPQueue,
                                              self._devId,
                                              result.getErrorMessage())
        log.error(error)
        self._eventService.sendEvent(dict(
                            summary=error,
                            component='amqp',
                            eventClass=Status_Amqp,
                            device=self._devId,
                            severity=Error,
                            agent=COLLECTOR_NAME))
        return result

    def cleanup(self):
        unused(self)
        pass

    def doTask(self):
        self.state = TaskStates.STATE_WAITING
        log.debug("Connecting to %s (%s)", self._devId, self._manageIp)

        spec = txamqp.spec.load(os.path.join(os.path.dirname(__file__), "lib/txamqp/specs/standard/amqp0-8.xml"))
        delegate = TwistedDelegate()
        d = ClientCreator(reactor,
                          AMQClient,
                          delegate=delegate,
                          spec=spec,
                          vhost=self._config.zAMQPVirtualHost).connectTCP(self._config.manageIp,
                                                                          self._config.zAMQPPort)
        d.addCallback(self._onConnSucc,
                      self._config.zAMQPQueue,
                      self._config.zAMQPUsername,
                      self._config.zAMQPPassword)
        d.addErrback(self._onConnFail)
        return d

if __name__ == '__main__':
    tf = SimpleTaskFactory(AMQPEventsTask)
    ts = SimpleTaskSplitter(tf)
    CollectorDaemon(AMQPEventPreferences(), ts).run()