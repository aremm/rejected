#!/usr/bin/env python
# encoding: utf-8
"""
monitors.py

Created by Gavin M. Roy on 2009-09-10.
Copyright (c) 2009 Insider Guides, Inc. All rights reserved.
"""

import sys
import os
import unittest
import urllib

class alice:
    def __init__(self):
	print 'alice class inited'
	pass

    def getQueueDepth(host = 'localhost', queue = 'test' ):

        url = 'http://%s:9999/queues' % localhost


class aliceTests(unittest.TestCase):
    def setUp(self):
        pass


if __name__ == '__main__':
    unittest.main()
