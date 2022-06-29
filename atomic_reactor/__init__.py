"""
Copyright (c) 2015-2022 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.


constants
"""

from locale import nl_langinfo, CODESET
import logging
from os import fdopen, dup
import sys

from atomic_reactor.version import __version__  # noqa

from osbs.constants import ATOMIC_REACTOR_LOGGING_FMT, USER_WARNING_LEVEL
from osbs.utils import user_warning_log_handler


class EncodedStream(object):
    # The point of this class is to force python to enocde UTF-8
    # over stderr.  Normal techniques were not working, so we dup
    # the file handler and force it UTF-8.  :-(
    # Any attempts to fix should run test_cli.py::TestCLISuite::test_log_encoding
    # to verify.
    def __init__(self, fileno, encoding):
        self.binarystream = fdopen(dup(fileno), 'wb')
        self.encoding = encoding

    def write(self, text):
        if not isinstance(text, bytes):
            self.binarystream.write(text.encode(self.encoding))
        else:
            self.binarystream.write(text)
        # We need to flush regularly, because launching plugins or running
        # subprocess calls breaks serialization of logging output otherwise
        self.binarystream.flush()

    def __del__(self):
        try:
            self.binarystream.close()
        except AttributeError:
            pass


def get_logging_encoding(name="atomic_reactor"):
    return logging.getLogger(name).handlers[0].stream.encoding


def set_logging(name="atomic_reactor", level=logging.DEBUG, handler=None):
    # add new level to loggers
    logging.addLevelName(USER_WARNING_LEVEL, "USER_WARNING")
    logging.Logger.user_warning = user_warning_log_handler

    # create logger
    logger = logging.getLogger(name)
    for hdlr in list(logger.handlers):  # make a copy so it doesn't change
        logger.removeHandler(hdlr)

    logger.setLevel(level)

    if not handler:
        # create console handler and set level to debug
        log_encoding = nl_langinfo(CODESET)
        encoded_stream = EncodedStream(sys.stderr.fileno(), log_encoding)
        handler = logging.StreamHandler(encoded_stream)
        handler.setLevel(logging.DEBUG)

        # create formatter
        formatter = logging.Formatter(ATOMIC_REACTOR_LOGGING_FMT)

        # add formatter to ch
        handler.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(handler)

    logger = logging.getLogger('osbs')
    for hdlr in list(logger.handlers):  # make a copy so it doesn't change
        hdlr.setFormatter(formatter)


set_logging(level=logging.WARNING)  # override this however you want
